import configparser
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

from primavera_sync import instalar
from primavera_sync.sincronizador import Estado, Sincronizador
from tests.test_sincronizador import CONFIG, FontePrimavera, LonntraFalsa, doc, linha


class SugestoesTest(unittest.TestCase):
    def test_tipos_de_documento(self):
        casos = {
            ("FA", "Fatura"): "FT", ("FT", ""): "FT", ("FAC", "Factura a Crédito"): "FT",
            ("FR", "Fatura-Recibo"): "FR", ("XFR", "Factura Recibo"): "FR",
            ("VD", "Venda a Dinheiro"): "TV", ("FS", "Fatura Simplificada"): "TV",
            ("NC", "Nota de Crédito"): "NC", ("NCR", ""): "NC", ("ND", "Nota de Débito"): "ND",
            ("FP", "Fatura Proforma"): "FP", ("PRO", "Pro-forma"): "FP", ("XPF", "Factura ProForma"): "FP",
            ("FR1", "Fatura Recibo (série 1)"): "FR", ("FR2", ""): "FR",
            ("DEV", "Devolução"): "DV", ("DV", ""): "DV",
            ("GR", "Guia de Remessa"): None, ("ECL", "Encomenda de Cliente"): None, ("ORC", "Orçamento"): None,
        }
        for (codigo, desc), esperado in casos.items():
            self.assertEqual(instalar.sugerir_tipo(codigo, desc), esperado, codigo)

    def test_metodos_de_pagamento(self):
        casos = {("NUM", "Numerário"): "10", ("CHQ", "Cheque"): "20", ("TRF", "Transferência Bancária"): "30",
                 ("MB", "Multibanco"): "55", ("CC", "Cartão de Crédito"): "54", ("XYZ", ""): "10"}
        for (codigo, desc), esperado in casos.items():
            self.assertEqual(instalar.sugerir_metodo(codigo, desc), esperado, codigo)

    def test_driver_e_ligacao(self):
        self.assertEqual(instalar.melhor_driver(["SQL Server", "ODBC Driver 17 for SQL Server"]), "ODBC Driver 17 for SQL Server")
        self.assertEqual(instalar.melhor_driver(["SQL Server"]), "SQL Server")
        self.assertIsNone(instalar.melhor_driver(["Oracle"]))
        self.assertEqual(instalar.ligacao_odbc("ODBC Driver 18 for SQL Server", "SRV\\PRI", "PRIAUTO", "u", "p;x"),
                         "DRIVER={ODBC Driver 18 for SQL Server};SERVER=SRV\\PRI;DATABASE=PRIAUTO;UID=u;PWD={p;x};TrustServerCertificate=yes")
        self.assertEqual(instalar.ligacao_odbc("SQL Server", ".", "PRIX"), "DRIVER={SQL Server};SERVER=.;DATABASE=PRIX;Trusted_Connection=yes")

    def test_tipo_de_artigo_que_parece_servico(self):
        self.assertTrue(instalar.parece_servico(["Mão de obra", "Serviço de alinhamento", "Montagem pneus"]))
        self.assertFalse(instalar.parece_servico(["Pneu 195/65", "Filtro de óleo", "Óleo 5W30", "Mão de obra"]))
        self.assertFalse(instalar.parece_servico([]))


