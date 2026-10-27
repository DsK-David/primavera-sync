"""Assistente de instalação: descobre o Primavera, configura, testa e diz o resultado.

Corre-o o instalar.bat (que antes instala o Python e o pyodbc). Lê o cliente.ini que estiver na pasta (URL e chave
da Lonntra, e opcionalmente o servidor, a base e os mapas) e só pergunta o que faltar; quando há uma resposta
óbvia, propõe-na e basta Enter. No fim escreve o config.ini e o relatorio-instalacao.txt.

    python -m primavera_sync.instalar            (na pasta do sync, como administrador)
"""
import configparser
import getpass
import re
import secrets
import socket
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from . import __version__

PASTA = Path(__file__).resolve().parent.parent
LOGIN = "lonntra_leitura"
DRIVERS = ["ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server", "SQL Server Native Client 11.0", "SQL Server"]
TIPOS_LONNTRA = ["FT", "FR", "TV", "NC", "ND"]
METODOS = {"10": "numerário", "20": "cheque", "30": "transferência", "54": "cartão de crédito", "55": "cartão de débito"}

relatorio = []


# ---------------------------------------------------------------- sugestões (sem base de dados, testáveis)

def sugerir_tipo(codigo, descricao=""):
    """Tipo da Lonntra para um documento do Primavera, ou None se não é uma venda a enviar."""
    c, d = codigo.upper().strip(), _sem_acentos(descricao)
    if c in ("NC", "NCR") or "nota de credito" in d or "credito" in d and "nota" in d:
        return "NC"
    if c in ("ND",) or "nota de debito" in d:
        return "ND"
    if c in ("FR", "FRC") or "fatura recibo" in d or "fatura-recibo" in d or "factura recibo" in d or "factura-recibo" in d:
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
    from .lonntra import ErroLigacao, Lonntra
    titulo("Lonntra")
    url = cliente.get("lonntra", "url", fallback="") or perguntar("Endereço da Lonntra", "https://app.lonntra.cv")
    chave = cliente.get("lonntra", "chave", fallback="")
    for _ in range(3):
        if not chave:
            chave = perguntar("Chave da empresa (php yii integracao/chave <NIF>)")
        try:
            r = Lonntra(url, chave).estado()
            ok(f"chave aceite ({url}, empresa {r.get('empresa')})")
            return url, chave
        except ErroLigacao as e:
            erro(f"a Lonntra recusou: {e}")
            if "401" not in str(e):
                print("    Confirme que este servidor tem internet e abre o endereço no browser.")
            chave = ""
    raise SystemExit("Sem uma chave válida não dá para continuar.")


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


def passo_base(admin, cliente):
    titulo("Empresa no Primavera")
    bases, nomes = bases_primavera(admin)
    pedida = cliente.get("primavera", "base", fallback="")
    if pedida:
        if pedida not in bases:
            raise SystemExit(f"A base {pedida} (cliente.ini) não existe nesta instância. Há: {', '.join(bases)}")
        base = pedida
    elif not bases:
        raise SystemExit("Não encontrei nenhuma base do Primavera (PRI…) nesta instância.")
    elif len(bases) == 1:
        base = bases[0]
    else:
        alvo = _sem_acentos(cliente.get("cliente", "nome", fallback=""))
        padrao = next((i for i, b in enumerate(bases) if alvo and alvo in _sem_acentos(nomes.get(b.upper(), ""))), 0)
        base = bases[escolher("Base da empresa", [f"{b}  {nomes.get(b.upper(), '')}".strip() for b in bases], padrao)]
    ok(f"base: {base} {nomes.get(base.upper(), '')}".rstrip())
    return base


def passo_login(admin, driver, servidor, base):
    titulo("Acesso só de leitura")
    cur = admin.cursor()
    so_windows = cur.execute("SELECT CAST(SERVERPROPERTY('IsIntegratedSecurityOnly') AS int)").fetchone()[0] == 1
    q = base.replace("]", "]]")
    if not so_windows:
        senha = secrets.token_urlsafe(18) + "aA9"
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
                r = perguntar(f"{codigo} ({(desc or '')[:30]}) -> FT, FR, TV, NC, ND ou vazio", mapa_tipos[codigo.strip().upper()] or "").upper()
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
        tipos_art = cur.execute("SELECT TipoArtigo, COUNT(*) FROM Artigo GROUP BY TipoArtigo ORDER BY 2 DESC").fetchall()
        propostos = []
        print("    Tipos de artigo no Primavera:")
        for t, n in tipos_art:
            ex = [r[0] for r in cur.execute("SELECT TOP 6 Descricao FROM Artigo WHERE TipoArtigo = ? ORDER BY Artigo", t).fetchall()]
            if parece_servico(ex):
                propostos.append(str(t).strip())
            print(f"      tipo {str(t).strip():<4} {n:>7} artigos   ex.: {'; '.join((e or '')[:22] for e in ex[:3])}")
        servicos = perguntar("Que tipos são serviços (sem stock)? Separe por vírgulas", ",".join(propostos))
    ok(f"serviços: tipo(s) {servicos or 'nenhum'}")

    # moeda dos preços
    try:
        moeda = cur.execute("SELECT TOP 1 Moeda FROM ArtigoMoeda GROUP BY Moeda ORDER BY COUNT(*) DESC").fetchone()
        moeda = (moeda[0] if moeda else "CVE").strip()
    except Exception:  # noqa: BLE001
        moeda = "CVE"
    ok(f"moeda dos preços: {moeda}")
    return mapa_tipos, mapa_modos, servicos, moeda


