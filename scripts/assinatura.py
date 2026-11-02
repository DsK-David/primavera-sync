"""Chaves Ed25519 para assinar as versões do primavera-sync (precisa de `cryptography`: ~/.primavera-sync/venv/bin/python).

    python scripts/assinatura.py gerar            cria a chave privada em ~/.primavera-sync/assinatura.key e escreve a pública em chaves.json
    python scripts/assinatura.py publica          mostra a chave pública (base64)

A chave PRIVADA fica só neste computador (permissões 600): quem a tiver pode pôr código a correr nos servidores dos clientes.
Faz uma cópia offline (pen, papel); se a perderes, é preciso reinstalar à mão em cada cliente. A pública vai no repositório
(chaves.json) e o instalador põe-na ao lado do launcher; ela aceita várias chaves, para poderes trocar de chave sem reinstalar.
"""
import base64
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from primavera_sync.atualizador import manifesto_bytes  # noqa: E402

CHAVE_PRIVADA = Path.home() / ".primavera-sync" / "assinatura.key"
CHAVES_JSON = Path(__file__).resolve().parent.parent / "chaves.json"


def publica_b64(privada):
    raw = privada.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def carregar_privada(caminho=CHAVE_PRIVADA):
    caminho = Path(caminho)
    if (caminho.stat().st_mode & 0o077) != 0:
        raise SystemExit(f"A chave {caminho} tem permissões a mais (faça: chmod 600 {caminho}).")
    return serialization.load_pem_private_key(caminho.read_bytes(), password=None)


def assinar(privada, versao, serie, sha256):
    return base64.b64encode(privada.sign(manifesto_bytes(versao, serie, sha256))).decode()


def gerar(caminho=CHAVE_PRIVADA, chaves_json=CHAVES_JSON):
    caminho = Path(caminho)
    if caminho.exists():
        raise SystemExit(f"Já existe uma chave em {caminho}. Não a substituo (perderia o acesso aos clientes já instalados).")
    caminho.parent.mkdir(parents=True, exist_ok=True)
    privada = Ed25519PrivateKey.generate()
    pem = privada.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    fd = os.open(caminho, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    Path(chaves_json).write_text(json.dumps({"chaves": [publica_b64(privada)]}, indent=2) + "\n", encoding="utf-8")
    return publica_b64(privada)


if __name__ == "__main__":
    comando = sys.argv[1] if len(sys.argv) > 1 else ""
    if comando == "gerar":
        print("Chave pública (vai em chaves.json):", gerar())
        print(f"Chave privada em {CHAVE_PRIVADA} (600). Faça uma cópia offline.")
    elif comando == "publica":
        print(publica_b64(carregar_privada()))
    else:
        print(__doc__)
        sys.exit(2)
