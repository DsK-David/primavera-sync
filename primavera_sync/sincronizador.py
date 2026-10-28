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

from . import __version__
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
        self.max_tentativas = self.s.getint("max_tentativas", 5)
        self.erro_catalogo = None

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
        return self.documentos(completo)

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
        novos = {}
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
            impressao = _impressao(item)
            if feitos.get(codigo) != impressao:
                novos[codigo] = (item, impressao)
        enviados = 0
        for parte in _partes(list(novos.values())[:limite]):
            r = self.lonntra.artigos([i for i, _ in parte])
            erros = self._erros("artigo", r)
            self.estado.guardar_catalogo("artigo", {i["codigo"]: h for i, h in parte if i["codigo"] not in erros})
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
        desde, carga = self._historico("movimentos", completo)
        movimentos = []
        for m in (self.fonte.movimentos(desde or date(1900, 1, 1)) if movimentos_ativos else []):
            if m["Id"] in feitos or _num(m.get("Quantidade")) == 0:
                continue
            tipo = _txt(m.get("EntradaSaida")).upper()[:1]
            if tipo not in ("E", "S"):
                tipo = "E" if _num(m.get("Quantidade")) > 0 else "S"
            movimentos.append({"id": m["Id"], "artigo": _txt(m["Artigo"]), "armazem": _txt(m.get("Armazem")),
                               "data": _data(m["Data"]), "tipo": tipo, "quantidade": abs(_num(m["Quantidade"])),
                               "documento": _txt(m.get("Documento")) or None})
        if limite:  # os mais recentes
            movimentos = sorted(movimentos, key=lambda m: m["data"] or "")[-limite:]
        for parte in _partes(movimentos):
            erros = self._erros("movimento", self.lonntra.stock(movimentos=parte))
            self.estado.guardar_catalogo("movimento", {m["id"]: "" for m in parte if m["id"] not in erros})
            r["movimentos"] += len(parte) - len(erros)
        if carga and movimentos_ativos and not limite:
            self._carga_feita("movimentos")

        feitos = self.estado.catalogo("existencia")
        existencias = {}
        for e in self.fonte.existencias():
            chave = f"{_txt(e['Artigo'])}|{_txt(e.get('Armazem'))}"
            valor = f"{_num(e.get('Quantidade')):.4f}"
            if feitos.get(chave) != valor:
                existencias[chave] = valor
        lista = [{"artigo": k.split("|")[0], "armazem": k.split("|")[1], "quantidade": float(v)} for k, v in existencias.items()]
        lista = lista[:limite]
        for parte in _partes(lista):
            erros = self._erros("existencia", self.lonntra.stock(existencias=parte))
            ok = {f"{e['artigo']}|{e['armazem']}": existencias[f"{e['artigo']}|{e['armazem']}"] for e in parte
                  if f"{e['artigo']}@{e['armazem']}" not in erros}
            self.estado.guardar_catalogo("existencia", ok)
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

        feitos, tentativas = self.estado.enviados(), self.estado.tentativas()
        docs = [d for d in self.fonte.documentos(list(self.tipos), desde)
                if d["Id"] not in feitos and tentativas.get(d["Id"], 0) < self.max_tentativas]
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

    def _enviar_documentos(self, docs):
        """Envia estes documentos. Devolve (enviados, com erro, números dos enviados)."""
        linhas = self.fonte.linhas([d["Id"] for d in docs])
        clientes = self.fonte.clientes({d["Entidade"] for d in docs if d["Entidade"]})
        artigos = self.fonte.artigos({l["Artigo"] for ls in linhas.values() for l in ls})
        servicos = {t.strip() for t in self.s.get("tipos_artigo_servico", "").split(",") if t.strip()}

        enviados = erros = 0
        numeros = []
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
            numeros.append(numero)
        return enviados, erros, numeros

    # ---- teste de envio

    def amostra(self, n=10):
        """Envia no máximo `n` de cada coisa (documentos mais recentes, artigos, armazéns, movimentos, existências) para
        confirmar que os dados chegam à Lonntra. Não conta como carga completa: o resto segue o horário normal.
        Devolve {"documentos": {"enviados", "erros", "numeros"}, "catalogo": {...}, "erros": [(item, mensagem)]}."""
        antes = {r[0] for r in self.estado.resumo()[1]}
        feitos, tentativas = self.estado.enviados(), self.estado.tentativas()
        recentes = self.fonte.documentos(list(self.tipos), date.today() - timedelta(days=90)) \
            or self.fonte.documentos(list(self.tipos), None)
        docs = [d for d in recentes if d["Id"] not in feitos and tentativas.get(d["Id"], 0) < self.max_tentativas][-n:]
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


