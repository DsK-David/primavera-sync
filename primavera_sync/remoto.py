"""Configuração e comandos remotos (U4 do plano): diagnosticar e afinar o sync de casa, sem entrar no servidor.

A Lonntra responde ao sinal de vida com (1) a configuração remota da empresa e (2) comandos pendentes; o sync aplica uma e
executa os outros, e devolve os resultados no sinal seguinte. Tudo é LISTA BRANCA, validado aqui (a Lonntra também valida,
mas aqui não se confia nela):

  configuração: só valores simples (intervalo, dias de revisão, horário do histórico, tipos de serviço, catálogo/movimentos
                ligados). Nunca ligação, chave, endereço, moeda nem consultas SQL: uma consulta nova entra por uma VERSÃO ASSINADA.
  comandos:     esquema, verificar, log [n], repetir_erros, completo, reenviar <doc>, teste [n], reiniciar. Nada de texto a executar.

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
import time
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
            "teste": (10, 1, 50), "reiniciar": None, "reenviar": "documento"}


def validar_comando(comando, args):
    """Normaliza os argumentos; levanta ValueError se o comando não for da lista branca ou o argumento for mau."""
    if comando not in COMANDOS:
        raise ValueError(f"comando desconhecido: {comando}")
    regra = COMANDOS[comando]
    if regra == "documento":  # «FP 2025/1»: tipo, série/número. Nada mais passa
        texto = " ".join(str(args or "").split())
        if not re.fullmatch(r"[A-Z][A-Z0-9]{0,5}( [A-Za-z0-9]{1,10}/\d{1,9})?", texto):
            raise ValueError("o argumento tem de ser um documento (FP 2025/1) ou um tipo (FP)")
        return texto
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


# ------------------------------------------------------------------------------------------ SQL assinado
#
# Consultas SQL à distância, para perceber a base do cliente. Ao contrário dos comandos acima, o texto da consulta NÃO vem da
# Lonntra em quem se confie: vem ASSINADO por quem publica as versões (Ed25519, a mesma chave de chaves.json) e o sync só o corre se
#   - a assinatura for válida (com o texto, a empresa, o id do comando e a validade dentro do que se assina);
#   - for para ESTA empresa, ainda estiver dentro da validade (no máximo 7 dias) e o id for o do comando;
#   - for uma só instrução SELECT/WITH, sem comentários, sem «;», sem escrita nem procedimentos (lista negra de palavras);
# e corre no login só de leitura, com limite de linhas e de tempo. Mesmo com a Lonntra comprometida, ninguém lê a base sem a chave.
PREFIXO_SQL = b"primavera-sync/sql/v1\n"   # separa estas assinaturas das dos pacotes (uma não serve para a outra)
MAX_SQL = 4000
MAX_LINHAS_SQL = 500
VALIDADE_MAXIMA = 7 * 24 * 3600
TEMPO_SQL = 30
PROIBIDAS = ("insert", "update", "delete", "merge", "drop", "alter", "create", "truncate", "exec", "execute", "grant", "revoke", "deny",
             "backup", "restore", "shutdown", "reconfigure", "openrowset", "openquery", "opendatasource", "bulk", "into", "waitfor",
             "kill", "dbcc", "use", "declare", "set", "go", "print", "raiserror", "throw", "begin", "commit", "rollback", "checkpoint",
             "xp_", "sp_")


def bytes_sql(payload):
    """O que se assina: o prefixo de domínio e o JSON canónico (igual aqui e em scripts/sql_remoto.py)."""
    return PREFIXO_SQL + json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def validar_sql(texto):
    """Devolve o SQL limpo; levanta ValueError se não for uma só consulta de leitura."""
    sql = str(texto or "").strip()
    if not sql or len(sql) > MAX_SQL:
        raise ValueError(f"a consulta tem de ter entre 1 e {MAX_SQL} caracteres")
    # o que está entre aspas ou parênteses retos é texto/nome, não código: tira-se para procurar as palavras proibidas
    codigo = re.sub(r"\[[^\]]*\]", "[]", re.sub(r"'(?:[^']|'')*'", "''", sql))
    if "--" in codigo or "/*" in codigo or "*/" in codigo:
        raise ValueError("comentários não são permitidos")
    if codigo.rstrip().rstrip(";").count(";") or ("\x00" in sql):
        raise ValueError("só uma instrução (sem «;» no meio)")
    sql = sql.rstrip().rstrip(";").rstrip()
    if not re.match(r"(?is)^\s*(select|with)\b", sql):
        raise ValueError("só consultas que começam por SELECT ou WITH")
    achadas = [p for p in PROIBIDAS if re.search(r"(?<![A-Za-z0-9_])" + re.escape(p) + (r"" if p.endswith("_") else r"(?![A-Za-z0-9_])"), codigo, re.IGNORECASE)]
    if achadas:
        raise ValueError("palavras não permitidas: " + ", ".join(achadas))
    return sql


def verificar_sql_assinado(cmd, empresa_id, chaves, agora=None):
    """Confere a assinatura e as regras do pedido. Devolve o payload (dict) ou levanta ValueError com o motivo."""
    from .atualizador import ErroAtualizacao, verificar_assinatura_bytes
    try:
        payload = json.loads(cmd.get("payload") or "")
    except (TypeError, ValueError):
        raise ValueError("pedido sem payload válido") from None
    if not isinstance(payload, dict) or payload.get("tipo") != "sql" or payload.get("v") != 1:
        raise ValueError("o pedido não é uma consulta SQL assinada")
    try:
        verificar_assinatura_bytes(bytes_sql(payload), str(cmd.get("assinatura") or ""), chaves)
    except ErroAtualizacao as e:
        raise ValueError(f"assinatura recusada: {e}") from None
    agora = agora if agora is not None else time.time()
    if str(payload.get("empresa")) != str(empresa_id):
        raise ValueError("a consulta é para outra empresa")
    if str(payload.get("id")) != str(cmd.get("id")):
        raise ValueError("a assinatura é de outro comando")
    ate = float(payload.get("ate") or 0)
    if ate < agora:
        raise ValueError("a consulta expirou")
    if ate > agora + VALIDADE_MAXIMA:
        raise ValueError("validade demasiado longa")
    payload["sql"] = validar_sql(payload.get("sql"))
    payload["linhas"] = max(1, min(int(payload.get("linhas") or 100), MAX_LINHAS_SQL))
    return payload


def _celula(v):
    if v is None:
        return "NULL"
    if isinstance(v, (bytes, bytearray)):
        return "0x" + bytes(v)[:8].hex() + ("…" if len(v) > 8 else "")
    t = str(v).replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return t if len(t) <= 80 else t[:79] + "…"


def correr_sql(conn, sql, linhas):
    """Corre a consulta já validada na ligação só de leitura e devolve um texto (cabeçalho, linhas, quantas). Só lê."""
    cur = conn.cursor()
    try:
        cur.timeout = TEMPO_SQL
    except Exception:  # noqa: BLE001 - driver sem suporte: fica o LOCK_TIMEOUT da ligação
        pass
    try:
        cur.execute("SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED")  # não bloqueia o Primavera
    except Exception:  # noqa: BLE001
        pass
    cur.execute(sql)
    if cur.description is None:
        raise ValueError("a consulta não devolve linhas")
    colunas = [d[0] for d in cur.description]
    dados = cur.fetchmany(linhas + 1)
    mais = len(dados) > linhas
    out = [" | ".join(colunas)]
    out += [" | ".join(_celula(v) for v in linha) for linha in dados[:linhas]]
    out.append(f"-- {min(len(dados), linhas)} linha(s)" + (f", cortado às {linhas}" if mais else ""))
    return "\n".join(out)


class Contexto:
    """O que os comandos precisam do sync em marcha."""

    def __init__(self, cfg, config, estado, enviar, obter_sinc, segredos=(), empresa_id="", base=None):
        self.empresa_id, self.base = str(empresa_id or ""), Path(base) if base else Path.cwd()
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
    if comando == "reenviar":
        return ctx.obter_sinc().reenviar_documento(args)
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


def _executar_sql(cmd, ctx):
    from .atualizador import carregar_chaves
    chaves = carregar_chaves(ctx.base)
    if not chaves:
        raise ValueError("este servidor não tem chaves públicas (chaves.json): não aceita consultas SQL")
    payload = verificar_sql_assinado(cmd, ctx.empresa_id, chaves)
    return correr_sql(ctx.obter_sinc().fonte.conn, payload["sql"], payload["linhas"])


def executar_comando(cmd, ctx):
    """Executa um comando da lista branca. Devolve ('ok'|'erro', texto sem segredos). Nunca levanta."""
    try:
        comando = str(cmd.get("comando", ""))
        if comando == "sql":  # a única exceção à lista branca simples: a consulta vem assinada
            texto = str(_executar_sql(cmd, ctx))
        else:
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
        executados = 0
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
            executados += 1
            log.info("Comando remoto %s: %s", cmd.get("comando"), estado_cmd)
        if executados and not (ctx.reiniciar or reiniciar):
            try:
                ctx.enviar()  # os resultados seguem já, sem esperar pelo fim do ciclo (que de noite pode demorar)
            except Exception:  # noqa: BLE001
                log.warning("Não consegui enviar já os resultados dos comandos; seguem no próximo sinal.")
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
