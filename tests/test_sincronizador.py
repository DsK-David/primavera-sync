import configparser
import tempfile
import unittest
from unittest import mock
from datetime import date, datetime, timedelta
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
        self.recebimentos_, self.totais_, self.recebimentos_prontos = [], {}, False

    def recebimentos(self, desde=None):
        return [r for r in self.recebimentos_ if desde is None or str(r.get("data_recebimento") or r.get("Data"))[:10] >= desde.isoformat()]

    def totais(self, ids):
        return {i: self.totais_[i] for i in ids if i in self.totais_}

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
        self.pedidos_recebimentos, self.recusar_recebimentos = [], {}

    def recebimentos(self, lista):
        self.pedidos_recebimentos.append(list(lista))
        erros = [{"codigo": r["id"], "erro": self.recusar_recebimentos[r["id"]]} for r in lista if r["id"] in self.recusar_recebimentos]
        return {"recebimentos": len(lista) - len(erros), "erros": erros}

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

    def test_cliente_apagado_usa_os_dados_do_proprio_documento(self):
        d = {**doc("A", "FA", 1, 2500, cliente="CLI9"), "NomeDoc": "Cliente Indiferenciado", "NifDoc": "000000000", "MoradaDoc": "praia", "LocalDoc": "praia"}
        self.fonte.docs = [d]
        self.fonte.linhas_ = {"A": [linha("A", "MO", 1, 2500)]}
        self.assertEqual(self.s.ciclo(), {"documentos": 1, "erros": 0})
        cliente = self.api.pedidos[-1]["cliente"]
        self.assertEqual((cliente["codigo"], cliente["nome"], cliente["nif"]), ("CLI9", "Cliente Indiferenciado", None))
        self.assertEqual(cliente["morada"], "praia, praia")

    def test_cliente_apagado_sem_dados_no_documento_continua_a_dar_erro(self):
        self.fonte.docs = [doc("A", "FA", 1, 2500, cliente="CLI9")]
        self.fonte.linhas_ = {"A": [linha("A", "MO", 1, 2500)]}
        self.assertEqual(self.s.ciclo(), {"documentos": 0, "erros": 1})

    def test_devolucoes_vao_como_notas_de_devolucao(self):
        self.assertEqual(self.s.tipos["DEV"], "DV")
        self.assertEqual(self.s.tipos["DV"], "DV")
        d = doc("A", "DEV", 1, -2500)
        self.fonte.docs = [d]
        self.fonte.linhas_ = {"A": [linha("A", "MO", -1, -2500)]}
        self.assertEqual(self.s.ciclo(), {"documentos": 1, "erros": 0})
        corpo = self.api.pedidos[-1]
        self.assertEqual((corpo["documento"]["tipo"], corpo["documento"]["total"]), ("DV", 2500.0))
        self.assertEqual(corpo["linhas"][0]["quantidade"], 1.0)

    def test_a_origem_da_nota_vem_do_cabecalho(self):
        d = {**doc("A", "NC", 1, -2500), "RefTipo": "fa", "RefSerie": "2026", "RefNum": "35"}
        self.fonte.docs = [d, doc("B", "NC", 2, -900)]
        self.fonte.linhas_ = {"A": [linha("A", "MO", -1, -2500)], "B": [linha("B", "MO", -1, -900)]}
        self.s.ciclo()
        origens = {p["documento"]["numero"]: p["documento"]["origem_numero"] for p in self.api.pedidos}
        self.assertEqual(origens, {"NC 2026/1": "FA 2026/35", "NC 2026/2": None})

    def test_proforma_e_fatura_recibo_de_outra_serie_corrigem_o_mapa_antigo(self):
        from primavera_sync.sincronizador import tipos_de
        self.cfg["tipos_documento"]["FP"] = "FT"      # como as primeiras instalações fizeram
        self.cfg["tipos_documento"]["FR1"] = "FT"
        self.assertEqual((tipos_de(self.cfg)["FP"], tipos_de(self.cfg)["FR1"]), ("FP", "FR"))
        self.cfg["tipos_documento"]["FP"] = "FP"
        self.cfg["tipos_documento"]["FR1"] = "TV"      # uma escolha deliberada da empresa não se mexe
        self.assertEqual((tipos_de(self.cfg)["FP"], tipos_de(self.cfg)["FR1"]), ("FP", "TV"))

    def test_reenviar_um_tipo_revê_todos_os_documentos_dele(self):
        self.fonte.docs = [doc("A", "FA", 1, 2500), doc("B", "FA", 2, 900), doc("C", "NC", 1, -100)]
        self.fonte.linhas_ = {"A": [linha("A", "MO", 1, 2500)], "B": [linha("B", "MO", 1, 900)], "C": [linha("C", "MO", -1, -100)]}
        self.s.ciclo()
        n = len(self.api.pedidos)
        texto = self.s.reenviar_documento("FA")
        self.assertIn("2 documento(s) revistos", texto)
        self.assertEqual(len(self.api.pedidos), n + 2, "só os FA, não a NC")
        self.assertIn("não é um tipo", self.s.reenviar_documento("GR"))

    def test_devolucoes_podem_desligar_se_ou_mapear_se_de_outra_forma(self):
        from primavera_sync.sincronizador import tipos_de
        self.cfg["sincronizacao"]["devolucoes"] = "nao"
        self.assertNotIn("DEV", tipos_de(self.cfg))
        self.cfg["sincronizacao"]["devolucoes"] = "sim"
        self.cfg["tipos_documento"]["DEV"] = "NC"
        self.assertEqual(tipos_de(self.cfg)["DEV"], "NC")

    def test_reenviar_um_documento_que_o_estado_da_como_enviado(self):
        self.fonte.docs = [doc("A", "FA", 1, 2500), doc("B", "FA", 2, 900)]
        self.fonte.linhas_ = {"A": [linha("A", "MO", 1, 2500)], "B": [linha("B", "MO", 1, 900)]}
        self.s.ciclo()
        self.api.ids.clear()  # a Lonntra perdeu-os (ou nunca os guardou)
        n = len(self.api.pedidos)
        texto = self.s.reenviar_documento("FA 2026/1")
        self.assertIn("reenviado", texto)
        self.assertEqual(len(self.api.pedidos), n + 1, "só esse documento")
        self.assertIn("A", self.estado.enviados())
        self.assertIn("não existe", self.s.reenviar_documento("FA 2026/99"))

    def test_envia_o_documento_completo_uma_so_vez(self):
        self.fonte.docs = [doc("A", "FA", 1, 21275)]
        self.fonte.linhas_ = {"A": [linha("A", "PNEU", 2, 16000), linha("A", "MO", 1, 2500)]}
        self.assertEqual(self.s.ciclo(), {"documentos": 1, "erros": 0})
        self.assertEqual(self.s.ciclo(), {"documentos": 0, "erros": 0})

        p = self.api.pedidos[0]
        self.assertEqual(p["documento"], {
            "id": "A", "tipo": "FT", "numero": "FA 2026/1", "data": "2026-10-01", "vencimento": "2026-10-31",
            "total": 21275, "desconto_financeiro": 0, "metodo_pagamento": "20", "origem_id": None,
            "origem_numero": None})
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
        hoje = date.today()
        self.fonte.docs = [doc("A", "FA", 1, 1040, data=hoje), doc("B", "FA", 2, 9200, data=hoje)]
        self.fonte.linhas_ = {"A": [linha("A", "OLEO", 1, 1000, taxa=4)], "B": [linha("B", "PNEU", 1, 8000)]}
        self.assertEqual(self.s.ciclo(), {"documentos": 1, "erros": 1})
        _, erros = self.estado.resumo()
        self.assertEqual(erros[0][0], "FA 2026/1")
        # não insiste de minuto a minuto: espera 5 min, depois 10, 20… (e nunca desiste)
        for _ in range(5):
            self.s.ciclo()
        self.assertEqual(len([p for p in self.api.pedidos if p["documento"]["id"] == "A"]), 1)
        for esperado in (2, 3, 4):
            self.recuar_ultima_tentativa(minutos=24 * 60)
            self.s.ciclo()
            self.assertEqual(len([p for p in self.api.pedidos if p["documento"]["id"] == "A"]), esperado)

    def recuar_ultima_tentativa(self, minutos):
        antes = (datetime.now() - timedelta(minutes=minutos)).isoformat(timespec="seconds")
        with self.estado.conn:
            self.estado.conn.execute("UPDATE erros SET ultima=?", (antes,))

    def test_documento_recusado_e_corrigido_depois_segue_mesmo_com_mais_de_30_dias(self):
        antigo = date.today() - timedelta(days=90)
        self.fonte.docs = [doc("A", "FA", 1, 1040, data=antigo)]
        self.fonte.linhas_ = {"A": [linha("A", "OLEO", 1, 1000, taxa=4)]}
        self.assertEqual(self.s.ciclo(), {"documentos": 0, "erros": 1})  # carga completa: lê tudo
        self.assertEqual(self.estado.datas_de_erros(), {"A": antigo.isoformat()})
        # a Lonntra passa a aceitar (criou-se a taxa): a linha passa a ter uma taxa que ela conhece
        self.fonte.linhas_ = {"A": [linha("A", "OLEO", 1, 1000, taxa=15)]}
        self.s.ciclo()  # ainda dentro da espera de 5 min
        self.assertNotIn("A", self.estado.enviados())
        self.recuar_ultima_tentativa(minutos=10)
        self.assertEqual(self.s.ciclo(), {"documentos": 1, "erros": 0})
        self.assertIn("A", self.estado.enviados())
        self.assertEqual(self.estado.resumo()[1], [])

    def test_espera_entre_tentativas(self):
        self.assertEqual([Estado.espera(n) for n in (1, 2, 3, 4, 10, 20)], [5, 10, 20, 40, 1440, 1440])

    def test_estado_antigo_sem_a_coluna_data_migra(self):
        caminho = str(Path(self.tmp.name) / "velho.db")
        import sqlite3
        c = sqlite3.connect(caminho)
        c.execute("CREATE TABLE erros (documento TEXT PRIMARY KEY, numero TEXT NOT NULL, mensagem TEXT NOT NULL, "
                  "tentativas INTEGER NOT NULL, ultima TEXT NOT NULL)")
        c.execute("INSERT INTO erros VALUES ('A','FA 1','x',1,'2026-10-01T10:00:00')")
        c.commit()
        c.close()
        e = Estado(caminho)
        e.erro("A", "FA 1", "y", "2026-09-01")
        self.assertEqual(e.datas_de_erros(), {"A": "2026-09-01"})
        e.conn.close()

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

    def test_recusas_ficam_registadas(self):
        self.fonte.catalogo_[1] = {**self.fonte.catalogo_[1], "Taxa": None}
        r = self.s.catalogo()
        self.assertEqual(r["artigos"], 1)
        _, erros = self.estado.resumo()
        self.assertEqual({e[0] for e in erros}, {"artigo MO"})

    def test_item_recusado_nao_se_reenvia_no_mesmo_dia_mas_volta_amanha(self):
        self.fonte.catalogo_[1] = {**self.fonte.catalogo_[1], "Taxa": None}
        self.s.catalogo()
        self.assertEqual(self.s.catalogo()["artigos"], 0)
        self.assertEqual(len(self.api.pedidos_artigos), 1, "hoje não volta a ser enviado")
        with mock.patch.object(Sincronizador, "_marca", staticmethod(lambda v: f"{v}@amanha")):
            self.s.catalogo()
        self.assertEqual(len(self.api.pedidos_artigos), 2, "no dia seguinte tenta-se outra vez")

    def test_item_recusado_que_muda_reenvia_logo(self):
        self.fonte.catalogo_[1] = {**self.fonte.catalogo_[1], "Taxa": None}
        self.s.catalogo()
        self.fonte.catalogo_[1] = {**self.fonte.catalogo_[1], "Descricao": "Mão de obra (nova)"}
        self.s.catalogo()
        self.assertEqual(len(self.api.pedidos_artigos), 2)

    def test_servicos_nao_enviam_existencias_e_limpam_erros_antigos(self):
        self.fonte.existencias_.append({"Artigo": "MO", "Armazem": "A1", "Quantidade": 1})
        self.estado.erro("existencia:MO@A1", "existencia MO@A1", "é um serviço")
        r = self.s.catalogo()
        self.assertEqual(r["existencias"], 1, "só o artigo com stock")
        enviadas = [e["artigo"] for p in self.api.pedidos_stock for e in p["existencias"]]
        self.assertNotIn("MO", enviadas)
        self.assertEqual(self.estado.resumo()[1], [], "o erro antigo desaparece")

    def test_movimentos_de_servicos_tambem_se_enviam(self):
        """O histórico de stock fica igual ao do Primavera: tudo o que existir lá existe na Lonntra (só as existências dos serviços não vão)."""
        self.s.codigos_servico = {"MO"}
        self.fonte.movimentos_.append({"Id": "M3", "Artigo": "MO", "Armazem": "A1", "Data": date(2020, 4, 1), "EntradaSaida": "S",
                                       "Quantidade": 1, "Documento": "FA 9"})
        self.s._stock(False)
        enviados = [m["id"] for p in self.api.pedidos_stock for m in p["movimentos"]]
        self.assertEqual(sorted(enviados), ["M1", "M2", "M3"])

    def test_recusas_de_movimentos_de_antes_voltam_a_tentar_se_uma_vez(self):
        self.estado.guardar_recusas("movimento", {"M1": self.s._marca("")})
        self.s._stock(False)
        enviados = [m["id"] for p in self.api.pedidos_stock for m in p["movimentos"]]
        self.assertIn("M1", enviados, "a recusa antiga não o impede de seguir")
        self.assertEqual(self.estado.marca("recusas_movimentos_servicos"), "1")

    def test_erros_antigos_de_itens_ja_aceites_desaparecem(self):
        """Versões antigas não apagavam o erro quando o item passava a ser aceite: ficava «com erros» para sempre."""
        self.estado.guardar_catalogo("existencia", {"PN195|A1": "8.0000"})
        self.estado.guardar_catalogo("artigo", {"PN195": "h"})
        self.estado.guardar_catalogo("movimento", {"M1": ""})
        self.estado.erro("existencia:PN195@A1", "existencia PN195@A1", "o artigo não está na Lonntra")
        self.estado.erro("artigo:PN195", "artigo PN195", "sem IVA")
        self.estado.erro("movimento:M1", "movimento M1", "x")
        self.estado.erro("existencia:OUTRO@A1", "existencia OUTRO@A1", "continua recusado")  # não está no catálogo: fica
        self.estado.erro("FA 2026/1", "FA 2026/1", "documento com erro")  # documentos não se tocam
        self.assertEqual(self.estado.limpar_erros_resolvidos(), 3)
        self.assertEqual(sorted(e[0] for e in self.estado.resumo()[1]), ["FA 2026/1", "existencia OUTRO@A1"])

    def test_existencia_aceite_depois_apaga_o_erro_dela(self):
        self.fonte.existencias_ = [{"Artigo": "PN195", "Armazem": "A1", "Quantidade": 3}]
        self.estado.erro("existencia:PN195@A1", "existencia PN195@A1", "o artigo não está na Lonntra")
        self.s.catalogo()
        self.assertEqual(self.estado.resumo()[1], [])

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


class RobustezTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = configparser.ConfigParser(interpolation=None)
        self.cfg.read(CONFIG, encoding="utf-8")
        self.cfg["sincronizacao"]["catalogo"] = "nao"
        self.fonte, self.api = FontePrimavera(), LonntraFalsa()
        self.estado = Estado(str(Path(self.tmp.name) / "estado.db"))
        self.s = Sincronizador(self.fonte, self.api, self.estado, self.cfg)
        self.fonte.clientes_ = {"C001": {"Cliente": "C001", "Nome": "Cliente"}}
        self.fonte.artigos_ = {"PNEU": {"Artigo": "PNEU", "Descricao": "Pneu", "TipoArtigo": "3"}}

    def tearDown(self):
        self.estado.conn.close()
        self.tmp.cleanup()

    def test_um_documento_com_dados_estranhos_nao_para_os_outros(self):
        hoje = date.today()
        estranho = doc("X", "FA", 1, 100, data=hoje)
        del estranho["TotalDocumento"]  # KeyError ao preparar o corpo
        self.fonte.docs = [estranho, doc("B", "FA", 2, 9200, data=hoje)]
        self.fonte.linhas_ = {"X": [linha("X", "PNEU", 1, 100)], "B": [linha("B", "PNEU", 1, 8000)]}
        with self.assertLogs("primavera_sync", level="ERROR"):
            r = self.s.ciclo()
        self.assertEqual(r, {"documentos": 1, "erros": 1})
        self.assertIn("KeyError", self.estado.resumo()[1][0][1])

    def test_a_lonntra_em_baixo_continua_a_parar_o_ciclo(self):
        self.fonte.docs = [doc("A", "FA", 1, 100, data=date.today())]
        self.fonte.linhas_ = {"A": [linha("A", "PNEU", 1, 100)]}
        self.api.em_baixo = True
        with self.assertRaises(ErroLigacao):
            self.s.ciclo()

    def test_o_sinal_nunca_derruba_o_sync(self):
        from primavera_sync.sincronizador import dar_sinal

        class Recusa:
            def sinal(self, estado):
                raise ErroApi("422")

        with self.assertLogs("primavera_sync", level="WARNING"):
            self.assertFalse(dar_sinal(Recusa(), self.estado))

        class Boa:
            def sinal(self, estado):
                return {"ok": True}
        self.assertTrue(dar_sinal(Boa(), self.estado))

    def test_copia_diaria_do_estado(self):
        self.estado.marcar("A", "FA 1", "x")
        self.estado.copia_diaria()
        copias = list(Path(self.tmp.name).glob("estado.db.*.bak"))
        self.assertEqual(len(copias), 1)
        copia = Estado(str(copias[0]))
        self.assertEqual(copia.resumo()[0], 1)
        copia.conn.close()
        self.estado.copia_diaria()  # no mesmo dia não repete
        self.assertEqual(len(list(Path(self.tmp.name).glob("estado.db.*.bak"))), 1)

    def test_copias_antigas_sao_apagadas(self):
        for dia in range(1, 11):
            (Path(self.tmp.name) / f"estado.db.2020-01-{dia:02d}.bak").write_bytes(b"")
        self.estado.copia_diaria(manter=7)
        self.assertLessEqual(len(list(Path(self.tmp.name).glob("estado.db.*.bak"))), 7)


