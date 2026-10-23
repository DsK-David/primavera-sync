"""Primavera ERP Evolution → Lonntra.

Envia os documentos de venda do Primavera (com o cliente e as linhas) para a rota de integração da Lonntra e fica a
correr, a cada `intervalo` segundos, para enviar os novos. Guarda num SQLite local o que já foi enviado; a Lonntra
também recusa o mesmo documento duas vezes, por isso pode parar e voltar a arrancar sem duplicar nada.

Uso:
    python -m primavera_sync.sincronizador config.ini            # corre sempre
    python -m primavera_sync.sincronizador config.ini --uma-vez  # um ciclo e sai
    python -m primavera_sync.sincronizador config.ini --estado   # resumo do que foi enviado e dos erros
"""
import argparse
import configparser
import logging
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta

from .lonntra import ErroApi, ErroLigacao, Lonntra

log = logging.getLogger("primavera_sync")

ESTADO_SQL = """
CREATE TABLE IF NOT EXISTS enviados (
    documento TEXT PRIMARY KEY,    -- Id do CabecDoc no Primavera
    numero TEXT NOT NULL,          -- ex.: FA 2026/15
    lonntra_id TEXT,
    enviado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS erros (
    documento TEXT PRIMARY KEY,
    numero TEXT NOT NULL,
    mensagem TEXT NOT NULL,
    tentativas INTEGER NOT NULL,
    ultima TEXT NOT NULL
);
"""

# NIF genéricos (consumidor final): não se guardam como NIF do cliente
NIF_GENERICOS = {"", "0", "000000000", "999999999", "999999990"}

# Tipo de cliente na Lonntra (coletivo/singular): o Primavera não o guarda de forma fiável, vai pelo nome
COLETIVO = re.compile(r"\b(lda|limitada|s\.?\s?a|unipessoal|sociedade|empresa|associa[cç][aã]o|cooperativa)\b\.?",
                      re.IGNORECASE)


class Estado:
    def __init__(self, caminho):
        self.conn = sqlite3.connect(caminho)
        self.conn.executescript(ESTADO_SQL)

    def _agora(self):
        return datetime.now().isoformat(timespec="seconds")

    def enviados(self):
        return {r[0] for r in self.conn.execute("SELECT documento FROM enviados")}

    def marcar(self, documento, numero, lonntra_id):
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO enviados VALUES (?,?,?,?)", (documento, numero, lonntra_id, self._agora()))
            self.conn.execute("DELETE FROM erros WHERE documento=?", (documento,))

    def erro(self, documento, numero, mensagem):
        with self.conn:
            self.conn.execute(
                "INSERT INTO erros VALUES (?,?,?,1,?) ON CONFLICT(documento) DO UPDATE SET "
                "mensagem=excluded.mensagem, tentativas=tentativas+1, ultima=excluded.ultima",
                (documento, numero, str(mensagem), self._agora()))

    def tentativas(self):
        return dict(self.conn.execute("SELECT documento, tentativas FROM erros"))

    def vazio(self):
        return self.conn.execute("SELECT 1 FROM enviados LIMIT 1").fetchone() is None

    def resumo(self):
        n = self.conn.execute("SELECT COUNT(*) FROM enviados").fetchone()[0]
        erros = self.conn.execute("SELECT numero, mensagem, tentativas, ultima FROM erros ORDER BY ultima DESC").fetchall()
        return n, erros


def _num(v):
    return float(v or 0)


def _txt(v):
    return str(v).strip() if v is not None else ""


def _data(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:10]


