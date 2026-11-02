"""Corre uma consulta SQL de LEITURA na base de uma empresa, à distância, para perceber a base do Primavera.

    ~/.primavera-sync/venv/bin/python scripts/sql_remoto.py 283048808 "SELECT TOP 10 name FROM sys.tables WHERE name LIKE 'CNO%'"
    ~/.primavera-sync/venv/bin/python scripts/sql_remoto.py 283048808 --ficheiro consulta.sql --linhas 300 --espera 240

A consulta é ASSINADA com a chave privada (~/.primavera-sync/assinatura.key) e vai com a empresa, o id do comando e a validade dentro
do que se assina. A Lonntra só a guarda e entrega; o sync confere a assinatura e só corre uma consulta de leitura (SELECT/WITH, sem
comentários nem «;», sem escrita nem procedimentos), no login só de leitura, com limite de linhas e de tempo. O resultado volta no sinal
de vida seguinte (de minuto a minuto; com o servidor fechado, só quando ele ligar) e este script espera por ele.

Preferir consultas de estrutura (sys.tables, sys.columns), contagens e valores distintos; evitar trazer linhas com dados pessoais.
"""
import argparse
import base64
import json
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import assinatura  # noqa: E402
from primavera_sync.remoto import MAX_LINHAS_SQL, bytes_sql, validar_sql  # noqa: E402

SERVIDOR = "lonntra"
APP = "/var/www/lonntra"


def ssh(comando, tempo=60, tentativas=3):
    """Corre um comando na Lonntra. A ligação às vezes demora: repete em vez de rebentar."""
    ultimo = ""
    for _ in range(tentativas):
        try:
            r = subprocess.run(["ssh", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=4", SERVIDOR, comando],
                               capture_output=True, text=True, timeout=tempo)
            return r.returncode, (r.stdout + r.stderr).strip()
        except subprocess.TimeoutExpired:
            ultimo = "a ligação à Lonntra demorou de mais"
            time.sleep(3)
    return 255, ultimo


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("nif")
    ap.add_argument("sql", nargs="?")
    ap.add_argument("--ficheiro", help="ler a consulta de um ficheiro")
    ap.add_argument("--linhas", type=int, default=100, help=f"máximo de linhas (até {MAX_LINHAS_SQL})")
    ap.add_argument("--validade", type=int, default=3600, help="segundos de validade do pedido (a Lonntra e o sync recusam depois)")
    ap.add_argument("--espera", type=int, default=180, help="segundos a esperar pelo resultado (0 = só enviar)")
    ap.add_argument("--chave", default=str(assinatura.CHAVE_PRIVADA))
    a = ap.parse_args()

    sql = Path(a.ficheiro).read_text(encoding="utf-8") if a.ficheiro else a.sql
    if not sql:
        ap.error("indique a consulta ou --ficheiro")
    try:
        sql = validar_sql(sql)  # as mesmas regras do sync: falha já aqui, sem enviar nada
    except ValueError as e:
        sys.exit(f"Consulta recusada: {e}")

    if not a.nif.isdigit():
        sys.exit("O NIF são só dígitos.")
    codigo, saida = ssh(f"mysql fatura -N -e \"SELECT ID FROM entidade WHERE NIF='{a.nif}' AND ESTADO='A'\"")
    empresa = saida.strip().splitlines()[-1] if codigo == 0 and saida.strip() else ""
    if len(empresa) != 36:
        sys.exit(f"Não encontrei uma empresa ativa com o NIF {a.nif} ({saida[:200]}).")

    payload = {"v": 1, "tipo": "sql", "empresa": empresa, "id": str(uuid.uuid4()), "sql": sql,
               "linhas": max(1, min(a.linhas, MAX_LINHAS_SQL)), "ate": int(time.time()) + a.validade}
    privada = assinatura.carregar_privada(a.chave)
    assin = base64.b64encode(privada.sign(bytes_sql(payload))).decode()
    b64 = base64.b64encode(json.dumps(payload).encode()).decode()

    codigo, saida = ssh(f"cd {APP} && sudo -u www-data php yii integracao/comando-assinado {a.nif} {shlex.quote(b64)} {shlex.quote(assin)}")
    print(saida.splitlines()[-1] if saida else "(sem resposta)")
    if codigo != 0:
        sys.exit(1)
    if not a.espera:
        return
    curto = payload["id"][:8]
    fim = time.time() + a.espera
    estado = ""
    while time.time() < fim:
        _, estado = ssh(f"mysql fatura -N -e \"SELECT ESTADO FROM integracao_comando WHERE ID='{payload['id']}'\"")
        estado = estado.strip().splitlines()[-1] if estado.strip() else ""
        if estado in ("ok", "erro", "expirado"):
            break
        time.sleep(10)
    if estado not in ("ok", "erro"):
        sys.exit(f"Ainda sem resultado (estado: {estado or '?'}). O sync só o executa quando der sinal; ver: php yii integracao/comandos {a.nif} {curto}")
    _, texto = ssh(f"cd {APP} && sudo -u www-data php yii integracao/comandos {a.nif} {curto}")
    print(texto)
    sys.exit(0 if estado == "ok" else 1)


if __name__ == "__main__":
    main()
