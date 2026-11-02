import configparser
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

from primavera_sync import recebimentos as r
from primavera_sync.descobrir import Tabela
from primavera_sync.esquema import ErroEsquema, Esquema
from primavera_sync import esquema
from primavera_sync.remoto import validar_comando, validar_config
from primavera_sync.sincronizador import Estado, Sincronizador
from tests.test_sincronizador import CONFIG, FontePrimavera, LonntraFalsa

ENVIADOS = [("D1", "FA 2026/12"), ("D2", "FA 2026/13"), ("D3", "FR 2026/5")]
TOTAIS = {"D1": 1150.0, "D2": 575.0, "D3": 230.0}


def mov(doc, liquido, iva, data="2026-10-20", id_="m", retencao=0, selo=0, moeda="CVE"):
    return {"id": id_, "documento": doc, "entidade": "C001", "data_recebimento": data, "moeda": moeda,
            "liquido": liquido, "iva": iva, "retencao": retencao, "selo": selo}


LINHAS = [mov("FA 2026/12", 500, 75, id_="1"), mov("FA 2026/12", 500, 75, id_="2"),   # duas linhas somam o total (1150)
          mov("2026/13", 500, 75, "2026-10-21", id_="3"),                              # outra forma de escrever o número
          mov("FR 2026/0005", 200, 30, "2026-10-22", id_="4")]                         # com zeros à esquerda


class NormalizarECasarTest(unittest.TestCase):
    def test_o_mesmo_numero_escrito_de_formas_diferentes(self):
        for a, b in (("FA 2026/0012", "fa2026/12"), ("FA  2026 / 12", "FA 2026/12"), ("fa-2026/12", "FA 2026/12")):
            self.assertEqual(r.normalizar(a), r.normalizar(b), (a, b))
        self.assertNotEqual(r.normalizar("FA 2026/12"), r.normalizar("FA 2026/13"))

    def test_casa_pela_chave_mais_especifica(self):
        c = r.Casador(ENVIADOS)
        self.assertEqual(c.casar("FA 2026/12"), ("D1", "completo"))
        self.assertEqual(c.casar("2026/13"), ("D2", "serie/numero"))
        self.assertEqual(c.casar("FR2026/0005"), ("D3", "completo"))
        self.assertEqual(c.casar("FA13"), ("D2", "tipo+numero"))
        self.assertEqual(c.casar("999/1"), (None, None))

    def test_chaves_ambiguas_nunca_se_usam(self):
        c = r.Casador([("A", "FA 2026/12"), ("B", "FA 2025/12"), ("C", "FR 2026/12")])
        self.assertEqual(c.casar("12"), (None, None), "o número sozinho serve vários documentos")
        self.assertEqual(c.casar("2026/12"), (None, None), "série/número também")
        self.assertEqual(c.casar("FA 2025/12"), ("B", "completo"))
        self.assertGreater(c.ambiguas, 0)

    def test_numero_sem_formato_usa_so_a_chave_completa(self):
        self.assertEqual(r.chaves_do_documento("abc")[0][0], "completo")


class AnaliseTest(unittest.TestCase):
    def analisar(self, linhas=LINHAS, totais=TOTAIS, **kw):
        return r.analisar(linhas, ENVIADOS, totais, **kw)

    def test_casa_e_escolhe_a_formula_que_bate_com_os_totais(self):
        a = self.analisar()
        self.assertEqual((a["movimentos"], a["casados"], a["documentos"]), (4, 4, 3))
        self.assertEqual(a["formula"], "liquido+iva")
        self.assertTrue(a["fiavel"], a["motivo"])
        self.assertEqual(a["confere"]["liquido+iva"], 3)
        self.assertEqual(a["confere"]["liquido"], 0)

    def test_escolhe_a_formula_com_retencao_quando_e_essa_que_bate(self):
        linhas = [mov("FA 2026/12", 1000, 150, retencao=100, id_="1"), mov("FA 2026/13", 500, 75, retencao=50, id_="2")]
        a = self.analisar(linhas, {"D1": 1050.0, "D2": 525.0})
        self.assertEqual(a["formula"], "liquido+iva-retencao")
        self.assertTrue(a["fiavel"])

    def test_nao_e_fiavel_se_poucos_movimentos_casam(self):
        linhas = [mov(f"ZZ {i}/1", 10, 1, id_=str(i)) for i in range(8)] + LINHAS[:2]
        a = self.analisar(linhas)
        self.assertFalse(a["fiavel"])
        self.assertIn("casam com uma fatura", a["motivo"])
        self.assertEqual(a["n_sem_casar"], 8)

    def test_nao_e_fiavel_se_nenhuma_formula_bate(self):
        a = self.analisar(totais={"D1": 9999.0, "D2": 9999.0, "D3": 9999.0})
        self.assertFalse(a["fiavel"])
        self.assertIn("nenhuma fórmula", a["motivo"])

    def test_outras_moedas_ficam_de_fora(self):
        a = self.analisar(LINHAS + [mov("FA 2026/12", 1, 1, moeda="EUR", id_="9")])
        self.assertEqual((a["movimentos"], a["na_moeda"]), (5, 4))

    def test_sem_movimentos_nao_e_fiavel_e_nao_rebenta(self):
        a = self.analisar([])
        self.assertFalse(a["fiavel"])
        self.assertIn("moeda", a["motivo"])

    def test_formula_fixada_pela_empresa(self):
        self.assertEqual(self.analisar(formula="liquido")["formula"], "liquido")

    def test_totais_pode_ser_uma_funcao_que_so_pede_os_casados(self):
        pedidos = []
        def totais(ids):
            pedidos.append(sorted(ids))
            return TOTAIS
        self.analisar(totais=totais)
        self.assertEqual(pedidos, [["D1", "D2", "D3"]])

    def test_um_recebimento_por_fatura_e_data_com_id_estavel(self):
        linhas = LINHAS + [mov("FA 2026/12", 100, 15, "2026-10-25", id_="5")]
        itens = r.recebimentos_a_enviar(self.analisar(linhas, {**TOTAIS, "D1": 1265.0}))
        por_id = {i["id"]: i for i in itens}
        self.assertEqual(por_id["D1|20261020"]["valor"], 1150.0)
        self.assertEqual(por_id["D1|20261025"]["valor"], 115.0)
        self.assertEqual(len(itens), 4)
        self.assertEqual(por_id["D2|20261021"]["documento_id"], "D2")

    def test_relatorio_em_texto(self):
        texto = r.formatar(self.analisar())
        self.assertIn("PRONTO PARA ENVIAR", texto)
        self.assertIn("<- escolhida", texto)
        self.assertIn("NÃO ENVIA", r.formatar(self.analisar(totais={"D1": 1.0, "D2": 1.0, "D3": 1.0})))


