import json
import sys
import tempfile
import unittest
from pathlib import Path

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
        self.assertEqual(f.cmd[1:4], ["-m", "primavera_sync.sincronizador", str(self.empresa / "config.ini")])
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
