"""Leitura (só SELECT) da base de dados de uma empresa do Primavera ERP no SQL Server.

As consultas são montadas a partir da estrutura real da base (esquema.py), por isso uma coluna ou tabela com outro
nome não pára o sync. As consultas abaixo são a reserva, se a estrutura da base não puder ser lida; uma consulta
escrita à mão em [consultas] do config.ini tem sempre prioridade.
"""
import logging

from . import descobrir, esquema
from .esquema import Consulta, Esquema, ErroEsquema

log = logging.getLogger("primavera_sync")

# Só documentos de cliente; as linhas sem artigo (comentários) não entram.
DOCUMENTOS_SQL = """
SELECT c.Id, c.TipoDoc, c.Serie, c.NumDoc, c.Data, c.DataVenc, c.Entidade, c.CondPag, c.ModoPag, c.Moeda,
       c.TotalDocumento, c.DescFinanceiro
FROM CabecDoc c
LEFT JOIN CabecDocStatus s ON s.IdCabecDoc = c.Id
WHERE c.TipoEntidade = 'C' AND COALESCE(s.Anulado, 0) = 0
  AND c.TipoDoc IN ({tipos}) {filtro_data}
ORDER BY c.Data, c.TipoDoc, c.Serie, c.NumDoc
"""

LINHAS_SQL = """
SELECT l.IdCabecDoc, l.NumLinha, l.Artigo, l.Descricao, l.Quantidade, l.PrecoLiquido, l.TaxaIva,
       o.IdCabecDoc AS IdCabecDocOrigem
FROM LinhasDoc l
LEFT JOIN LinhasDoc o ON o.Id = l.IdLinhaOrigemCopia
WHERE l.IdCabecDoc IN ({ids}) AND l.Artigo IS NOT NULL AND l.Artigo <> ''
ORDER BY l.IdCabecDoc, l.NumLinha
"""

CLIENTES_SQL = """
SELECT Cliente, Nome, NumContrib, Fac_Mor, Fac_Local, Fac_Tel
FROM Clientes WHERE Cliente IN ({codigos})
"""

ARTIGOS_SQL = """
SELECT a.Artigo, a.Descricao, a.TipoArtigo, a.CodBarras,
       (SELECT TOP 1 m.PVP1 FROM ArtigoMoeda m WHERE m.Artigo = a.Artigo) AS PVP1
FROM Artigo a WHERE a.Artigo IN ({codigos})
"""

# Catálogo e stock (ADR-061, fase 3). Os nomes das tabelas de stock mudam com a versão do Primavera: cada consulta
# pode ser substituída na secção [consultas] do config.ini, desde que devolva as mesmas colunas (os mesmos nomes).
CATALOGO_SQL = {
    # Artigo, Descricao, TipoArtigo, Taxa (IVA %), CodBarras, Familia, FamiliaNome, Anulado, Custo, PVP, PVPComIva
    "artigos": """
SELECT a.Artigo, a.Descricao, a.TipoArtigo, i.Taxa, a.CodBarras, a.Familia, f.Descricao AS FamiliaNome,
       a.ArtigoAnulado AS Anulado, a.PCMedio AS Custo, m.PVP1 AS PVP, m.PVP1IvaIncluido AS PVPComIva
FROM Artigo a
LEFT JOIN Iva i ON i.Iva = a.Iva
LEFT JOIN Familias f ON f.Familia = a.Familia
OUTER APPLY (SELECT TOP 1 am.PVP1, am.PVP1IvaIncluido FROM ArtigoMoeda am WHERE am.Artigo = a.Artigo AND am.Moeda = ?) m
""",
    # Armazem, Descricao
    "armazens": "SELECT Armazem, Descricao FROM Armazens",
    # Artigo, Armazem, Quantidade
    "existencias": """
SELECT Artigo, Armazem, SUM(StkActual) AS Quantidade FROM ArtigoArmazem GROUP BY Artigo, Armazem
""",
    # Id, Artigo, Armazem, Data, EntradaSaida (E/S), Quantidade, Documento; o ? é a data desde quando
    "movimentos": """
SELECT m.Id, m.Artigo, m.Armazem, m.Data, m.EntradaSaida, m.Quantidade,
       CONCAT(m.TipoDoc, ' ', m.Serie, '/', m.NumDoc) AS Documento
FROM INV_Movimentos m WHERE m.Data >= ?
""",
}