def cfg_com(recebimentos="sim", extra=None):
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(CONFIG, encoding="utf-8")
    cfg["sincronizacao"]["catalogo"] = "nao"
    cfg["sincronizacao"]["recebimentos"] = recebimentos
    for sec, valores in (extra or {}).items():
        cfg[sec] = valores
    return cfg


class SincronizadorRecebimentosTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fonte, self.api = FontePrimavera(), LonntraFalsa()
        self.estado = Estado(str(Path(self.tmp.name) / "estado.db"))
        for doc_id, numero in ENVIADOS:
            self.estado.marcar(doc_id, numero, "lt-" + doc_id)
        hoje = date.today()
        self.fonte.totais_ = dict(TOTAIS)
        self.fonte.recebimentos_ = [dict(l, data_recebimento=(hoje - timedelta(days=i)).isoformat()) for i, l in enumerate(LINHAS)]
        self.sinc = Sincronizador(self.fonte, self.api, self.estado, cfg_com())

    def tearDown(self):
        self.estado.conn.close()
        self.tmp.cleanup()

    def enviados(self):
        return [i for p in self.api.pedidos_recebimentos for i in p]

    def test_desligado_por_omissao(self):
        s = Sincronizador(self.fonte, self.api, self.estado, cfg_com("não"))
        s.ciclo()
        self.assertEqual(self.api.pedidos_recebimentos, [])

    def test_envia_uma_vez_e_so_reenvia_o_que_mudou(self):
        self.sinc.ciclo()
        self.assertEqual({i["documento_id"] for i in self.enviados()}, {"D1", "D2", "D3"})
        n = len(self.enviados())
        self.sinc.ciclo()
        self.assertEqual(len(self.enviados()), n, "igual: não reenvia")
        # a linha de uma fatura mudou (outra linha recebida no mesmo dia)
        self.fonte.recebimentos_.append(dict(LINHAS[0], id="99", liquido=0.0, iva=0.0, data_recebimento=self.fonte.recebimentos_[0]["data_recebimento"]))
        self.sinc.ciclo()
        self.assertEqual(len(self.enviados()), n, "o valor somado não mudou")
        self.fonte.recebimentos_[0]["liquido"] = 400
        self.fonte.totais_["D1"] = 1050.0
        self.sinc.ciclo()
        self.assertEqual(len(self.enviados()), n + 1, "só o recebimento que mudou")
        hoje = date.today().isoformat().replace("-", "")
        self.assertEqual(self.api.pedidos_recebimentos[-1], [{"id": f"D1|{hoje}", "documento_id": "D1", "data": date.today().isoformat(), "valor": 475.0}])

    def test_recusa_da_lonntra_fica_registada_e_nao_repete_no_mesmo_dia(self):
        self.api.recusar_recebimentos = {"D1|" + self.fonte.recebimentos_[0]["data_recebimento"].replace("-", ""): "passa do que falta pagar (0.00)"}
        self.sinc.ciclo()
        self.assertIn("recebimento:D1", " ".join(f"{e[0]}" for e in self.estado.resumo()[1]).replace("recebimento D1|", "recebimento:D1|"))
        n = len(self.enviados())
        self.sinc.ciclo()
        self.assertEqual(len(self.enviados()), n)

    def test_relatorio_nao_fiavel_nao_envia_nada_mas_diz_porque_e_os_documentos_seguem(self):
        self.fonte.totais_ = {"D1": 1.0, "D2": 1.0, "D3": 1.0}
        with self.assertLogs("primavera_sync", level="WARNING"):
            self.sinc.ciclo()
        self.assertEqual(self.api.pedidos_recebimentos, [])
        erros = self.estado.resumo()[1]
        self.assertTrue(any("nenhuma fórmula" in e[1] for e in erros))

    def test_um_erro_nos_recebimentos_nao_trava_os_documentos(self):
        self.fonte.recebimentos = mock.Mock(side_effect=RuntimeError("Invalid object name 'CNO_recibimento'"))
        with self.assertLogs("primavera_sync", level="ERROR"):
            self.sinc.ciclo()
        self.assertIn("CNO_recibimento", self.sinc.erro_recebimentos)
        self.fonte.recebimentos = lambda desde=None: []
        self.sinc.ciclo()
        self.assertIsNone(self.sinc.erro_recebimentos)

    def test_o_relatorio_para_ver_antes_de_ligar(self):
        texto = self.sinc.relatorio_recebimentos()
        self.assertIn("PRONTO PARA ENVIAR", texto)
        self.assertIn("Recebimentos que seguiam", texto)
        self.assertEqual(self.api.pedidos_recebimentos, [], "o relatório não envia nada")

    def test_com_a_consulta_propria_as_linhas_ja_vem_prontas(self):
        self.fonte.recebimentos_prontos = True
        self.fonte.recebimentos_ = [{"Id": "R1", "IdCabecDoc": "d1", "Data": date.today(), "Valor": 1150.0, "ModoPag": "NUM", "Numero": "RE 1"}]
        self.sinc.ciclo()
        self.assertEqual(self.enviados(), [{"id": "R1", "documento_id": "D1", "data": date.today().isoformat(), "valor": 1150.0,
                                            "numero": "RE 1", "metodo_pagamento": self.sinc.modos.get("NUM")}])


