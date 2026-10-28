import configparser
import tempfile
import unittest
from unittest import mock
from datetime import date, timedelta
from pathlib import Path

from primavera_sync.lonntra import ErroApi, ErroLigacao
from primavera_sync.sincronizador import COLETIVO, Estado, Sincronizador, estado_do_sync

CONFIG = Path(__file__).resolve().parent.parent / "config.example.ini"


class FontePrimavera:
    """Primavera em memória, com a mesma interface do Evolution."""

    def __init__(self):
        self.docs, self.linhas_, self.clientes_, self.artigos_ = [], {}, {}, {}
        self.catalogo_, self.armazens_, self.existencias_, self.movimentos_ = [], [], [], []
        self.desde_movimentos = None

    def catalogo(self, moeda):
        return self.catalogo_

    def armazens(self):
        return self.armazens_

    def existencias(self):
        return self.existencias_

    def movimentos(self, desde):
        self.desde_movimentos = desde
        return [m for m in self.movimentos_ if m["Data"] >= desde]

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
        self.pedidos_artigos, self.pedidos_stock = [], []

    def artigos(self, artigos):
        self.pedidos_artigos.append(artigos)
        erros = [{"codigo": a["codigo"], "erro": "não há taxa de IVA"} for a in artigos if a["taxa_iva"] is None]
        return {"criados": len(artigos) - len(erros), "atualizados": 0, "erros": erros}

    def stock(self, armazens=(), movimentos=(), existencias=()):
        self.pedidos_stock.append({"armazens": list(armazens), "movimentos": list(movimentos), "existencias": list(existencias)})
        erros = [{"codigo": f"{e['artigo']}@{e['armazem']}", "erro": "serviço"} for e in existencias if e["artigo"] == "MO"]
        return {"armazens": len(armazens), "movimentos": len(movimentos), "existencias": len(existencias) - len(erros), "erros": erros}

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
        self.cfg["sincronizacao"]["catalogo"] = "nao"  # estes testes são dos documentos; o catálogo tem os seus
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


class CatalogoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = configparser.ConfigParser(interpolation=None)
        cfg.read(CONFIG, encoding="utf-8")
        cfg["sincronizacao"]["tipos_artigo_servico"] = "0"
        self.fonte, self.api = FontePrimavera(), LonntraFalsa()
        self.estado = Estado(str(Path(self.tmp.name) / "estado.db"))
        self.s = Sincronizador(self.fonte, self.api, self.estado, cfg)
        self.fonte.catalogo_ = [
            {"Artigo": "PN195", "Descricao": "Pneu 195/65", "TipoArtigo": "3", "Taxa": 15, "CodBarras": "560123",
             "Familia": "PNEUS", "FamiliaNome": "Pneus", "Anulado": False, "Custo": 5200, "PVP": 9200, "PVPComIva": True},
            {"Artigo": "MO", "Descricao": "Mão de obra", "TipoArtigo": "0", "Taxa": 15, "CodBarras": None,
             "Familia": None, "FamiliaNome": None, "Anulado": False, "Custo": 0, "PVP": 2500, "PVPComIva": False},
        ]
        self.fonte.armazens_ = [{"Armazem": "A1", "Descricao": "Oficina"}]
        self.fonte.existencias_ = [{"Artigo": "PN195", "Armazem": "A1", "Quantidade": 8}]
        self.fonte.movimentos_ = [
            {"Id": "M1", "Artigo": "PN195", "Armazem": "A1", "Data": date(2020, 3, 1), "EntradaSaida": "E", "Quantidade": 10, "Documento": "VGR 1"},
            {"Id": "M2", "Artigo": "PN195", "Armazem": "A1", "Data": date(2020, 3, 15), "EntradaSaida": None, "Quantidade": -2, "Documento": "FA 7"},
        ]

    def tearDown(self):
        self.estado.conn.close()
        self.tmp.cleanup()

    def test_envia_o_catalogo_e_o_stock_e_depois_so_o_que_mudou(self):
        self.assertEqual(self.s.catalogo(), {"artigos": 2, "armazens": 1, "movimentos": 2, "existencias": 1})
        pneu = self.api.pedidos_artigos[0][0]
        self.assertEqual(pneu, {"codigo": "PN195", "descricao": "Pneu 195/65", "servico": False, "taxa_iva": 15,
                                "preco": 8000, "custo": 5200, "codigo_barras": "560123",
                                "familia": {"codigo": "PNEUS", "nome": "Pneus"}, "ativo": True})
        self.assertTrue(self.api.pedidos_artigos[0][1]["servico"])
        movimentos = [m for p in self.api.pedidos_stock for m in p["movimentos"]]
        self.assertEqual([(m["id"], m["tipo"], m["quantidade"], m["data"]) for m in movimentos],
                         [("M1", "E", 10, "2020-03-01"), ("M2", "S", 2, "2020-03-15")])
        self.assertEqual(self.fonte.desde_movimentos, date(1900, 1, 1), "a primeira vez lê os movimentos todos")

        # segundo ciclo sem mudanças: nada é enviado; depois só o que mudou
        self.assertEqual(self.s.catalogo(), {"artigos": 0, "armazens": 0, "movimentos": 0, "existencias": 0})
        self.fonte.catalogo_[0] = {**self.fonte.catalogo_[0], "PVP": 9775, "Anulado": True}
        self.fonte.existencias_ = [{"Artigo": "PN195", "Armazem": "A1", "Quantidade": 5}]
        self.assertEqual(self.s.catalogo(), {"artigos": 1, "armazens": 0, "movimentos": 0, "existencias": 1})
        self.assertEqual(self.api.pedidos_artigos[-1][0]["preco"], 8500)
        self.assertFalse(self.api.pedidos_artigos[-1][0]["ativo"])
        self.assertNotEqual(self.fonte.desde_movimentos, date(1900, 1, 1), "depois só os movimentos recentes")

    def test_recusas_ficam_registadas_e_repetem(self):
        self.fonte.catalogo_[1] = {**self.fonte.catalogo_[1], "Taxa": None}
        self.fonte.existencias_.append({"Artigo": "MO", "Armazem": "A1", "Quantidade": 1})
        r = self.s.catalogo()
        self.assertEqual((r["artigos"], r["existencias"]), (1, 1))
        _, erros = self.estado.resumo()
        self.assertEqual({e[0] for e in erros}, {"artigo MO", "existencia MO@A1"})
        self.assertEqual(self.s.catalogo()["artigos"], 0)
        self.assertEqual(len(self.api.pedidos_artigos[-1]), 1, "o artigo recusado volta a ser enviado")

    def test_erro_no_stock_nao_impede_os_documentos(self):
        def falha(desde):
            raise RuntimeError("Invalid object name 'INV_Movimentos'")
        self.fonte.movimentos = falha
        self.fonte.docs = [doc("A", "FA", 1, 9200)]
        self.fonte.linhas_ = {"A": [linha("A", "PN195", 1, 8000)]}
        self.fonte.clientes_ = {"C001": {"Cliente": "C001", "Nome": "Cliente"}}
        with self.assertLogs("primavera_sync", level="ERROR"):
            self.assertEqual(self.s.ciclo()["documentos"], 1)


class SinalTest(unittest.TestCase):
    def test_sinal_de_vida_leva_o_resumo_e_o_ultimo_erro(self):
        with tempfile.TemporaryDirectory() as tmp:
            e = Estado(str(Path(tmp) / "e.db"))
            e.marcar("A", "FA 2026/1", "x")
            e.erro("B", "FA 2026/2", "Não há taxa de IVA de 4%")
            r = estado_do_sync(e)
            self.assertEqual((r["documentos"], r["erros"], r["ultimo_erro"]), (1, 1, "FA 2026/2: Não há taxa de IVA de 4%"))
            r = estado_do_sync(e, erro_ciclo="OperationalError: SQL Server não responde")
            self.assertEqual((r["erros"], r["ultimo_erro"]), (2, "OperationalError: SQL Server não responde"))
            self.assertTrue(r["versao"] and r["maquina"])
            e.conn.close()


if __name__ == "__main__":
    unittest.main()


