"""Atualização do próprio sync, à distância, sem ninguém entrar no servidor (U2 do plano).

A Lonntra só OFERECE uma versão (no sinal de vida: versão, série, SHA-256, assinatura e onde a descarregar). Quem decide aceitá-la
é este módulo, e só aceita um pacote que:
  1. tenha o SHA-256 que a Lonntra disse;
  2. esteja ASSINADO por uma das chaves em `chaves.json` (Ed25519; a chave privada fica só no Mac de quem publica);
  3. tenha uma série MAIOR do que a versão que corre (ninguém reenvia uma versão antiga e vulnerável);
  4. se extraia sem sair da pasta (zip-slip), compile, importe e passe no `--verificar` com o config real.
Só então troca o `atual.txt`; o launcher vê a mudança e reinicia o sync na versão nova. Se a nova falhar, o launcher recua sozinho.

Uma versão que já esteja no disco (e não tenha falhado) troca-se sem descarregar: é assim que se recua a uma versão antiga.
Só funciona debaixo do launcher (variável PRIMAVERA_LAUNCHER) e com `chaves.json` ao lado dele.
"""
import base64
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

log = logging.getLogger("primavera_sync")

REGEX_VERSAO = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")
MAX_BYTES = 8 * 1024 * 1024       # tamanho máximo do zip
MAX_FICHEIROS = 400               # e de ficheiros lá dentro
MAX_DESCOMPRIMIDO = 40 * 1024 * 1024
ESPERA_RECUSA = 6 * 3600          # uma versão recusada só se volta a tentar passado este tempo
LOCK_VELHO = 15 * 60              # um lock com mais disto é de um processo que morreu
MANTER_VERSOES = 3


class ErroAtualizacao(Exception):
    """A atualização não se fez; o motivo vai para o log e para a Lonntra. Nada ficou alterado."""


def manifesto_bytes(versao, serie, sha256):
    """O que se assina: tem de ser igual aqui e em scripts/publicar.py (JSON canónico)."""
    return json.dumps({"versao": versao, "serie": int(serie), "sha256": sha256}, sort_keys=True, separators=(",", ":")).encode()


def carregar_chaves(base):
    """As chaves públicas aceites (base64 de 32 bytes), em `chaves.json` ao lado do launcher. Vazio = atualização desligada."""
    try:
        return [k for k in json.loads((Path(base) / "chaves.json").read_text(encoding="utf-8")).get("chaves", []) if k]
    except (OSError, ValueError):
        return []


