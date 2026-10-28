import unittest

from primavera_sync.descobrir import Tabela, escolher, formatar_ini, propor


def t(nome, colunas, linhas=100):
    return Tabela("dbo", nome, colunas, linhas)


TABELAS = [
    t("Armazens", ["Armazem", "Descricao", "Morada"], 3),
    t("Clientes", ["Cliente", "Nome"], 50),
    t("INV_ArtigosArmazem", ["Artigo", "Armazem", "StkActual", "Lote"]),
    t("INV_Movimentos", ["Id", "Artigo", "Armazem", "Data", "TipoDoc", "Serie", "NumDoc", "Quantidade", "EntradaSaida"]),
    t("LinhasDoc", ["Id", "Artigo", "Armazem", "Data", "Quantidade"]),
    t("INV_MovimentosHist", ["Id", "Artigo", "Armazem", "Data", "Quantidade"], 0),
]


class DescobrirTest(unittest.TestCase):
    def setUp(self):
        self.p = propor(TABELAS)

    def test_escolhe_as_tabelas_certas(self):
        self.assertEqual(self.p["armazens"][0].tabela.nome, "Armazens")
        self.assertEqual(self.p["existencias"][0].tabela.nome, "INV_ArtigosArmazem")
        self.assertEqual(self.p["movimentos"][0].tabela.nome, "INV_Movimentos")

    def test_movimentos_em_cima_de_linhas_de_documento(self):
        nomes = [x.tabela.nome for x in self.p["movimentos"]]
        self.assertLess(nomes.index("INV_Movimentos"), nomes.index("LinhasDoc"))
        self.assertLess(nomes.index("INV_Movimentos"), nomes.index("INV_MovimentosHist"))

    def test_sql_devolve_as_colunas_esperadas(self):
        self.assertIn("SUM(StkActual) AS Quantidade", self.p["existencias"][0].sql)
        sql = self.p["movimentos"][0].sql
        for col in ("AS Id", "AS Artigo", "AS Armazem", "AS Data", "AS EntradaSaida", "AS Quantidade", "AS Documento"):
            self.assertIn(col, sql)
        self.assertTrue(sql.endswith("?"))

    def test_sem_coluna_entrada_saida_avisa(self):
        sem = [t("Mov", ["Id", "Artigo", "Armazem", "Data", "Quantidade"])]
        prop = propor(sem)["movimentos"][0]
        self.assertIn("CASE WHEN m.Quantidade < 0", prop.sql)
        self.assertTrue(any("entrada/saída" in a for a in prop.avisos))

    def test_sem_tabelas_adequadas(self):
        self.assertEqual(propor([t("Clientes", ["Cliente", "Nome"])])["existencias"], [])

    def test_ini_numa_linha_por_consulta(self):
        ini = formatar_ini({k: v[0] for k, v in self.p.items()})
        self.assertEqual(len(ini.splitlines()), 4)
        self.assertTrue(ini.startswith("[consultas]"))

    def test_escolher_prefere_quem_tem_dados(self):
        vazia, cheia, com_erro = (propor(TABELAS)["existencias"][0] for _ in range(3))
        vazia.sem_erro, vazia.com_dados = True, False
        cheia.sem_erro, cheia.com_dados = True, True
        com_erro.sem_erro = com_erro.com_dados = False
        self.assertIs(escolher([com_erro, vazia, cheia]), cheia)
        self.assertIs(escolher([com_erro, vazia]), vazia)
        self.assertIsNone(escolher([com_erro]))


if __name__ == "__main__":
    unittest.main()
