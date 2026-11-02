"""Configuração e comandos remotos (U4 do plano): diagnosticar e afinar o sync de casa, sem entrar no servidor.

A Lonntra responde ao sinal de vida com (1) a configuração remota da empresa e (2) comandos pendentes; o sync aplica uma e
executa os outros, e devolve os resultados no sinal seguinte. Tudo é LISTA BRANCA, validado aqui (a Lonntra também valida,
mas aqui não se confia nela):

  configuração: só valores simples (intervalo, dias de revisão, horário do histórico, tipos de serviço, catálogo/movimentos
                ligados). Nunca ligação, chave, endereço, moeda nem consultas SQL: uma consulta nova entra por uma VERSÃO ASSINADA.
  comandos:     esquema, verificar, log [n], repetir_erros, completo, teste [n], reiniciar. Nada de texto a executar.

A configuração fica em `config.remoto.ini` ao lado do `config.ini` e sobrepõe-se a ele; um comando executa-se uma só vez
(os ids ficam no estado.db); os resultados (sem palavras-passe nem chaves, no máximo 20 KB) ficam guardados até a Lonntra
confirmar que os recebeu.
"""
import configparser
import contextlib
import io
import json
import logging
import re
from pathlib import Path

log = logging.getLogger("primavera_sync")

FICHEIRO = "config.remoto.ini"
MAX_RESULTADO = 20_000
SAIDA_REINICIAR = 75


# ------------------------------------------------------------------------------------------ configuração remota

def _inteiro(minimo, maximo):
    def validar(v):
        n = int(str(v).strip())
        if not minimo <= n <= maximo:
            raise ValueError(f"tem de estar entre {minimo} e {maximo}")
        return str(n)
    return validar


def _sim_nao(v):
    t = str(v).strip().lower()
    if t in ("sim", "s", "1", "true"):
        return "sim"
    if t in ("nao", "não", "n", "0", "false"):
        return "não"
    raise ValueError("tem de ser sim ou não")


def _horario(v):
    t = str(v).strip()
    if t == "":
        return ""
    m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", t)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 23:
        raise ValueError("tem de ser vazio ou HH-HH (0 a 23), ex.: 18-7")
    return f"{int(m.group(1))}-{int(m.group(2))}"


def _codigos(v):
    t = ",".join(x.strip() for x in str(v).split(",") if x.strip())
    if not re.fullmatch(r"[A-Za-z0-9_\-,]{0,100}", t):
        raise ValueError("só letras, números, «-», «_» e vírgulas")
    return t


PERMITIDAS = {
    "intervalo": _inteiro(30, 3600),
    "revisao_dias": _inteiro(1, 365),
    "catalogo": _sim_nao,
    "movimentos_stock": _sim_nao,
    "recebimentos": _sim_nao,
    "historico_horario": _horario,
    "tipos_artigo_servico": _codigos,
}


def validar_config(dados):
    """({chave: valor validado}, {chave: motivo da recusa}). Chaves fora da lista branca são recusadas."""
    boas, recusadas = {}, {}
    for chave, valor in (dados or {}).items():
        if chave not in PERMITIDAS:
            recusadas[str(chave)] = "não é configurável à distância"
            continue
        try:
            boas[chave] = PERMITIDAS[chave](valor)
        except (ValueError, TypeError) as e:
            recusadas[chave] = str(e)
    return boas, recusadas


def _texto_config(boas):
    return "# Gerado pela Lonntra (configuração remota). Sobrepõe-se ao config.ini; não editar à mão.\n[sincronizacao]\n" + \
        "".join(f"{k} = {boas[k]}\n" for k in sorted(boas))


def aplicar_config(pasta_empresa, dados):
    """Grava o config.remoto.ini com exatamente o que a Lonntra mandou (e apaga-o se já não manda nada).
    Devolve (mudou, recusadas)."""
    boas, recusadas = validar_config(dados)
    ficheiro = Path(pasta_empresa) / FICHEIRO
    antes = ficheiro.read_text(encoding="utf-8") if ficheiro.exists() else ""
    depois = _texto_config(boas) if boas else ""
    if antes == depois:
        return False, recusadas
    if depois:
        temp = ficheiro.with_name(ficheiro.name + ".tmp")
        temp.write_text(depois, encoding="utf-8")
        temp.replace(ficheiro)
    else:
        ficheiro.unlink()
    return True, recusadas