class AmostraTest(unittest.TestCase):
    """O teste de envio do instalador: no máximo N de cada coisa, sem contar como carga completa."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = configparser.ConfigParser(interpolation=None)
        self.cfg.read(CONFIG, encoding="utf-8")
        self.cfg["sincronizacao"]["historico_horario"] = "18-7"
        self.fonte, self.api = FontePrimavera(), LonntraFalsa()
        self.estado = Estado(str(Path(self.tmp.name) / "estado.db"))
        self.s = Sincronizador(self.fonte, self.api, self.estado, self.cfg)
        self.fonte.clientes_ = {"C001": {"Cliente": "C001", "Nome": "Cliente", "NumContrib": "212345678"}}
        hoje = date.today()
        self.fonte.docs = [doc(f"D{i}", "FA", i, 1150, data=hoje - timedelta(days=30 - i)) for i in range(1, 26)]
        self.fonte.linhas_ = {f"D{i}": [linha(f"D{i}", "PNEU", 1, 1000)] for i in range(1, 26)}
        self.fonte.artigos_ = {"PNEU": {"Artigo": "PNEU", "Descricao": "Pneu", "TipoArtigo": "3"}}
        self.fonte.catalogo_ = [{"Artigo": f"A{i}", "Descricao": f"Artigo {i}", "TipoArtigo": "3", "Taxa": 15, "Familia": "F",
                                 "Anulado": 0, "Custo": 1, "PVP": 2, "PVPComIva": 0} for i in range(30)]
        self.fonte.armazens_ = [{"Armazem": f"{i}", "Descricao": f"Armazém {i}"} for i in range(15)]
        self.fonte.existencias_ = [{"Artigo": f"A{i}", "Armazem": "1", "Quantidade": 5} for i in range(30)]
        self.fonte.movimentos_ = [{"Id": f"M{i}", "Artigo": "A1", "Armazem": "1", "Data": hoje - timedelta(days=i),
                                   "EntradaSaida": "E", "Quantidade": 1, "Documento": "X"} for i in range(1, 25)]

    def tearDown(self):
        self.estado.conn.close()
        self.tmp.cleanup()

    def test_envia_no_maximo_dez_de_cada(self):
        r = self.s.amostra(10)
        self.assertEqual(r["documentos"]["enviados"], 10)
        self.assertEqual(r["catalogo"], {"artigos": 10, "armazens": 10, "movimentos": 10, "existencias": 10})
        self.assertEqual(r["erros"], [])
        self.assertEqual(len(self.api.pedidos), 10)

    def test_documentos_sao_os_mais_recentes(self):
        r = self.s.amostra(10)
        self.assertEqual(r["documentos"]["numeros"][-1], "FA 2026/25")
        self.assertEqual(r["documentos"]["numeros"][0], "FA 2026/16")

    def test_nao_conta_como_carga_completa(self):
        self.s.amostra(10)
        self.assertIsNone(self.estado.marca("carga_documentos"))
        self.assertIsNone(self.estado.marca("carga_movimentos"))

    def test_o_resto_segue_depois_sem_repetir_o_teste(self):
        self.s.amostra(10)
        self.s.catalogo()
        self.assertEqual(sum(len(p["movimentos"]) for p in self.api.pedidos_stock if p["movimentos"]), 24)
        self.s.documentos()
        self.assertEqual(len({p["documento"]["id"] for p in self.api.pedidos}), 25, "cada documento vai uma vez")

    def test_catalogo_desligado(self):
        self.cfg["sincronizacao"]["catalogo"] = "nao"
        self.assertIsNone(self.s.amostra(10)["catalogo"])

    def test_erros_da_lonntra_aparecem(self):
        self.fonte.docs = [doc("X", "FA", 1, 100, data=date.today())]
        self.fonte.linhas_ = {"X": [linha("X", "PNEU", 1, 100, taxa=4)]}
        r = self.s.amostra(10)
        self.assertEqual(r["documentos"]["erros"], 1)
        self.assertEqual(len(r["erros"]), 1)


class ImprimirTesteTest(unittest.TestCase):
    def resultado(self, enviados=10, erros=(), catalogo=None):
        return {"documentos": {"enviados": enviados, "erros": 0, "numeros": [], "ja_enviados": 0},
                "catalogo": {"artigos": 0} if catalogo is None else catalogo, "erros": list(erros)}

    def test_recusas_parciais_nao_falham_a_instalacao(self):
        from primavera_sync.sincronizador import imprimir_teste
        r = self.resultado(10, [("artigo X", "Não há taxa de IVA de 6%"), ("artigo Y", "Não há taxa de IVA de 6%")])
        with mock.patch("builtins.print") as imprime:
            self.assertTrue(imprimir_teste(r, 10))
        texto = " ".join(str(c.args[0]) for c in imprime.call_args_list)
        self.assertIn("2x Não há taxa de IVA de 6%", texto)

    def test_nada_chegou_e_ha_recusas_falha(self):
        from primavera_sync.sincronizador import imprimir_teste
        with mock.patch("builtins.print"):
            self.assertFalse(imprimir_teste(self.resultado(0, [("FA 1", "motivo")]), 10))

    def test_nada_a_enviar_sem_erros_nao_falha(self):
        from primavera_sync.sincronizador import imprimir_teste
        with mock.patch("builtins.print"):
            self.assertTrue(imprimir_teste(self.resultado(0), 10))
