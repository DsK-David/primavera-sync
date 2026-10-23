import configparser
import tempfile
import unittest
from datetime import date
from pathlib import Path

from primavera_sync.lonntra import ErroApi, ErroLigacao
from primavera_sync.sincronizador import COLETIVO, Estado, Sincronizador

CONFIG = Path(__file__).resolve().parent.parent / "config.example.ini"


class FontePrimavera:
    """Primavera em memória, com a mesma interface do Evolution."""

    def __init__(self):
        self.docs, self.linhas_, self.clientes_, self.artigos_ = [], {}, {}, {}

    def documentos(self, tipos, desde=None):
        return [d for d in self.docs if d["TipoDoc"] in tipos and (desde is None or d["Data"] >= desde)]

    def linhas(self, ids):
        return {i: self.linhas_[i] for i in ids if i in self.linhas_}

    def clientes(self, codigos):
        return {c: self.clientes_[c] for c in codigos if c in self.clientes_}

    def artigos(self, codigos):
        return {a: self.artigos_[a] for a in codigos if a in self.artigos_}


class LonntraFalsa:
    def __init__(self):
        self.pedidos, self.ids = [], set()
        self.em_baixo = False

    def documento(self, corpo):
        if self.em_baixo:
            raise ErroLigacao("connection refused")
        self.pedidos.append(corpo)
        if corpo["linhas"] and corpo["linhas"][0]["taxa_iva"] == 4:
            raise ErroApi("Não há taxa de IVA de 4% na Lonntra")
        existia = corpo["documento"]["id"] in self.ids
        self.ids.add(corpo["documento"]["id"])
        return {"id": "lt-" + corpo["documento"]["id"], "ja_existia": existia, "avisos": []}


def doc(id_, tipo, num, total, cliente="C001", data=date(2026, 10, 1)):
    return {"Id": id_, "TipoDoc": tipo, "Serie": "2026", "NumDoc": num, "Data": data, "DataVenc": date(2026, 10, 31),
            "Entidade": cliente, "CondPag": "1", "ModoPag": "CHQ", "Moeda": "CVE", "TotalDocumento": total,
            "DescFinanceiro": 0}


def linha(doc_id, artigo, qtd, liquido, taxa=15, origem=None):
    return {"IdCabecDoc": doc_id, "NumLinha": 1, "Artigo": artigo, "Descricao": f"Artigo {artigo}",
            "Quantidade": qtd, "PrecoLiquido": liquido, "TaxaIva": taxa, "IdCabecDocOrigem": origem}


class SincronizadorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = configparser.ConfigParser(interpolation=None)
        self.cfg.read(CONFIG, encoding="utf-8")
        self.cfg["sincronizacao"]["tipos_artigo_servico"] = "0"
        self.fonte, self.api = FontePrimavera(), LonntraFalsa()
        self.estado = Estado(str(Path(self.tmp.name) / "estado.db"))
        self.s = Sincronizador(self.fonte, self.api, self.estado, self.cfg)
        self.fonte.clientes_ = {
            "C001": {"Cliente": "C001", "Nome": "Oficina Lima, Lda", "NumContrib": "212 345 678",
                     "Fac_Mor": "Rua A", "Fac_Local": "Praia", "Fac_Tel": "9912345"},
            "VD": {"Cliente": "VD", "Nome": "Consumidor final", "NumContrib": "999999999"},
        }
        self.fonte.artigos_ = {"PNEU": {"Artigo": "PNEU", "Descricao": "Pneu", "TipoArtigo": "3"},
                               "MO": {"Artigo": "MO", "Descricao": "Mão de obra", "TipoArtigo": "0"}}

    def tearDown(self):
        self.estado.conn.close()
        self.tmp.cleanup()

    def test_envia_o_documento_completo_uma_so_vez(self):
        self.fonte.docs = [doc("A", "FA", 1, 21275)]
        self.fonte.linhas_ = {"A": [linha("A", "PNEU", 2, 16000), linha("A", "MO", 1, 2500)]}
        self.assertEqual(self.s.ciclo(), {"documentos": 1, "erros": 0})
        self.assertEqual(self.s.ciclo(), {"documentos": 0, "erros": 0})

        p = self.api.pedidos[0]
        self.assertEqual(p["documento"], {
            "id": "A", "tipo": "FT", "numero": "FA 2026/1", "data": "2026-10-01", "vencimento": "2026-10-31",
            "total": 21275, "desconto_financeiro": 0, "metodo_pagamento": "20", "origem_id": None})
        self.assertEqual(p["cliente"], {"codigo": "C001", "nome": "Oficina Lima, Lda", "nif": "212345678",
                                        "morada": "Rua A, Praia", "telefone": "9912345", "coletivo": True})
        self.assertEqual(p["linhas"][0], {"artigo": "PNEU", "descricao": "Artigo PNEU", "quantidade": 2,
                                          "total_sem_iva": 16000, "taxa_iva": 15, "servico": False})
        self.assertTrue(p["linhas"][1]["servico"])

    def test_consumidor_final_vai_sem_nif(self):
        self.fonte.docs = [doc("B", "VD", 7, 9200, cliente="VD")]
        self.fonte.linhas_ = {"B": [linha("B", "PNEU", 1, 8000)]}
        self.s.ciclo()
        p = self.api.pedidos[0]
        self.assertEqual(p["documento"]["tipo"], "TV")
        self.assertIsNone(p["cliente"]["nif"])
        self.assertFalse(p["cliente"]["coletivo"])

    def test_nota_de_credito_leva_o_documento_de_origem(self):
        self.fonte.docs = [doc("A", "FA", 1, 18400), doc("N", "NC", 1, 9200)]
        self.fonte.linhas_ = {"A": [linha("A", "PNEU", 2, 16000)], "N": [linha("N", "PNEU", 1, 8000, origem="A")]}
        self.s.ciclo()
        self.assertEqual(self.api.pedidos[1]["documento"]["origem_id"], "A")
        self.assertEqual(self.api.pedidos[1]["documento"]["tipo"], "NC")

    def test_recusa_da_lonntra_fica_em_erro_e_nao_trava_os_outros(self):
        self.fonte.docs = [doc("A", "FA", 1, 1040), doc("B", "FA", 2, 9200)]
        self.fonte.linhas_ = {"A": [linha("A", "OLEO", 1, 1000, taxa=4)], "B": [linha("B", "PNEU", 1, 8000)]}
        self.assertEqual(self.s.ciclo(), {"documentos": 1, "erros": 1})
        _, erros = self.estado.resumo()
        self.assertEqual(erros[0][0], "FA 2026/1")
        # tenta outra vez nos ciclos seguintes, até max_tentativas
        for _ in range(10):
            self.s.ciclo()
        self.assertEqual(len([p for p in self.api.pedidos if p["documento"]["id"] == "A"]), 5)

    def test_lonntra_em_baixo_para_o_ciclo_e_repete_depois(self):
        self.fonte.docs = [doc("A", "FA", 1, 18400)]
        self.fonte.linhas_ = {"A": [linha("A", "PNEU", 2, 16000)]}
        self.api.em_baixo = True
        with self.assertRaises(ErroLigacao):
            self.s.ciclo()
        self.api.em_baixo = False
        self.assertEqual(self.s.ciclo()["documentos"], 1)

    def test_tipos_nao_configurados_e_outras_moedas_nao_vao(self):
        self.fonte.docs = [doc("G", "GR", 1, 100), {**doc("E", "FA", 2, 100), "Moeda": "EUR"}]
        self.fonte.linhas_ = {"E": [linha("E", "PNEU", 1, 87)]}
        self.assertEqual(self.s.ciclo(), {"documentos": 0, "erros": 1})
        self.assertEqual(self.api.pedidos, [])

    def test_depois_da_primeira_carga_so_rele_os_recentes(self):
        self.fonte.docs = [doc("A", "FA", 1, 18400, data=date(2020, 1, 1))]
        self.fonte.linhas_ = {"A": [linha("A", "PNEU", 2, 16000)]}
        self.assertEqual(self.s.ciclo()["documentos"], 1)   # primeira carga: todo o histórico
        self.fonte.docs.append(doc("B", "FA", 2, 9200, data=date(2020, 1, 2)))
        self.fonte.linhas_["B"] = [linha("B", "PNEU", 1, 8000)]
        self.assertEqual(self.s.ciclo()["documentos"], 0)   # antigo demais para a revisão
        self.assertEqual(self.s.ciclo(completo=True)["documentos"], 1)

    def test_tipo_de_cliente_pelo_nome(self):
        for nome in ("Auto Peças, Lda", "Transportes Ilha S.A.", "Rent Unipessoal", "SA Comércio"):
            self.assertTrue(COLETIVO.search(nome), nome)
        for nome in ("Maria Lima", "João Salazar", "Sandra Assis"):
            self.assertFalse(COLETIVO.search(nome), nome)


if __name__ == "__main__":
    unittest.main()
