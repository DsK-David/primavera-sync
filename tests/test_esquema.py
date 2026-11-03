import unittest

from primavera_sync import esquema
from primavera_sync.descobrir import Tabela
from primavera_sync.esquema import ErroEsquema, Esquema
from primavera_sync.evolution import Evolution, estrutura_mudou

CABEC = ["Id", "TipoDoc", "Serie", "NumDoc", "Data", "DataVenc", "Entidade", "TipoEntidade", "CondPag", "ModoPag",
         "Moeda", "TotalDocumento", "DescFinanceiro", "Nome", "NumContribuinte", "Morada", "Localidade"]
LINHAS = ["Id", "IdCabecDoc", "NumLinha", "Artigo", "Descricao", "Quantidade", "PrecoLiquido", "TaxaIva", "IdLinhaOrigemCopia"]


def t(nome, colunas, linhas=10, esq="dbo"):
    return Tabela(esq, nome, list(colunas), linhas)


def base(**subs):
    """Uma base com as tabelas habituais; `subs` troca as colunas de uma tabela."""
    tabs = {"CabecDoc": CABEC, "CabecDocStatus": ["IdCabecDoc", "Anulado"], "LinhasDoc": LINHAS,
            "Clientes": ["Cliente", "Nome", "NumContrib", "Fac_Mor", "Fac_Local", "Fac_Tel"],
            "Artigo": ["Artigo", "Descricao", "TipoArtigo", "CodBarras", "Familia", "ArtigoAnulado", "PCMedio", "Iva"],
            "Iva": ["Iva", "Taxa"], "Familias": ["Familia", "Descricao"],
            "ArtigoMoeda": ["Artigo", "Moeda", "PVP1", "PVP1IvaIncluido"]}
    tabs.update(subs)
    return Esquema([t(n, c) for n, c in tabs.items()])


class DocumentosTest(unittest.TestCase):
    def test_estrutura_habitual(self):
        e = base()
        c = esquema.documentos(e)
        self.assertIn("c.[TotalDocumento] AS [TotalDocumento]", c.sql)
        self.assertIn("c.[TipoEntidade] = 'C'", c.sql)
        self.assertIn("COALESCE(s.[Anulado], 0) = 0", c.sql)
        self.assertIn("{tipos}", c.sql)
        self.assertEqual(c.extra["filtro_data"], "AND c.[Data] >= ?")
        self.assertEqual(e.notas, [])

    def test_coluna_com_outro_nome(self):
        cab = [("DataDoc" if c == "Data" else "TotalDoc" if c == "TotalDocumento" else c) for c in CABEC]
        c = esquema.documentos(base(CabecDoc=cab))
        self.assertIn("c.[DataDoc] AS [Data]", c.sql)
        self.assertIn("c.[TotalDoc] AS [TotalDocumento]", c.sql)
        self.assertEqual(c.extra["filtro_data"], "AND c.[DataDoc] >= ?")
        self.assertIn("ORDER BY c.[DataDoc]", c.sql)

    def test_maiusculas_acentos_e_underscore_nao_importam(self):
        cab = [c.upper() if c in ("Serie", "NumDoc") else ("Tipo_Doc" if c == "TipoDoc" else c) for c in CABEC]
        c = esquema.documentos(base(CabecDoc=cab))
        self.assertIn("c.[Tipo_Doc] AS [TipoDoc]", c.sql)
        self.assertIn("c.[SERIE] AS [Serie]", c.sql)

    def test_opcional_em_falta_fica_nulo(self):
        c = esquema.documentos(base(CabecDoc=[x for x in CABEC if x not in ("DataVenc", "DescFinanceiro")]))
        self.assertIn("NULL AS [DataVenc]", c.sql)
        self.assertIn("NULL AS [DescFinanceiro]", c.sql)

    def test_obrigatorio_em_falta_diz_qual(self):
        with self.assertRaises(ErroEsquema) as cm:
            esquema.documentos(base(CabecDoc=[x for x in CABEC if x != "TotalDocumento"]))
        self.assertIn("TotalDocumento", str(cm.exception))

    def test_sem_tipo_de_entidade_filtra_pelos_clientes(self):
        c = esquema.documentos(base(CabecDoc=[x for x in CABEC if x != "TipoEntidade"]))
        self.assertIn("EXISTS (SELECT 1 FROM [dbo].[Clientes] k WHERE k.[Cliente] = c.[Entidade])", c.sql)

    def test_sem_tabela_de_estado_nao_pede_o_join(self):
        e = Esquema([t("CabecDoc", CABEC)])
        c = esquema.documentos(e)
        self.assertNotIn("CabecDocStatus", c.sql)
        self.assertTrue(any("anulados" in n for n in e.notas))

    def test_sem_a_tabela_principal(self):
        with self.assertRaises(ErroEsquema):
            esquema.documentos(Esquema([t("Outra", ["x"])]))


