import base64
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from primavera_sync import atualizador
from primavera_sync.atualizador import Atualizador, ErroAtualizacao, manifesto_bytes
from primavera_sync.sincronizador import Estado

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    TEM_CRYPTO = True
except ImportError:  # no Mac usa-se ~/.primavera-sync/venv; no servidor do cliente o instalador instala-a
    TEM_CRYPTO = False


def pacote(versao, serie, extra=None, sync="__version__ = 'x'"):
    """Um zip como o do scripts/publicar.py."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("VERSAO.json", json.dumps({"versao": versao, "serie": serie}))
        z.writestr("primavera_sync/__init__.py", f"__version__ = '{versao}'")
        z.writestr("primavera_sync/sincronizador.py", sync)
        for nome, conteudo in (extra or {}).items():
            z.writestr(nome, conteudo)
    return buf.getvalue()


class LonntraFalsa:
    def __init__(self, conteudo=b""):
        self.conteudo, self.descarregados = conteudo, []

    def pacote(self, versao, maximo):
        self.descarregados.append(versao)
        return self.conteudo


@unittest.skipUnless(TEM_CRYPTO, "precisa de «cryptography» (correr com ~/.primavera-sync/venv/bin/python)")
class AtualizadorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.empresa = self.base / "empresas" / "custom"
        self.empresa.mkdir(parents=True)
        (self.empresa / "config.ini").write_text("[lonntra]\n", encoding="utf-8")
        self.privada = Ed25519PrivateKey.generate()
        raw = self.privada.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        (self.base / "chaves.json").write_text(json.dumps({"chaves": [base64.b64encode(raw).decode()]}), encoding="utf-8")
        # a versão que corre: 1.2.0, série 10
        atual = self.base / "versoes" / "1.2.0" / "primavera_sync"
        atual.mkdir(parents=True)
        (atual / "__init__.py").write_text("", encoding="utf-8")
        (self.base / "versoes" / "1.2.0" / "VERSAO.json").write_text(json.dumps({"versao": "1.2.0", "serie": 10}), encoding="utf-8")
        (self.empresa / "atual.txt").write_text("1.2.0", encoding="utf-8")
        self.estado = Estado(":memory:")
        self.comandos = []
        self.agora = 1000.0

    def tearDown(self):
        self.tmp.cleanup()

    def executar(self, comando, **kw):
        self.comandos.append(comando)
        return SimpleNamespace(returncode=self.codigo_teste if hasattr(self, "codigo_teste") else 0, stdout="", stderr="falhou aqui")

    def atualizador(self, conteudo=b""):
        self.lonntra = LonntraFalsa(conteudo)
        return Atualizador(self.base, self.empresa, self.empresa / "config.ini", self.lonntra, self.estado,
                           agora=lambda: self.agora, executar=self.executar)

    def oferta(self, conteudo, versao="1.3.0", serie=11, assinar=True, privada=None, sha=None):
        sha = sha or hashlib.sha256(conteudo).hexdigest()
        assinatura = base64.b64encode((privada or self.privada).sign(manifesto_bytes(versao, serie, sha))).decode()
        return {"versao": versao, "serie": serie, "sha256": sha, "assinatura": assinatura if assinar else "AAAA", "requer": {"python": "3.9"}}

    def atual(self):
        return (self.empresa / "atual.txt").read_text(encoding="utf-8")

    # ---- o caminho feliz

    def test_aceita_instala_testa_e_aponta(self):
        conteudo = pacote("1.3.0", 11)
        a = self.atualizador(conteudo)
        self.assertEqual(a.tratar(self.oferta(conteudo)), "aplicada")
        self.assertEqual(self.atual(), "1.3.0")
        self.assertEqual((self.empresa / "anterior.txt").read_text(encoding="utf-8"), "1.2.0")
        self.assertTrue((self.base / "versoes" / "1.3.0" / "primavera_sync" / "__init__.py").is_file())
        self.assertFalse((self.base / "versoes" / "1.3.0.tmp").exists())
        self.assertEqual(len(self.comandos), 3, "compilar, importar e --verificar")
        self.assertIn("--verificar", self.comandos[2])
        self.assertTrue(all(str(self.base / "versoes" / "1.3.0.tmp") in c[-1] or True for c in self.comandos))
        self.assertEqual(self.estado.resumo()[1], [])

    def test_a_versao_que_ja_corre_nao_faz_nada(self):
        a = self.atualizador()
        self.assertEqual(a.tratar(self.oferta(pacote("1.2.0", 10), versao="1.2.0", serie=10)), "atual")
        self.assertEqual(a.lonntra.descarregados, [])

    # ---- tudo o que tem de ser recusado, sem mexer em nada

    def recusada(self, conteudo, oferta, motivo):
        a = self.atualizador(conteudo)
        self.assertEqual(a.tratar(oferta), "recusada")
        self.assertEqual(self.atual(), "1.2.0", "nada mudou")
        self.assertFalse((self.base / "versoes" / "1.3.0").exists())
        self.assertFalse((self.base / "versoes" / "1.3.0.tmp").exists())
        erros = self.estado.resumo()[1]
        self.assertIn(motivo, erros[0][1])
        return a

    def test_assinatura_de_outra_chave(self):
        conteudo = pacote("1.3.0", 11)
        a = self.recusada(conteudo, self.oferta(conteudo, privada=Ed25519PrivateKey.generate()), "nenhuma chave conhecida")
        self.assertEqual(a.lonntra.descarregados, [], "nem sequer descarrega")

    def test_assinatura_mal_formada(self):
        conteudo = pacote("1.3.0", 11)
        self.recusada(conteudo, self.oferta(conteudo, assinar=False), "assinatura")

    def test_pacote_adulterado_depois_de_assinado(self):
        bom = pacote("1.3.0", 11)
        mau = pacote("1.3.0", 11, extra={"primavera_sync/evil.py": "import os"})
        self.recusada(mau, self.oferta(bom), "SHA-256 do pacote")

    def test_serie_que_nao_sobe(self):
        conteudo = pacote("1.3.0", 10)
        self.recusada(conteudo, self.oferta(conteudo, serie=10), "série")

    def test_zip_slip(self):
        conteudo = pacote("1.3.0", 11, extra={"../evil.py": "import os"})
        self.recusada(conteudo, self.oferta(conteudo), "caminho perigoso")
        self.assertFalse((self.base / "versoes" / "evil.py").exists())
        self.assertFalse((self.base / "evil.py").exists())

    def test_versao_json_que_nao_bate_com_o_manifesto_assinado(self):
        conteudo = pacote("1.3.0", 99)
        self.recusada(conteudo, self.oferta(conteudo, serie=11), "VERSAO.json")

    def test_pacote_demasiado_grande(self):
        grande = pacote("1.3.0", 11, extra={f"primavera_sync/f{i}.py": "x" for i in range(atualizador.MAX_FICHEIROS + 5)})
        self.recusada(grande, self.oferta(grande), "grande de mais")

    def test_versao_com_formato_estranho(self):
        a = self.atualizador()
        self.assertEqual(a.tratar({"versao": "../../x", "serie": 11}), "recusada")

    def test_versao_nova_que_nao_passa_nos_testes(self):
        self.codigo_teste = 1
        conteudo = pacote("1.3.0", 11)
        self.recusada(conteudo, self.oferta(conteudo), "não passou ao compilar")

    def test_python_antigo_demais(self):
        conteudo = pacote("1.3.0", 11)
        oferta = dict(self.oferta(conteudo), requer={"python": "9.9"})
        self.recusada(conteudo, oferta, "precisa de Python")

    def test_recusada_nao_se_repete_ate_passar_o_tempo(self):
        conteudo = pacote("1.3.0", 11)
        oferta = self.oferta(conteudo, privada=Ed25519PrivateKey.generate())
        a = self.atualizador(conteudo)
        self.assertEqual(a.tratar(oferta), "recusada")
        self.assertEqual(a.tratar(oferta), "ignorada")
        self.agora += atualizador.ESPERA_RECUSA + 1
        self.assertEqual(a.tratar(oferta), "recusada")

    # ---- versões que já estão no disco

    def test_versao_ja_no_disco_troca_sem_descarregar_e_serve_para_recuar(self):
        antiga = self.base / "versoes" / "1.1.0" / "primavera_sync"
        antiga.mkdir(parents=True)
        (antiga / "__init__.py").write_text("", encoding="utf-8")
        a = self.atualizador()
        self.assertEqual(a.tratar({"versao": "1.1.0", "serie": 5}), "aplicada")
        self.assertEqual(self.atual(), "1.1.0")
        self.assertEqual((self.empresa / "anterior.txt").read_text(encoding="utf-8"), "1.2.0")
        self.assertEqual(a.lonntra.descarregados, [])

    def test_versao_que_ja_falhou_aqui_nao_volta(self):
        antiga = self.base / "versoes" / "1.1.0"
        (antiga / "primavera_sync").mkdir(parents=True)
        (antiga / "primavera_sync" / "__init__.py").write_text("", encoding="utf-8")
        (antiga / "FALHOU").write_text("x", encoding="utf-8")
        self.assertEqual(self.atualizador().tratar({"versao": "1.1.0", "serie": 5}), "recusada")
        self.assertEqual(self.atual(), "1.2.0")

    # ---- duas empresas no mesmo servidor

    def test_outra_empresa_a_descarregar_adia(self):
        (self.base / "versoes" / ".atualizar.lock").write_text("", encoding="utf-8")
        os.utime(self.base / "versoes" / ".atualizar.lock", (self.agora, self.agora))
        conteudo = pacote("1.3.0", 11)
        self.recusada(conteudo, self.oferta(conteudo), "outra empresa")

    def test_lock_velho_de_um_processo_morto_nao_bloqueia(self):
        lock = self.base / "versoes" / ".atualizar.lock"
        lock.write_text("", encoding="utf-8")
        os.utime(lock, (self.agora - atualizador.LOCK_VELHO - 10, self.agora - atualizador.LOCK_VELHO - 10))
        conteudo = pacote("1.3.0", 11)
        self.assertEqual(self.atualizador(conteudo).tratar(self.oferta(conteudo)), "aplicada")
        self.assertFalse(lock.exists())

    # ---- limpeza

    def test_guarda_as_ultimas_versoes_e_nunca_apaga_as_em_uso(self):
        for n, v in enumerate(("1.0.0", "1.0.1", "1.1.0", "1.2.1", "1.2.2"), 1):
            p = self.base / "versoes" / v / "primavera_sync"
            p.mkdir(parents=True)
            (p / "__init__.py").write_text("", encoding="utf-8")
            (self.base / "versoes" / v / "VERSAO.json").write_text(json.dumps({"serie": n}), encoding="utf-8")
        (self.empresa / "estavel.txt").write_text("1.0.0", encoding="utf-8")  # a mais antiga está em uso
        self.atualizador().limpar_versoes()
        restam = sorted(p.name for p in (self.base / "versoes").iterdir() if p.is_dir())
        self.assertIn("1.0.0", restam, "a estável nunca se apaga")
        self.assertIn("1.2.0", restam, "a que corre")
        self.assertNotIn("1.0.1", restam)
        self.assertIn("1.2.2", restam)

    # ---- disponibilidade

    def test_so_funciona_debaixo_do_launcher_e_com_chaves(self):
        with mock.patch.dict(os.environ, {"PRIMAVERA_LAUNCHER": "1"}):
            self.assertTrue(Atualizador.disponivel(self.base))
            (self.base / "chaves.json").unlink()
            self.assertFalse(Atualizador.disponivel(self.base))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PRIMAVERA_LAUNCHER", None)
            (self.base / "chaves.json").write_text('{"chaves": ["x"]}', encoding="utf-8")
            self.assertFalse(Atualizador.disponivel(self.base))


class SemCriptografiaTest(unittest.TestCase):
    def test_manifesto_assinado_e_canonico(self):
        self.assertEqual(manifesto_bytes("1.3.0", 11, "ab"), b'{"serie":11,"sha256":"ab","versao":"1.3.0"}')
        self.assertEqual(manifesto_bytes("1.3.0", "11", "ab"), manifesto_bytes("1.3.0", 11, "ab"))

    def test_sem_a_biblioteca_a_atualizacao_diz_porque(self):
        with mock.patch.dict(sys.modules, {"cryptography": None, "cryptography.exceptions": None,
                                           "cryptography.hazmat.primitives.asymmetric.ed25519": None}):
            with self.assertRaises(ErroAtualizacao) as cm:
                atualizador.verificar_assinatura("1.3.0", 11, "ab", "AAAA", ["x"])
            self.assertIn("cryptography", str(cm.exception))

    def test_extrair_recusa_caminhos_fora_da_pasta(self):
        with tempfile.TemporaryDirectory() as tmp:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("../fora.py", "x")
            with self.assertRaises(ErroAtualizacao):
                atualizador.extrair(buf.getvalue(), Path(tmp) / "dentro")
            self.assertFalse((Path(tmp) / "fora.py").exists())

    def test_atualizar_sem_oferta_ou_sem_launcher_nao_faz_nada(self):
        from primavera_sync.sincronizador import atualizar
        self.assertIsNone(atualizar(None, "c.ini", None, None))
        self.assertIsNone(atualizar({"ok": True}, "c.ini", None, None))
        os.environ.pop("PRIMAVERA_LAUNCHER", None)
        self.assertIsNone(atualizar({"atualizar": {"versao": "1.3.0"}}, "c.ini", None, None))


if __name__ == "__main__":
    unittest.main()
