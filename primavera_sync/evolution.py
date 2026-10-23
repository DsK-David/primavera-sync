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


def _lotes(valores, tamanho=500):
    valores = list(valores)
    for i in range(0, len(valores), tamanho):
        yield valores[i:i + tamanho]


class Evolution:
    """Fonte de dados: uma ligação pyodbc à base da empresa (ex.: PRIAUTOSTYLE)."""

    def __init__(self, ligacao):
        self.conn = ligacao

    @classmethod
    def ligar(cls, connection_string):
        import pyodbc  # só é preciso no servidor do cliente
        return cls(pyodbc.connect(connection_string, autocommit=True, readonly=True))

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
