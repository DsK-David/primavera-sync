"""Cliente da rota de integração do Primavera na Lonntra (/integracao/primavera, ADR-061 da Lonntra)."""
import json
import re
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

    def sinal(self, estado):
        """Sinal de vida de cada ciclo: a Lonntra avisa a empresa se deixar de o receber ou se houver erros."""
        return self._pedido("POST", "/integracao/primavera/estado", estado)

    def documento(self, dados):
        """Devolve {'id', 'referencia', 'ja_existia', 'avisos'}."""
        return self._pedido("POST", "/integracao/primavera/documento", dados)

    def artigos(self, artigos):
        """Cria ou atualiza artigos. Devolve {'criados', 'atualizados', 'erros': [{'codigo', 'erro'}]}."""
        return self._pedido("POST", "/integracao/primavera/artigos", {"artigos": artigos})

    def stock(self, armazens=(), movimentos=(), existencias=()):
        """Devolve {'armazens', 'movimentos', 'existencias', 'erros': [{'codigo', 'erro'}]}."""
        return self._pedido("POST", "/integracao/primavera/stock",
                            {"armazens": list(armazens), "movimentos": list(movimentos), "existencias": list(existencias)})

    def pacote(self, versao, maximo=8 * 1024 * 1024):
        """Descarrega o zip de uma versão do sync. O endereço é montado aqui, nunca vem da resposta: a chave da empresa só
        vai para a Lonntra. O que se devolve só é de confiança depois de conferido o SHA-256 e a assinatura (atualizador.py)."""
        if not re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}", str(versao)):
            raise ErroApi("versão inválida")
        pedido = urllib.request.Request(self.url + f"/integracao/primavera/pacote/{versao}", method="GET",
                                        headers={"Authorization": f"Bearer {self.chave}"})
        try:
            with urllib.request.urlopen(pedido, timeout=self.timeout) as r:
                dados = r.read(maximo + 1)
        except urllib.error.HTTPError as e:
            raise ErroLigacao(f"{e.code}: {e.reason}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise ErroLigacao(str(e)) from None
        if len(dados) > maximo:
            raise ErroApi("o pacote é maior do que o permitido")
        return dados
