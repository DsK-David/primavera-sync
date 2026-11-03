import configparser
import json
import tempfile
import unittest
from unittest import mock
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

    def test_reenviar_so_aceita_um_documento(self):
        self.assertEqual(validar_comando("reenviar", " FP  2025/1 "), "FP 2025/1")
        for mau in ("", "FP", "fp 2025/1", "FP 2025/1; DROP", "FP 2025/1 FP 2025/2", "../x", "FP 2025/"):
            with self.assertRaises(ValueError):
                validar_comando("reenviar", mau)

    def test_reenviar_chama_o_sincronizador(self):
        class Sinc:
            pedido = None

            def reenviar_documento(self, numero):
                Sinc.pedido = numero
                return "feito"
        ctx = contexto(self.tmp.name, sinc=Sinc())
        self.assertEqual(executar_comando({"comando": "reenviar", "args": "FP 2025/1"}, ctx), ("ok", "feito"))
        self.assertEqual(Sinc.pedido, "FP 2025/1")

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


class SqlValidarTest(unittest.TestCase):
    def test_aceita_consultas_de_leitura(self):
        for sql in ("SELECT TOP 5 * FROM sys.tables",
                    "select name from sys.tables where name like 'CNO%';",
                    "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
                    "SELECT t.name, c.name FROM sys.tables t JOIN sys.columns c ON c.object_id = t.object_id WHERE t.name = 'update'",
                    "SELECT [Update], [Delete] FROM dbo.Tabela",
                    "SELECT CreateDate, UpdatedBy, Setting FROM dbo.Tabela",
                    "SELECT COUNT(*) FROM CNO_Recebimentos WHERE DataRecebimento IS NOT NULL"):
            remoto.validar_sql(sql)

    def test_tira_o_ponto_e_virgula_final(self):
        self.assertEqual(remoto.validar_sql("SELECT 1 ; "), "SELECT 1")

    def test_recusa_escrita_procedimentos_e_truques(self):
        for sql in ("INSERT INTO t VALUES (1)", "UPDATE t SET a = 1", "DELETE FROM t", "DROP TABLE t", "TRUNCATE TABLE t", "EXEC sp_who",
                    "SELECT * INTO nova FROM t", "SELECT 1; DROP TABLE t", "SELECT 1; SELECT 2", "SELECT 1 -- comentário",
                    "SELECT /* x */ 1", "USE master", "DECLARE @a int", "EXEC xp_cmdshell 'dir'", "SELECT * FROM OPENROWSET('x','y','z')",
                    "SELECT * FROM OPENQUERY(srv, 'select 1')", "WAITFOR DELAY '00:00:05'", "BACKUP DATABASE x TO DISK='y'",
                    "SELECT sp_executesql", "BEGIN TRAN", "MERGE t USING s ON 1=1 WHEN MATCHED THEN DELETE", "  ", "", "CREATE TABLE t (a int)",
                    "GRANT SELECT ON t TO x", "SHUTDOWN", "DBCC CHECKDB", "SELECT 1" + " " * 5000 + "FROM t"):
            with self.assertRaises(ValueError, msg=sql[:40]):
                remoto.validar_sql(sql)

    def test_o_texto_entre_aspas_nao_engana_nem_e_apanhado(self):
        remoto.validar_sql("SELECT 'drop table; --' AS texto")          # é texto, não código
        with self.assertRaises(ValueError):
            remoto.validar_sql("SELECT 'a' ; DROP TABLE t")              # o «;» está fora das aspas


class FalsaLigacaoSql:
    def __init__(self, linhas=(), colunas=("a", "b"), description=True):
        self.executados, self.linhas, self.colunas, self.tem_desc = [], list(linhas), colunas, description

    def cursor(self):
        return self

    @property
    def description(self):
        return [(c,) for c in self.colunas] if self.tem_desc else None

    def execute(self, sql):
        self.executados.append(sql)

    def fetchmany(self, n):
        return self.linhas[:n]


class SqlCorrerTest(unittest.TestCase):
    def test_formata_cabecalho_linhas_e_total(self):
        conn = FalsaLigacaoSql([(1, "x"), (2, None)])
        texto = remoto.correr_sql(conn, "SELECT a, b FROM t", 10)
        self.assertEqual(texto.splitlines(), ["a | b", "1 | x", "2 | NULL", "-- 2 linha(s)"])
        self.assertEqual(conn.executados[-1], "SELECT a, b FROM t")
        self.assertIn("READ UNCOMMITTED", conn.executados[0], "não bloqueia o Primavera")

    def test_corta_ao_limite_e_diz_que_cortou(self):
        texto = remoto.correr_sql(FalsaLigacaoSql([(i, "x") for i in range(50)]), "SELECT 1", 20)
        self.assertEqual(len(texto.splitlines()), 22)
        self.assertIn("cortado às 20", texto)

    def test_celulas_compridas_e_binarios_encurtam(self):
        texto = remoto.correr_sql(FalsaLigacaoSql([("x" * 500, b"\x01\x02\x03" * 10)]), "SELECT 1", 5)
        self.assertLess(len(texto.splitlines()[1]), 200)
        self.assertIn("0x010203010203", texto)

    def test_sem_resultado_da_erro(self):
        with self.assertRaises(ValueError):
            remoto.correr_sql(FalsaLigacaoSql(description=False), "SELECT 1", 5)


try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    import base64
    TEM_CRYPTO = True