def sobrepor(cfg, caminho_config):
    """Sobrepõe ao `cfg` (lido do config.ini) o config.remoto.ini que esteja ao lado, revalidando cada valor."""
    ficheiro = Path(caminho_config).resolve().parent / FICHEIRO
    if not ficheiro.exists():
        return cfg
    remoto = configparser.ConfigParser(interpolation=None)
    try:
        remoto.read(ficheiro, encoding="utf-8")
        dados = dict(remoto["sincronizacao"]) if remoto.has_section("sincronizacao") else {}
    except (configparser.Error, OSError):
        log.warning("O %s não se lê; ignoro-o.", FICHEIRO)
        return cfg
    boas, recusadas = validar_config(dados)
    for chave, valor in boas.items():
        cfg["sincronizacao"][chave] = valor
    if recusadas:
        log.warning("%s: valores ignorados: %s", FICHEIRO, recusadas)
    return cfg


# ------------------------------------------------------------------------------------------ comandos remotos

def _argumento_inteiro(args, padrao, minimo, maximo):
    t = str(args or "").strip()
    n = int(t) if t else padrao
    if not minimo <= n <= maximo:
        raise ValueError(f"o argumento tem de estar entre {minimo} e {maximo}")
    return n


COMANDOS = {"esquema": None, "recebimentos": None, "verificar": None, "log": (50, 1, 500), "repetir_erros": None, "completo": None,
            "teste": (10, 1, 50), "reiniciar": None}


def validar_comando(comando, args):
    """Normaliza os argumentos; levanta ValueError se o comando não for da lista branca ou o argumento for mau."""
    if comando not in COMANDOS:
        raise ValueError(f"comando desconhecido: {comando}")
    regra = COMANDOS[comando]
    if regra is None:
        if str(args or "").strip():
            raise ValueError(f"«{comando}» não leva argumentos")
        return ""
    return str(_argumento_inteiro(args, *regra))


def limpar_segredos(texto, segredos=()):
    """Tira palavras-passe, chaves e cabeçalhos de autorização de um texto antes de sair do servidor do cliente."""
    for s in segredos:
        if s and len(s) >= 6:
            texto = texto.replace(s, "***")
    texto = re.sub(r"(?i)(PWD|PASSWORD|UID)\s*=\s*\{?[^;}\r\n]*", r"\1=***", texto)
    texto = re.sub(r"(?i)(Bearer\s+)\S+", r"\1***", texto)
    texto = re.sub(r"(?im)^(\s*(?:chave|ligacao)\s*=).*$", r"\1 ***", texto)
    return texto


class Contexto:
    """O que os comandos precisam do sync em marcha."""

    def __init__(self, cfg, config, estado, enviar, obter_sinc, segredos=()):
        self.cfg, self.config, self.estado = cfg, str(config), estado
        self.enviar = enviar          # envia já um sinal de vida completo (com os resultados por confirmar)
        self.obter_sinc = obter_sinc  # devolve o Sincronizador (liga ao SQL se for preciso)
        self.segredos = list(segredos)
        self.completo = False         # o ciclo seguinte revê todo o histórico
        self.reiniciar = False        # sair com 75 a seguir a enviar os resultados


def _executar(comando, args, ctx):
    from . import sincronizador as sinc_mod
    if comando == "esquema":
        return ctx.obter_sinc().fonte.relatorio()
    if comando == "recebimentos":
        return ctx.obter_sinc().relatorio_recebimentos()
    if comando == "verificar":
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            bem = sinc_mod.verificar(ctx.cfg)
        return saida.getvalue() + ("\nTUDO CERTO" if bem else "\nHÁ ERROS")
    if comando == "log":
        caminho = Path(ctx.cfg["sincronizacao"].get("log", "primavera-sync.log"))
        try:
            return "".join(caminho.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)[-int(args):])
        except OSError as e:
            return f"não consegui ler o log: {e}"
    if comando == "repetir_erros":
        with ctx.estado.conn:
            n = ctx.estado.conn.execute("UPDATE erros SET ultima='1970-01-01T00:00:00' WHERE documento NOT LIKE '%:%'").rowcount
        return f"{n} documento(s) com erro voltam a ser tentados no próximo ciclo."
    if comando == "completo":
        ctx.completo = True
        return "O próximo ciclo revê todo o histórico."
    if comando == "teste":
        saida = io.StringIO()
        with contextlib.redirect_stdout(saida):
            bem = sinc_mod.imprimir_teste(ctx.obter_sinc().amostra(int(args)), int(args))
        return saida.getvalue() + ("" if bem else "\nCOM ERROS")
    if comando == "reiniciar":
        ctx.reiniciar = True
        return "O sync reinicia a seguir a enviar este resultado."
    raise ValueError(comando)