def passo_stock(ligacao, moeda):
    """Experimenta as consultas do catálogo e do stock; desliga o que esta versão do Primavera não tiver."""
    from .evolution import Evolution
    titulo("Consultas do catálogo e do stock")
    fonte = Evolution.ligar(ligacao)
    falhas = {}
    for nome, fn in (("artigos", lambda: fonte.catalogo(moeda)), ("armazéns", fonte.armazens),
                     ("existências", fonte.existencias), ("movimentos", lambda: fonte.movimentos(date.today() - timedelta(days=7)))):
        try:
            ok(f"{nome}: {len(fn())}")
        except Exception as e:  # noqa: BLE001
            falhas[nome] = str(e)[:160]
            erro(f"{nome}: {falhas[nome]}")
    movimentos = "movimentos" not in falhas
    catalogo = not ({"artigos", "armazéns", "existências"} & set(falhas))
    if not movimentos and catalogo:
        aviso("os movimentos de stock ficam desligados: a tabela INV_Movimentos não existe ou é diferente nesta versão.")
        cur = fonte.conn.cursor()
        candidatas = [r[0] for r in cur.execute(
            "SELECT name FROM sys.tables WHERE name LIKE '%Mov%' OR name LIKE '%STK%' OR name LIKE 'INV%' ORDER BY name")]
        aviso("tabelas candidatas: " + (", ".join(candidatas) or "nenhuma") + " (ver [consultas] no config.ini)")
    if not catalogo:
        aviso("o catálogo e o stock ficam desligados; os documentos são enviados na mesma.")
    return catalogo, movimentos


def escrever_config(url, chave, ligacao, mapa_tipos, mapa_modos, servicos, moeda, catalogo, movimentos, horario):
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
    cfg.remove_section("tipos_documento")
    cfg.add_section("tipos_documento")
    for k, v in mapa_tipos.items():
        cfg["tipos_documento"][k] = v
    cfg.remove_section("modos_pagamento")
    cfg.add_section("modos_pagamento")
    for k, v in mapa_modos.items():
        cfg["modos_pagamento"][k] = v
    destino = PASTA / "config.ini"
    if destino.exists():
        destino.replace(PASTA / f"config.ini.{datetime.now():%Y%m%d-%H%M%S}.bak")
    with open(destino, "w", encoding="utf-8") as f:
        f.write(f"# Gerado pelo instalador do primavera-sync {__version__} em {datetime.now():%d/%m/%Y %H:%M}.\n"
                "# Tem a chave da Lonntra e a palavra-passe de leitura do SQL Server: não partilhar.\n\n")
        cfg.write(f)
    ok(f"config.ini escrito em {destino}")
    return destino


def main():
    print(f"Instalador do primavera-sync {__version__} (Primavera ERP Evolution -> Lonntra)")
    relatorio.append(f"Instalação do primavera-sync {__version__} em {socket.gethostname()}, {datetime.now():%d/%m/%Y %H:%M}")
    cliente = ler_cliente()
    if cliente.has_section("cliente"):
        relatorio.append(f"Cliente: {cliente.get('cliente', 'nome', fallback='?')} (NIF {cliente.get('cliente', 'nif', fallback='?')})")
    try:
        url, chave = passo_lonntra(cliente)
        driver, servidor, admin = passo_sql(cliente)
        base = passo_base(admin, cliente)
        ligacao, windows = passo_login(admin, driver, servidor, base)
        mapa_tipos, mapa_modos, servicos, moeda = passo_codigos(admin, base, cliente)
        admin.close()
        catalogo, movimentos = passo_stock(ligacao if not windows else ligacao_odbc(driver, servidor, base), moeda)

        titulo("Histórico")
        horario = cliente.get("sincronizacao", "historico_horario", fallback=None)
        if horario is None:
            print("    O histórico todo lê anos de documentos e pesa no Primavera durante uns minutos (ou horas).")
            horario = "" if sim("Enviar o histórico já, sem esperar pela noite?", False) else "20-7"
        ok("histórico: " + (f"entre as {horario.split('-')[0]}h e as {horario.split('-')[1]}h" if horario else "já"))

        config = escrever_config(url, chave, ligacao, mapa_tipos, mapa_modos, servicos, moeda, catalogo, movimentos, horario)

        titulo("Verificação final")
        from .sincronizador import ler_config, verificar
        bem = verificar(ler_config(str(config)))
        (ok if bem else erro)("tudo verificado" if bem else "a verificação tem erros (ver acima)")
        return 0 if bem else 1
    except SystemExit as e:
        erro(str(e))
        return 1
    except KeyboardInterrupt:
        erro("interrompido")
        return 1
    finally:
        (PASTA / "relatorio-instalacao.txt").write_text("\n".join(relatorio) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
