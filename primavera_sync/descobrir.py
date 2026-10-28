"""Descobre, varrendo a base da empresa, que tabelas do Primavera guardam armazéns, existências e movimentos de stock,
e propõe as consultas para a secção [consultas] do config.ini. Só lê (catálogo do sistema e SELECT TOP).

Uso: python -m primavera_sync.descobrir config.ini
"""
import argparse
import re
import sys
from dataclasses import dataclass, field

from .sincronizador import ler_config

# nomes de coluna (minúsculas) que cada papel aceita, por ordem de preferência
COL_ARTIGO = ["artigo"]
COL_ARMAZEM = ["armazem", "codarmazem", "idarmazem"]
COL_QTD_STOCK = ["stkactual", "stkatual", "stockactual", "stockatual", "stock", "quantidade", "qtd"]
COL_QTD_MOV = ["quantidade", "qtd", "quant", "qtdmovimento"]
COL_DATA = ["data", "datamov", "datamovimento", "datadoc", "datamovim"]
COL_ID = ["id", "idmovimento", "idmov", "idlinha"]
COL_ENTRADA_SAIDA = ["entradasaida", "es", "tipomovimento", "tipomov", "sinal", "tipoes"]
COL_DOC = ["tipodoc", "serie", "numdoc"]
COL_DESCR = ["descricao", "nome", "desig"]
COL_COD_ARMAZEM = ["armazem", "codigo", "cod"]


@dataclass
class Tabela:
    esquema: str
    nome: str
    colunas: list
    linhas: int = 0
    _min: dict = field(default_factory=dict, repr=False)

    def __post_init__(self):
        self._min = {c.lower(): c for c in self.colunas}

    @property
    def completo(self):
        return f"[{self.esquema}].[{self.nome}]"

    def coluna(self, candidatos):
        """Nome real da primeira coluna que existir entre os candidatos."""
        for c in candidatos:
            if c in self._min:
                return self._min[c]
        return None


@dataclass
class Proposta:
    papel: str
    tabela: Tabela
    pontos: int
    sql: str
    avisos: list = field(default_factory=list)
    resultado: str = ""
    sem_erro: bool = False   # a consulta correu na base
    com_dados: bool = False  # e devolveu pelo menos uma linha


def _nome(t):
    return t.nome.lower()


def _penaliza(t):
    return -15 if re.search(r"hist|log|tmp|temp|bak|backup|old|audit", _nome(t)) else 0


def _bonus_linhas(t):
    return 5 if t.linhas > 0 else -20


# ---- armazéns -------------------------------------------------------------------------------------------------

def _armazens(tabelas):
    out = []
    for t in tabelas:
        cod, desc = t.coluna(COL_COD_ARMAZEM), t.coluna(COL_DESCR)
        if not cod or not desc or t.coluna(COL_ARTIGO):
            continue
        pontos = _bonus_linhas(t) + _penaliza(t)
        pontos += 40 if "armaz" in _nome(t) else 0
        pontos += 10 if _nome(t) in ("armazens", "inv_armazens") else 0
        pontos += 10 if t.coluna(["armazem"]) else 0
        pontos += 5 if t.linhas and t.linhas < 500 else 0  # lista curta, como deve ser
        if "armaz" not in _nome(t):
            continue
        out.append(Proposta("armazens", t, pontos, f"SELECT {cod} AS Armazem, {desc} AS Descricao FROM {t.completo}"))
    return out


# ---- existências ----------------------------------------------------------------------------------------------

