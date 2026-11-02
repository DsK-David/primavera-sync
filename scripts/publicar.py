"""Publica uma versão do primavera-sync: testes → pacote reprodutível → SHA-256 → assinatura → (opcional) Lonntra.

    ~/.primavera-sync/venv/bin/python scripts/publicar.py 1.2.0                      só constrói em dist/ (não envia nada)
    ~/.primavera-sync/venv/bin/python scripts/publicar.py 1.2.0 --publicar           envia para a Lonntra, no canal «teste»
    ~/.primavera-sync/venv/bin/python scripts/publicar.py 1.2.0 --publicar --canal estavel

A versão tem de ser a de primavera_sync/__init__.py e o código tem de estar commitado: a série (que só sobe) é o nº de
commits, e o sync recusa pacotes cuja série não seja maior do que a da versão que corre.
Depois de publicar no canal «teste», só as empresas desse canal a recebem; para as outras: php yii integracao/versao-promover.
"""
import argparse
import hashlib
import json
import shlex
import subprocess
import sys
import zipfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import assinatura  # noqa: E402
from primavera_sync import __version__  # noqa: E402
from primavera_sync.atualizador import carregar_chaves, verificar_assinatura  # noqa: E402

SERVIDOR = "lonntra"
PASTA_REMOTA = "/var/www/lonntra/runtime/primavera-sync"
APP_REMOTA = "/var/www/lonntra"
REQUER = {"python": "3.9"}


def git(*args):
    return subprocess.run(["git", *args], cwd=RAIZ, capture_output=True, text=True, check=True).stdout.strip()


def construir_zip(versao, serie, destino):
    """Zip reprodutível: ficheiros ordenados, datas e permissões fixas (o mesmo código dá sempre o mesmo SHA-256)."""
    ficheiros = sorted(p for p in (RAIZ / "primavera_sync").rglob("*.py") if "__pycache__" not in p.parts)
    destino.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        def escrever(nome, dados):
            info = zipfile.ZipInfo(nome, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, dados)
        escrever("VERSAO.json", json.dumps({"versao": versao, "serie": serie}, sort_keys=True))
        escrever("launcher.py", (RAIZ / "launcher.py").read_bytes())  # o launcher atualiza-se com o pacote (atualizador.atualizar_launcher)
        for f in ficheiros:
            escrever(f.relative_to(RAIZ).as_posix(), f.read_bytes())
    return destino


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("versao")
    ap.add_argument("--canal", choices=("teste", "estavel"), default="teste")
    ap.add_argument("--notas", default="")
    ap.add_argument("--publicar", action="store_true", help="envia para a Lonntra (sem isto só constrói)")
    ap.add_argument("--sem-testes", action="store_true")
    ap.add_argument("--chave", default=str(assinatura.CHAVE_PRIVADA))
    a = ap.parse_args()

    if a.versao != __version__:
        sys.exit(f"A versão {a.versao} não é a de primavera_sync/__init__.py ({__version__}). Atualize um dos dois.")
    if git("status", "--porcelain", "--", "primavera_sync", "launcher.py", "chaves.json"):
        sys.exit("Há alterações por commitar no sync. Faça commit primeiro: a série vem do número de commits.")
    if not a.sem_testes:
        print(">> testes")
        if subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."], cwd=RAIZ).returncode != 0:
            sys.exit("Os testes falharam: não publico.")

    serie = int(git("rev-list", "--count", "HEAD"))
    zip_ = construir_zip(a.versao, serie, RAIZ / "dist" / f"primavera_sync-{a.versao}.zip")
    sha256 = hashlib.sha256(zip_.read_bytes()).hexdigest()
    privada = assinatura.carregar_privada(a.chave)
    assin = assinatura.assinar(privada, a.versao, serie, sha256)
    verificar_assinatura(a.versao, serie, sha256, assin, carregar_chaves(RAIZ))  # a chave de chaves.json tem de bater certo com a privada
    manifesto = {"versao": a.versao, "serie": serie, "sha256": sha256, "assinatura": assin, "ficheiro": zip_.name,
                 "tamanho": zip_.stat().st_size, "canal": a.canal, "notas": a.notas, "commit": git("rev-parse", "--short", "HEAD")}
    (RAIZ / "dist" / f"primavera_sync-{a.versao}.json").write_text(json.dumps(manifesto, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f">> {zip_.name}: {manifesto['tamanho']} bytes, série {serie}, sha256 {sha256[:16]}…, assinatura verificada")

    if not a.publicar:
        print(">> só construído (use --publicar para enviar para a Lonntra)")
        return
    print(f">> a enviar para {SERVIDOR}")
    subprocess.run(["ssh", SERVIDOR, f"install -d -o www-data -g www-data {PASTA_REMOTA}"], check=True)
    subprocess.run(["scp", "-q", str(zip_), f"{SERVIDOR}:{PASTA_REMOTA}/{zip_.name}"], check=True)
    subprocess.run(["ssh", SERVIDOR, f"chown www-data:www-data {PASTA_REMOTA}/{zip_.name}"], check=True)
    registar = (f"cd {APP_REMOTA} && sudo -u www-data php yii integracao/versao-registar {shlex.quote(a.versao)} {serie} {sha256} "
                f"{shlex.quote(assin)} {shlex.quote(zip_.name)} {a.canal} {shlex.quote(a.notas)}")
    subprocess.run(["ssh", SERVIDOR, registar], check=True)
    print(f">> publicada no canal {a.canal}. Ver: ssh {SERVIDOR} 'cd {APP_REMOTA} && sudo -u www-data php yii integracao/versoes'")


if __name__ == "__main__":
    main()
