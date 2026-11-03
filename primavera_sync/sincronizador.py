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
import glob
import os
import hashlib
import json
import logging
import logging.handlers
import socket
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import __version__, recebimentos as receb
from .lonntra import ErroApi, ErroLigacao, Lonntra

log = logging.getLogger("primavera_sync")

ESTADO_SQL = """
CREATE TABLE IF NOT EXISTS enviados (
    documento TEXT PRIMARY KEY,    -- Id do CabecDoc no Primavera
    numero TEXT NOT NULL,          -- ex.: FA 2026/15
    lonntra_id TEXT,
    enviado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS catalogo (
    tipo TEXT NOT NULL,            -- artigo | armazem | existencia | movimento
    chave TEXT NOT NULL,           -- código do artigo / armazém, «artigo|armazém», Id do movimento
    valor TEXT NOT NULL,           -- o que foi enviado (impressão do artigo, nome, quantidade)
    PRIMARY KEY (tipo, chave)
);
CREATE TABLE IF NOT EXISTS marcas (
    chave TEXT PRIMARY KEY,        -- ex.: carga_documentos (o histórico já foi todo enviado)
    valor TEXT NOT NULL
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
        self.caminho = str(caminho)
        self.conn = sqlite3.connect(caminho)
        self.conn.executescript(ESTADO_SQL)
        if "data" not in {r[1] for r in self.conn.execute("PRAGMA table_info(erros)")}:  # estado de antes desta coluna
            with self.conn:
                self.conn.execute("ALTER TABLE erros ADD COLUMN data TEXT")

    def copia_diaria(self, manter=7):
        """Uma cópia do estado por dia (perdê-lo fazia reenviar o histórico todo). Nunca pára o sync."""
        if self.caminho == ":memory:":
            return
        try:
            destino = f"{self.caminho}.{date.today().isoformat()}.bak"
            if os.path.exists(destino):
                return
            copia = sqlite3.connect(destino)
            try:
                self.conn.backup(copia)
            finally:
                copia.close()
            for velha in sorted(glob.glob(f"{self.caminho}.*.bak"))[:-manter]:
                os.remove(velha)
        except Exception:  # noqa: BLE001
            log.warning("Não consegui fazer a cópia diária do estado.", exc_info=True)

    def limpar_erros(self, chaves):
        with self.conn:
            self.conn.executemany("DELETE FROM erros WHERE documento=?", [(c,) for c in chaves])

    # itens que a Lonntra recusou: não se reenviam a cada ciclo (só se mudarem, ou uma vez por dia, para ver se já se
    # resolveu o motivo, ex.: o artigo entretanto chegou)
    def recusas(self, tipo):
        return self.catalogo(f"recusa_{tipo}")

    def guardar_recusas(self, tipo, valores):
        self.guardar_catalogo(f"recusa_{tipo}", valores)

    def esquecer_recusas(self, tipo, chaves):
        with self.conn:
            self.conn.executemany("DELETE FROM catalogo WHERE tipo=? AND chave=?", [(f"recusa_{tipo}", c) for c in chaves])

    def _agora(self):
        return datetime.now().isoformat(timespec="seconds")

    def enviados(self):
        return {r[0] for r in self.conn.execute("SELECT documento FROM enviados")}

    def marcar(self, documento, numero, lonntra_id):
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO enviados VALUES (?,?,?,?)", (documento, numero, lonntra_id, self._agora()))
            self.conn.execute("DELETE FROM erros WHERE documento=?", (documento,))

    def erro(self, documento, numero, mensagem, data=None):
        with self.conn:
            self.conn.execute(
                "INSERT INTO erros (documento, numero, mensagem, tentativas, ultima, data) VALUES (?,?,?,1,?,?) "
                "ON CONFLICT(documento) DO UPDATE SET mensagem=excluded.mensagem, tentativas=tentativas+1, "
                "ultima=excluded.ultima, data=COALESCE(excluded.data, data)",
                (documento, numero, str(mensagem), self._agora(), data))

    def tentativas(self):
        return dict(self.conn.execute("SELECT documento, tentativas FROM erros"))

    @staticmethod
    def espera(tentativas):
        """Minutos até voltar a tentar um documento recusado: 5, 10, 20… até 1 dia. Nunca desiste: se o motivo se
        resolver (uma taxa de IVA criada na Lonntra, um cliente corrigido), o documento segue sozinho."""
        return min(5 * 2 ** max(tentativas - 1, 0), 24 * 60)

    def em_espera(self, agora=None):
        """Documentos com erro cuja próxima tentativa ainda não chegou."""
        agora = agora or datetime.now()
        out = set()
        for doc, tentativas, ultima in self.conn.execute("SELECT documento, tentativas, ultima FROM erros WHERE documento NOT LIKE '%:%'"):
            if datetime.fromisoformat(ultima) + timedelta(minutes=self.espera(tentativas)) > agora:
                out.add(doc)
        return out

    def datas_de_erros(self):
        """{documento: data} dos documentos com erro, para os reler mesmo que já saiam da janela de revisão."""
        return {d: dt for d, dt in self.conn.execute(
            "SELECT documento, data FROM erros WHERE data IS NOT NULL AND documento NOT LIKE '%:%'")}

    def catalogo(self, tipo):
        return dict(self.conn.execute("SELECT chave, valor FROM catalogo WHERE tipo=?", (tipo,)))

    def guardar_catalogo(self, tipo, valores):
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO catalogo VALUES (?,?,?)", [(tipo, k, v) for k, v in valores.items()])
            self.conn.executemany("DELETE FROM erros WHERE documento=?", [(f"{tipo}:{k}",) for k in valores])

    def marca(self, chave):
        r = self.conn.execute("SELECT valor FROM marcas WHERE chave=?", (chave,)).fetchone()
        return r[0] if r else None

    def marcar_marca(self, chave, valor):
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO marcas VALUES (?,?)", (chave, valor))

    def vazio(self):
        return self.conn.execute("SELECT 1 FROM enviados LIMIT 1").fetchone() is None

    def resumo(self):
        n = self.conn.execute("SELECT COUNT(*) FROM enviados").fetchone()[0]
        erros = self.conn.execute("SELECT numero, mensagem, tentativas, ultima FROM erros ORDER BY ultima DESC").fetchall()
        return n, erros

    def ultimo_erro(self):
        """(número de registos com erro, o texto do mais recente)"""
        n = self.conn.execute("SELECT COUNT(*) FROM erros").fetchone()[0]
        r = self.conn.execute("SELECT numero, mensagem FROM erros ORDER BY ultima DESC LIMIT 1").fetchone()
        return n, (f"{r[0]}: {r[1]}" if r else None)

    def resumo_catalogo(self):
        return dict(self.conn.execute("SELECT tipo, COUNT(*) FROM catalogo GROUP BY tipo"))


def _num(v):
    return float(v or 0)


def _txt(v):
    return str(v).strip() if v is not None else ""


def _impressao(dados):
    return hashlib.sha1(json.dumps(dados, sort_keys=True, default=str).encode()).hexdigest()


def _partes(lista, tamanho=500):
    for i in range(0, len(lista), tamanho):
        yield lista[i:i + tamanho]


def _data(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:10]


class Sincronizador:
    """Um ciclo: artigos e stock do Primavera (só o que mudou), depois os documentos ainda não enviados, pela ordem
    da data. Um erro no catálogo ou no stock fica no log e não impede os documentos."""

    def __init__(self, fonte, lonntra, estado, cfg):
        self.fonte, self.lonntra, self.estado, self.cfg = fonte, lonntra, estado, cfg
        self.s = cfg["sincronizacao"]
        self.tipos = {k.upper(): v.strip().upper() for k, v in cfg["tipos_documento"].items()}  # FA = FT
        self.modos = {k.upper(): v.strip() for k, v in cfg["modos_pagamento"].items()} if cfg.has_section("modos_pagamento") else {}
        self.erro_catalogo = None
        self.erro_recebimentos = None
        self.pulso = None  # função chamada entre lotes, para dar sinal e tratar comandos dentro de um ciclo longo (ver main)
        self.codigos_servico = set()  # artigos que são serviços (sem stock): preenchido ao enviar o catálogo
        self._servicos_limpos = False
        self._movimentos_limpos = False

    def _pulsar(self):
        """Dentro de um ciclo longo (a carga do histórico demora horas) continua a dar sinal e a tratar os comandos remotos."""
        if self.pulso:
            self.pulso()

    @staticmethod
    def _marca(valor):
        """Recusa de hoje: o mesmo valor volta a tentar-se amanhã, ou já se mudar."""
        return f"{valor}@{date.today().isoformat()}"

    def ciclo(self, completo=False):
        if _txt(self.s.get("catalogo", "sim")).lower() not in ("nao", "não", "no", "false", "0", "off"):
            try:
                self.catalogo(completo)
                self.erro_catalogo = None
            except ErroLigacao:
                raise
            except Exception as e:  # noqa: BLE001 - ex.: uma consulta de stock que não existe nesta versão do Primavera
                self.erro_catalogo = f"catálogo/stock: {e}"[:300]
                log.exception("Catálogo ou stock: erro; os documentos continuam. Ver [consultas] no config.ini.")
        resultado = self.documentos(completo)
        if _txt(self.s.get("recebimentos", "não")).lower() in ("sim", "s", "1", "true", "yes", "on"):
            try:
                self.recebimentos(completo)
                self.erro_recebimentos = None
            except ErroLigacao:
                raise
            except Exception as e:  # noqa: BLE001 - os recebimentos não travam os documentos
                self.erro_recebimentos = f"recebimentos: {e}"[:300]
                log.exception("Recebimentos: erro; os documentos continuam.")
        return resultado

    # ---- recebimentos (ver recebimentos.py)

    def _recebimentos_lidos(self, desde):
        """(analise ou None, [recebimentos a enviar]). Com a consulta própria da empresa ([consultas] recebimentos) as linhas já
        vêm prontas; senão casam-se com as faturas enviadas e infere-se o valor."""
        linhas = self.fonte.recebimentos(desde)
        if getattr(self.fonte, "recebimentos_prontos", False):
            # as linhas já vêm prontas (liquidações do Primavera, ou a consulta da empresa): só interessam as de faturas já enviadas
            ja_enviados = self.estado.enviados()
            # as faturas-recibo e as vendas a dinheiro entram pagas na Lonntra (o recibo é automático): não se enviam os dela
            pagas_no_ato = {doc for doc, numero in self.estado.conn.execute("SELECT documento, numero FROM enviados")
                            if self.tipos.get(str(numero).split(" ")[0].upper()) in ("FR", "TV")}
            itens = [{"id": str(r["Id"]).upper(), "documento_id": str(r["IdCabecDoc"]).upper(), "data": receb._data(r["Data"]),
                      "valor": round(abs(_num(r["Valor"])), 2), "numero": _txt(r.get("Numero")) or None,
                      "metodo_pagamento": self.modos.get(_txt(r.get("ModoPag")).upper())}
                     for r in linhas if _num(r.get("Valor")) and str(r["IdCabecDoc"]).upper() in ja_enviados
                     and str(r["IdCabecDoc"]).upper() not in {d.upper() for d in pagas_no_ato}]
            return {"prontos": True, "lidas": len(linhas), "de_faturas_enviadas": len(itens), "exemplos": itens[:5]}, itens
        enviados = list(self.estado.conn.execute("SELECT documento, numero FROM enviados"))
        analise = receb.analisar(linhas, enviados, self.fonte.totais, self.s.get("moeda", "CVE"),
                                 self.cfg["recebimentos"].get("formula") if self.cfg.has_section("recebimentos") else None)
        return analise, (receb.recebimentos_a_enviar(analise) if analise["fiavel"] else [])

    def relatorio_recebimentos(self):
        """O relatório (sem enviar nada): como os movimentos casam com as faturas e que fórmula do valor bate com os totais."""
        analise, itens = self._recebimentos_lidos(None)
        if analise and analise.get("prontos"):
            total = round(sum(i["valor"] for i in itens), 2)
            texto = [f"Recebimentos lidos do Primavera: {analise['lidas']}",
                     f"De faturas já enviadas à Lonntra (os que seguem): {analise['de_faturas_enviadas']}, no valor de {total:.2f}"]
            if analise["exemplos"]:
                texto.append("Exemplos (recibo, data, valor):")
                texto += [f"    {i['numero']}  {i['data']}  {i['valor']:.2f}" for i in analise["exemplos"]]
            texto.append("PRONTO PARA ENVIAR (ligar com recebimentos = sim em [sincronizacao])" if itens else "NADA PARA ENVIAR")
            return "\n".join(texto)
        texto = receb.formatar(analise) + f"\nRecebimentos que seguiam (um por fatura e data): {len(itens)}"
        if analise["movimentos"] == 0 and hasattr(self.fonte, "diagnostico_recebimentos"):
            texto += "\n\nPorque não leu nada?\n" + self.fonte.diagnostico_recebimentos()
        return texto

    def recebimentos(self, completo=False):
        """Envia os recebimentos novos ou que mudaram. Só envia se o relatório for fiável (ver recebimentos.py)."""
        desde, carga = self._historico("recebimentos", completo)
        analise, itens = self._recebimentos_lidos(desde)
        if analise is not None and not analise.get("prontos") and not analise["fiavel"]:
            self.estado.erro("recebimentos:analise", "recebimentos", analise["motivo"])
            log.warning("Recebimentos não enviados: %s", analise["motivo"])
            return 0
        self.estado.limpar_erros(["recebimentos:analise"])
        feitos, recusados = self.estado.catalogo("recebimento"), self.estado.recusas("recebimento")
        novos = [i for i in itens if feitos.get(i["id"]) != f"{i['valor']:.2f}" and recusados.get(i["id"]) != self._marca(f"{i['valor']:.2f}")]
        enviados = 0
        for parte in _partes(novos):
            self._pulsar()
            erros = self._erros("recebimento", self.lonntra.recebimentos(parte))
            aceites = {i["id"]: f"{i['valor']:.2f}" for i in parte if i["id"] not in erros}
            self.estado.guardar_catalogo("recebimento", aceites)
            self.estado.esquecer_recusas("recebimento", aceites)
            self.estado.guardar_recusas("recebimento", {i["id"]: self._marca(f"{i['valor']:.2f}") for i in parte if i["id"] in erros})
            enviados += len(aceites)
        if carga:
            self._carga_feita("recebimentos")
        if enviados:
            log.info("Recebimentos: %d enviado(s).", enviados)
        return enviados

    # ---- histórico: a primeira carga é completa, depois só o recente

    def _historico(self, tipo, completo):
        """(desde, a carga completa vai ser feita agora?) para «documentos» ou «movimentos».

        A carga completa faz-se uma vez (sozinha, sem --completo) e, se houver `historico_horario` (ex.: 20-7), só
        dentro desse horário: durante o dia vai só o recente, para não pesar no Primavera.
        """
        inicio = self.s.get("data_inicio", "").strip()
        alvo = inicio or "tudo"
        if completo or (self.estado.marca(f"carga_{tipo}") != alvo and self._na_janela()):
            return (date.fromisoformat(inicio) if inicio else None), True
        desde = date.today() - timedelta(days=self.s.getint("revisao_dias", 30))
        if inicio and date.fromisoformat(inicio) > desde:
            desde = date.fromisoformat(inicio)
        return desde, False

    def _carga_feita(self, tipo):
        self.estado.marcar_marca(f"carga_{tipo}", self.s.get("data_inicio", "").strip() or "tudo")
        log.info("Carga completa de %s feita.", tipo)

    def _na_janela(self, agora=None):
        janela = self.s.get("historico_horario", "").strip()
        if not janela:
            return True
        de, ate = (int(x) for x in janela.split("-"))
        hora = (agora or datetime.now()).hour
        return de <= hora < ate if de < ate else hora >= de or hora < ate

    # ---- catálogo e stock (ADR-061, fase 3)

    def catalogo(self, completo=False, limite=None):
        """Envia os artigos, armazéns, movimentos e existências que mudaram desde o último envio.
        `limite`: no máximo tantos de cada (teste de envio); não conta como carga completa."""
        r = {"artigos": self._artigos(limite)}
        r.update(self._stock(completo, limite))
        if any(r.values()):
            log.info("Catálogo e stock: %s.", ", ".join(f"{v} {k}" for k, v in r.items() if v))
        return r

    def _artigos(self, limite=None):
        servicos = {t.strip() for t in self.s.get("tipos_artigo_servico", "").split(",") if t.strip()}
        feitos = self.estado.catalogo("artigo")
        recusados = self.estado.recusas("artigo")
        novos = {}
        codigos_servico = set()
        for a in self.fonte.catalogo(self.s.get("moeda", "CVE")):
            codigo = _txt(a["Artigo"])
            if not codigo:
                continue
            taxa = a.get("Taxa")
            preco = _num(a.get("PVP"))
            if a.get("PVPComIva") and taxa is not None:
                preco = preco / (1 + _num(taxa) / 100)
            item = {
                "codigo": codigo,
                "descricao": _txt(a.get("Descricao")) or codigo,
                "servico": _txt(a.get("TipoArtigo")) in servicos,
                "taxa_iva": _num(taxa) if taxa is not None else None,
                "preco": round(preco, 4),
                "custo": round(_num(a.get("Custo")), 4),
                "codigo_barras": _txt(a.get("CodBarras")) or None,
                "familia": {"codigo": _txt(a["Familia"]), "nome": _txt(a.get("FamiliaNome")) or _txt(a["Familia"])}
                if _txt(a.get("Familia")) else None,
                "ativo": not a.get("Anulado"),
            }
            if item["servico"]:
                codigos_servico.add(codigo)
            impressao = _impressao(item)
            if feitos.get(codigo) != impressao and recusados.get(codigo) != self._marca(impressao):
                novos[codigo] = (item, impressao)
        self.codigos_servico = codigos_servico
        enviados = 0
        for parte in _partes(list(novos.values())[:limite]):
            self._pulsar()
            r = self.lonntra.artigos([i for i, _ in parte])
            erros = self._erros("artigo", r)
            aceites = {i["codigo"]: h for i, h in parte if i["codigo"] not in erros}
            self.estado.guardar_catalogo("artigo", aceites)
            self.estado.esquecer_recusas("artigo", aceites)
            self.estado.guardar_recusas("artigo", {i["codigo"]: self._marca(h) for i, h in parte if i["codigo"] in erros})
            enviados += len(parte) - len(erros)
        return enviados

    def _stock(self, completo, limite=None):
        r = {"armazens": 0, "movimentos": 0, "existencias": 0}

        feitos = self.estado.catalogo("armazem")
        armazens = {_txt(a["Armazem"]): _txt(a.get("Descricao")) for a in self.fonte.armazens() if _txt(a.get("Armazem"))}
        novos = {k: v for k, v in armazens.items() if feitos.get(k) != v}
        if limite:
            novos = dict(list(novos.items())[:limite])
        if novos:
            self.lonntra.stock(armazens=[{"codigo": k, "nome": v} for k, v in novos.items()])
            self.estado.guardar_catalogo("armazem", novos)
            r["armazens"] = len(novos)

        movimentos_ativos = _txt(self.s.get("movimentos_stock", "sim")).lower() not in ("nao", "não", "no", "false", "0", "off")
        feitos = self.estado.catalogo("movimento")
        recusados = self.estado.recusas("movimento")
        desde, carga = self._historico("movimentos", completo)
        movimentos, de_servicos = [], []
        for m in (self.fonte.movimentos(desde or date(1900, 1, 1)) if movimentos_ativos else []):
            if _txt(m.get("Artigo")) in self.codigos_servico:  # serviço não tem stock: a Lonntra recusa-o (como as existências)
                de_servicos.append(m["Id"])
                continue
            if m["Id"] in feitos or _num(m.get("Quantidade")) == 0 or recusados.get(m["Id"]) == self._marca(""):
                continue
            tipo = _txt(m.get("EntradaSaida")).upper()[:1]
            if tipo not in ("E", "S"):
                tipo = "E" if _num(m.get("Quantidade")) > 0 else "S"
            movimentos.append({"id": m["Id"], "artigo": _txt(m["Artigo"]), "armazem": _txt(m.get("Armazem")),
                               "data": _data(m["Data"]), "tipo": tipo, "quantidade": abs(_num(m["Quantidade"])),
                               "documento": _txt(m.get("Documento")) or None})
        if de_servicos and not self._movimentos_limpos:  # recusas e erros de antes (quando ainda se enviavam)
            self.estado.esquecer_recusas("movimento", de_servicos)
            self.estado.limpar_erros([f"movimento:{i}" for i in de_servicos])
            self._movimentos_limpos = True
        if limite:  # os mais recentes
            movimentos = sorted(movimentos, key=lambda m: m["data"] or "")[-limite:]
        for parte in _partes(movimentos):
            self._pulsar()
            erros = self._erros("movimento", self.lonntra.stock(movimentos=parte))
            self.estado.guardar_catalogo("movimento", {m["id"]: "" for m in parte if m["id"] not in erros})
            self.estado.guardar_recusas("movimento", {m["id"]: self._marca("") for m in parte if m["id"] in erros})
            r["movimentos"] += len(parte) - len(erros)
        if carga and movimentos_ativos and not limite:
            self._carga_feita("movimentos")

        feitos = self.estado.catalogo("existencia")
        recusadas = self.estado.recusas("existencia")
        existencias, servicos_ignorados = {}, []
        for e in self.fonte.existencias():
            artigo = _txt(e["Artigo"])
            chave = f"{artigo}|{_txt(e.get('Armazem'))}"
            if artigo in self.codigos_servico:  # serviço não tem stock: a Lonntra recusa-o
                servicos_ignorados.append(f"existencia:{artigo}@{_txt(e.get('Armazem'))}")
                continue
            valor = f"{_num(e.get('Quantidade')):.4f}"
            if feitos.get(chave) != valor and recusadas.get(chave) != self._marca(valor):
                existencias[chave] = valor
        if servicos_ignorados and not self._servicos_limpos:  # erros de antes (quando ainda se enviavam)
            self.estado.limpar_erros(servicos_ignorados)
            self._servicos_limpos = True
        lista = [{"artigo": k.split("|")[0], "armazem": k.split("|")[1], "quantidade": float(v)} for k, v in existencias.items()]
        lista = lista[:limite]
        for parte in _partes(lista):
            self._pulsar()
            erros = self._erros("existencia", self.lonntra.stock(existencias=parte))
            chaves = {f"{e['artigo']}|{e['armazem']}": f"{e['artigo']}@{e['armazem']}" for e in parte}
            ok = {c: existencias[c] for c, recusa in chaves.items() if recusa not in erros}
            self.estado.guardar_catalogo("existencia", ok)
            self.estado.esquecer_recusas("existencia", ok)
            # o erro de uma existência chama-se «existencia:artigo@armazem»: o guardar_catalogo apaga «artigo|armazem»
            self.estado.limpar_erros([f"existencia:{chaves[c]}" for c in ok])
            self.estado.guardar_recusas("existencia", {c: self._marca(existencias[c]) for c, recusa in chaves.items() if recusa in erros})
            r["existencias"] += len(ok)
        return r

    def _erros(self, tipo, resposta):
        """Regista os itens recusados pela Lonntra e devolve os códigos deles."""
        codigos = set()
        for e in resposta.get("erros") or []:
            codigo = _txt(e.get("codigo"))
            codigos.add(codigo)
            self.estado.erro(f"{tipo}:{codigo}", f"{tipo} {codigo}", e.get("erro"))
        if codigos:
            log.warning("%d %s(s) recusado(s) pela Lonntra (ver --estado).", len(codigos), tipo)
        return codigos

    # ---- documentos

    def documentos(self, completo=False):
        desde, carga = self._historico("documentos", completo)

        feitos, em_espera = self.estado.enviados(), self.estado.em_espera()
        docs = [d for d in self.fonte.documentos(list(self.tipos), self._desde_com_erros(desde))
                if d["Id"] not in feitos and d["Id"] not in em_espera]
        if not docs:
            if carga:
                self._carga_feita("documentos")
            log.info("Nada de novo no Primavera.")
            return {"documentos": 0, "erros": 0}
        log.info("%d documento(s) por enviar%s.", len(docs), f" desde {desde}" if desde else "")
        enviados, erros, _ = self._enviar_documentos(docs)
        if carga:
            self._carga_feita("documentos")
        log.info("Ciclo terminado: %d enviado(s), %d com erro.", enviados, erros)
        return {"documentos": enviados, "erros": erros}

    def reenviar_documento(self, numero):
        """Reenvia UM documento («FP 2025/1») mesmo que o estado local o dê como enviado (a Lonntra não o tem). Devolve o resultado em texto."""
        tipo, resto = numero.split(" ", 1)
        serie, num = resto.split("/", 1)
        alvo = [d for d in self.fonte.documentos([tipo], date(1900, 1, 1))
                if _txt(d["Serie"]) == serie and _txt(d["NumDoc"]) == num]
        if not alvo:
            return f"«{numero}» não existe no Primavera (ou está anulado ou não é de cliente): nada a reenviar."
        d = alvo[0]
        with self.estado.conn:
            self.estado.conn.execute("DELETE FROM enviados WHERE documento=?", (d["Id"],))
        enviados, erros, _ = self._enviar_documentos([d])
        if enviados:
            return f"«{numero}» reenviado: a Lonntra aceitou."
        motivo = self.estado.conn.execute("SELECT mensagem FROM erros WHERE documento=?", (d["Id"],)).fetchone()
        return f"«{numero}» NÃO foi aceite: {motivo[0] if motivo else 'sem detalhe'}"

    def _desde_com_erros(self, desde):
        """A janela de revisão alarga-se até ao documento com erro mais antigo: um documento recusado e corrigido depois
        (já passaram mais de `revisao_dias`) volta a ser tentado."""
        if desde is None:
            return None
        antigas = [date.fromisoformat(str(d)[:10]) for d in self.estado.datas_de_erros().values() if d]
        return min([desde, *antigas])

    def _enviar_documentos(self, docs):
        """Envia estes documentos. Devolve (enviados, com erro, números dos enviados)."""
        linhas = self.fonte.linhas([d["Id"] for d in docs])
        clientes = self.fonte.clientes({d["Entidade"] for d in docs if d["Entidade"]})
        artigos = self.fonte.artigos({l["Artigo"] for ls in linhas.values() for l in ls})
        servicos = {t.strip() for t in self.s.get("tipos_artigo_servico", "").split(",") if t.strip()}

        enviados = erros = 0
        numeros = []
        for d in docs:
            self._pulsar()
            numero = f"{d['TipoDoc']} {d['Serie']}/{d['NumDoc']}"
            try:
                corpo = self.corpo(d, linhas.get(d["Id"], []), clientes, artigos, servicos)
                r = self.lonntra.documento(corpo)
            except ErroLigacao:
                raise
            except (ErroApi, ValueError) as e:
                self.estado.erro(d["Id"], numero, e, _data(d.get("Data")))
                log.warning("%s: %s", numero, e)
                erros += 1
                continue
            except Exception as e:  # noqa: BLE001 - dados inesperados (KeyError, TypeError…): este documento fica com erro, os outros seguem
                self.estado.erro(d["Id"], numero, f"{type(e).__name__}: {e}", _data(d.get("Data")))
                log.exception("%s: erro inesperado ao preparar ou enviar o documento", numero)
                erros += 1
                continue
            self.estado.marcar(d["Id"], numero, r.get("id"))
            for aviso in r.get("avisos") or []:
                log.warning("%s: %s", numero, aviso)
            log.info("%s %s.", numero, "já estava na Lonntra" if r.get("ja_existia") else "enviado")
            enviados += 1
            numeros.append(numero)
        return enviados, erros, numeros

    # ---- teste de envio

    def amostra(self, n=10):
        """Envia no máximo `n` de cada coisa (documentos mais recentes, artigos, armazéns, movimentos, existências) para
        confirmar que os dados chegam à Lonntra. Não conta como carga completa: o resto segue o horário normal.
        Devolve {"documentos": {"enviados", "erros", "numeros"}, "catalogo": {...}, "erros": [(item, mensagem)]}."""
        antes = {r[0] for r in self.estado.resumo()[1]}
        feitos, em_espera = self.estado.enviados(), self.estado.em_espera()
        recentes = self.fonte.documentos(list(self.tipos), date.today() - timedelta(days=90)) \
            or self.fonte.documentos(list(self.tipos), None)
        docs = [d for d in recentes if d["Id"] not in feitos and d["Id"] not in em_espera][-n:]
        enviados, erros, numeros = self._enviar_documentos(docs) if docs else (0, 0, [])
        out = {"documentos": {"enviados": enviados, "erros": erros, "numeros": numeros,
                              "ja_enviados": len([d for d in recentes if d["Id"] in feitos])}}
        if _txt(self.s.get("catalogo", "sim")).lower() in ("nao", "não", "no", "false", "0", "off"):
            out["catalogo"] = None
        else:
            out["catalogo"] = self.catalogo(limite=n)
        out["erros"] = [(r[0], r[1]) for r in self.estado.resumo()[1] if r[0] not in antes]
        return out

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


def opcoes_de(cfg):
    """Opções da empresa para o Evolution: o nome da tabela própria dos recebimentos, se a indicou ([recebimentos] tabela)."""
    return {"tabela_recebimentos": cfg["recebimentos"].get("tabela", "").strip()} if cfg.has_section("recebimentos") else {}


def ler_config(caminho):
    cfg = configparser.ConfigParser(interpolation=None)
    if not cfg.read(caminho, encoding="utf-8"):
        sys.exit(f"Não encontrei o ficheiro de configuração {caminho}")
    from .remoto import sobrepor  # a configuração remota (lista branca) sobrepõe-se ao config.ini
    return sobrepor(cfg, caminho)


def escrever_saude(pasta, erro=None, ciclo_ok=True, agora=None):
    """saude.json na pasta da empresa: o launcher vê aqui se uma versão nova dá ciclos bons. Nunca pára o sync."""
    try:
        agora = agora if agora is not None else time.time()
        ficheiro = Path(pasta) / "saude.json"
        antes = {}
        try:
            antes = json.loads(ficheiro.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        saude = {"versao": __version__, "pid": os.getpid(), "ciclo_em": agora, "ultimo_erro": erro,
                 "ciclo_ok_em": agora if ciclo_ok else (antes.get("ciclo_ok_em") if antes.get("versao") == __version__ else None)}
        temp = ficheiro.with_name("saude.json.tmp")
        temp.write_text(json.dumps(saude), encoding="utf-8")
        os.replace(temp, ficheiro)
    except Exception:  # noqa: BLE001
        log.warning("Não consegui escrever o saude.json.", exc_info=True)


def dar_sinal(lonntra, estado, erro_ciclo=None, erro_catalogo=None, extra=None):
    """O sinal de vida nunca derruba o sync: qualquer falha (a Lonntra em baixo, uma recusa 422…) fica só no log.
    Devolve a resposta da Lonntra (que pode trazer uma versão nova do sync para atualizar), ou None."""
    try:
        corpo = estado_do_sync(estado, erro_ciclo, erro_catalogo)
        corpo.update(extra or {})
        return lonntra.sinal(corpo) or {"ok": True}
    except Exception as e:  # noqa: BLE001
        log.warning("Sem sinal para a Lonntra: %s", e)
        return None


def atualizar(resposta, config, lonntra, estado):
    """Se a Lonntra ofereceu uma versão nova do sync, trata dela (atualizador.py). Nunca derruba o sync."""
    oferta = (resposta or {}).get("atualizar")
    if not oferta:
        return None
    try:
        from .atualizador import Atualizador
        base = Path.cwd()  # o launcher arranca o sync na pasta onde está (launcher.py, chaves.json, versoes\)
        if not Atualizador.disponivel(base):
            log.info("A Lonntra ofereceu a versão %s, mas a atualização à distância não está ligada (sem launcher ou chaves.json).",
                     oferta.get("versao"))
            return None
        return Atualizador(base, Path(config).resolve().parent, config, lonntra, estado).tratar(oferta)
    except Exception:  # noqa: BLE001
        log.exception("Erro ao tratar a atualização")
        return None


def estado_do_sync(estado, erro_ciclo=None, erro_catalogo=None):
    """O que vai no sinal de vida para a Lonntra."""
    n, ultimo = estado.ultimo_erro()
    c = estado.resumo_catalogo()
    extra = [e for e in (erro_ciclo, erro_catalogo) if e]
    return {
        "versao": __version__, "maquina": socket.gethostname(),
        "documentos": estado.resumo()[0], "artigos": c.get("artigo", 0), "movimentos": c.get("movimento", 0),
        "erros": n + len(extra), "ultimo_erro": (extra[0] if extra else ultimo),
        "ciclo": datetime.now().isoformat(timespec="seconds"),
    }


def verificar(cfg):
    """Testa a ligação ao SQL Server, cada consulta e a chave da Lonntra, sem enviar nada. Devolve True se está tudo bem."""
    ok = True

    def passo(nome, fn):
        nonlocal ok
        try:
            print(f"  OK   {nome}: {fn()}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"  ERRO {nome}: {e}")

    print("Lonntra")
    lonntra = Lonntra(cfg["lonntra"]["url"], cfg["lonntra"]["chave"])
    passo("chave e endereço", lambda: f"empresa {lonntra.estado().get('empresa')}")
    print("Primavera (SQL Server)")
    from .evolution import Evolution
    try:
        fonte = Evolution.ligar(cfg["primavera"]["ligacao"], dict(cfg["consultas"]) if cfg.has_section("consultas") else {}, opcoes_de(cfg))
        print("  OK   ligação")
    except Exception as e:  # noqa: BLE001
        print(f"  ERRO ligação: {e}")
        return False
    s = cfg["sincronizacao"]
    tipos = [k.upper() for k in cfg["tipos_documento"]]
    semana = date.today() - timedelta(days=7)
    passo(f"documentos {', '.join(tipos)} (últimos 7 dias)", lambda: f"{len(fonte.documentos(tipos, semana))} documentos")
    passo("linhas e clientes", lambda: _amostra(fonte, tipos))
    desligado = ("nao", "não", "no", "false", "0", "off")
    if _txt(s.get("catalogo", "sim")).lower() in desligado:
        print("  --   catálogo e stock desligados no config.ini: não testados")
    else:
        passo("artigos", lambda: f"{len(fonte.catalogo(s.get('moeda', 'CVE')))} artigos")
        passo("armazéns", lambda: ", ".join(_txt(a['Armazem']) for a in fonte.armazens()) or "nenhum")
        passo("existências", lambda: f"{len(fonte.existencias())} linhas")
        if _txt(s.get("movimentos_stock", "sim")).lower() in desligado:
            print("  --   movimentos de stock desligados no config.ini: não testados")
        else:
            passo("movimentos de stock (últimos 7 dias)", lambda: f"{len(fonte.movimentos(semana))} movimentos")
    for nota in getattr(fonte, "notas", []):  # o que o sync assumiu ou ficou vazio por a base ser diferente
        print(f"  !!   estrutura: {nota}")
    print("Tudo certo." if ok else "Há erros: corrija o config.ini ([primavera], [lonntra] ou [consultas]) e volte a verificar.")
    return ok


def imprimir_teste(r, n):
    """Mostra o resultado de Sincronizador.amostra. Devolve True se a instalação pode continuar: chegou alguma coisa à
    Lonntra, ou não havia nada a recusar. Itens recusados um a um (ex.: artigo com uma taxa de IVA que a Lonntra não
    tem) são avisos com o motivo, não uma falha da instalação: o resto foi enviado e esses repetem-se a cada ciclo."""
    d = r["documentos"]
    print(f"  Documentos: {d['enviados']} enviados, {d['erros']} com erro" +
          (f" (e {d['ja_enviados']} dos recentes já estavam enviados)" if d["ja_enviados"] else ""))
    for numero in d["numeros"]:
        print(f"      {numero}")
    if r["catalogo"] is None:
        print("  Artigos e stock: desligados no config.ini")
    else:
        nomes = {"artigos": "Artigos", "armazens": "Armazéns", "movimentos": "Movimentos de stock", "existencias": "Existências"}
        for k, v in r["catalogo"].items():
            print(f"  {nomes.get(k, k)}: {v} enviados")
    total = d["enviados"] + sum((r["catalogo"] or {}).values())
    if r["erros"]:
        motivos = {}
        for item, msg in r["erros"]:
            motivos.setdefault(str(msg), []).append(item)
        print(f"  {len(r['erros'])} item(ns) recusado(s) pela Lonntra, por estes motivos:")
        for msg, itens in sorted(motivos.items(), key=lambda m: -len(m[1])):
            print(f"      {len(itens)}x {msg}  (ex.: {', '.join(itens[:3])})")
    if total == 0 and r["erros"]:
        print("  Nada chegou à Lonntra. Corrija os motivos acima e volte a testar com --teste.")
        return False
    if r["erros"]:
        print(f"  Chegaram {total} itens; os recusados repetem-se a cada ciclo e resolvem-se na Lonntra (ver o motivo), "
              "sem impedir a instalação.")
    elif total == 0:
        print("  Nada novo para enviar (já estava tudo enviado, ou o Primavera não tem esses dados).")
    else:
        print(f"  Conferir na Lonntra: os {total} itens acima (até {n} de cada tipo) devem aparecer em Faturação e em Artigos/Stock.")
    return True


def _amostra(fonte, tipos):
    docs = fonte.documentos(tipos, date.today() - timedelta(days=90))[-5:]
    if not docs:
        return "sem documentos nos últimos 90 dias para testar"
    linhas = fonte.linhas([d["Id"] for d in docs])
    clientes = fonte.clientes({d["Entidade"] for d in docs if d["Entidade"]})
    return f"{sum(len(v) for v in linhas.values())} linhas e {len(clientes)} clientes em {len(docs)} documentos"


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m primavera_sync.sincronizador",
                                description=f"primavera-sync {__version__}: envia os dados do Primavera para a Lonntra.")
    p.add_argument("config")
    p.add_argument("--verificar", action="store_true", help="testa a ligação, as consultas e a chave, sem enviar nada")
    p.add_argument("--uma-vez", action="store_true", help="corre um ciclo e sai")
    p.add_argument("--completo", action="store_true", help="revê todos os documentos e movimentos, não só os recentes")
    p.add_argument("--esquema", action="store_true",
                   help="mostra o que o sync percebeu da base do Primavera: tabelas e colunas usadas, e o que falta")
    p.add_argument("--recebimentos", action="store_true",
                   help="mostra como os recebimentos da empresa casam com as faturas enviadas e que fórmula do valor bate (não envia nada)")
    p.add_argument("--teste", nargs="?", const=10, type=int, metavar="N",
                   help="envia só N (10) documentos, artigos, armazéns, movimentos e existências para confirmar que chegam")
    p.add_argument("--estado", action="store_true", help="mostra o que foi enviado e os erros")
    p.add_argument("--repetir-erros", action="store_true", help="volta a tentar já os documentos com erro, sem esperar pela próxima tentativa")
    a = p.parse_args(argv)
    cfg = ler_config(a.config)
    s = cfg["sincronizacao"]

    if a.verificar:
        sys.exit(0 if verificar(cfg) else 1)

    # log com rotação: 5 ficheiros de 5 MB, para não encher o disco do servidor
    ficheiro = logging.handlers.RotatingFileHandler(s.get("log", "primavera-sync.log"), maxBytes=5_000_000,
                                                    backupCount=5, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), ficheiro])
    estado = Estado(s.get("estado", "estado.db"))

    if a.estado:
        n, erros = estado.resumo()
        print(f"Documentos enviados: {n}")
        c = estado.resumo_catalogo()
        print(f"Artigos: {c.get('artigo', 0)}, armazéns: {c.get('armazem', 0)}, "
              f"existências: {c.get('existencia', 0)}, movimentos de stock: {c.get('movimento', 0)}")
        for numero, msg, vezes, ultima in erros:
            print(f"  ERRO {numero} ({vezes}x, {ultima}): {msg}")
        return
    if a.repetir_erros:
        with estado.conn:
            estado.conn.execute("DELETE FROM erros")

    log.info("primavera-sync %s a arrancar.", __version__)
    lonntra = Lonntra(cfg["lonntra"]["url"], cfg["lonntra"]["chave"])
    from .evolution import Evolution
    consultas = dict(cfg["consultas"]) if cfg.has_section("consultas") else {}
    if a.recebimentos:
        print(Sincronizador(Evolution.ligar(cfg["primavera"]["ligacao"], consultas, opcoes_de(cfg)), lonntra, estado, cfg).relatorio_recebimentos())
        return
    if a.esquema:
        print(Evolution.ligar(cfg["primavera"]["ligacao"], consultas, opcoes_de(cfg)).relatorio())
        return
    if a.teste:
        sys.exit(0 if imprimir_teste(Sincronizador(Evolution.ligar(cfg["primavera"]["ligacao"], consultas, opcoes_de(cfg)), lonntra, estado, cfg)
                                     .amostra(a.teste), a.teste) else 1)
    intervalo = s.getint("intervalo", 60)
    completo = a.completo
    sinc = None

    from . import remoto
    erros_ciclo = {"ciclo": None, "catalogo": None}

    def enviar_sinal():
        return dar_sinal(lonntra, estado, erros_ciclo["ciclo"], erros_ciclo["catalogo"],
                         {"resultados": remoto.resultados_pendentes(estado)})

    def obter_sinc():
        nonlocal sinc
        if sinc is None:
            sinc = Sincronizador(Evolution.ligar(cfg["primavera"]["ligacao"], consultas, opcoes_de(cfg)), lonntra, estado, cfg)
        return sinc

    ultimo_sinal = [time.time()]

    def pulsar():
        """Chamado entre lotes: se passou um minuto desde o último sinal, dá sinal, trata os comandos remotos e as atualizações."""
        if time.time() - ultimo_sinal[0] < 60:
            return
        ultimo_sinal[0] = time.time()
        resposta = enviar_sinal()
        codigo = remoto.tratar_remoto(resposta, ctx)
        atualizar(resposta, a.config, lonntra, estado)
        escrever_saude(Path(a.config).resolve().parent, None, ciclo_ok=False)  # está vivo (o ciclo bom regista-se no fim)
        if codigo:
            log.info("A reiniciar (%d) a meio do ciclo, para aplicar a configuração ou o comando remoto.", codigo)
            sys.exit(codigo)

    segredos = [cfg["lonntra"].get("chave", "")] + re.findall(r"(?i)PWD=\{?([^;}]+)", cfg["primavera"].get("ligacao", ""))
    empresa_id = cfg["lonntra"].get("chave", "").split(".")[0]  # a chave é «<empresa>.<segredo>»
    ctx = remoto.Contexto(cfg, a.config, estado, enviar_sinal, obter_sinc, segredos, empresa_id, Path.cwd())
    while True:
        erro_ciclo = None
        try:
            if sinc is None:  # a primeira vez, e depois de a ligação ao SQL Server cair
                sinc = Sincronizador(Evolution.ligar(cfg["primavera"]["ligacao"], consultas, opcoes_de(cfg)), lonntra, estado, cfg)
            sinc.pulso = pulsar
            sinc.ciclo(completo=completo)
            completo = False
        except ErroLigacao as e:
            erro_ciclo = f"Lonntra: {e}"
            log.error("A Lonntra não respondeu: %s. Tento outra vez daqui a %ds.", e, intervalo)
        except Exception as e:  # noqa: BLE001 - o serviço não pode morrer por um erro de um ciclo
            erro_ciclo = f"{type(e).__name__}: {e}"[:300]
            log.exception("Erro no ciclo; volto a ligar ao SQL Server e tento outra vez daqui a %ds.", intervalo)
            sinc = None
        erros_ciclo["ciclo"], erros_ciclo["catalogo"] = erro_ciclo, ((sinc.erro_catalogo or sinc.erro_recebimentos) if sinc else None)
        ultimo_sinal[0] = time.time()
        resposta = enviar_sinal()
        reiniciar = remoto.tratar_remoto(resposta, ctx)  # configuração e comandos da Lonntra (lista branca)
        completo, ctx.completo = completo or ctx.completo, False
        atualizar(resposta, a.config, lonntra, estado)
        escrever_saude(Path(a.config).resolve().parent, erro_ciclo, ciclo_ok=erro_ciclo is None)
        estado.copia_diaria()
        if reiniciar:
            log.info("A reiniciar (%d) para aplicar a configuração ou o comando remoto.", reiniciar)
            sys.exit(reiniciar)
        if a.uma_vez:
            return
        time.sleep(intervalo)


if __name__ == "__main__":
    main()
