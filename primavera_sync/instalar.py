"""Assistente de instalação: descobre o Primavera, configura, testa e diz o resultado.

Corre-o o instalar.bat (que antes instala o Python e o pyodbc). Lê o cliente.ini que estiver na pasta (URL e chave
da Lonntra, e opcionalmente o servidor, a base e os mapas) e só pergunta o que faltar; quando há uma resposta
óbvia, propõe-na e basta Enter. No fim escreve o config.ini e o relatorio-instalacao.txt.

    python -m primavera_sync.instalar            (na pasta do sync, como administrador)
"""
import configparser
import getpass
import json
import re
import secrets
import shutil
import socket
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from . import __version__

PASTA = Path(__file__).resolve().parent.parent
LOGIN = "lonntra_leitura"
DRIVERS = ["ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server", "SQL Server Native Client 11.0", "SQL Server"]
TIPOS_LONNTRA = ["FT", "FR", "TV", "NC", "ND", "DV", "FP", "DT"]
METODOS = {"10": "numerário", "20": "cheque", "30": "transferência", "54": "cartão de crédito", "55": "cartão de débito"}

relatorio = []


# ---------------------------------------------------------------- sugestões (sem base de dados, testáveis)

def sugerir_tipo(codigo, descricao=""):
    """Tipo da Lonntra para um documento do Primavera, ou None se não é uma venda a enviar."""
    c, d = codigo.upper().strip(), _sem_acentos(descricao)
    if c in ("FP", "PRO", "PF") or "proforma" in d or "pro-forma" in d or "pro forma" in d:  # antes de «fatura»: uma proforma não é uma fatura
        return "FP"
    if c in ("DEV", "DV", "NDV") or "devolucao" in d:
        return "DV"
    if c in ("GR", "GT") or re.search(r"\bguia (de )?(remessa|transporte)\b", d):  # documentos de transporte na Lonntra
        return "DT"
    if c in ("NC", "NCR") or "nota de credito" in d or "credito" in d and "nota" in d:
        return "NC"
    if c in ("ND",) or "nota de debito" in d:
        return "ND"
    if c in ("FR", "FRC") or re.fullmatch(r"FR\d+", c) or "fatura recibo" in d or "fatura-recibo" in d or "factura recibo" in d or "factura-recibo" in d:
        return "FR"
    if c in ("VD", "TV", "FS", "VDI") or "venda a dinheiro" in d or "talao" in d or "simplificada" in d:
        return "TV"
    if c in ("FA", "FT", "FAC", "FTC") or re.search(r"\bfa(c)?tura\b", d):
        return "FT"
    return None


def sugerir_metodo(codigo, descricao=""):
    """Código do método de pagamento da Lonntra para um modo do Primavera."""
    c, d = codigo.upper().strip(), _sem_acentos(descricao)
    texto = f"{c} {d}"
    if "debito" in texto or "multibanco" in texto or c in ("MB", "TPA", "CD"):
        return "55"
    if "cartao" in texto or "credito" in texto or c in ("CC", "CRT", "VISA"):
        return "54"
    if "transf" in texto or c in ("TRF", "TB", "TR"):
        return "30"
    if "cheque" in texto or c in ("CHQ", "CH"):
        return "20"
    return "10"


def melhor_driver(disponiveis):
    for d in DRIVERS:
        if d in disponiveis:
            return d
    return None