except ImportError:
    TEM_CRYPTO = False


@unittest.skipUnless(TEM_CRYPTO, "precisa de «cryptography»")
class SqlAssinadoTest(unittest.TestCase):
    EMPRESA, ID = "006f0d7f-0ae4", "aaaaaaaa-1111"

    def setUp(self):
        self.chave = Ed25519PrivateKey.generate()
        raw = self.chave.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.chaves = [base64.b64encode(raw).decode()]
        self.agora = 1_000_000.0

    def pedido(self, chave=None, bytes_fn=None, **mudar):
        payload = {"v": 1, "tipo": "sql", "empresa": self.EMPRESA, "id": self.ID, "sql": "SELECT 1", "linhas": 50, "ate": self.agora + 3600}
        payload.update(mudar)
        assinatura = (chave or self.chave).sign((bytes_fn or remoto.bytes_sql)(payload))
        return {"id": self.ID, "comando": "sql", "payload": json.dumps(payload), "assinatura": base64.b64encode(assinatura).decode()}

    def verificar(self, cmd, empresa=None):
        return remoto.verificar_sql_assinado(cmd, empresa or self.EMPRESA, self.chaves, self.agora)

    def test_pedido_valido(self):
        p = self.verificar(self.pedido())
        self.assertEqual((p["sql"], p["linhas"]), ("SELECT 1", 50))

    def test_recusas(self):
        casos = {
            "assinatura de outra chave": self.pedido(chave=Ed25519PrivateKey.generate()),
            "outra empresa": self.pedido(empresa="outra"),
            "outro comando": dict(self.pedido(), id="bbbbbbbb-2222"),
            "expirada": self.pedido(ate=self.agora - 1),
            "validade longa de mais": self.pedido(ate=self.agora + 30 * 24 * 3600),
            "escrita": self.pedido(sql="DROP TABLE t"),
        }
        for nome, cmd in casos.items():
            with self.assertRaises(ValueError, msg=nome):
                self.verificar(cmd)

    def test_texto_alterado_depois_de_assinado(self):
        cmd = self.pedido()
        payload = json.loads(cmd["payload"])
        payload["sql"] = "SELECT * FROM Clientes"
        cmd["payload"] = json.dumps(payload)
        with self.assertRaises(ValueError) as cm:
            self.verificar(cmd)
        self.assertIn("assinatura", str(cm.exception))

    def test_uma_assinatura_de_pacote_nao_serve_para_sql(self):
        from primavera_sync.atualizador import manifesto_bytes
        cmd = self.pedido(bytes_fn=lambda payload: json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())  # sem o prefixo de domínio
        with self.assertRaises(ValueError):
            self.verificar(cmd)

    def test_sem_payload_ou_de_outro_tipo(self):
        for cmd in ({"id": self.ID}, {"id": self.ID, "payload": "lixo"}, dict(self.pedido(), payload='{"tipo": "outro", "v": 1}')):
            with self.assertRaises(ValueError):
                self.verificar(cmd)

    def test_limite_de_linhas(self):
        self.assertEqual(self.verificar(self.pedido(linhas=99999))["linhas"], remoto.MAX_LINHAS_SQL)
        self.assertEqual(self.verificar(self.pedido(linhas=0))["linhas"], 100)

    def test_executar_comando_corre_a_consulta_assinada_e_so_essa(self):
        conn = FalsaLigacaoSql([(7, "ok")])
        ctx = contexto_sql(conn, self.EMPRESA, self.chaves)
        with mock.patch("time.time", return_value=self.agora):
            estado, texto = executar_comando(self.pedido(), ctx)
        self.assertEqual(estado, "ok", texto)
        self.assertIn("7 | ok", texto)
        self.assertEqual(conn.executados[-1], "SELECT 1")

    def test_pedido_mau_nunca_toca_na_base(self):
        conn = FalsaLigacaoSql([(1, "x")])
        ctx = contexto_sql(conn, self.EMPRESA, self.chaves)
        with mock.patch("time.time", return_value=self.agora), self.assertLogs("primavera_sync", level="ERROR"):
            estado, texto = executar_comando(self.pedido(chave=Ed25519PrivateKey.generate()), ctx)
        self.assertEqual(estado, "erro")
        self.assertIn("assinatura recusada", texto)
        self.assertEqual(conn.executados, [])

    def test_sem_chaves_no_servidor_nao_aceita_sql(self):
        conn = FalsaLigacaoSql()
        ctx = contexto_sql(conn, self.EMPRESA, [])
        with self.assertLogs("primavera_sync", level="ERROR"):
            estado, texto = executar_comando(self.pedido(), ctx)
        self.assertEqual(estado, "erro")
        self.assertEqual(conn.executados, [])

    def test_o_comando_sql_nao_passa_pela_lista_branca_simples(self):
        with self.assertRaises(ValueError):
            validar_comando("sql", "SELECT 1")


def contexto_sql(conn, empresa, chaves):
    import json as _json
    tmp = tempfile.mkdtemp()
    (Path(tmp) / "chaves.json").write_text(_json.dumps({"chaves": chaves}), encoding="utf-8")
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read(CONFIG, encoding="utf-8")
    sinc = type("S", (), {"fonte": type("F", (), {"conn": conn})()})()
    return Contexto(cfg, Path(tmp) / "config.ini", Estado(":memory:"), lambda: None, lambda: sinc, [], empresa, tmp)
