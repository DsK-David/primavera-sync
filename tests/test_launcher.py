import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import launcher
from launcher import Launcher, atraso


class Filho:
    """Um processo simulado."""

    def __init__(self):
        self.codigo = None
        self.terminado = False

    def poll(self):
        return self.codigo

    def terminate(self):
        self.terminado = True
        self.codigo = -15

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return self.codigo

    def morrer(self, codigo):
        self.codigo = codigo


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.empresa = self.base / "empresas" / "custom"
        self.empresa.mkdir(parents=True)
        (self.empresa / "config.ini").write_text("[lonntra]\nurl = http://x\nchave = k\n", encoding="utf-8")
        self.agora = 1000.0
        self.lancados, self.avisos, self.logs = [], [], []
        self.l = self.novo()

    def tearDown(self):
        self.tmp.cleanup()

    def novo(self):
        return Launcher(self.base, "empresas/custom/config.ini", agora=lambda: self.agora, lancar=self.lancar,
                        relatar=self.avisos.append, log=self.logs.append)

    def lancar(self, cmd, cwd=None, env=None):
        f = Filho()
        f.cmd, f.cwd, f.env = cmd, cwd, env
        self.lancados.append(f)
        return f

    def versao(self, v, valida=True):
        pasta = self.base / "versoes" / v / "primavera_sync"
        pasta.mkdir(parents=True)
        (pasta / "__init__.py").write_text(f'__version__ = "{v}"', encoding="utf-8")

    def apontar(self, atual, estavel=None):
        (self.empresa / "atual.txt").write_text(atual, encoding="utf-8")
        if estavel:
            (self.empresa / "estavel.txt").write_text(estavel, encoding="utf-8")

    def saude(self, versao, ciclo_ok_em):
        (self.empresa / "saude.json").write_text(json.dumps({"versao": versao, "ciclo_ok_em": ciclo_ok_em}), encoding="utf-8")

    def avancar(self, segundos):
        self.agora += segundos

    # ---- arranque

    def test_arranca_a_versao_do_atual_txt(self):
        self.versao("1.0.0")
        self.apontar("1.0.0")
        self.l.tick()
        f = self.lancados[0]
        self.assertEqual(f.cmd[1], "-c")
        self.assertEqual(f.cmd[3:], [str(self.base / "versoes" / "1.0.0"), str(self.empresa / "config.ini")])
        self.assertTrue(f.env["PYTHONPATH"].startswith(str(self.base / "versoes" / "1.0.0")))
        self.assertEqual(f.cwd, str(self.base))

    def test_sem_versoes_usa_o_pacote_ao_lado_do_launcher(self):
        (self.base / "primavera_sync").mkdir()
        (self.base / "primavera_sync" / "__init__.py").write_text("", encoding="utf-8")
        self.l.tick()
        self.assertEqual(self.l.versao_em_execucao, launcher.RAIZ)
        self.assertTrue(self.lancados[0].env["PYTHONPATH"].startswith(str(self.base)))

    def test_versao_inexistente_ou_falhada_nao_se_usa(self):
        self.versao("1.0.0")
        self.versao("1.1.0")
        (self.base / "versoes" / "1.1.0" / "FALHOU").write_text("x", encoding="utf-8")
        self.apontar("1.1.0", estavel="1.0.0")
        self.assertEqual(self.l.alvo(), "1.0.0")
        self.apontar("9.9.9", estavel="1.0.0")
        self.assertEqual(self.l.alvo(), "1.0.0")

    # ---- recuo nos reinícios

    def test_recuo_crescente_e_sem_tempestade(self):
        self.versao("1.0.0")
        self.apontar("1.0.0")
        self.l.tick()
        esperas = []
        for _ in range(5):
            self.lancados[-1].morrer(1)
            antes = len(self.lancados)
            self.l.tick()  # regista a morte
            self.assertEqual(len(self.lancados), antes, "não arranca logo")
            esperas.append(int(self.l.proximo_arranque - self.agora))
            self.avancar(esperas[-1])
            self.l.tick()
            self.assertEqual(len(self.lancados), antes + 1)
        self.assertEqual(esperas, [10, 30, 60, 300, 300])

    def test_saida_75_reinicia_logo(self):
        self.versao("1.0.0")
        self.apontar("1.0.0")
        self.l.tick()
        self.lancados[0].morrer(launcher.SAIDA_REINICIAR)
        self.l.tick()
        self.assertEqual(len(self.lancados), 2)

    def test_um_filho_estavel_zera_as_falhas(self):
        self.versao("1.0.0")
        self.apontar("1.0.0")
        self.l.tick()
        self.lancados[0].morrer(1)
        self.l.tick()
        self.avancar(10)
        self.l.tick()
        self.avancar(launcher.ESTAVEL_APOS + 5)
        self.l.tick()
        self.assertEqual(self.l.falhas, 0)

    def test_atraso(self):
        self.assertEqual([atraso(n) for n in (0, 1, 2, 3, 4, 9)], [10, 10, 30, 60, 300, 300])

    # ---- mudança de versão e prova

    def test_atual_txt_a_mudar_reinicia_na_versao_nova(self):
        self.versao("1.0.0")
        self.versao("1.1.0")
        self.apontar("1.0.0", estavel="1.0.0")
        self.l.tick()
        self.apontar("1.1.0")
        self.l.tick()
        self.assertTrue(self.lancados[0].terminado)
        self.l.tick()
        self.assertEqual(self.l.versao_em_execucao, "1.1.0")
        self.assertEqual(self.l.prova["anterior"], "1.0.0")

    def test_versao_nova_que_da_um_ciclo_bom_passa_a_estavel(self):
        self.versao("1.0.0")
        self.versao("1.1.0")
        self.apontar("1.1.0", estavel="1.0.0")
        self.l.tick()
        self.assertIsNotNone(self.l.prova)
        self.avancar(60)
        self.saude("1.1.0", self.agora)
        self.l.tick()
        self.assertEqual((self.empresa / "estavel.txt").read_text(encoding="utf-8"), "1.1.0")
        self.assertIsNone(self.l.prova)
        self.assertFalse((self.empresa / "prova.json").exists())

    def test_saude_de_outra_versao_nao_conta(self):
        self.versao("1.0.0")
        self.versao("1.1.0")
        self.apontar("1.1.0", estavel="1.0.0")
        self.l.tick()
        self.avancar(60)
        self.saude("1.0.0", self.agora)  # a antiga
        self.l.tick()
        self.assertIsNotNone(self.l.prova)

    def test_versao_nova_sem_ciclo_bom_em_10_minutos_reverte(self):
        self.versao("1.0.0")
        self.versao("1.1.0")
        self.apontar("1.1.0", estavel="1.0.0")
        self.l.tick()
        self.avancar(launcher.PROVA_SEGUNDOS + 1)
        self.l.tick()
        self.assertEqual((self.empresa / "atual.txt").read_text(encoding="utf-8"), "1.0.0")
        self.assertTrue((self.base / "versoes" / "1.1.0" / "FALHOU").exists())
        self.assertTrue(self.lancados[0].terminado)
        self.assertIn("revertida", self.avisos[0])
        self.l.tick()
        self.assertEqual(self.l.versao_em_execucao, "1.0.0")

    def test_versao_nova_que_morre_3_vezes_reverte(self):
        self.versao("1.0.0")
        self.versao("1.1.0")
        self.apontar("1.1.0", estavel="1.0.0")
        self.l.tick()
        for _ in range(launcher.PROVA_FALHAS):
            self.lancados[-1].morrer(1)
            self.l.tick()
            self.avancar(300)
            self.l.tick()
        self.assertTrue((self.base / "versoes" / "1.1.0" / "FALHOU").exists())
        self.assertEqual(self.l.versao_em_execucao, "1.0.0")
        self.assertTrue(any("morreu 3 vezes" in a for a in self.avisos))

    def test_primeira_instalacao_nao_tem_para_onde_voltar(self):
        self.versao("1.0.0")
        self.apontar("1.0.0")
        self.l.tick()
        self.assertIsNone(self.l.prova)
        self.avancar(launcher.PROVA_SEGUNDOS * 3)
        self.l.tick()
        self.assertFalse((self.base / "versoes" / "1.0.0" / "FALHOU").exists())
        self.saude("1.0.0", self.agora)
        self.l.tick()
        self.assertEqual((self.empresa / "estavel.txt").read_text(encoding="utf-8"), "1.0.0")

    def test_launcher_reiniciado_a_meio_da_prova_recomeca_o_tempo(self):
        self.versao("1.0.0")
        self.versao("1.1.0")
        self.apontar("1.1.0", estavel="1.0.0")
        self.l.tick()
        self.avancar(launcher.PROVA_SEGUNDOS - 10)
        # o servidor foi desligado e voltou na segunda-feira
        self.avancar(3 * 24 * 3600)
        self.l = self.novo()
        self.l.tick()
        self.avancar(30)
        self.l.tick()
        self.assertEqual((self.empresa / "atual.txt").read_text(encoding="utf-8"), "1.1.0", "não reverteu")
        self.assertIsNotNone(self.l.prova)

    def test_nova_versao_a_meio_da_prova_substitui_a_prova(self):
        for v in ("1.0.0", "1.1.0", "1.2.0"):
            self.versao(v)
        self.apontar("1.1.0", estavel="1.0.0")
        self.l.tick()
        self.apontar("1.2.0")
        self.l.tick()
        self.l.tick()
        self.assertEqual(self.l.prova["versao"], "1.2.0")
        self.assertEqual(self.l.prova["anterior"], "1.0.0")