def verificar_assinatura(versao, serie, sha256, assinatura_b64, chaves):
    """Levanta ErroAtualizacao se nenhuma das chaves assinou este manifesto."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        raise ErroAtualizacao("falta a biblioteca «cryptography» neste servidor (pip install cryptography)") from None
    try:
        assinatura = base64.b64decode(assinatura_b64, validate=True)
    except (ValueError, TypeError):
        raise ErroAtualizacao("assinatura mal formada") from None
    dados = manifesto_bytes(versao, serie, sha256)
    for chave in chaves:
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(chave)).verify(assinatura, dados)
            return
        except (InvalidSignature, ValueError):
            continue
    raise ErroAtualizacao("a assinatura não é de nenhuma chave conhecida")


def extrair(conteudo, destino):
    """Extrai o zip para `destino` sem deixar nada sair dela e com limites de tamanho."""
    destino = Path(destino).resolve()
    with zipfile.ZipFile(io.BytesIO(conteudo)) as z:
        infos = z.infolist()
        if len(infos) > MAX_FICHEIROS or sum(i.file_size for i in infos) > MAX_DESCOMPRIMIDO:
            raise ErroAtualizacao("o pacote é grande de mais")
        for i in infos:
            alvo = (destino / i.filename).resolve()
            if destino != alvo and destino not in alvo.parents:
                raise ErroAtualizacao(f"o pacote tem um caminho perigoso: {i.filename}")
            if i.is_dir():
                alvo.mkdir(parents=True, exist_ok=True)
                continue
            alvo.parent.mkdir(parents=True, exist_ok=True)
            with z.open(i) as origem, open(alvo, "wb") as saida:
                shutil.copyfileobj(origem, saida)


def _ler(caminho, padrao=""):
    try:
        return Path(caminho).read_text(encoding="utf-8").strip()
    except OSError:
        return padrao


def _escrever(caminho, texto):
    caminho = Path(caminho)
    temp = caminho.with_name(caminho.name + ".tmp")
    temp.write_text(texto, encoding="utf-8")
    os.replace(temp, caminho)


class Atualizador:
    def __init__(self, base, pasta_empresa, config, lonntra, estado, agora=time.time, executar=subprocess.run):
        self.base, self.pasta, self.config = Path(base), Path(pasta_empresa), str(config)
        self.lonntra, self.estado, self.agora, self.executar = lonntra, estado, agora, executar
        self.versoes = self.base / "versoes"

    @staticmethod
    def disponivel(base):
        """Só debaixo do launcher, com a pasta versoes\\ e com chaves públicas."""
        return bool(os.environ.get("PRIMAVERA_LAUNCHER")) and (Path(base) / "versoes").is_dir() and bool(carregar_chaves(base))

    # ---- estado local

    def versao_atual(self):
        return _ler(self.pasta / "atual.txt")

    def serie_de(self, versao):
        try:
            return int(json.loads((self.versoes / versao / "VERSAO.json").read_text(encoding="utf-8")).get("serie", 0))
        except (OSError, ValueError, TypeError):
            return 0  # instalada à mão, antes de haver séries

    def _recusada_ha_pouco(self, versao):
        marca = self.estado.marca(f"atualizacao_recusada:{versao}")
        return bool(marca) and self.agora() - float(marca) < ESPERA_RECUSA

    def _recusar(self, versao, motivo):
        self.estado.marcar_marca(f"atualizacao_recusada:{versao}", str(self.agora()))
        self.estado.erro(f"atualizacao:{versao}", f"atualização {versao}", motivo)
        log.warning("Atualização para %s recusada: %s", versao, motivo)

    def _aplicar(self, versao):
        anterior = self.versao_atual()
        if anterior and anterior != versao:
            _escrever(self.pasta / "anterior.txt", anterior)
        _escrever(self.pasta / "atual.txt", versao)
        self.estado.limpar_erros([f"atualizacao:{versao}"])
        log.info("Atualização: %s → %s. O launcher reinicia o sync na versão nova.", anterior or "?", versao)

    # ---- a oferta da Lonntra

    def tratar(self, oferta):
        """Devolve 'atual' | 'aplicada' | 'recusada' | 'ignorada'."""
        try:
            versao = str(oferta.get("versao", ""))
            if not REGEX_VERSAO.match(versao):
                raise ErroAtualizacao("versão oferecida com formato inválido")
            if versao == self.versao_atual():
                return "atual"
            if (self.versoes / versao / "FALHOU").exists():
                self._recusar(versao, "esta versão já falhou neste servidor")
                return "recusada"
            if self._recusada_ha_pouco(versao):
                return "ignorada"
            if (self.versoes / versao / "primavera_sync" / "__init__.py").is_file():  # já está no disco: troca local (também recua)
                self._aplicar(versao)
                return "aplicada"
            self._instalar_nova(oferta, versao)
            self._aplicar(versao)
            self.limpar_versoes()
            return "aplicada"
        except ErroAtualizacao as e:
            self._recusar(str(oferta.get("versao", "?")), str(e))
            return "recusada"
        except Exception as e:  # noqa: BLE001 - uma atualização que falha nunca derruba o sync
            self._recusar(str(oferta.get("versao", "?")), f"{type(e).__name__}: {e}")
            log.exception("Erro inesperado na atualização")
            return "recusada"

    def _instalar_nova(self, oferta, versao):
        serie = int(oferta.get("serie", 0))
        if serie <= self.serie_de(self.versao_atual()):
            raise ErroAtualizacao(f"a série {serie} não é maior do que a que corre ({self.serie_de(self.versao_atual())})")
        self._requer(oferta.get("requer") or {})
        chaves = carregar_chaves(self.base)
        if not chaves:
            raise ErroAtualizacao("não há chaves públicas (chaves.json) neste servidor")
        sha256 = str(oferta.get("sha256", "")).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ErroAtualizacao("SHA-256 mal formado")
        # primeiro a assinatura: não se descarrega nem se mexe em nada do que não vem de quem publica
        verificar_assinatura(versao, serie, sha256, str(oferta.get("assinatura", "")), chaves)

        self.versoes.mkdir(exist_ok=True)
        lock = self.versoes / ".atualizar.lock"
        if not self._tomar_lock(lock):
            raise ErroAtualizacao("outra empresa está a atualizar; tenta-se mais tarde")
        temp = self.versoes / f"{versao}.tmp"
        try:
            if (self.versoes / versao / "primavera_sync" / "__init__.py").is_file():  # a outra empresa acabou entretanto
                return
            conteudo = self.lonntra.pacote(versao, MAX_BYTES)
            if hashlib.sha256(conteudo).hexdigest() != sha256:
                raise ErroAtualizacao("o SHA-256 do pacote descarregado não bate certo")
            shutil.rmtree(temp, ignore_errors=True)
            extrair(conteudo, temp)
            info = json.loads((temp / "VERSAO.json").read_text(encoding="utf-8")) if (temp / "VERSAO.json").is_file() else {}
            if info.get("versao") != versao or int(info.get("serie", -1)) != serie:
                raise ErroAtualizacao("o VERSAO.json do pacote não bate certo com o manifesto assinado")
            if not (temp / "primavera_sync" / "__init__.py").is_file():
                raise ErroAtualizacao("o pacote não tem o primavera_sync")
            self._testar(temp)
            shutil.rmtree(self.versoes / versao, ignore_errors=True)
            os.replace(temp, self.versoes / versao)
        finally:
            shutil.rmtree(temp, ignore_errors=True)
            try:
                lock.unlink()
            except OSError:
                pass

    def _tomar_lock(self, lock):
        for _ in range(2):
            try:
                os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return True
            except FileExistsError:
                try:
                    if self.agora() - lock.stat().st_mtime > LOCK_VELHO:
                        lock.unlink()
                        continue
                except OSError:
                    pass
                return False
        return False

    def _requer(self, requer):
        minimo = str(requer.get("python", ""))
        if minimo and tuple(int(x) for x in minimo.split(".")[:2]) > sys.version_info[:2]:
            raise ErroAtualizacao(f"a versão precisa de Python {minimo}+ e aqui há {sys.version_info[0]}.{sys.version_info[1]}")

    def _testar(self, pasta):
        """Compila, importa e corre o --verificar com o config real, tudo com o pacote novo e sem enviar nada."""
        env = dict(os.environ, PYTHONPATH=str(pasta), PYTHONDONTWRITEBYTECODE="1")
        for descricao, comando in (
                ("compilar", [sys.executable, "-m", "compileall", "-q", str(pasta / "primavera_sync")]),
                ("importar", [sys.executable, "-c", "import primavera_sync.sincronizador"]),
                ("verificar", [sys.executable, "-m", "primavera_sync.sincronizador", self.config, "--verificar"])):
            r = self.executar(comando, cwd=str(self.base), env=env, capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                saida = (r.stdout + r.stderr).strip()[-300:]
                raise ErroAtualizacao(f"a versão nova não passou ao {descricao}: {saida}")

    # ---- limpeza

    def limpar_versoes(self):
        """Guarda as últimas versões e nunca apaga uma que alguma empresa use (atual, anterior, estável, em prova)."""
        try:
            protegidas = set()
            for pasta in [self.base, *sorted((self.base / "empresas").glob("*"))]:
                for nome in ("atual.txt", "anterior.txt", "estavel.txt"):
                    v = _ler(pasta / nome)
                    if v:
                        protegidas.add(v)
                prova = _ler(pasta / "prova.json")
                if prova:
                    try:
                        protegidas.add(json.loads(prova).get("versao", ""))
                    except ValueError:
                        pass
            existentes = sorted((p for p in self.versoes.iterdir() if p.is_dir() and REGEX_VERSAO.match(p.name)),
                                key=lambda p: self.serie_de(p.name), reverse=True)
            for p in existentes[MANTER_VERSOES:]:
                if p.name not in protegidas:
                    shutil.rmtree(p, ignore_errors=True)
        except Exception:  # noqa: BLE001
            log.warning("Não consegui limpar as versões antigas.", exc_info=True)
