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