class TabelaDosRecebimentosTest(unittest.TestCase):
    COLUNAS = ["Livro", "NumeroMov", "NumeroLinha", "Conferido", "DataMov", "Moeda", "NumeroDoc", "Rubrica", "ValorLiquido", "ValorRetencao",
               "ValorIva", "ValorSelo", "TaxaIva", "IDMovimento", "Ano", "Recibido", "DataRecebimento", "Conta", "Entidade"]

    def esquema(self, *tabelas):
        return Esquema([Tabela("dbo", n, c, 10) for n, c in tabelas])

    def test_encontra_a_tabela_pelas_colunas_e_monta_a_consulta(self):
        e = self.esquema(("CNO_recibimento", self.COLUNAS), ("Clientes", ["Cliente", "Nome"]))
        c = esquema.recebimentos(e)
        for parte in ("t.[NumeroDoc] AS [documento]", "t.[IDMovimento] AS [id]", "t.[ValorLiquido] AS [liquido]", "t.[ValorIva] AS [iva]",
                      "t.[DataRecebimento] AS [data_recebimento]", "[dbo].[CNO_recibimento]", "{filtro_data}", "IS NOT NULL"):
            self.assertIn(parte, c.sql)
        self.assertEqual(c.extra["filtro_data"], "AND t.[DataRecebimento] >= ?")
        self.assertEqual(c.extra["tabela"], "CNO_recibimento")

    def test_funciona_com_o_nome_da_tabela_diferente_e_colunas_em_falta_ficam_nulas(self):
        colunas = [x for x in self.COLUNAS if x not in ("ValorSelo", "Moeda")]
        c = esquema.recebimentos(self.esquema(("Qualquer_Coisa", colunas)))
        self.assertIn("NULL AS [selo]", c.sql)
        self.assertIn("NULL AS [moeda]", c.sql)

    def test_o_nome_indicado_pela_empresa_manda(self):
        e = self.esquema(("CNO_recibimento", self.COLUNAS), ("CNO_outra", self.COLUNAS))
        self.assertEqual(esquema.recebimentos(e, "CNO_outra").extra["tabela"], "CNO_outra")

    def test_sem_a_tabela_diz_o_que_fazer(self):
        with self.assertRaises(ErroEsquema) as cm:
            esquema.recebimentos(self.esquema(("Clientes", ["Cliente", "Nome"])))
        self.assertIn("[recebimentos] tabela", str(cm.exception))

    def test_prefere_a_tabela_com_nome_de_recebimento(self):
        e = self.esquema(("Movimentos", self.COLUNAS), ("CNO_recibimento", self.COLUNAS))
        self.assertEqual(esquema.recebimentos(e).extra["tabela"], "CNO_recibimento")


class RemotoRecebimentosTest(unittest.TestCase):
    def test_pode_ligar_e_pedir_o_relatorio_de_casa(self):
        self.assertEqual(validar_config({"recebimentos": "SIM"})[0], {"recebimentos": "sim"})
        self.assertEqual(validar_comando("recebimentos", ""), "")


if __name__ == "__main__":
    unittest.main()