class VersaoNaPastaCertaTest(unittest.TestCase):
    """O defeito que só apareceu em campo: a cópia do primavera_sync ao lado do launcher ganhava às versões de versoes\\."""

    def preparar(self, tmp):
        base = Path(tmp)
        for pasta, versao in ((base / "primavera_sync", "ANTIGA"), (base / "versoes" / "1.3.0" / "primavera_sync", "NOVA")):
            pasta.mkdir(parents=True)
            (pasta / "__init__.py").write_text(f'__version__ = "{versao}"', encoding="utf-8")
            (pasta / "sincronizador.py").write_text(
                "import sys\nfrom . import __version__\nprint('A CORRER', __version__, sys.argv[1:])\n", encoding="utf-8")
        empresa = base / "empresas" / "x"
        empresa.mkdir(parents=True)
        (empresa / "config.ini").write_text("[lonntra]\n", encoding="utf-8")
        (empresa / "atual.txt").write_text("1.3.0", encoding="utf-8")
        return base, empresa

    def test_o_filho_corre_o_codigo_da_versao_e_nao_a_copia_ao_lado_do_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, empresa = self.preparar(tmp)
            l = Launcher(base, "empresas/x/config.ini", log=lambda t: None, relatar=lambda t: None)
            r = launcher.subprocess.run(l.comando("1.3.0"), cwd=str(base), env=l.ambiente("1.3.0"), capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("A CORRER NOVA", r.stdout)
            self.assertNotIn("ANTIGA", r.stdout)
            self.assertIn(str(empresa / "config.ini"), r.stdout, "o config chega ao sync como argumento")

    def test_a_versao_raiz_continua_a_ser_a_copia_ao_lado(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, empresa = self.preparar(tmp)
            (empresa / "atual.txt").write_text("9.9.9", encoding="utf-8")  # não existe: cai na raiz
            l = Launcher(base, "empresas/x/config.ini", log=lambda t: None, relatar=lambda t: None)
            self.assertEqual(l.alvo(), launcher.RAIZ)
            r = launcher.subprocess.run(l.comando(), cwd=str(base), env=l.ambiente(launcher.RAIZ), capture_output=True, text=True, timeout=60)
            self.assertIn("A CORRER ANTIGA", r.stdout)

    def test_o_autoteste_verifica_a_versao_alvo_e_nao_a_copia_ao_lado(self):
        with tempfile.TemporaryDirectory() as tmp:
            base, empresa = self.preparar(tmp)
            (base / "versoes" / "1.3.0" / "primavera_sync" / "sincronizador.py").write_text("__version__ = 'NOVA'", encoding="utf-8")
            (base / "primavera_sync" / "sincronizador.py").write_text("__version__ = 'ANTIGA'", encoding="utf-8")
            with mock.patch("builtins.print") as imprime:
                self.assertEqual(launcher.autoteste(base, empresa / "config.ini"), 0)
            self.assertIn("sync NOVA", str(imprime.call_args_list))


class AutotesteTest(unittest.TestCase):
    def test_autoteste_com_o_pacote_ao_lado(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            pacote = base / "primavera_sync"
            pacote.mkdir()
            (pacote / "__init__.py").write_text('__version__ = "9.9.9"', encoding="utf-8")
            (pacote / "sincronizador.py").write_text("__version__ = '9.9.9'", encoding="utf-8")
            self.assertEqual(launcher.autoteste(base, base / "config.ini"), 0)
            (pacote / "sincronizador.py").write_text("isto não é python (", encoding="utf-8")
            self.assertEqual(launcher.autoteste(base, base / "config.ini"), 1)

    def test_o_proprio_pacote_do_repositorio_passa_no_autoteste(self):
        self.assertEqual(launcher.autoteste(Path(launcher.__file__).resolve().parent), 0)


if __name__ == "__main__":
    unittest.main()


class LauncherAtualizadoTest(unittest.TestCase):
    """O launcher.py da pasta mudou (veio num pacote do sync): o em marcha passa o comando ao novo."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "empresas" / "x").mkdir(parents=True)
        (self.base / "empresas" / "x" / "config.ini").write_text("[lonntra]\n", encoding="utf-8")
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def novo(self):
        return Launcher(self.base, "empresas/x/config.ini", log=self.logs.append, relatar=lambda t: None)

    def test_sem_launcher_na_pasta_ou_igual_ao_carregado_nao_muda(self):
        l = self.novo()
        self.assertFalse(l.launcher_mudou(), "sem launcher.py na pasta")
        (self.base / "launcher.py").write_bytes(Path(launcher.__file__).read_bytes())
        self.assertFalse(l.launcher_mudou(), "igual ao que está a correr")

    def test_launcher_diferente_na_pasta_conta_como_atualizado(self):
        l = self.novo()
        (self.base / "launcher.py").write_text("# outro launcher", encoding="utf-8")
        self.assertTrue(l.launcher_mudou())

    def test_passa_o_comando_ao_launcher_novo_e_espera_por_ele(self):
        l = self.novo()
        (self.base / "launcher.py").write_text("# outro", encoding="utf-8")
        lancados = []

        class Novo:
            def wait(self):
                return 0

        l.lancar_launcher = lambda cmd, cwd=None: (lancados.append((cmd, cwd)), Novo())[1]
        l.filho = Filho()
        self.assertEqual(l.correr(dormir=lambda s: None), 0)
        self.assertEqual(len(lancados), 1)
        cmd, cwd = lancados[0]
        self.assertEqual(cmd[1:], [str(self.base / "launcher.py"), str(self.base / "empresas" / "x" / "config.ini")])
        self.assertEqual(cwd, str(self.base))
        self.assertTrue(l.filho is None, "o sync foi parado antes de passar o comando")

    def test_o_autoteste_aceita_a_pasta_da_instalacao_como_argumento(self):
        base = self.base
        pacote = base / "primavera_sync"
        pacote.mkdir()
        (pacote / "__init__.py").write_text('__version__ = "7.7.7"', encoding="utf-8")
        (pacote / "sincronizador.py").write_text("__version__ = '7.7.7'", encoding="utf-8")
        # como o atualizador o corre: o launcher NOVO, que está noutra pasta, a verificar a instalação
        outra = base / "versoes" / "9.9.9"
        outra.mkdir(parents=True)
        (outra / "launcher.py").write_bytes(Path(launcher.__file__).read_bytes())
        r = launcher.subprocess.run([sys.executable, str(outra / "launcher.py"), "--autoteste", str(base / "empresas" / "x" / "config.ini"), str(base)],
                                    capture_output=True, text=True, cwd=str(base))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("sync 7.7.7", r.stdout)


class AutocuraDasTarefasTest(unittest.TestCase):
    """Uma reinstalação de uma só empresa apagou a tarefa da outra: o launcher volta a criá-la, sem ninguém ir ao servidor."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.logs, self.chamadas = [], []

    def tearDown(self):
        self.tmp.cleanup()

    def empresa(self, pasta, valida=True):
        d = self.base / pasta if pasta else self.base
        d.mkdir(parents=True, exist_ok=True)
        texto = "[primavera]\nligacao = DRIVER={x};SERVER=S\n[lonntra]\nchave = k.s\n" if valida else "[lonntra]\n"
        (d / "config.ini").write_text(texto, encoding="utf-8")

    def schtasks(self, existentes):
        saida = "".join(f'"\\{n}","N/A","Ready"\r\n' for n in existentes)

        def executar(cmd, **kw):
            self.chamadas.append(cmd)
            if cmd[0] == "schtasks":
                return SimpleNamespace(returncode=0, stdout=saida, stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return executar

    def test_empresas_instaladas_so_as_com_config_valido(self):
        self.empresa(None)
        self.empresa("empresas/auto-style-custom")
        self.empresa("empresas/lixo", valida=False)
        (self.base / "empresas" / "sem-config").mkdir()
        self.assertEqual([n for n, _ in launcher.empresas_instaladas(self.base)], ["PrimaveraLonntra", "PrimaveraLonntra-auto-style-custom"])

    def test_le_os_nomes_das_tarefas_do_schtasks(self):
        nomes = launcher.tarefas_registadas(self.schtasks(["PrimaveraLonntra-x", "Outra tarefa"]))
        self.assertEqual(nomes, {"PrimaveraLonntra-x", "Outra tarefa"})

    def test_cria_so_a_tarefa_que_falta(self):
        self.empresa(None)
        self.empresa("empresas/auto-style-custom")
        criadas = launcher.curar_tarefas(self.base, self.logs.append, self.schtasks(["PrimaveraLonntra-auto-style-custom"]), python="python.exe")
        self.assertEqual(criadas, ["PrimaveraLonntra"])
        powershell = [c for c in self.chamadas if c[0] == "powershell"]
        self.assertEqual(len(powershell), 1)
        import base64
        script = base64.b64decode(powershell[0][-1]).decode("utf-16-le")
        self.assertIn("-TaskName 'PrimaveraLonntra'", script)
        self.assertIn("-Execute 'python.exe'", script)
        self.assertIn("launcher.py \"config.ini\"", script)
        self.assertIn("-UserId 'SYSTEM'", script)
        self.assertIn("Start-ScheduledTask -TaskName 'PrimaveraLonntra'", script)

    def test_tudo_em_dia_nao_faz_nada(self):
        self.empresa(None)
        self.assertEqual(launcher.curar_tarefas(self.base, self.logs.append, self.schtasks(["PrimaveraLonntra"])), [])
        self.assertFalse([c for c in self.chamadas if c[0] == "powershell"])

    def test_o_ficheiro_tarefas_manual_desliga_a_autocura(self):
        self.empresa(None)
        (self.base / "tarefas.manual").write_text("", encoding="utf-8")
        self.assertEqual(launcher.curar_tarefas(self.base, self.logs.append, self.schtasks([])), [])
        self.assertEqual(self.chamadas, [])

    def test_nunca_levanta_e_diz_o_que_falhou(self):
        self.empresa(None)

        def executar(cmd, **kw):
            raise OSError("sem permissões")
        self.assertEqual(launcher.curar_tarefas(self.base, self.logs.append, executar), [])
        self.assertTrue(any("falhou" in l for l in self.logs))

        def falha_a_criar(cmd, **kw):
            return SimpleNamespace(returncode=1 if cmd[0] == "powershell" else 0, stdout="", stderr="acesso negado")
        self.assertEqual(launcher.curar_tarefas(self.base, self.logs.append, falha_a_criar), [])
        self.assertTrue(any("não consegui criar" in l for l in self.logs))

    def test_nomes_e_caminhos_estranhos_sao_recusados(self):
        base, config = self.base, self.base / "empresas" / "x" / "config.ini"
        for nome in ("PrimaveraLonntra'; Remove-Item *", "Outra", "PrimaveraLonntra-a b"):
            with self.assertRaises(ValueError):
                launcher.script_tarefa(base, nome, config, "python.exe")
        with self.assertRaises(ValueError):
            launcher.script_tarefa(base, "PrimaveraLonntra-x", base / "empresas" / "x'y" / "config.ini", "python.exe")
        with self.assertRaises(ValueError):
            launcher.script_tarefa(base, "PrimaveraLonntra-x", config, "py'thon.exe")

    def test_o_launcher_cura_ao_arrancar_e_depois_de_15_minutos(self):
        self.empresa(None)
        agora = [1000.0]
        l = Launcher(self.base, "config.ini", agora=lambda: agora[0], lancar=lambda *a, **k: Filho(), relatar=lambda t: None, log=self.logs.append)
        l.curar = True
        l.executar = self.schtasks([])
        l.tick()
        self.assertEqual(len([c for c in self.chamadas if c[0] == "powershell"]), 1, "ao arrancar")
        agora[0] += 60
        l.tick()
        self.assertEqual(len([c for c in self.chamadas if c[0] == "powershell"]), 1, "ainda não passou o intervalo")
        agora[0] += launcher.INTERVALO_CURA
        l.tick()
        self.assertEqual(len([c for c in self.chamadas if c[0] == "powershell"]), 2, "outra vez, passados 15 minutos")

    def test_fora_do_windows_nao_cura(self):
        self.empresa(None)
        l = Launcher(self.base, "config.ini", lancar=lambda *a, **k: Filho(), relatar=lambda t: None, log=self.logs.append)
        l.executar = self.schtasks([])
        l.tick()
        self.assertEqual(self.chamadas, [] if os.name != "nt" else self.chamadas)