class Sincronizador:
    """Um ciclo: lê no Primavera os documentos ainda não enviados e envia-os, um a um, pela ordem da data."""

    def __init__(self, fonte, lonntra, estado, cfg):
        self.fonte, self.lonntra, self.estado, self.cfg = fonte, lonntra, estado, cfg
        self.s = cfg["sincronizacao"]
        self.tipos = {k.upper(): v.strip().upper() for k, v in cfg["tipos_documento"].items()}  # FA = FT
        self.modos = {k.upper(): v.strip() for k, v in cfg["modos_pagamento"].items()} if cfg.has_section("modos_pagamento") else {}
        self.max_tentativas = self.s.getint("max_tentativas", 5)

    def ciclo(self, completo=False):
        desde = None
        if not completo and not self.estado.vazio():
            desde = date.today() - timedelta(days=self.s.getint("revisao_dias", 30))
        inicio = self.s.get("data_inicio", "").strip()
        if inicio and (desde is None or date.fromisoformat(inicio) > desde):
            desde = date.fromisoformat(inicio)

        feitos, tentativas = self.estado.enviados(), self.estado.tentativas()
        docs = [d for d in self.fonte.documentos(list(self.tipos), desde)
                if d["Id"] not in feitos and tentativas.get(d["Id"], 0) < self.max_tentativas]
        if not docs:
            log.info("Nada de novo no Primavera.")
            return {"documentos": 0, "erros": 0}
        log.info("%d documento(s) por enviar%s.", len(docs), f" desde {desde}" if desde else "")

        linhas = self.fonte.linhas([d["Id"] for d in docs])
        clientes = self.fonte.clientes({d["Entidade"] for d in docs if d["Entidade"]})
        artigos = self.fonte.artigos({l["Artigo"] for ls in linhas.values() for l in ls})
        servicos = {t.strip() for t in self.s.get("tipos_artigo_servico", "").split(",") if t.strip()}

        enviados = erros = 0
        for d in docs:
            numero = f"{d['TipoDoc']} {d['Serie']}/{d['NumDoc']}"
            try:
                corpo = self.corpo(d, linhas.get(d["Id"], []), clientes, artigos, servicos)
                r = self.lonntra.documento(corpo)
            except (ErroApi, ValueError) as e:
                self.estado.erro(d["Id"], numero, e)
                log.warning("%s: %s", numero, e)
                erros += 1
                continue
            self.estado.marcar(d["Id"], numero, r.get("id"))
            for aviso in r.get("avisos") or []:
                log.warning("%s: %s", numero, aviso)
            log.info("%s %s.", numero, "já estava na Lonntra" if r.get("ja_existia") else "enviado")
            enviados += 1
        log.info("Ciclo terminado: %d enviado(s), %d com erro.", enviados, erros)
        return {"documentos": enviados, "erros": erros}

    def corpo(self, d, linhas, clientes, artigos, servicos):
        moeda = self.s.get("moeda", "CVE")
        if _txt(d.get("Moeda")) and _txt(d["Moeda"]).upper() != moeda.upper():
            raise ValueError(f"moeda {d['Moeda']} (só se envia {moeda})")
        if not linhas:
            raise ValueError("documento sem linhas de artigo")
        c = clientes.get(d["Entidade"])
        if c is None:
            raise ValueError(f"o cliente {d['Entidade']} não existe na tabela Clientes do Primavera")

        tipo = self.tipos[_txt(d["TipoDoc"]).upper()]
        origens = {l["IdCabecDocOrigem"] for l in linhas if l.get("IdCabecDocOrigem")}
        nif = re.sub(r"\D", "", _txt(c.get("NumContrib")))
        nome = _txt(c.get("Nome")) or d["Entidade"]
        return {
            "documento": {
                "id": d["Id"],
                "tipo": tipo,
                "numero": f"{d['TipoDoc']} {d['Serie']}/{d['NumDoc']}",
                "data": _data(d["Data"]),
                "vencimento": _data(d.get("DataVenc")),
                "total": round(abs(_num(d["TotalDocumento"])), 2),
                "desconto_financeiro": _num(d.get("DescFinanceiro")),
                "metodo_pagamento": self.modos.get(_txt(d.get("ModoPag")).upper()),
                "origem_id": origens.pop() if len(origens) == 1 else None,
            },
            "cliente": {
                "codigo": d["Entidade"],
                "nome": nome,
                "nif": None if nif in NIF_GENERICOS else nif,
                "morada": ", ".join(x for x in (_txt(c.get("Fac_Mor")), _txt(c.get("Fac_Local"))) if x) or None,
                "telefone": _txt(c.get("Fac_Tel")) or None,
                "coletivo": bool(COLETIVO.search(nome)),
            },
            "linhas": [{
                "artigo": l["Artigo"],
                "descricao": _txt(l.get("Descricao")) or _txt(artigos.get(l["Artigo"], {}).get("Descricao")),
                "quantidade": abs(_num(l["Quantidade"])),
                # PrecoLiquido: total da linha sem IVA, já com os descontos da linha
                "total_sem_iva": round(abs(_num(l["PrecoLiquido"])), 4),
                "taxa_iva": _num(l["TaxaIva"]),
                "servico": _txt(artigos.get(l["Artigo"], {}).get("TipoArtigo")) in servicos,
            } for l in linhas if _num(l["Quantidade"]) != 0],
        }


def ler_config(caminho):
    cfg = configparser.ConfigParser(interpolation=None)
    if not cfg.read(caminho, encoding="utf-8"):
        sys.exit(f"Não encontrei o ficheiro de configuração {caminho}")
    return cfg


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m primavera_sync.sincronizador",
                                description="Envia os documentos do Primavera para a Lonntra.")
    p.add_argument("config")
    p.add_argument("--uma-vez", action="store_true", help="corre um ciclo e sai")
    p.add_argument("--completo", action="store_true", help="revê todos os documentos, não só os recentes")
    p.add_argument("--estado", action="store_true", help="mostra quantos foram enviados e os erros")
    p.add_argument("--repetir-erros", action="store_true", help="volta a tentar os documentos que esgotaram as tentativas")
    a = p.parse_args(argv)
    cfg = ler_config(a.config)
    s = cfg["sincronizacao"]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(s.get("log", "primavera-sync.log"), encoding="utf-8")])
    estado = Estado(s.get("estado", "estado.db"))

    if a.estado:
        n, erros = estado.resumo()
        print(f"Documentos enviados: {n}")
        for numero, msg, vezes, ultima in erros:
            print(f"  ERRO {numero} ({vezes}x, {ultima}): {msg}")
        return
    if a.repetir_erros:
        with estado.conn:
            estado.conn.execute("DELETE FROM erros")

    lonntra = Lonntra(cfg["lonntra"]["url"], cfg["lonntra"]["chave"])
    try:
        lonntra.estado()
    except ErroLigacao as e:
        sys.exit(f"A Lonntra recusou a ligação ou a chave: {e}")

    from .evolution import Evolution
    sinc = Sincronizador(Evolution.ligar(cfg["primavera"]["ligacao"]), lonntra, estado, cfg)
    intervalo = s.getint("intervalo", 60)
    completo = a.completo
    while True:
        try:
            sinc.ciclo(completo=completo)
            completo = False
        except ErroLigacao as e:
            log.error("A Lonntra não respondeu: %s. Tento outra vez daqui a %ds.", e, intervalo)
        except Exception:  # noqa: BLE001 - o serviço não pode morrer por um erro de um ciclo
            log.exception("Erro no ciclo; tento outra vez daqui a %ds.", intervalo)
        if a.uma_vez:
            return
        time.sleep(intervalo)


if __name__ == "__main__":
    main()