def ler_config(caminho):
    cfg = configparser.ConfigParser(interpolation=None)
    if not cfg.read(caminho, encoding="utf-8"):
        sys.exit(f"Não encontrei o ficheiro de configuração {caminho}")
    return cfg


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
        fonte = Evolution.ligar(cfg["primavera"]["ligacao"], dict(cfg["consultas"]) if cfg.has_section("consultas") else {})
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
    """Mostra o resultado de Sincronizador.amostra. Devolve True se chegou alguma coisa e nada foi recusado."""
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
    for item, msg in r["erros"]:
        print(f"  ERRO {item}: {msg}")
    total = d["enviados"] + sum((r["catalogo"] or {}).values())
    if r["erros"]:
        print("  Alguns itens foram recusados pela Lonntra (acima). Corrija e volte a testar com --teste.")
    elif total == 0:
        print("  Nada novo para enviar (já estava tudo enviado, ou o Primavera não tem esses dados).")
    else:
        print(f"  Conferir na Lonntra: os {total} itens acima (até {n} de cada tipo) devem aparecer em Faturação e em Artigos/Stock.")
    return not r["erros"]


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
    p.add_argument("--teste", nargs="?", const=10, type=int, metavar="N",
                   help="envia só N (10) documentos, artigos, armazéns, movimentos e existências para confirmar que chegam")
    p.add_argument("--estado", action="store_true", help="mostra o que foi enviado e os erros")
    p.add_argument("--repetir-erros", action="store_true", help="volta a tentar os documentos que esgotaram as tentativas")
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
    if a.esquema:
        print(Evolution.ligar(cfg["primavera"]["ligacao"], consultas).relatorio())
        return
    if a.teste:
        sys.exit(0 if imprimir_teste(Sincronizador(Evolution.ligar(cfg["primavera"]["ligacao"], consultas), lonntra, estado, cfg)
                                     .amostra(a.teste), a.teste) else 1)
    intervalo = s.getint("intervalo", 60)
    completo = a.completo
    sinc = None
    while True:
        erro_ciclo = None
        try:
            if sinc is None:  # a primeira vez, e depois de a ligação ao SQL Server cair
                sinc = Sincronizador(Evolution.ligar(cfg["primavera"]["ligacao"], consultas), lonntra, estado, cfg)
            sinc.ciclo(completo=completo)
            completo = False
        except ErroLigacao as e:
            erro_ciclo = f"Lonntra: {e}"
            log.error("A Lonntra não respondeu: %s. Tento outra vez daqui a %ds.", e, intervalo)
        except Exception as e:  # noqa: BLE001 - o serviço não pode morrer por um erro de um ciclo
            erro_ciclo = f"{type(e).__name__}: {e}"[:300]
            log.exception("Erro no ciclo; volto a ligar ao SQL Server e tento outra vez daqui a %ds.", intervalo)
            sinc = None
        try:
            lonntra.sinal(estado_do_sync(estado, erro_ciclo, sinc.erro_catalogo if sinc else None))
        except ErroLigacao as e:
            log.warning("Sem sinal para a Lonntra: %s", e)
        if a.uma_vez:
            return
        time.sleep(intervalo)


if __name__ == "__main__":
    main()
