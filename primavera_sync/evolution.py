"""Leitura (só SELECT) da base de dados de uma empresa do Primavera ERP Evolution (v10) no SQL Server."""

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


class Evolution:
    """Fonte de dados: uma ligação pyodbc à base da empresa (ex.: PRIAUTOSTYLE)."""

    def __init__(self, ligacao, consultas=None):
        self.conn = ligacao
        self.consultas = {**CATALOGO_SQL, **{k: v for k, v in (consultas or {}).items() if v.strip()}}

    @classmethod
    def ligar(cls, connection_string, consultas=None):
        import pyodbc  # só é preciso no servidor do cliente
        conn = pyodbc.connect(connection_string, autocommit=True, readonly=True)
        # o Primavera tem sempre prioridade: num conflito de bloqueios perde o sync, e o sync não espera mais de 5 s
        # por uma tabela ocupada (desiste e tenta no ciclo seguinte)
        conn.execute("SET DEADLOCK_PRIORITY LOW; SET LOCK_TIMEOUT 5000;")
        return cls(conn, consultas)

    def _rows(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def documentos(self, tipos, desde=None):
        """Cabeçalhos dos documentos dos tipos dados (códigos do Primavera: FA, FR, VD, NC…), desde uma data."""
        filtro, params = ("AND c.Data >= ?", [*tipos, desde]) if desde else ("", list(tipos))
        sql = DOCUMENTOS_SQL.format(tipos=",".join("?" * len(tipos)), filtro_data=filtro)
        docs = self._rows(sql, params)
        for d in docs:
            d["Id"] = str(d["Id"]).upper()
        return docs

    def linhas(self, ids):
        """Linhas por documento: {IdCabecDoc: [linhas]}."""
        out = {}
        for lote in _lotes(ids):
            for r in self._rows(LINHAS_SQL.format(ids=",".join("?" * len(lote))), lote):
                r["IdCabecDoc"] = str(r["IdCabecDoc"]).upper()
                if r["IdCabecDocOrigem"] is not None:
                    r["IdCabecDocOrigem"] = str(r["IdCabecDocOrigem"]).upper()
                out.setdefault(r["IdCabecDoc"], []).append(r)
        return out

    def clientes(self, codigos):
        out = {}
        for lote in _lotes(codigos):
            for r in self._rows(CLIENTES_SQL.format(codigos=",".join("?" * len(lote))), lote):
                out[r["Cliente"]] = r
        return out

    def artigos(self, codigos):
        out = {}
        for lote in _lotes(codigos):
            for r in self._rows(ARTIGOS_SQL.format(codigos=",".join("?" * len(lote))), lote):
                out[r["Artigo"]] = r
        return out

    def catalogo(self, moeda):
        """Todos os artigos, com o preço na moeda dada."""
        return self._rows(self.consultas["artigos"], [moeda])

    def armazens(self):
        return self._rows(self.consultas["armazens"])

    def existencias(self):
        return self._rows(self.consultas["existencias"])

    def movimentos(self, desde):
        rows = self._rows(self.consultas["movimentos"], [desde])
        for r in rows:
            r["Id"] = str(r["Id"]).upper()
        return rows