def _existencias(tabelas):
    out = []
    for t in tabelas:
        art, arm, qtd = t.coluna(COL_ARTIGO), t.coluna(COL_ARMAZEM), t.coluna(COL_QTD_STOCK)
        if not (art and arm and qtd):
            continue
        pontos = _bonus_linhas(t) + _penaliza(t)
        pontos += 30 if "armaz" in _nome(t) else 0
        pontos += 20 if "artigo" in _nome(t) else 0
        pontos += 25 if qtd.lower().startswith(("stk", "stock")) else 0
        pontos -= 40 if re.search(r"moviment|linhas|cabec|doc", _nome(t)) else 0
        pontos -= 15 if t.coluna(COL_DATA) else 0  # uma tabela com data costuma ser histórico, não o estado atual
        avisos = []
        if not qtd.lower().startswith(("stk", "stock")):
            avisos.append(f"a coluna de quantidade é «{qtd}» (não tem «Stk/Stock» no nome): confirmar")
        sql = f"SELECT {art} AS Artigo, {arm} AS Armazem, SUM({qtd}) AS Quantidade FROM {t.completo} GROUP BY {art}, {arm}"
        out.append(Proposta("existencias", t, pontos, sql, avisos))
    return out


# ---- movimentos -----------------------------------------------------------------------------------------------

def _movimentos(tabelas):
    out = []
    for t in tabelas:
        art, arm, qtd, data = t.coluna(COL_ARTIGO), t.coluna(COL_ARMAZEM), t.coluna(COL_QTD_MOV), t.coluna(COL_DATA)
        if not (art and arm and qtd and data):
            continue
        pontos = _bonus_linhas(t) + _penaliza(t)
        pontos += 40 if "moviment" in _nome(t) else 0
        pontos += 15 if _nome(t).startswith("inv_") else 0
        pontos -= 30 if re.search(r"cabec|linhas|doc$", _nome(t)) else 0
        avisos = []

        idc = t.coluna(COL_ID)
        if idc:
            pontos += 10
            id_sql = f"m.{idc}"
        else:
            id_sql = f"CONCAT(m.{art}, '|', m.{arm}, '|', CONVERT(varchar(30), m.{data}, 126), '|', m.{qtd})"
            avisos.append("não há coluna de identificador: usei uma chave composta (artigo|armazém|data|quantidade); "
                          "dois movimentos iguais no mesmo instante contariam como um")

        es = t.coluna(COL_ENTRADA_SAIDA)
        if es:
            pontos += 10
            es_sql = f"m.{es}"
            avisos.append(f"«{es}» tem de ter os valores E/S; ver a amostra abaixo e confirmar")
        else:
            es_sql = f"CASE WHEN m.{qtd} < 0 THEN 'S' ELSE 'E' END"
            avisos.append("não há coluna de entrada/saída: assumi que quantidade negativa = saída; confirmar")

        doc = [t.coluna([c]) for c in COL_DOC]
        if all(doc):
            pontos += 5
            doc_sql = f"CONCAT(m.{doc[0]}, ' ', m.{doc[1]}, '/', m.{doc[2]})"
        else:
            doc_col = t.coluna(["documento", "descricao", "referencia"])
            doc_sql = f"CAST(m.{doc_col} AS varchar(100))" if doc_col else "CAST(NULL AS varchar(100))"
            avisos.append("não há tipo/série/número de documento nesta tabela: o campo Documento fica incompleto")

        sql = (f"SELECT {id_sql} AS Id, m.{art} AS Artigo, m.{arm} AS Armazem, m.{data} AS Data, {es_sql} AS EntradaSaida, "
               f"m.{qtd} AS Quantidade, {doc_sql} AS Documento FROM {t.completo} m WHERE m.{data} >= ?")
        out.append(Proposta("movimentos", t, pontos, sql, avisos))
    return out


def propor(tabelas):
    """Para cada papel, as propostas por ordem de pontos (a primeira é a melhor)."""
    return {
        "armazens": sorted(_armazens(tabelas), key=lambda p: -p.pontos),
        "existencias": sorted(_existencias(tabelas), key=lambda p: -p.pontos),
        "movimentos": sorted(_movimentos(tabelas), key=lambda p: -p.pontos),
    }


# ---- ligação à base -------------------------------------------------------------------------------------------