def parece_servico(descricoes):
    """Um tipo de artigo cujos exemplos são quase todos serviços (mão de obra, serviço, transporte…)."""
    if not descricoes:
        return False
    n = sum(1 for d in descricoes if re.search(r"servi[cç]o|m[aã]o.de.obra|transporte|deslocac|repara[cç]|montagem|instala[cç]",
                                               d or "", re.I))
    return n >= max(1, len(descricoes) // 2)


def ligacao_odbc(driver, servidor, base, utilizador=None, senha=None):
    partes = [f"DRIVER={{{driver}}}", f"SERVER={servidor}", f"DATABASE={base}"]
    partes += [f"UID={utilizador}", f"PWD={{{senha}}}"] if utilizador else ["Trusted_Connection=yes"]
    if driver.startswith("ODBC Driver 18"):
        partes.append("TrustServerCertificate=yes")
    return ";".join(partes)


def _sem_acentos(t):
    t = (t or "").lower()
    for a, b in (("á", "a"), ("à", "a"), ("ã", "a"), ("â", "a"), ("é", "e"), ("ê", "e"), ("í", "i"), ("ó", "o"),
                 ("õ", "o"), ("ô", "o"), ("ú", "u"), ("ç", "c")):
        t = t.replace(a, b)
    return t


def separar_chaves(texto):
    """«k1, k2» (vírgulas, pontos e vírgulas ou espaços) -> ['k1', 'k2'], sem repetidas."""
    vistas = []
    for k in re.split(r"[,;\s]+", texto or ""):
        if k and k not in vistas:
            vistas.append(k)
    return vistas


def _compacto(t):
    return re.sub(r"[^a-z0-9]", "", _sem_acentos(t))


def melhor_base(bases, nomes, alvo):
    """Índice da base cujo nome de empresa melhor corresponde ao alvo: igual, senão a mais parecida que o contém."""
    a = _compacto(alvo)
    if not a:
        return 0
    nomes_c = [_compacto(nomes.get(b.upper(), "")) for b in bases]
    for i, n in enumerate(nomes_c):
        if n == a:
            return i
    parecidas = [(abs(len(n) - len(a)), i) for i, n in enumerate(nomes_c) if n and (a in n or n in a)]
    return min(parecidas)[1] if parecidas else 0


def slug(nome, chave=""):
    t = re.sub(r"[^a-z0-9]+", "-", _sem_acentos(nome)).strip("-")
    return t or (chave[:8] or "empresa")


def senha_existente(pasta=None):
    """A palavra-passe do login de leitura que já está num config.ini deste servidor (da raiz ou de empresas/*).
    O login é um só no SQL Server: mudar-lhe a palavra-passe deixava sem acesso as empresas instaladas antes."""
    pasta = pasta or PASTA
    for config in [pasta / "config.ini", *sorted((pasta / "empresas").glob("*/config.ini"))]:
        try:
            texto = config.read_text(encoding="utf-8")
        except OSError:
            continue
        m = re.search(r"UID=" + re.escape(LOGIN) + r";PWD=\{?([^;}\r\n]+)", texto, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def instalar_versao(pasta_empresa):
    """Põe o pacote em versoes/<versão>/ (as versões ficam lado a lado) e aponta a empresa para ela. O launcher arranca
    sempre a versão do atual.txt; a anterior fica em anterior.txt e a última que deu ciclos bons em estavel.txt."""
    destino = PASTA / "versoes" / __version__ / "primavera_sync"
    shutil.copytree(PASTA / "primavera_sync", destino, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    falhou = PASTA / "versoes" / __version__ / "FALHOU"
    if falhou.exists():  # uma instalação à mão é uma decisão: a versão volta a poder correr
        falhou.unlink()
    pasta_empresa = Path(pasta_empresa)
    pasta_empresa.mkdir(parents=True, exist_ok=True)
    atual = pasta_empresa / "atual.txt"
    antes = atual.read_text(encoding="utf-8").strip() if atual.exists() else ""
    if antes and antes != __version__:
        (pasta_empresa / "anterior.txt").write_text(antes, encoding="utf-8")
    atual.write_text(__version__, encoding="utf-8")
    return __version__


def parece_id(texto):
    """A Lonntra devolve às vezes o ID da empresa (UUID) em vez do nome."""
    return bool(re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", (texto or "").strip()))


def sugerir_moeda(moedas, usos=None):
    """Código da moeda em escudos de Cabo Verde entre (codigo, descricao); sem correspondência, 'CVE'.
    A Lonntra só recebe escudos: a moeda mais usada nos artigos pode ser o euro, por isso não é ela que se propõe."""
    for codigo, desc in moedas:
        texto = _sem_acentos(f"{codigo} {desc}")
        if re.search(r"escudo|cabo.?verde|\bcve\b|\becv\b|\bcve", texto):
            return str(codigo).strip()
    return "CVE"


@dataclass
class Empresa:
    url: str
    chave: str
    nome: str
    pasta: Path = None
    tarefa: str = "PrimaveraLonntra"


def pasta_da_empresa(chave, nome):
    """(pasta, tarefa). Uma instalação antiga (config.ini na raiz com esta chave) mantém a pasta e a tarefa de sempre,
    para não perder o estado.db nem reenviar o histórico; as outras ficam em empresas/<nome>."""
    antigo = PASTA / "config.ini"
    if antigo.exists():
        c = configparser.ConfigParser(interpolation=None)
        c.read(antigo, encoding="utf-8")
        if c.get("lonntra", "chave", fallback="") == chave:
            return PASTA, "PrimaveraLonntra"
    nome_pasta = slug(nome, chave)
    return PASTA / "empresas" / nome_pasta, f"PrimaveraLonntra-{nome_pasta}"


# ---------------------------------------------------------------- conversa

def titulo(t):
    print(f"\n== {t}")
    relatorio.append(f"\n== {t}")


def ok(t):
    print(f"  OK   {t}")
    relatorio.append(f"  OK   {t}")


def aviso(t):
    print(f"  !!   {t}")
    relatorio.append(f"  !!   {t}")


def erro(t):
    print(f"  ERRO {t}")
    relatorio.append(f"  ERRO {t}")


def perguntar(texto, padrao=""):
    r = input(f"  {texto}" + (f" [{padrao}]" if padrao else "") + ": ").strip()
    return r or padrao


def escolher(texto, opcoes, padrao=0):
    for i, o in enumerate(opcoes, 1):
        print(f"    {i}. {o}")
    while True:
        r = perguntar(texto, str(padrao + 1))
        if r.isdigit() and 1 <= int(r) <= len(opcoes):
            return int(r) - 1
        print("    Escolha um dos números.")


def sim(texto, padrao=True):
    r = perguntar(texto + " (s/n)", "s" if padrao else "n").lower()
    return r.startswith("s") or r.startswith("y")


# ---------------------------------------------------------------- passos

def ler_cliente():
    c = configparser.ConfigParser(interpolation=None)
    c.read(PASTA / "cliente.ini", encoding="utf-8")
    return c


def passo_lonntra(cliente):
    """Valida cada chave (uma por empresa; várias separadas por vírgulas) e devolve as empresas."""
    from .lonntra import ErroLigacao, Lonntra
    titulo("Lonntra")
    url = cliente.get("lonntra", "url", fallback="") or perguntar("Endereço da Lonntra", "https://app.lonntra.cv")
    chaves = separar_chaves(cliente.get("lonntra", "chave", fallback=""))
    if chaves:
        print(f"    O cliente.ini tem {len(chaves)} chave(s).")
        mais = separar_chaves(perguntar("Chaves de outras empresas deste servidor (separadas por vírgulas; Enter = nenhuma)"))
        chaves += [k for k in mais if k not in chaves]
    else:
        chaves = separar_chaves(perguntar("Chave(s) da empresa (php yii integracao/chave <NIF>); várias, separadas por vírgulas"))
    if not chaves:
        raise SystemExit("Sem uma chave não dá para continuar.")
    empresas = []
    for chave in chaves:
        for _ in range(3):
            try:
                r = Lonntra(url, chave).estado()
                nome = r.get("empresa") or ""
                if parece_id(nome):  # só o ID: o nome vem da empresa escolhida no Primavera
                    nome = ""
                ok(f"chave aceite ({url}, chave {chave[:8]}…" + (f", empresa {nome})" if nome else ")"))
                if any(e.chave == chave for e in empresas):
                    break
                empresas.append(Empresa(url, chave, nome))
                break
            except ErroLigacao as e:
                erro(f"a Lonntra recusou a chave {chave[:8]}…: {e}")
                if "401" not in str(e):
                    print("    Confirme que este servidor tem internet e abre o endereço no browser.")
                nova = separar_chaves(perguntar("Chave certa desta empresa"))
                if not nova:
                    break
                chave = nova[0]
        else:
            raise SystemExit("Sem uma chave válida não dá para continuar.")
    if not empresas:
        raise SystemExit("Sem uma chave válida não dá para continuar.")
    return empresas


def instancias_locais():
    """Instâncias do SQL Server instaladas neste Windows (registo)."""
    try:
        import winreg
    except ImportError:
        return []
    nomes = []
    for vista in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Microsoft SQL Server\Instance Names\SQL",
                               0, winreg.KEY_READ | vista)
        except OSError:
            continue
        i = 0
        while True:
            try:
                nome = winreg.EnumValue(k, i)[0]
            except OSError:
                break
            nomes.append(nome)
            i += 1
    maquina = socket.gethostname()
    return sorted({maquina if n == "MSSQLSERVER" else f"{maquina}\\{n}" for n in nomes})


def passo_sql(cliente):
    import pyodbc
    titulo("SQL Server do Primavera")
    driver = melhor_driver(pyodbc.drivers())
    if not driver:
        raise SystemExit("Não há nenhum driver ODBC do SQL Server neste Windows.")
    ok(f"driver: {driver}")

    servidor = cliente.get("primavera", "servidor", fallback="")
    if not servidor:
        locais = instancias_locais()
        if len(locais) == 1:
            servidor = locais[0]
        elif locais:
            servidor = locais[escolher("Instância do SQL Server do Primavera", locais)]
        else:
            servidor = perguntar("Instância do SQL Server (ex.: SERVIDOR\\PRIMAVERA)")
    ok(f"instância: {servidor}")

    admin = None
    try:
        admin = pyodbc.connect(ligacao_odbc(driver, servidor, "master"), autocommit=True, timeout=10)
        ok(f"entrada com o utilizador do Windows ({getpass.getuser()})")
    except pyodbc.Error:
        aviso("o utilizador do Windows não entra no SQL Server; é preciso um login de administrador (ex.: sa).")
        print("    Só é usado agora, para criar o login de leitura. Não fica guardado.")
        for _ in range(3):
            u = perguntar("Login de administrador do SQL Server", "sa")
            p = getpass.getpass("  Palavra-passe: ")
            try:
                admin = pyodbc.connect(ligacao_odbc(driver, servidor, "master", u, p), autocommit=True, timeout=10)
                ok(f"entrada como {u}")
                break
            except pyodbc.Error as e:
                erro(f"não entrou: {e.args[-1] if e.args else e}")
        if admin is None:
            raise SystemExit("Sem acesso de administrador ao SQL Server não dá para continuar.")
    return driver, servidor, admin


def bases_primavera(admin):
    bases = [r[0] for r in admin.cursor().execute(
        "SELECT name FROM sys.databases WHERE name LIKE 'PRI%' AND name NOT IN ('PRIEMPRE') AND state = 0 ORDER BY name")]
    nomes = {}
    try:  # a lista de empresas do Primavera, para mostrar o nome de cada base
        for codigo, nome in admin.cursor().execute("SELECT Codigo, IdNome FROM PRIEMPRE.dbo.Empresas"):
            nomes["PRI" + str(codigo).strip().upper()] = str(nome).strip()
    except Exception:  # noqa: BLE001 - versões com outra tabela: mostra só o nome da base
        pass
    return bases, nomes


def passo_base(admin, alvo, pedida="", usadas=(), rotulo=""):
    """Escolhe a base da empresa `alvo` (nome na Lonntra, se se souber). `rotulo`: como identificar a chave no ecrã.
    `usadas`: bases já atribuídas a outras chaves. Devolve (base, nome da empresa no Primavera)."""
    titulo(f"Empresa no Primavera: {alvo or rotulo}" if (alvo or rotulo) else "Empresa no Primavera")
    todas, nomes = bases_primavera(admin)
    bases = [b for b in todas if b not in usadas]
    if pedida:
        if pedida not in todas:
            raise SystemExit(f"A base {pedida} (cliente.ini) não existe nesta instância. Há: {', '.join(todas)}")
        base = pedida
    elif not bases:
        raise SystemExit("Não encontrei nenhuma base do Primavera (PRI…) livre nesta instância.")
    elif len(bases) == 1:
        base = bases[0]
    else:
        if rotulo:
            print(f"    Qual destas empresas do Primavera é a da {rotulo}?")
        padrao = melhor_base(bases, nomes, alvo)
        base = bases[escolher("Base da empresa", [f"{b}  {nomes.get(b.upper(), '')}".strip() for b in bases], padrao)]
    nome = nomes.get(base.upper(), "")
    ok(f"base: {base} {nome}".rstrip())
    return base, nome


def passo_login(admin, driver, servidor, base, senha=None):
    """`senha`: a mesma para todas as empresas da instalação (o login lonntra_leitura é um só no servidor)."""
    titulo("Acesso só de leitura")
    cur = admin.cursor()
    so_windows = cur.execute("SELECT CAST(SERVERPROPERTY('IsIntegratedSecurityOnly') AS int)").fetchone()[0] == 1
    q = base.replace("]", "]]")
    if not so_windows:
        senha = senha or secrets.token_urlsafe(18) + "aA9"
        cur.execute(f"IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'{LOGIN}') "
                    f"CREATE LOGIN [{LOGIN}] WITH PASSWORD = N'{senha}', CHECK_POLICY = ON, DEFAULT_DATABASE = [{q}] "
                    f"ELSE ALTER LOGIN [{LOGIN}] WITH PASSWORD = N'{senha}'")
        cur.execute(f"USE [{q}]; IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'{LOGIN}') "
                    f"CREATE USER [{LOGIN}] FOR LOGIN [{LOGIN}]; EXEC sp_addrolemember 'db_datareader', '{LOGIN}';")
        ok(f"login {LOGIN} (só leitura, palavra-passe gerada e guardada só no config.ini)")
        return ligacao_odbc(driver, servidor, base, LOGIN, senha), False
    # SQL Server só com autenticação do Windows: o sync corre como SYSTEM e entra com essa conta, só leitura
    conta = "NT AUTHORITY\\SYSTEM"
    cur.execute(f"IF NOT EXISTS (SELECT 1 FROM sys.server_principals WHERE name = N'{conta}') CREATE LOGIN [{conta}] FROM WINDOWS")
    cur.execute(f"USE [{q}]; IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = N'{conta}') "
                f"CREATE USER [{conta}] FOR LOGIN [{conta}]; EXEC sp_addrolemember 'db_datareader', '{conta}';")
    ok("SQL Server só com autenticação do Windows: o sync entra como SYSTEM, com leitura na base")
    return ligacao_odbc(driver, servidor, base), True


def passo_codigos(admin, base, cliente):
    titulo("Documentos, pagamentos, artigos e stock")
    cur = admin.cursor()
    cur.execute(f"USE [{base.replace(']', ']]')}]")

    # tipos de documento de venda
    try:
        tipos = cur.execute("""SELECT c.TipoDoc, COUNT(*), MAX(d.Descricao) FROM CabecDoc c
            LEFT JOIN DocumentosVenda d ON d.Documento = c.TipoDoc WHERE c.TipoEntidade = 'C' GROUP BY c.TipoDoc ORDER BY 2 DESC""").fetchall()
    except Exception:  # noqa: BLE001
        tipos = [(r[0], r[1], "") for r in cur.execute(
            "SELECT TipoDoc, COUNT(*) FROM CabecDoc WHERE TipoEntidade = 'C' GROUP BY TipoDoc ORDER BY 2 DESC").fetchall()]
    mapa_tipos = dict(cliente.items("tipos_documento")) if cliente.has_section("tipos_documento") else {}
    if not mapa_tipos:
        print("    Documentos de cliente no Primavera e o tipo proposto na Lonntra (vazio = não enviar):")
        for codigo, n, desc in tipos:
            proposta = sugerir_tipo(codigo, desc or "")
            print(f"      {codigo:<6} {n:>8} documentos  {(desc or '')[:40]:<40} -> {proposta or '(não enviar)'}")
            mapa_tipos[codigo.strip().upper()] = proposta
        if not sim("Estes tipos estão certos?"):
            for codigo, _, desc in tipos:
                r = perguntar(f"{codigo} ({(desc or '')[:30]}) -> FT, FR, TV, NC, ND, DV, FP ou vazio", mapa_tipos[codigo.strip().upper()] or "").upper()
                mapa_tipos[codigo.strip().upper()] = r if r in TIPOS_LONNTRA else None
        mapa_tipos = {k: v for k, v in mapa_tipos.items() if v}
    if not mapa_tipos:
        raise SystemExit("Nenhum tipo de documento para enviar.")
    ok("documentos: " + ", ".join(f"{k}={v}" for k, v in mapa_tipos.items()))

    # modos de pagamento
    try:
        modos = cur.execute("""SELECT c.ModoPag, COUNT(*), MAX(m.Descricao) FROM CabecDoc c LEFT JOIN ModosPag m ON m.Modo = c.ModoPag
            WHERE c.TipoEntidade = 'C' AND c.ModoPag IS NOT NULL AND c.ModoPag <> '' GROUP BY c.ModoPag""").fetchall()
    except Exception:  # noqa: BLE001
        modos = [(r[0], r[1], "") for r in cur.execute("""SELECT ModoPag, COUNT(*) FROM CabecDoc
            WHERE TipoEntidade = 'C' AND ModoPag IS NOT NULL AND ModoPag <> '' GROUP BY ModoPag""").fetchall()]
    mapa_modos = dict(cliente.items("modos_pagamento")) if cliente.has_section("modos_pagamento") else {}
    if not mapa_modos:
        mapa_modos = {m.strip().upper(): sugerir_metodo(m, d or "") for m, _, d in modos}
    ok("pagamentos: " + (", ".join(f"{k}={METODOS.get(v, v)}" for k, v in mapa_modos.items()) or "nenhum (fica numerário)"))

    # tipos de artigo que são serviços
    servicos = cliente.get("sincronizacao", "tipos_artigo_servico", fallback=None)
    if servicos is None:
        propostos = []
        try:
            tipos_art = cur.execute("SELECT TipoArtigo, COUNT(*) FROM Artigo GROUP BY TipoArtigo ORDER BY 2 DESC").fetchall()
            print("    Tipos de artigo no Primavera:")
            for t, n in tipos_art:
                ex = [r[0] for r in cur.execute("SELECT TOP 6 Descricao FROM Artigo WHERE TipoArtigo = ? ORDER BY Artigo", t).fetchall()]
                if parece_servico(ex):
                    propostos.append(str(t).strip())
                print(f"      tipo {str(t).strip():<4} {n:>7} artigos   ex.: {'; '.join((e or '')[:22] for e in ex[:3])}")
        except Exception as e:  # noqa: BLE001 - outra versão do Primavera: pergunta sem a lista
            aviso(f"não consegui listar os tipos de artigo ({str(e)[:140]}).")
        servicos = perguntar("Que tipos são serviços (sem stock)? Separe por vírgulas", ",".join(propostos))
        while not servicos.strip():
            aviso("nenhum tipo de artigo foi marcado como serviço: a mão de obra e outros serviços iriam como artigos com stock, "
                  "e a Lonntra recusaria as existências deles a cada ciclo.")
            if sim("Confirma que esta empresa não tem serviços?", False):
                break
            servicos = perguntar("Que tipos são serviços (sem stock)? Separe por vírgulas", ",".join(propostos))
    ok(f"serviços: tipo(s) {servicos or 'nenhum'}")

    # moeda dos preços e dos documentos: a Lonntra só recebe escudos
    moeda = cliente.get("sincronizacao", "moeda", fallback=None)
    if moeda is None:
        try:
            moedas = [(str(r[0]).strip(), str(r[1] or "").strip()) for r in cur.execute("SELECT Moeda, Descricao FROM Moedas")]
        except Exception:  # noqa: BLE001 - sem a tabela de moedas: propõe só o código
            moedas = []
        proposta = sugerir_moeda(moedas)
        try:
            usos = cur.execute("SELECT Moeda, COUNT(*) FROM CabecDoc WHERE TipoEntidade = 'C' GROUP BY Moeda ORDER BY 2 DESC").fetchall()
            print("    Moedas dos documentos de venda: " + ", ".join(f"{str(m).strip()} ({n})" for m, n in usos))
        except Exception:  # noqa: BLE001
            pass
        moeda = perguntar("Código da moeda em escudos no Primavera (só se enviam documentos nesta moeda)", proposta)
    ok(f"moeda dos preços e dos documentos: {moeda}")
    return mapa_tipos, mapa_modos, servicos, moeda


def _testar_stock(fonte, moeda):
    falhas = {}
    for nome, fn in (("artigos", lambda: fonte.catalogo(moeda)), ("armazéns", fonte.armazens),
                     ("existências", fonte.existencias), ("movimentos", lambda: fonte.movimentos(date.today() - timedelta(days=7)))):
        try:
            ok(f"{nome}: {len(fn())}")
        except Exception as e:  # noqa: BLE001
            falhas[nome] = str(e)[:160]
            erro(f"{nome}: {falhas[nome]}")
    return falhas


PAPEL_DA_FALHA = {"armazéns": "armazens", "existências": "existencias", "movimentos": "movimentos"}


def passo_stock(ligacao, moeda):
    """Experimenta as consultas do catálogo e do stock. Se alguma falhar, varre a base à procura das tabelas certas
    (descobrir.py) e propõe as consultas. Devolve (catalogo, movimentos, consultas)."""
    from . import descobrir
    from .evolution import Evolution
    titulo("Consultas do catálogo e do stock")
    consultas = {}
    fonte = Evolution.ligar(ligacao)
    falhas = _testar_stock(fonte, moeda)

    por_papel = [PAPEL_DA_FALHA[f] for f in falhas if f in PAPEL_DA_FALHA]
    if por_papel:
        aviso("a base não tem as tabelas de stock de origem; a varrer a base para as encontrar…")
        try:
            achadas = descobrir.sugerir(fonte.conn, papeis=por_papel)
        except Exception as e:  # noqa: BLE001
            achadas = {}
            erro(f"não consegui varrer a base: {str(e)[:160]}")
        for papel in por_papel:
            escolhida, _ = achadas.get(papel, (None, []))
            if not escolhida:
                aviso(f"{papel}: nenhuma tabela serve; fica desligado.")
                continue
            print(f"    {papel}: {escolhida.tabela.completo} ({escolhida.tabela.linhas} linhas) -> {escolhida.resultado}")
            for a in escolhida.avisos:
                print(f"      ! {a}")
            if sim(f"Usar a tabela {escolhida.tabela.nome} para {papel}?"):
                consultas[papel] = " ".join(escolhida.sql.split())
        if consultas:
            fonte = Evolution.ligar(ligacao, consultas)
            ok("consultas escritas no config.ini ([consultas]); a testar de novo")
            falhas = _testar_stock(fonte, moeda)

    movimentos = "movimentos" not in falhas
    catalogo = not ({"artigos", "armazéns", "existências"} & set(falhas))
    if not movimentos and catalogo:
        aviso("os movimentos de stock ficam desligados: não encontrei uma tabela que sirva.")
    if not catalogo:
        aviso("o catálogo e o stock ficam desligados; os documentos são enviados na mesma.")
    return catalogo, movimentos, consultas


def escrever_config(url, chave, ligacao, mapa_tipos, mapa_modos, servicos, moeda, catalogo, movimentos, horario,
                    pasta=None, consultas=None):
    pasta = pasta or PASTA
    exemplo = PASTA / "config.example.ini"
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.optionxform = str
    cfg.read(exemplo, encoding="utf-8")
    cfg["primavera"]["ligacao"] = ligacao
    cfg["lonntra"]["url"] = url
    cfg["lonntra"]["chave"] = chave
    s = cfg["sincronizacao"]
    s["data_inicio"] = ""
    s["moeda"] = moeda
    s["tipos_artigo_servico"] = servicos
    s["catalogo"] = "sim" if catalogo else "não"
    s["movimentos_stock"] = "sim" if movimentos else "não"
    s["historico_horario"] = horario
    if pasta != PASTA:  # várias empresas no mesmo servidor: cada uma com o seu estado.db e o seu log
        rel = pasta.relative_to(PASTA)
        s["estado"] = str(rel / "estado.db")
        s["log"] = str(rel / "primavera-sync.log")
    for papel, sql in (consultas or {}).items():
        cfg["consultas"][papel] = sql
    cfg.remove_section("tipos_documento")
    cfg.add_section("tipos_documento")
    for k, v in mapa_tipos.items():
        cfg["tipos_documento"][k] = v
    cfg.remove_section("modos_pagamento")
    cfg.add_section("modos_pagamento")
    for k, v in mapa_modos.items():
        cfg["modos_pagamento"][k] = v
    pasta.mkdir(parents=True, exist_ok=True)
    destino = pasta / "config.ini"
    if destino.exists():
        destino.replace(pasta / f"config.ini.{datetime.now():%Y%m%d-%H%M%S}.bak")
    with open(destino, "w", encoding="utf-8") as f:
        f.write(f"# Gerado pelo instalador do primavera-sync {__version__} em {datetime.now():%d/%m/%Y %H:%M}.\n"
                "# Tem a chave da Lonntra e a palavra-passe de leitura do SQL Server: não partilhar.\n\n")
        cfg.write(f)
    ok(f"config.ini escrito em {destino}")
    return destino


def passo_teste(empresa, config):
    """Envia 10 de cada coisa e diz se chegou. O resto do histórico fica para o horário escolhido."""
    from .evolution import Evolution
    from .lonntra import ErroLigacao, Lonntra
    from .sincronizador import Estado, Sincronizador, imprimir_teste, ler_config
    titulo(f"Teste de envio: {empresa.nome}")
    cfg = ler_config(str(config))
    try:
        sinc = Sincronizador(Evolution.ligar(cfg["primavera"]["ligacao"], dict(cfg["consultas"])),
                             Lonntra(cfg["lonntra"]["url"], cfg["lonntra"]["chave"]), Estado(cfg["sincronizacao"]["estado"]), cfg)
        bem = imprimir_teste(sinc.amostra(10), 10)
    except ErroLigacao as e:
        erro(f"a Lonntra não respondeu: {e}")
        return False
    except Exception as e:  # noqa: BLE001
        erro(f"o teste falhou: {type(e).__name__}: {e}")
        return False
    (ok if bem else erro)("teste de envio " + ("concluído" if bem else "falhou: nada chegou à Lonntra"))
    return bem


def main():
    print(f"Instalador do primavera-sync {__version__} (Primavera ERP -> Lonntra)")
    relatorio.append(f"Instalação do primavera-sync {__version__} em {socket.gethostname()}, {datetime.now():%d/%m/%Y %H:%M}")
    cliente = ler_cliente()
    if cliente.has_section("cliente"):
        relatorio.append(f"Cliente: {cliente.get('cliente', 'nome', fallback='?')} (NIF {cliente.get('cliente', 'nif', fallback='?')})")
    try:
        empresas = passo_lonntra(cliente)
        driver, servidor, admin = passo_sql(cliente)
        # a mesma para todas as bases e a que já estiver em uso: o login de leitura é um só no SQL Server
        senha = senha_existente() or secrets.token_urlsafe(18) + "aA9"
        usadas, dados = [], []
        for n, e in enumerate(empresas, 1):
            pedida = cliente.get("primavera", "base", fallback="") if len(empresas) == 1 else ""
            alvo = e.nome or (cliente.get("cliente", "nome", fallback="") if len(empresas) == 1 else "")
            rotulo = f"chave {n} ({e.chave[:8]}…)"
            base, nome_base = passo_base(admin, alvo, pedida, usadas, rotulo)
            usadas.append(base)
            e.nome = e.nome or nome_base or base
            e.pasta, e.tarefa = pasta_da_empresa(e.chave, e.nome)
            ligacao, windows = passo_login(admin, driver, servidor, base, senha)
            mapa_tipos, mapa_modos, servicos, moeda = passo_codigos(admin, base, cliente)
            catalogo, movimentos, consultas = passo_stock(
                ligacao if not windows else ligacao_odbc(driver, servidor, base), moeda)
            dados.append((e, ligacao, mapa_tipos, mapa_modos, servicos, moeda, catalogo, movimentos, consultas))
        admin.close()

        titulo("Histórico")
        horario = cliente.get("sincronizacao", "historico_horario", fallback=None)
        if horario is None:
            print("    O histórico todo lê anos de documentos e pesa no Primavera durante uns minutos (ou horas).")
            print("    Por isso corre à noite; antes disso vai só o teste de 10 de cada coisa, mais os últimos 30 dias.")
            hora = perguntar("A que horas começa o histórico? (hora de 0 a 23, ou «ja» para já)", "18").lower()
            if hora.startswith("j"):
                horario = ""
            else:
                hora = int(hora) if hora.isdigit() and 0 <= int(hora) <= 23 else 18
                horario = f"{hora}-7"
        ok("histórico: " + (f"entre as {horario.split('-')[0]}h e as {horario.split('-')[1]}h" if horario else "já"))

        titulo("Verificação final")
        from .sincronizador import ler_config, verificar
        quero_teste = sim("Depois de verificar, enviar já 10 documentos, 10 artigos, etc. para confirmar que chegam à Lonntra?")
        bem, manifesto = True, []
        for e, ligacao, mapa_tipos, mapa_modos, servicos, moeda, catalogo, movimentos, consultas in dados:
            print(f"\n  -- {e.nome or e.chave[:8]}")
            config = escrever_config(e.url, e.chave, ligacao, mapa_tipos, mapa_modos, servicos, moeda, catalogo, movimentos,
                                     horario, e.pasta, consultas)
            este = verificar(ler_config(str(config)))
            (ok if este else erro)(f"{e.nome}: " + ("tudo verificado" if este else "a verificação tem erros (ver acima)"))
            if este and quero_teste:
                este = passo_teste(e, config)
            bem = bem and este
            instalar_versao(e.pasta)
            rel = e.pasta.relative_to(PASTA)
            manifesto.append({"nome": e.nome, "tarefa": e.tarefa, "config": str(rel / "config.ini"),
                              "log": str(rel / "primavera-sync.log")})
        (PASTA / "instalacao.json").write_text(json.dumps(manifesto, ensure_ascii=False, indent=2), encoding="utf-8")
        # 0: tudo bem; 2: as empresas ficaram configuradas mas alguma tem erros (o instalar.ps1 arranca as tarefas na mesma:
        # o sync repete e avisa a Lonntra); 1 (nos outros casos): a configuração não ficou escrita
        return 0 if bem else 2
    except SystemExit as e:
        erro(str(e))
        return 1
    except KeyboardInterrupt:
        erro("interrompido")
        return 1
    except Exception as e:  # noqa: BLE001 - qualquer outro erro (ex.: coluna que não existe nesta versão) fica no relatório
        import traceback
        erro(f"{type(e).__name__}: {e}")
        relatorio.append(traceback.format_exc())
        print("    O erro completo está em relatorio-instalacao.txt; mande-o para corrigir.")
        return 1
    finally:
        (PASTA / "relatorio-instalacao.txt").write_text("\n".join(relatorio) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