class EscreverConfigTest(unittest.TestCase):
    def test_config_gerado_e_lido_pelo_sync(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(instalar, "PASTA", Path(tmp)):
            (Path(tmp) / "config.example.ini").write_text(Path(CONFIG).read_text(encoding="utf-8"), encoding="utf-8")
            (Path(tmp) / "config.ini").write_text("antigo", encoding="utf-8")
            destino = instalar.escrever_config("https://app.lonntra.cv", "E.S", "DRIVER={SQL Server};SERVER=.;DATABASE=PRIX",
                                               {"FA": "FT", "VD": "TV"}, {"NUM": "10"}, "0", "CVE", True, False, "20-7")
            cfg = configparser.ConfigParser(interpolation=None)
            cfg.read(destino, encoding="utf-8")
            self.assertEqual(cfg["lonntra"]["chave"], "E.S")
            self.assertEqual({k.upper(): v for k, v in cfg["tipos_documento"].items()}, {"FA": "FT", "VD": "TV"})
            self.assertEqual((cfg["sincronizacao"]["movimentos_stock"], cfg["sincronizacao"]["historico_horario"]), ("não", "20-7"))
            self.assertTrue(cfg.has_section("consultas"))
            self.assertEqual(len(list(Path(tmp).glob("config.ini.*.bak"))), 1, "o config.ini anterior fica guardado")
            Sincronizador(FontePrimavera(), LonntraFalsa(), Estado(str(Path(tmp) / "e.db")), cfg)  # o sync aceita-o


class HistoricoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = configparser.ConfigParser(interpolation=None)
        self.cfg.read(CONFIG, encoding="utf-8")
        self.cfg["sincronizacao"]["catalogo"] = "nao"
        self.fonte, self.api = FontePrimavera(), LonntraFalsa()
        self.fonte.clientes_ = {"C001": {"Cliente": "C001", "Nome": "Cliente"}}
        self.fonte.docs = [doc("A", "FA", 1, 9200, data=date(2019, 5, 1))]
        self.fonte.linhas_ = {"A": [linha("A", "PN", 1, 8000)]}
        self.estado = Estado(str(Path(self.tmp.name) / "estado.db"))

    def tearDown(self):
        self.estado.conn.close()
        self.tmp.cleanup()

    def test_janela_de_horas(self):
        s = Sincronizador(self.fonte, self.api, self.estado, self.cfg)
        self.cfg["sincronizacao"]["historico_horario"] = "20-7"
        self.assertTrue(s._na_janela(datetime(2026, 10, 27, 22)))
        self.assertTrue(s._na_janela(datetime(2026, 10, 27, 3)))
        self.assertFalse(s._na_janela(datetime(2026, 10, 27, 10)))
        self.cfg["sincronizacao"]["historico_horario"] = "12-14"
        self.assertTrue(s._na_janela(datetime(2026, 10, 27, 13)))
        self.assertFalse(s._na_janela(datetime(2026, 10, 27, 15)))

    def test_historico_so_dentro_da_janela_e_uma_vez(self):
        self.cfg["sincronizacao"]["historico_horario"] = "20-7"
        s = Sincronizador(self.fonte, self.api, self.estado, self.cfg)
        with mock.patch.object(s, "_na_janela", return_value=False):
            self.assertEqual(s.ciclo()["documentos"], 0, "de dia: só o recente")
        with mock.patch.object(s, "_na_janela", return_value=True):
            self.assertEqual(s.ciclo()["documentos"], 1, "à noite: o histórico todo")
        self.fonte.docs.append(doc("B", "FA", 2, 9200, data=date(2019, 6, 1)))
        self.fonte.linhas_["B"] = [linha("B", "PN", 1, 8000)]
        with mock.patch.object(s, "_na_janela", return_value=True):
            self.assertEqual(s.ciclo()["documentos"], 0, "o histórico só se faz uma vez")


if __name__ == "__main__":
    unittest.main()


class VariasEmpresasTest(unittest.TestCase):
    def test_separar_chaves(self):
        self.assertEqual(instalar.separar_chaves("a.b, c.d"), ["a.b", "c.d"])
        self.assertEqual(instalar.separar_chaves(" a ; b  c,a "), ["a", "b", "c"])
        self.assertEqual(instalar.separar_chaves(""), [])

    def test_melhor_base_prefere_o_nome_igual(self):
        bases = ["PRIAUTOSTYLE", "PRICUSTOM", "PRIOUTRA"]
        nomes = {"PRIAUTOSTYLE": "Auto Style, Lda", "PRICUSTOM": "Auto Style Custom", "PRIOUTRA": "Outra"}
        self.assertEqual(instalar.melhor_base(bases, {**nomes, "PRIAUTOSTYLE": "Auto Style"}, "AutoStyle"), 0)
        self.assertEqual(instalar.melhor_base(bases, nomes, "Auto Style Custom"), 1)
        self.assertEqual(instalar.melhor_base(bases, nomes, "Auto Style"), 0, "contém: fica a mais curta")
        self.assertEqual(instalar.melhor_base(bases, nomes, "Desconhecida"), 0)
        self.assertEqual(instalar.melhor_base(bases, nomes, ""), 0)

    def test_slug(self):
        self.assertEqual(instalar.slug("Auto Style Custom, Lda."), "auto-style-custom-lda")
        self.assertEqual(instalar.slug("Ação Ltda"), "acao-ltda")
        self.assertEqual(instalar.slug("", "abcdef123456"), "abcdef12")

    def test_pasta_da_empresa(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(instalar, "PASTA", Path(tmp)):
            raiz = Path(tmp)
            self.assertEqual(instalar.pasta_da_empresa("K1", "Auto Style"), (raiz / "empresas" / "auto-style", "PrimaveraLonntra-auto-style"))
            (raiz / "config.ini").write_text("[lonntra]\nchave = K1\n", encoding="utf-8")
            self.assertEqual(instalar.pasta_da_empresa("K1", "Auto Style"), (raiz, "PrimaveraLonntra"),
                             "a instalação antiga mantém a pasta e a tarefa")
            self.assertEqual(instalar.pasta_da_empresa("K2", "Auto Style Custom")[1], "PrimaveraLonntra-auto-style-custom")

    def test_config_de_uma_segunda_empresa_fica_na_sua_pasta(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(instalar, "PASTA", Path(tmp)):
            (Path(tmp) / "config.example.ini").write_text(Path(CONFIG).read_text(encoding="utf-8"), encoding="utf-8")
            pasta = Path(tmp) / "empresas" / "custom"
            destino = instalar.escrever_config("https://app.lonntra.cv", "K2", "DRIVER={SQL Server};DATABASE=PRIC",
                                               {"FA": "FT"}, {}, "0", "CVE", True, True, "", pasta,
                                               {"existencias": "SELECT 1 AS Artigo"})
            self.assertEqual(destino, pasta / "config.ini")
            cfg = configparser.ConfigParser(interpolation=None)
            cfg.read(destino, encoding="utf-8")
            self.assertEqual(cfg["sincronizacao"]["estado"], str(Path("empresas") / "custom" / "estado.db"))
            self.assertEqual(cfg["sincronizacao"]["log"], str(Path("empresas") / "custom" / "primavera-sync.log"))
            self.assertEqual(cfg["consultas"]["existencias"], "SELECT 1 AS Artigo")
            self.assertEqual(cfg["consultas"]["armazens"], "")


class NomeEMoedaTest(unittest.TestCase):
    def test_parece_id(self):
        self.assertTrue(instalar.parece_id("006f0d7f-0ae4-4457-affe-0bbc0ca0c9ec"))
        self.assertFalse(instalar.parece_id("Auto Style"))
        self.assertFalse(instalar.parece_id(""))

    def test_moeda_proposta_e_o_escudo_nao_o_euro(self):
        self.assertEqual(instalar.sugerir_moeda([("EUR", "Euro"), ("CVE", "Escudo de Cabo Verde")]), "CVE")
        self.assertEqual(instalar.sugerir_moeda([("EUR", "Euro"), ("ECV", "Escudo cabo-verdiano")]), "ECV")
        self.assertEqual(instalar.sugerir_moeda([("EUR", "Euro"), ("USD", "Dólar")]), "CVE")
        self.assertEqual(instalar.sugerir_moeda([]), "CVE")


class SenhaExistenteTest(unittest.TestCase):
    def test_reaproveita_a_palavra_passe_dos_config_ini_ja_instalados(self):
        with tempfile.TemporaryDirectory() as tmp:
            raiz = Path(tmp)
            self.assertIsNone(instalar.senha_existente(raiz))
            (raiz / "empresas" / "custom").mkdir(parents=True)
            (raiz / "empresas" / "custom" / "config.ini").write_text(
                "[primavera]\nligacao = DRIVER={ODBC Driver 17 for SQL Server};SERVER=S;DATABASE=PRIX;UID=lonntra_leitura;"
                "PWD=abc-DEF_123aA9;TrustServerCertificate=yes\n", encoding="utf-8")
            self.assertEqual(instalar.senha_existente(raiz), "abc-DEF_123aA9")
            (raiz / "config.ini").write_text("[primavera]\nligacao = DRIVER={SQL Server};SERVER=.;DATABASE=P;"
                                             "Trusted_Connection=yes\n", encoding="utf-8")
            self.assertEqual(instalar.senha_existente(raiz), "abc-DEF_123aA9", "config sem login SQL não conta")


class InstalarVersaoTest(unittest.TestCase):
    def preparar(self, tmp):
        raiz = Path(tmp)
        (raiz / "primavera_sync").mkdir()
        (raiz / "primavera_sync" / "__init__.py").write_text("", encoding="utf-8")
        (raiz / "primavera_sync" / "__pycache__").mkdir()
        (raiz / "primavera_sync" / "__pycache__" / "x.pyc").write_bytes(b"")
        return raiz

    def test_copia_o_pacote_para_versoes_e_aponta_a_empresa(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(instalar, "PASTA", Path(tmp)):
            raiz = self.preparar(tmp)
            empresa = raiz / "empresas" / "custom"
            v = instalar.instalar_versao(empresa)
            self.assertTrue((raiz / "versoes" / v / "primavera_sync" / "__init__.py").is_file())
            self.assertFalse((raiz / "versoes" / v / "primavera_sync" / "__pycache__").exists())
            self.assertEqual((empresa / "atual.txt").read_text(encoding="utf-8"), v)
            self.assertFalse((empresa / "anterior.txt").exists())

    def test_reinstalar_guarda_a_versao_anterior_e_limpa_a_marca_de_falha(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(instalar, "PASTA", Path(tmp)):
            raiz = self.preparar(tmp)
            empresa = raiz / "empresas" / "custom"
            empresa.mkdir(parents=True)
            (empresa / "atual.txt").write_text("1.0.0", encoding="utf-8")
            (raiz / "versoes" / instalar.__version__).mkdir(parents=True)
            (raiz / "versoes" / instalar.__version__ / "FALHOU").write_text("x", encoding="utf-8")
            instalar.instalar_versao(empresa)
            self.assertEqual((empresa / "anterior.txt").read_text(encoding="utf-8"), "1.0.0")
            self.assertFalse((raiz / "versoes" / instalar.__version__ / "FALHOU").exists())