def ler_esquema(conn):
    cur = conn.cursor()
    cur.execute("SELECT s.name, t.name, t.object_id FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id")
    tabs = {oid: Tabela(s, n, []) for s, n, oid in cur.fetchall()}
    cur.execute("SELECT object_id, name FROM sys.columns")
    for oid, nome in cur.fetchall():
        if oid in tabs:
            tabs[oid].colunas.append(nome)
    try:
        cur.execute("SELECT object_id, SUM(rows) FROM sys.partitions WHERE index_id IN (0, 1) GROUP BY object_id")
        for oid, n in cur.fetchall():
            if oid in tabs:
                tabs[oid].linhas = int(n or 0)
    except Exception:  # noqa: BLE001 - sem permissão para ver as contagens
        for t in tabs.values():
            t.linhas = 1
    out = []
    for t in tabs.values():
        t.__post_init__()
        out.append(t)
    return out


def validar(conn, proposta):
    """Corre a consulta (só as primeiras linhas) e guarda o resultado ou o erro na proposta."""
    params = ["1900-01-01"] if "?" in proposta.sql else []
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT TOP 3 * FROM ({proposta.sql}) x", params)
        cols = [d[0] for d in cur.description]
        linhas = cur.fetchall()
        proposta.resultado = "OK, " + ("sem linhas" if not linhas else f"exemplo: {dict(zip(cols, linhas[0]))}")
        proposta.sem_erro, proposta.com_dados = True, bool(linhas)
    except Exception as e:  # noqa: BLE001
        proposta.resultado = f"ERRO: {str(e)[:200]}"
        proposta.sem_erro = proposta.com_dados = False
    return proposta.com_dados


def escolher(candidatas):
    """A melhor candidata já validada: a primeira que devolveu dados; senão a primeira que correu sem erro."""
    for p in candidatas:
        if p.com_dados:
            return p
    return next((p for p in candidatas if p.sem_erro), None)


def sugerir(conn, candidatas=3, papeis=("armazens", "existencias", "movimentos")):
    """Varre a base e devolve {papel: (escolhida ou None, candidatas validadas)}."""
    todas = propor(ler_esquema(conn))
    out = {}
    for papel in papeis:
        props = todas[papel][:candidatas]
        for p in props:
            validar(conn, p)
        out[papel] = (escolher(props), props)
    return out


def formatar_ini(escolhidas):
    linhas = ["[consultas]"]
    for papel in ("armazens", "existencias", "movimentos"):
        p = escolhidas.get(papel)
        if p:
            linhas.append(f"{papel} = {' '.join(p.sql.split())}")
    return "\n".join(linhas)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m primavera_sync.descobrir",
                                 description="Varre a base do Primavera e propõe as consultas de stock.")
    ap.add_argument("config")
    ap.add_argument("--candidatas", type=int, default=3, help="quantas tabelas candidatas mostrar por papel")
    args = ap.parse_args(argv)

    import pyodbc  # só no servidor do cliente
    cfg = ler_config(args.config)
    conn = pyodbc.connect(cfg["primavera"]["ligacao"], autocommit=True, readonly=True)
    conn.execute("SET LOCK_TIMEOUT 5000;")
    tabelas = ler_esquema(conn)
    print(f"{len(tabelas)} tabelas lidas.\n")

    escolhidas = {}
    for papel, props in propor(tabelas).items():
        print(f"== {papel} ==")
        props = props[:args.candidatas]
        if not props:
            print("  nenhuma tabela parece servir (a funcionalidade fica desligada ou a consulta tem de ser escrita à mão)\n")
            continue
        for p in props:
            validar(conn, p)
            print(f"  {p.tabela.completo}  ({p.tabela.linhas} linhas, {p.pontos} pontos)  {p.resultado}")
            for a in p.avisos:
                print(f"      ! {a}")
        if escolher(props):
            escolhidas[papel] = escolher(props)
        print()
    print("Escolha automática: a candidata com mais pontos que devolveu linhas.\n")
    print(formatar_ini(escolhidas))
    print("\nCopiar para o config.ini (secção [consultas]), confirmar os avisos acima e correr --verificar.")


if __name__ == "__main__":
    sys.exit(main())
