"""Launcher do primavera-sync: arranca o sincronizador como processo filho, na versão que o `atual.txt` indicar, e
vigia-o. É a única peça que não se atualiza sozinha: só biblioteca standard, pequeno, e quase nunca muda.

    python launcher.py empresas\\<empresa>\\config.ini        (é o que a tarefa do Windows corre)
    python launcher.py --autoteste [config.ini]              (verifica que consegue arrancar; não arranca nada)

Faz quatro coisas:
  1. arranca `python -m primavera_sync.sincronizador <config>` com a versão de `versoes\\<v>` (o `atual.txt` da empresa);
  2. se o filho morre, espera 10 s, 30 s, 1 min, 5 min… antes de o voltar a arrancar (e nunca em tempestade);
  3. depois de mudar de versão, se a nova não der um ciclo bom em 10 min, ou morrer 3 vezes, volta à anterior,
     marca a nova como FALHOU e avisa a Lonntra (mesmo que a nova nem chegue a arrancar);
  4. se o `atual.txt` mudar, reinicia o filho na versão nova (é assim que uma atualização se aplica).
Sem a pasta `versoes\\`, usa o `primavera_sync` ao lado do launcher (instalações antigas).
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

LAUNCHER_VERSAO = "1.0.0"
ATRASOS = (10, 30, 60, 300)  # segundos entre arranques depois de o filho morrer
SAIDA_REINICIAR = 75         # o sync pede para ser arrancado já (atualização aplicada)
ESTAVEL_APOS = 600           # um filho que dura isto sem morrer deixa de contar falhas
PROVA_SEGUNDOS = 600         # tempo que uma versão nova tem para dar um ciclo bom
PROVA_FALHAS = 3             # ou mortes seguidas que a reprovam
RAIZ = "raiz"                # «versão» = o primavera_sync ao lado do launcher


def _ler(caminho, padrao=""):
    try:
        return Path(caminho).read_text(encoding="utf-8").strip()
    except OSError:
        return padrao


def _escrever(caminho, texto):
    """Escrita atómica: nunca fica um ficheiro a meio."""
    caminho = Path(caminho)
    temp = caminho.with_name(caminho.name + ".tmp")
    temp.write_text(texto, encoding="utf-8")
    os.replace(temp, caminho)


def _json(caminho):
    try:
        return json.loads(Path(caminho).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def atraso(falhas):
    """Segundos a esperar antes do arranque seguinte, dado o nº de mortes seguidas."""
    return ATRASOS[min(max(falhas, 1), len(ATRASOS)) - 1]


class Launcher:
    def __init__(self, base, config, agora=time.time, lancar=subprocess.Popen, relatar=None, log=None):
        self.base = Path(base)
        self.config = Path(config) if Path(config).is_absolute() else self.base / config
        self.pasta = self.config.parent  # a pasta da empresa: atual.txt, saude.json…
        self.agora, self.lancar = agora, lancar
        self.relatar = relatar or self._relatar_lonntra
        self.log = log or self._log_ficheiro
        self.filho = None
        self.versao_em_execucao = None
        self.arrancou_em = 0.0
        self.falhas = 0
        self.proximo_arranque = 0.0
        self.prova = _json(self.pasta / "prova.json") or None

    # ---- versões

    def pacote(self, versao):
        """A pasta que tem o pacote primavera_sync dessa versão."""
        return self.base if versao == RAIZ else self.base / "versoes" / versao

    def versao_valida(self, versao):
        return bool(versao) and (self.pacote(versao) / "primavera_sync" / "__init__.py").is_file() \
            and not (self.pacote(versao) / "FALHOU").exists()

    def alvo(self):
        """A versão que deve correr: o atual.txt, se existir e não tiver falhado; senão a estável; senão o pacote da raiz."""
        for candidata in (_ler(self.pasta / "atual.txt"), _ler(self.pasta / "estavel.txt"), RAIZ):
            if self.versao_valida(candidata):
                return candidata
        return RAIZ

    # ---- arranque do filho

    def comando(self):
        return [sys.executable, "-m", "primavera_sync.sincronizador", str(self.config)]

    def ambiente(self, versao):
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        pacote = str(self.pacote(versao))
        env["PYTHONPATH"] = pacote + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        return env

    def arrancar(self, versao):
        self.log(f"a arrancar o sync, versão {versao}")
        self.filho = self.lancar(self.comando(), cwd=str(self.base), env=self.ambiente(versao))
        _prender_ao_launcher(self.filho, self.log)
        self.versao_em_execucao = versao
        self.arrancou_em = self.agora()
        estavel = _ler(self.pasta / "estavel.txt")
        if versao == estavel or not (estavel and self.versao_valida(estavel)):
            self._fechar_prova()  # a estável, ou a primeira instalação (sem versão para onde voltar)
        elif not self.prova or self.prova["versao"] != versao:  # versão nova: tem 10 minutos para provar que funciona
            self.prova = {"versao": versao, "anterior": estavel, "inicio": self.arrancou_em, "falhas": 0}
            _escrever(self.pasta / "prova.json", json.dumps(self.prova))
        else:  # o launcher reiniciou a meio da prova (ex.: o servidor foi desligado): recomeça o tempo, mantém as falhas
            self.prova["inicio"] = self.arrancou_em
            _escrever(self.pasta / "prova.json", json.dumps(self.prova))

    def parar(self):
        if self.filho is not None and self.filho.poll() is None:
            self.filho.terminate()
            try:
                self.filho.wait(timeout=15)
            except Exception:  # noqa: BLE001
                self.filho.kill()
        self.filho = None

    # ---- prova de uma versão nova

    def _fechar_prova(self):
        self.prova = None
        try:
            (self.pasta / "prova.json").unlink()
        except OSError:
            pass

    def _prova_superada(self):
        saude = _json(self.pasta / "saude.json")
        return saude.get("versao") == self.prova["versao"] and float(saude.get("ciclo_ok_em") or 0) >= self.prova["inicio"]

    def _reverter(self, motivo):
        nova, anterior = self.prova["versao"], self.prova["anterior"]
        self.log(f"a versão {nova} não passou na prova ({motivo}): volto à {anterior}")
        try:
            _escrever(self.pacote(nova) / "FALHOU", motivo)
        except OSError:
            pass
        _escrever(self.pasta / "atual.txt", anterior)
        self.relatar(f"[launcher] a versão {nova} foi revertida para a {anterior}: {motivo}")
        self.parar()
        self._fechar_prova()
        self.falhas = 0
        self.proximo_arranque = 0.0

    # ---- um passo de vigilância

    def tick(self):
        agora = self.agora()
        if self.filho is not None and self.filho.poll() is None:  # a correr
            if agora - self.arrancou_em > ESTAVEL_APOS:
                self.falhas = 0
            alvo = self.alvo()
            if alvo != self.versao_em_execucao:
                self.log(f"nova versão pedida ({self.versao_em_execucao} → {alvo}): a reiniciar")
                self.parar()
                self.proximo_arranque = 0.0
                return
            if self.prova:
                if self._prova_superada():
                    self.log(f"a versão {self.prova['versao']} deu um ciclo bom: fica como estável")
                    _escrever(self.pasta / "estavel.txt", self.prova["versao"])
                    self._fechar_prova()
                elif agora - self.prova["inicio"] > PROVA_SEGUNDOS:
                    self._reverter(f"sem um ciclo bom em {PROVA_SEGUNDOS // 60} minutos")
                    return
            else:
                self._marcar_primeira_estavel()
            return

        if self.filho is not None:  # morreu
            codigo = self.filho.poll()
            duracao = agora - self.arrancou_em
            self.filho = None
            if codigo == SAIDA_REINICIAR:
                self.log("o sync pediu para ser reiniciado (atualização)")
                self.proximo_arranque = 0.0
            else:
                self.falhas = 1 if duracao > ESTAVEL_APOS else self.falhas + 1
                espera = atraso(self.falhas)
                self.proximo_arranque = agora + espera
                self.log(f"o sync terminou com o código {codigo} ao fim de {int(duracao)} s; volto a arrancar daqui a {espera} s")
                if self.prova:
                    self.prova["falhas"] = int(self.prova.get("falhas", 0)) + 1
                    _escrever(self.pasta / "prova.json", json.dumps(self.prova))
                    if self.prova["falhas"] >= PROVA_FALHAS:
                        self._reverter(f"morreu {PROVA_FALHAS} vezes seguidas (último código {codigo})")
                elif self.falhas == 1 or self.falhas == len(ATRASOS):
                    self.relatar(f"[launcher] o sync terminou com o código {codigo} (versão {self.versao_em_execucao}); "
                                 f"{self.falhas} morte(s) seguida(s)")
        if agora >= self.proximo_arranque:
            self.arrancar(self.alvo())

    def _marcar_primeira_estavel(self):
        """Primeira instalação (sem versão estável): a que está a correr passa a ser a estável quando der um ciclo bom."""
        if _ler(self.pasta / "estavel.txt"):
            return
        saude = _json(self.pasta / "saude.json")
        if saude.get("versao") == self.versao_em_execucao and float(saude.get("ciclo_ok_em") or 0) >= self.arrancou_em:
            _escrever(self.pasta / "estavel.txt", self.versao_em_execucao)
            self.log(f"a versão {self.versao_em_execucao} deu um ciclo bom: fica como estável")

    def correr(self, dormir=time.sleep):
        self.log(f"launcher {LAUNCHER_VERSAO} a vigiar {self.config}")
        try:
            while True:
                try:
                    self.tick()
                except Exception as e:  # noqa: BLE001 - o launcher não pode morrer por um erro seu
                    self.log(f"erro no launcher: {type(e).__name__}: {e}")
                dormir(5)
        except KeyboardInterrupt:
            self.parar()

    # ---- registo e avisos

    def _log_ficheiro(self, texto):
        linha = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {texto}"
        print(linha, flush=True)
        try:
            ficheiro = self.pasta / "launcher.log"
            if ficheiro.exists() and ficheiro.stat().st_size > 1_000_000:
                os.replace(ficheiro, self.pasta / "launcher.log.1")
            with open(ficheiro, "a", encoding="utf-8") as f:
                f.write(linha + "\n")
        except OSError:
            pass

    def _relatar_lonntra(self, texto):
        """Um aviso à Lonntra pelo sinal de vida (o launcher fala por si quando a versão nova nem arranca). Nunca falha."""
        try:
            import configparser
            import socket
            c = configparser.ConfigParser(interpolation=None)
            c.read(self.config, encoding="utf-8")
            corpo = json.dumps({"versao": self.versao_em_execucao or "?", "maquina": socket.gethostname(), "erros": 1,
                                "ultimo_erro": texto[:290], "ciclo": time.strftime("%Y-%m-%dT%H:%M:%S")}).encode()
            pedido = urllib.request.Request(
                c["lonntra"]["url"].rstrip("/") + "/integracao/primavera/estado", data=corpo, method="POST",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {c['lonntra']['chave']}"})
            urllib.request.urlopen(pedido, timeout=20).read()
        except Exception as e:  # noqa: BLE001
            self.log(f"não consegui avisar a Lonntra: {e}")


def _prender_ao_launcher(filho, log):
    """Windows: o filho morre se o launcher morrer (Job Object com KILL_ON_JOB_CLOSE). Sem isto, parar a tarefa deixava o
    sync órfão a correr e o arranque seguinte criava um segundo."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        class BASICO(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("a", "b", "c", "d", "e", "f")]

        class ESTENDIDO(ctypes.Structure):
            _fields_ = [("Basic", BASICO), ("Io", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.windll.kernel32
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        job = k32.CreateJobObjectW(None, None)
        info = ESTENDIDO()
        info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError("SetInformationJobObject")
        if not k32.AssignProcessToJobObject(job, wintypes.HANDLE(int(filho._handle))):
            raise OSError("AssignProcessToJobObject")
        _prender_ao_launcher.job = job  # o handle fica aberto enquanto o launcher viver
    except Exception as e:  # noqa: BLE001
        log(f"aviso: não consegui prender o sync ao launcher ({e}); parar a tarefa pode deixá-lo a correr")


def autoteste(base, config=None):
    """Verifica que o launcher consegue arrancar o sync: Python, pacote da versão alvo e importação. Devolve 0 se estiver bem."""
    base = Path(base)
    if sys.version_info < (3, 9):
        print(f"ERRO Python {sys.version_info[0]}.{sys.version_info[1]} é antigo (precisa de 3.9+)")
        return 1
    launcher = Launcher(base, config or base / "config.ini", log=lambda t: None)
    versao = launcher.alvo()
    env = dict(launcher.ambiente(versao), PYTHONDONTWRITEBYTECODE="1")  # a verificação não deixa ficheiros nem aproveita os velhos
    r = subprocess.run([sys.executable, "-c", "import primavera_sync.sincronizador as s; print(s.__version__)"],
                       cwd=str(base), env=env, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        print(f"ERRO a importar o sync (versão {versao}): {r.stderr.strip()[-300:]}")
        return 1
    print(f"OK launcher {LAUNCHER_VERSAO}, sync {r.stdout.strip()} ({'pacote ao lado do launcher' if versao == RAIZ else 'versoes/' + versao})")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    base = Path(__file__).resolve().parent
    if argv and argv[0] == "--autoteste":
        return autoteste(base, argv[1] if len(argv) > 1 else None)
    if len(argv) != 1:
        print(__doc__)
        return 2
    Launcher(base, argv[0]).correr()
    return 0


if __name__ == "__main__":
    sys.exit(main())