class OutrasConsultasTest(unittest.TestCase):
    def test_linhas_com_origem(self):
        c = esquema.linhas(base())
        self.assertIn("LEFT JOIN [dbo].[LinhasDoc] o ON o.[Id] = l.[IdLinhaOrigemCopia]", c.sql)
        self.assertIn("o.[IdCabecDoc] AS [IdCabecDocOrigem]", c.sql)
        self.assertIn("{ids}", c.sql)

    def test_linhas_sem_origem_nem_numlinha(self):
        cols = [x for x in LINHAS if x not in ("IdLinhaOrigemCopia", "NumLinha")]
        c = esquema.linhas(base(LinhasDoc=cols))
        self.assertIn("NULL AS [IdCabecDocOrigem]", c.sql)
        self.assertIn("NULL AS [NumLinha]", c.sql)
        self.assertTrue(c.sql.endswith("ORDER BY l.[IdCabecDoc]"))

    def test_linhas_sem_preco_nao_se_envia(self):
        with self.assertRaises(ErroEsquema):
            esquema.linhas(base(LinhasDoc=[x for x in LINHAS if x != "PrecoLiquido"]))

    def test_clientes_sem_nif(self):
        c = esquema.clientes(base(Clientes=["Cliente", "Nome"]))
        self.assertIn("NULL AS [NumContrib]", c.sql)

    def test_catalogo_completo(self):
        c = esquema.catalogo(base())
        for parte in ("i.[Taxa] AS [Taxa]", "f.[Descricao] AS [FamiliaNome]", "a.[PCMedio] AS [Custo]",
                      "OUTER APPLY", "am.[Moeda] = ?", "m.[PVP] AS [PVP]", "m.[PVPComIva] AS [PVPComIva]"):
            self.assertIn(parte, c.sql)
        self.assertEqual(c.sql.count("?"), 1)

    def test_catalogo_sem_iva_nem_precos_nem_familias(self):
        e = Esquema([t("Artigo", ["Artigo", "Descricao"])])
        c = esquema.catalogo(e)
        self.assertEqual(c.sql.count("?"), 0)
        for nulo in ("NULL AS [Taxa]", "NULL AS [FamiliaNome]", "NULL AS [PVP]", "NULL AS [PVPComIva]", "NULL AS [Custo]"):
            self.assertIn(nulo, c.sql)


class FalsoCursor:
    def __init__(self, conn):
        self.conn, self.description = conn, [("Artigo",)]

    def execute(self, sql, params=()):
        self.conn.executados.append(sql)
        if self.conn.falhas:
            self.conn.falhas -= 1
            raise Exception("('42S22', \"[42S22] Invalid column name 'X'. (207)\")")

    def fetchall(self):
        return [("A1",)]


class FalsaLigacao:
    def __init__(self, falhas=0):
        self.executados, self.falhas = [], falhas

    def cursor(self):
        return FalsoCursor(self)


class EvolutionTest(unittest.TestCase):
    def evolution(self, falhas=0, manuais=None):
        ev = Evolution.__new__(Evolution)
        ev.conn, ev.manuais, ev.notas, ev._cache, ev.recarregadas = FalsaLigacao(falhas), manuais or {}, [], {}, 0
        ev.esquema = base()

        def recarregar():
            ev._cache, ev.recarregadas = {}, ev.recarregadas + 1
        ev._carregar = recarregar
        return ev

    def test_estrutura_mudou(self):
        self.assertTrue(estrutura_mudou(Exception("('42S02', \"Invalid object name 'X'\")")))
        self.assertTrue(estrutura_mudou(Exception("Nome de coluna inválido 'X'")))
        self.assertFalse(estrutura_mudou(Exception("timeout")))

    def test_erro_de_coluna_rele_a_estrutura_e_repete(self):
        ev = self.evolution(falhas=1)
        rows = ev._rows("clientes", lambda c: (c.sql.format(codigos="?"), ["C1"]))
        self.assertEqual(rows, [{"Artigo": "A1"}])
        self.assertEqual(ev.recarregadas, 1)
        self.assertEqual(len(ev.conn.executados), 2)

    def test_so_repete_uma_vez(self):
        ev = self.evolution(falhas=5)
        with self.assertRaises(Exception):
            ev._rows("clientes", lambda c: (c.sql.format(codigos="?"), ["C1"]))
        self.assertEqual(ev.recarregadas, 1)

    def test_consulta_manual_tem_prioridade_e_nao_se_reconstroi(self):
        ev = self.evolution(falhas=1, manuais={"clientes": "SELECT 1 WHERE {codigos}"})
        with self.assertRaises(Exception):
            ev._rows("clientes", lambda c: (c.sql.format(codigos="?"), []))
        self.assertEqual(ev.recarregadas, 0)

    def test_relatorio_diz_o_que_falta(self):
        ev = self.evolution()
        ev.esquema = base(LinhasDoc=[x for x in LINHAS if x != "PrecoLiquido"])
        texto = ev.relatorio()
        self.assertIn("linhas: FALTA", texto)
        self.assertIn("PrecoLiquido", texto)
        self.assertIn("documentos: OK", texto)


if __name__ == "__main__":
    unittest.main()