def executar_comando(cmd, ctx):
    """Executa um comando da lista branca. Devolve ('ok'|'erro', texto sem segredos). Nunca levanta."""
    try:
        comando = str(cmd.get("comando", ""))
        args = validar_comando(comando, cmd.get("args"))
        texto = str(_executar(comando, args, ctx))
        estado = "ok"
    except Exception as e:  # noqa: BLE001
        estado, texto = "erro", f"{type(e).__name__}: {e}"
        log.exception("Comando remoto %s falhou", cmd.get("comando"))
    texto = limpar_segredos(texto, ctx.segredos)
    if len(texto) > MAX_RESULTADO:
        texto = texto[:MAX_RESULTADO // 2] + "\n[…cortado…]\n" + texto[-MAX_RESULTADO // 2:]
    return estado, texto


# ------------------------------------------------------------------------------------------ o ciclo do sinal

def resultados_pendentes(estado):
    """Os resultados por confirmar, para ir no sinal de vida."""
    out = []
    for chave, valor in estado.conn.execute("SELECT chave, valor FROM marcas WHERE chave LIKE 'resultado:%'"):
        try:
            out.append(json.loads(valor))
        except ValueError:
            pass
    return out


def guardar_resultado(estado, id_, estado_cmd, texto):
    estado.marcar_marca(f"resultado:{id_}", json.dumps({"id": id_, "estado": estado_cmd, "resultado": texto}))


def tratar_remoto(resposta, ctx):
    """Aplica a resposta da Lonntra ao sinal: confirmações, configuração e comandos. Devolve o código de saída se o sync
    tem de reiniciar (75), senão None. Nunca levanta."""
    if not resposta:
        return None
    try:
        for id_ in resposta.get("ack") if isinstance(resposta.get("ack"), list) else []:
            with ctx.estado.conn:
                ctx.estado.conn.execute("DELETE FROM marcas WHERE chave=?", (f"resultado:{id_}",))
        reiniciar = False
        if isinstance(resposta.get("config"), dict):
            mudou, recusadas = aplicar_config(Path(ctx.config).resolve().parent, resposta["config"])
            if recusadas:
                log.warning("Configuração remota: recusado %s", recusadas)
            if mudou:
                log.info("Configuração remota mudou: o sync reinicia para a aplicar.")
                reiniciar = True
        comandos = resposta.get("comandos")
        for cmd in comandos if isinstance(comandos, list) else []:
            if not isinstance(cmd, dict):
                continue
            id_ = str(cmd.get("id", ""))
            if not re.fullmatch(r"[0-9a-fA-F-]{8,40}", id_):
                continue
            if ctx.estado.marca(f"comando:{id_}"):  # já executado (a Lonntra ainda não recebeu o resultado)
                continue
            ctx.estado.marcar_marca(f"comando:{id_}", "1")
            estado_cmd, texto = executar_comando(cmd, ctx)
            guardar_resultado(ctx.estado, id_, estado_cmd, texto)
            log.info("Comando remoto %s: %s", cmd.get("comando"), estado_cmd)
        if ctx.reiniciar or reiniciar:
            # os resultados têm de chegar antes de sair
            try:
                ctx.enviar()
            except Exception:  # noqa: BLE001
                log.warning("Não consegui enviar os resultados antes de reiniciar.")
            return SAIDA_REINICIAR
    except Exception:  # noqa: BLE001
        log.exception("Erro ao tratar a configuração e os comandos remotos")
    return None