def _lotes(valores, tamanho=500):
    valores = list(valores)
    for i in range(0, len(valores), tamanho):
        yield valores[i:i + tamanho]


RESERVA = {
    "documentos": Consulta(DOCUMENTOS_SQL, {"filtro_data": "AND c.Data >= ?"}),
    "linhas": Consulta(LINHAS_SQL),
    "clientes": Consulta(CLIENTES_SQL),
    "artigos_codigos": Consulta(ARTIGOS_SQL),
    **{k: Consulta(v) for k, v in CATALOGO_SQL.items()},
}
STOCK = ("armazens", "existencias", "movimentos")
ORDEM = ("documentos", "linhas", "clientes", "artigos_codigos", "artigos", *STOCK)


def estrutura_mudou(erro):
    """O SQL Server diz que uma tabela ou coluna não existe (42S02/42S22), em qualquer idioma."""
    estado = str(erro.args[0]) if getattr(erro, "args", None) else ""
    texto = str(erro).lower()
    return estado in ("42S02", "42S22") or any(x in texto for x in (
        "invalid object name", "invalid column name", "nome de objeto inválido", "nome de coluna inválido"))


class Evolution:
    """Fonte de dados: uma ligação pyodbc à base da empresa (ex.: PRIAUTOSTYLE)."""

    def __init__(self, ligacao, consultas=None):
        self.conn = ligacao
        self.manuais = {k: v for k, v in (consultas or {}).items() if v.strip()}
        self.esquema = None
        self._cache = {}
        self.notas = []
        self._carregar()

    @classmethod
    def ligar(cls, connection_string, consultas=None):
        import pyodbc  # só é preciso no servidor do cliente
        conn = pyodbc.connect(connection_string, autocommit=True, readonly=True)
        # o Primavera tem sempre prioridade: num conflito de bloqueios perde o sync, e o sync não espera mais de 5 s
        # por uma tabela ocupada (desiste e tenta no ciclo seguinte)
        conn.execute("SET DEADLOCK_PRIORITY LOW; SET LOCK_TIMEOUT 5000;")
        return cls(conn, consultas)

    # ---- estrutura e consultas

    def _carregar(self):
        """(Re)lê a estrutura da base. Chama-se ao arrancar e quando uma consulta falha por tabela ou coluna inexistente
        (por exemplo, depois de uma atualização do Primavera)."""
        self._cache = {}
        try:
            self.esquema = Esquema(descobrir.ler_esquema(self.conn))
        except Exception as e:  # noqa: BLE001
            self.esquema = None
            log.warning("Não consegui ler a estrutura da base (%s); uso as consultas de origem.", str(e)[:160])

    def consulta(self, papel):
        if papel in self.manuais:
            return Consulta(self.manuais[papel], RESERVA[papel].extra if papel in RESERVA else {})
        if papel not in self._cache:
            self._cache[papel] = self._construir(papel)
        return self._cache[papel]

    def _construir(self, papel):
        if self.esquema is None:
            return RESERVA[papel]
        antes = len(self.esquema.notas)
        if papel in esquema.BUILDERS:
            c = esquema.BUILDERS[papel](self.esquema)
        else:
            c = self._stock(papel)
        for nota in self.esquema.notas[antes:]:
            self.notas.append(f"{papel}: {nota}")
            log.warning("Estrutura da base (%s): %s", papel, nota)
        return c

    def _corre(self, sql, params=()):
        try:
            cur = self.conn.cursor()
            cur.execute(f"SELECT TOP 1 * FROM ({sql}) x", params)
            cur.fetchall()
            return True
        except Exception:  # noqa: BLE001
            return False

    def _stock(self, papel):
        """Armazéns, existências e movimentos: a consulta de origem se funcionar nesta base; senão procura as tabelas certas."""
        origem = RESERVA[papel]
        if self._corre(origem.sql, ["1900-01-01"] if "?" in origem.sql else []):
            return origem
        candidatas = descobrir.propor(self.esquema.tabelas)[papel][:3]
        for p in candidatas:
            descobrir.validar(self.conn, p)
        escolhida = descobrir.escolher(candidatas)
        if escolhida is None:
            raise ErroEsquema(f"não encontrei na base nenhuma tabela de {papel} de stock")
        self.esquema.notas.append(f"a consulta de origem não serve nesta base; uso a tabela {escolhida.tabela.nome}"
                                  + ("".join(f" ({a})" for a in escolhida.avisos)))
        return Consulta(escolhida.sql)

    def relatorio(self):
        """Texto com o que o sync percebeu da base: tabela escolhida, avisos e erros de cada consulta."""
        linhas = []
        for papel in ORDEM:
            try:
                c = self.consulta(papel)
                linhas.append(f"{papel}: OK" + (" (consulta manual)" if papel in self.manuais else ""))
                linhas.append("    " + " ".join(c.sql.split())[:400])
            except ErroEsquema as e:
                linhas.append(f"{papel}: FALTA — {e}")
        if self.notas:
            linhas.append("Avisos:")
            linhas += [f"    {n}" for n in self.notas]
        return "\n".join(linhas)

    # ---- execução

    def _rows(self, papel, preparar):
        """`preparar(consulta)` devolve (sql, parâmetros). Se a base disser que uma tabela ou coluna não existe, relê a
        estrutura e tenta uma vez mais com a consulta reconstruída."""
        for tentativa in (1, 2):
            sql, params = preparar(self.consulta(papel))
            try:
                cur = self.conn.cursor()
                cur.execute(sql, params)
                cols = [d[0] for d in cur.description]
                return [dict(zip(cols, r)) for r in cur.fetchall()]
            except Exception as e:  # noqa: BLE001
                if tentativa == 1 and papel not in self.manuais and estrutura_mudou(e):
                    log.warning("A estrutura da base mudou (%s); a ler de novo e a reconstruir as consultas.", str(e)[:160])
                    self._carregar()
                    continue
                raise

    def documentos(self, tipos, desde=None):
        """Cabeçalhos dos documentos dos tipos dados (códigos do Primavera: FA, FR, VD, NC…), desde uma data."""
        def preparar(c):
            filtro, params = (c.extra.get("filtro_data", ""), [*tipos, desde]) if desde else ("", list(tipos))
            return c.sql.format(tipos=",".join("?" * len(tipos)), filtro_data=filtro), params
        docs = self._rows("documentos", preparar)
        for d in docs:
            d["Id"] = str(d["Id"]).upper()
        return docs

    def linhas(self, ids):
        """Linhas por documento: {IdCabecDoc: [linhas]}."""
        out = {}
        for lote in _lotes(ids):
            for r in self._rows("linhas", lambda c: (c.sql.format(ids=",".join("?" * len(lote))), lote)):
                r["IdCabecDoc"] = str(r["IdCabecDoc"]).upper()
                if r["IdCabecDocOrigem"] is not None:
                    r["IdCabecDocOrigem"] = str(r["IdCabecDocOrigem"]).upper()
                out.setdefault(r["IdCabecDoc"], []).append(r)
        return out

    def clientes(self, codigos):
        out = {}
        for lote in _lotes(codigos):
            for r in self._rows("clientes", lambda c: (c.sql.format(codigos=",".join("?" * len(lote))), lote)):
                out[r["Cliente"]] = r
        return out

    def artigos(self, codigos):
        out = {}
        for lote in _lotes(codigos):
            for r in self._rows("artigos_codigos", lambda c: (c.sql.format(codigos=",".join("?" * len(lote))), lote)):
                out[r["Artigo"]] = r
        return out

    def catalogo(self, moeda):
        """Todos os artigos, com o preço na moeda dada."""
        return self._rows("artigos", lambda c: (c.sql, [moeda] * c.sql.count("?")))

    def armazens(self):
        return self._rows("armazens", lambda c: (c.sql, []))

    def existencias(self):
        return self._rows("existencias", lambda c: (c.sql, []))

    def movimentos(self, desde):
        rows = self._rows("movimentos", lambda c: (c.sql, [desde]))
        for r in rows:
            r["Id"] = str(r["Id"]).upper()
        return rows
