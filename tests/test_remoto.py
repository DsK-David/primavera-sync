import configparser
import tempfile
import unittest
from pathlib import Path

from primavera_sync import remoto
from primavera_sync.remoto import Contexto, aplicar_config, executar_comando, limpar_segredos, sobrepor, tratar_remoto
from primavera_sync.remoto import validar_comando, validar_config
from primavera_sync.sincronizador import Estado, ler_config

CONFIG = Path(__file__).resolve().parent.parent / "config.example.ini"


class ConfigRemotaTest(unittest.TestCase):
    def test_valida_e_normaliza(self):
        boas, recusadas = validar_config({"intervalo": " 120 ", "catalogo": "NAO", "movimentos_stock": "Sim",
                                          "historico_horario": "18-07", "tipos_artigo_servico": "0, 5 ,9", "revisao_dias": "45"})
        self.assertEqual(recusadas, {})
        self.assertEqual(boas, {"intervalo": "120", "catalogo": "não", "movimentos_stock": "sim", "historico_horario": "18-7",
                                "tipos_artigo_servico": "0,5,9", "revisao_dias": "45"})

    def test_recusa_valores_maus(self):
        _, r = validar_config({"intervalo": "5", "revisao_dias": "0", "catalogo": "talvez", "historico_horario": "25-3",
                               "tipos_artigo_servico": "0;DROP TABLE", "intervalo2": "1"})
        self.assertEqual(set(r), {"intervalo", "revisao_dias", "catalogo", "historico_horario", "tipos_artigo_servico", "intervalo2"})

    def test_nunca_aceita_ligacao_chave_endereco_moeda_nem_consultas(self):
        for chave in ("ligacao", "chave", "url", "moeda", "armazens", "existencias", "movimentos", "artigos", "estado", "log"):
            boas, recusadas = validar_config({chave: "x"})
            self.assertEqual(boas, {}, chave)
            self.assertIn(chave, recusadas)

    def test_horario_vazio_e_valido(self):
        self.assertEqual(validar_config({"historico_horario": ""})[0], {"historico_horario": ""})

    def test_aplicar_grava_so_se_mudar_e_apaga_quando_nao_manda_nada(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(aplicar_config(tmp, {"intervalo": "90"}), (True, {}))
            self.assertEqual(aplicar_config(tmp, {"intervalo": "90"}), (False, {}), "igual: não mexe")
            self.assertTrue((Path(tmp) / remoto.FICHEIRO).exists())
            mudou, r = aplicar_config(tmp, {"intervalo": "100", "chave": "roubada"})
            self.assertTrue(mudou)
            self.assertIn("chave", r)
            self.assertNotIn("roubada", (Path(tmp) / remoto.FICHEIRO).read_text(encoding="utf-8"))
            self.assertEqual(aplicar_config(tmp, {}), (True, {}))
            self.assertFalse((Path(tmp) / remoto.FICHEIRO).exists())

    def test_sobrepoe_ao_config_e_ignora_o_que_nao_e_da_lista(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.ini").write_text(CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
            (Path(tmp) / remoto.FICHEIRO).write_text(
                "[sincronizacao]\nintervalo = 120\nmoeda = EUR\nhistorico_horario = 99-99\ncatalogo = não\n", encoding="utf-8")
            cfg = ler_config(str(Path(tmp) / "config.ini"))
            s = cfg["sincronizacao"]
            self.assertEqual((s["intervalo"], s["catalogo"]), ("120", "não"))
            self.assertEqual(s["moeda"], "CVE", "moeda não é configurável à distância")
            self.assertNotEqual(s["historico_horario"], "99-99")

    def test_ficheiro_remoto_estragado_nao_derruba(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.ini").write_text(CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
            (Path(tmp) / remoto.FICHEIRO).write_text("isto nem é ini [[[", encoding="utf-8")
            with self.assertLogs("primavera_sync", level="WARNING"):
                cfg = ler_config(str(Path(tmp) / "config.ini"))
            self.assertEqual(cfg["sincronizacao"]["intervalo"], "60")


class ComandosTest(unittest.TestCase):
    def test_lista_branca_e_argumentos(self):
        self.assertEqual(validar_comando("log", ""), "50")
        self.assertEqual(validar_comando("log", "200"), "200")
        self.assertEqual(validar_comando("teste", None), "10")
        self.assertEqual(validar_comando("esquema", ""), "")
        for comando, args in (("log", "5000"), ("log", "abc"), ("teste", "0"), ("esquema", "x"), ("apagar_tudo", ""), ("", ""),
                              ("log", "1; rm"), ("rm -rf /", "")):
            with self.assertRaises(ValueError, msg=f"{comando} {args}"):
                validar_comando(comando, args)

    def test_limpar_segredos(self):
        t = ("DRIVER={x};SERVER=S;UID=lonntra_leitura;PWD=Segredo123;DB=P\nAuthorization: Bearer abc.def\n"
             "chave = 006f0d7f.abcdef\nligacao = DRIVER=...\nSegredo123 outra vez, e MINHACHAVE12345")
        r = limpar_segredos(t, ["Segredo123", "MINHACHAVE12345"])
        for proibido in ("Segredo123", "abc.def", "006f0d7f.abcdef", "MINHACHAVE12345", "lonntra_leitura"):
            self.assertNotIn(proibido, r)
        self.assertIn("SERVER=S", r)


def contexto(tmp, estado=None, enviar=None, sinc=None):
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(CONFIG, encoding="utf-8")
    cfg["sincronizacao"]["log"] = str(Path(tmp) / "sync.log")
    ctx = Contexto(cfg, Path(tmp) / "config.ini", estado or Estado(":memory:"), enviar or (lambda: None), lambda: sinc,
                   ["SegredoDoSql", "ChaveDaLonntra"])
    return ctx


class ExecutarTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        (Path(self.tmp.name) / "config.ini").write_text(CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
        self.ctx = contexto(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_log_devolve_as_ultimas_linhas_sem_segredos(self):
        (Path(self.tmp.name) / "sync.log").write_text(
            "\n".join(f"linha {i}" for i in range(100)) + "\nligou com PWD=SegredoDoSql e ChaveDaLonntra\n", encoding="utf-8")
        estado, texto = executar_comando({"comando": "log", "args": "3"}, self.ctx)
        self.assertEqual(estado, "ok")
        self.assertEqual(len(texto.splitlines()), 3)
        self.assertIn("linha 99", texto)
        self.assertNotIn("linha 50", texto)
        self.assertNotIn("SegredoDoSql", texto)
        self.assertNotIn("ChaveDaLonntra", texto)

    def test_comando_desconhecido_ou_mau_da_erro_sem_levantar(self):
        for cmd in ({"comando": "apagar"}, {"comando": "log", "args": "999999"}, {}):
            with self.assertLogs("primavera_sync", level="ERROR"):
                estado, texto = executar_comando(cmd, self.ctx)
            self.assertEqual(estado, "erro")

    def test_completo_e_reiniciar_ligam_bandeiras(self):
        self.assertEqual(executar_comando({"comando": "completo"}, self.ctx)[0], "ok")
        self.assertTrue(self.ctx.completo)
        executar_comando({"comando": "reiniciar"}, self.ctx)
        self.assertTrue(self.ctx.reiniciar)

    def test_repetir_erros_poe_os_documentos_a_tentar_ja(self):
        e = self.ctx.estado
        e.erro("DOC1", "FA 1", "x", "2026-10-01")
        e.erro("atualizacao:1.3.0", "atualização", "y")
        self.assertIn("DOC1", e.em_espera())
        estado, texto = executar_comando({"comando": "repetir_erros"}, self.ctx)
        self.assertEqual(estado, "ok")
        self.assertNotIn("DOC1", e.em_espera())
        self.assertIn("1 documento", texto)

    def test_resultado_grande_e_cortado(self):
        (Path(self.tmp.name) / "sync.log").write_text("x" * 100 + "\n" * 1 + ("y" * 90 + "\n") * 500, encoding="utf-8")
        _, texto = executar_comando({"comando": "log", "args": "500"}, self.ctx)
        self.assertLessEqual(len(texto), remoto.MAX_RESULTADO + 50)
        self.assertIn("cortado", texto)


class TratarRemotoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        (Path(self.tmp.name) / "config.ini").write_text(CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
        self.enviados = []
        self.ctx = contexto(self.tmp.name, enviar=lambda: self.enviados.append(remoto.resultados_pendentes(self.ctx.estado)))

    def tearDown(self):
        self.tmp.cleanup()

    def test_comando_corre_uma_so_vez_e_o_resultado_fica_ate_ser_confirmado(self):
        r = {"comandos": [{"id": "aaaaaaaa-1111", "comando": "completo"}]}
        self.assertIsNone(tratar_remoto(r, self.ctx))
        self.assertTrue(self.ctx.completo)
        pend = remoto.resultados_pendentes(self.ctx.estado)
        self.assertEqual([(p["id"], p["estado"]) for p in pend], [("aaaaaaaa-1111", "ok")])
        self.ctx.completo = False
        tratar_remoto(r, self.ctx)  # a Lonntra ainda o manda (não recebeu o resultado)
        self.assertFalse(self.ctx.completo, "não executa outra vez")
        tratar_remoto({"ack": ["aaaaaaaa-1111"]}, self.ctx)
        self.assertEqual(remoto.resultados_pendentes(self.ctx.estado), [])

    def test_ids_estranhos_sao_ignorados(self):
        tratar_remoto({"comandos": [{"id": "../../x", "comando": "completo"}, {"id": "", "comando": "completo"}]}, self.ctx)
        self.assertFalse(self.ctx.completo)

    def test_configuracao_nova_pede_reinicio_e_igual_nao(self):
        self.assertEqual(tratar_remoto({"config": {"intervalo": "120"}}, self.ctx), 75)
        self.assertEqual(len(self.enviados), 1, "enviou o sinal antes de sair")
        self.assertIsNone(tratar_remoto({"config": {"intervalo": "120"}}, self.ctx), "igual: sem reinício")

    def test_reiniciar_envia_o_resultado_e_sai(self):
        r = {"comandos": [{"id": "bbbbbbbb-2222", "comando": "reiniciar"}]}
        self.assertEqual(tratar_remoto(r, self.ctx), 75)
        self.assertEqual([p["id"] for p in self.enviados[-1]], ["bbbbbbbb-2222"])

    def test_sem_resposta_ou_resposta_estranha_nao_faz_nada(self):
        self.assertIsNone(tratar_remoto(None, self.ctx))
        self.assertIsNone(tratar_remoto({"ok": True}, self.ctx))
        self.assertIsNone(tratar_remoto({"comandos": "lixo", "config": 7}, self.ctx))


if __name__ == "__main__":
    unittest.main()