class SaudeTest(unittest.TestCase):
    def test_saude_regista_o_ciclo_bom_e_mantem_o_ultimo_bom_quando_falha(self):
        import json
        from primavera_sync.sincronizador import escrever_saude
        with tempfile.TemporaryDirectory() as tmp:
            escrever_saude(tmp, None, ciclo_ok=True, agora=100.0)
            s = json.loads((Path(tmp) / "saude.json").read_text(encoding="utf-8"))
            self.assertEqual((s["ciclo_ok_em"], s["ultimo_erro"]), (100.0, None))
            escrever_saude(tmp, "Lonntra: 500", ciclo_ok=False, agora=200.0)
            s = json.loads((Path(tmp) / "saude.json").read_text(encoding="utf-8"))
            self.assertEqual((s["ciclo_ok_em"], s["ciclo_em"], s["ultimo_erro"]), (100.0, 200.0, "Lonntra: 500"))

    def test_saude_nao_herda_o_ciclo_bom_de_outra_versao(self):
        import json
        from primavera_sync.sincronizador import escrever_saude
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "saude.json").write_text(json.dumps({"versao": "0.0.1", "ciclo_ok_em": 999.0}), encoding="utf-8")
            escrever_saude(tmp, "erro", ciclo_ok=False, agora=5.0)
            self.assertIsNone(json.loads((Path(tmp) / "saude.json").read_text(encoding="utf-8"))["ciclo_ok_em"])

    def test_saude_numa_pasta_que_nao_existe_nao_derruba(self):
        from primavera_sync.sincronizador import escrever_saude
        with self.assertLogs("primavera_sync", level="WARNING"):
            escrever_saude("/nao/existe/mesmo", None)
