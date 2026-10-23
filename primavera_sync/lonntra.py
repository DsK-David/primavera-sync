"""Cliente da rota de integração do Primavera na Lonntra (/integracao/primavera, ADR-061 da Lonntra)."""
import json
import urllib.error
import urllib.request


class ErroApi(Exception):
    """A Lonntra recusou o documento (dados inválidos). Não adianta repetir sem mudar os dados."""


class ErroLigacao(Exception):
    """A Lonntra não respondeu, recusou a chave ou falhou do lado dela. Tenta-se no ciclo seguinte."""


class Lonntra:
    def __init__(self, url_base, chave, timeout=60):
        self.url = url_base.rstrip("/")
        self.chave = chave
        self.timeout = timeout

    def _pedido(self, metodo, caminho, corpo=None):
        pedido = urllib.request.Request(
            self.url + caminho, method=metodo,
            data=json.dumps(corpo, default=str).encode() if corpo is not None else None,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.chave}"})
        try:
            with urllib.request.urlopen(pedido, timeout=self.timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                resposta = json.loads(e.read() or b"{}")
            except ValueError:
                resposta = {}
            msg = resposta.get("erro") or e.reason
            if e.code in (400, 422):
                raise ErroApi(msg) from None
            raise ErroLigacao(f"{e.code}: {msg}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise ErroLigacao(str(e)) from None

    def estado(self):
        return self._pedido("GET", "/integracao/primavera/estado")

    def documento(self, dados):
        """Devolve {'id', 'referencia', 'ja_existia', 'avisos'}."""
        return self._pedido("POST", "/integracao/primavera/documento", dados)
