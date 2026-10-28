"""Monta as consultas ao Primavera a partir da estrutura real da base (tabelas e colunas), em vez de nomes fixos.

Cada dado que o sync precisa tem uma lista de nomes possíveis (as variações entre versões do Primavera). O Esquema
procura-os na base, também com pequenas diferenças de escrita (maiúsculas, acentos, «_»), e o que for opcional e não
existir fica NULL em vez de parar o sync. Só o que é indispensável para um documento estar certo (número, data, total,
artigo, quantidade, preço, IVA) obriga a parar, com uma mensagem a dizer o que falta.
"""
import difflib
import re
import unicodedata
from dataclasses import dataclass, field


class ErroEsquema(Exception):
    """A estrutura da base não tem algo indispensável."""


@dataclass
class Consulta:
    sql: str
    extra: dict = field(default_factory=dict)  # pedaços que dependem dos nomes reais (ex.: o filtro da data)


def _norm(t):
    t = unicodedata.normalize("NFKD", str(t or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", t.lower())


class Esquema:
    def __init__(self, tabelas):
        self.tabelas = tabelas
        self.notas = []  # o que se assumiu ou ficou vazio, para o log e o --esquema
        self._por_nome = {}
        for t in sorted(tabelas, key=lambda t: t.esquema.lower() != "dbo"):  # dbo primeiro
            self._por_nome.setdefault(_norm(t.nome), t)

    # ---- procura

    def tabela(self, candidatas):
        for c in candidatas:
            t = self._por_nome.get(_norm(c))
            if t:
                return t
        for c in candidatas:
            parecidos = difflib.get_close_matches(_norm(c), list(self._por_nome), n=1, cutoff=0.9)
            if parecidos:
                t = self._por_nome[parecidos[0]]
                self.notas.append(f"assumi a tabela {t.nome} em vez de {c}")
                return t
        return None

    def coluna(self, tabela, candidatos):
        """Nome real da coluna: igual a um candidato (ignorando maiúsculas, acentos e «_»), senão a mais parecida."""
        if tabela is None:
            return None
        por_norm = {}
        for c in tabela.colunas:
            por_norm.setdefault(_norm(c), c)
        for cand in candidatos:
            if _norm(cand) in por_norm:
                return por_norm[_norm(cand)]
        for cand in candidatos:
            parecidos = difflib.get_close_matches(_norm(cand), list(por_norm), n=1, cutoff=0.88)
            if parecidos:
                real = por_norm[parecidos[0]]
                self.notas.append(f"{tabela.nome}: assumi a coluna {real} em vez de {cand}")
                return real
        return None

    def selecionar(self, t, alias, campos):
        """campos: [(nome lógico, [nomes possíveis], obrigatório)] -> (partes do SELECT, em falta, {lógico: real})."""
        partes, faltam, mapa = [], [], {}
        for logico, candidatos, obrigatorio in campos:
            real = self.coluna(t, candidatos)
            if real:
                mapa[logico] = real
                partes.append(f"{alias}.[{real}] AS [{logico}]")
            elif obrigatorio:
                faltam.append(logico)
            else:
                partes.append(f"NULL AS [{logico}]")
                self.notas.append(f"{t.nome}: não há a coluna {logico} (fica vazia)")
        return partes, faltam, mapa

    def exigir(self, papel, candidatas):
        t = self.tabela(candidatas)
        if t is None:
            raise ErroEsquema(f"não encontrei a tabela de {papel} (procurei: {', '.join(candidatas)})")
        return t

    def _faltam(self, t, faltam):
        if faltam:
            raise ErroEsquema(f"a tabela {t.nome} não tem a(s) coluna(s) {', '.join(faltam)} (nem parecidas): "
                              f"o sync não pode enviar documentos certos sem elas")


# ---------------------------------------------------------------- documentos, linhas, clientes

def documentos(e):
    t = e.exigir("cabeçalhos de documentos", ["CabecDoc"])
    sel, faltam, m = e.selecionar(t, "c", [
        ("Id", ["Id", "IdCabecDoc", "IdDoc"], True),
        ("TipoDoc", ["TipoDoc", "TipoDocumento", "Tipo"], True),
        ("Serie", ["Serie"], True),
        ("NumDoc", ["NumDoc", "NumDocumento", "Numero"], True),
        ("Data", ["Data", "DataDoc", "DataDocumento"], True),
        ("DataVenc", ["DataVenc", "DataVencimento", "Vencimento"], False),
        ("Entidade", ["Entidade", "Cliente", "CodCliente"], True),
        ("CondPag", ["CondPag", "CondicaoPagamento"], False),
        ("ModoPag", ["ModoPag", "ModoPagamento"], False),
        ("Moeda", ["Moeda"], False),
        ("TotalDocumento", ["TotalDocumento", "TotalDoc", "TotalGeral", "Total"], True),
        ("DescFinanceiro", ["DescFinanceiro", "DescontoFinanceiro"], False),
    ])
    e._faltam(t, faltam)

    onde, juntar = [], ""
    entidade = e.coluna(t, ["TipoEntidade", "TipoEnt"])
    if entidade:
        onde.append(f"c.[{entidade}] = 'C'")
    else:  # sem o tipo de entidade: são de cliente os que têm a entidade na tabela de clientes
        cli = e.tabela(["Clientes"])
        chave = e.coluna(cli, ["Cliente", "Codigo"])
        if not (cli and chave):
            raise ErroEsquema("CabecDoc não tem TipoEntidade e não encontrei a tabela Clientes: não sei quais são de cliente")
        onde.append(f"EXISTS (SELECT 1 FROM [{cli.esquema}].[{cli.nome}] k WHERE k.[{chave}] = c.[{m['Entidade']}])")
        e.notas.append("CabecDoc sem TipoEntidade: filtrei os documentos pelos clientes existentes na tabela Clientes")

    estado = e.tabela(["CabecDocStatus"])
    ligacao = e.coluna(estado, ["IdCabecDoc", "IdDoc"])
    anulado = e.coluna(estado, ["Anulado"])
    if estado and ligacao and anulado:
        juntar = f"LEFT JOIN [{estado.esquema}].[{estado.nome}] s ON s.[{ligacao}] = c.[{m['Id']}]"
        onde.append(f"COALESCE(s.[{anulado}], 0) = 0")
    elif e.coluna(t, ["Anulado"]):
        onde.append(f"COALESCE(c.[{e.coluna(t, ['Anulado'])}], 0) = 0")
    else:
        e.notas.append("não encontrei onde o Primavera marca os documentos anulados: podem ir anulados")

    ordem = ", ".join(f"c.[{m[k]}]" for k in ("Data", "TipoDoc", "Serie", "NumDoc"))
    sql = (f"SELECT {', '.join(sel)} FROM [{t.esquema}].[{t.nome}] c {juntar} WHERE {' AND '.join(onde)} "
           f"AND c.[{m['TipoDoc']}] IN ({{tipos}}) {{filtro_data}} ORDER BY {ordem}")
    return Consulta(sql, {"filtro_data": f"AND c.[{m['Data']}] >= ?"})


def linhas(e):
    t = e.exigir("linhas de documentos", ["LinhasDoc"])
    sel, faltam, m = e.selecionar(t, "l", [
        ("IdCabecDoc", ["IdCabecDoc", "IdDoc", "IdCabec"], True),
        ("NumLinha", ["NumLinha", "Linha", "NLinha"], False),
        ("Artigo", ["Artigo"], True),
        ("Descricao", ["Descricao", "Designacao"], False),
        ("Quantidade", ["Quantidade", "Qtd"], True),
        ("PrecoLiquido", ["PrecoLiquido", "TotalLiquido", "PrecoLiq"], True),
        ("TaxaIva", ["TaxaIva", "TaxaIVA", "Taxa"], True),
    ])
    e._faltam(t, faltam)
    id_linha = e.coluna(t, ["Id", "IdLinha"])
    origem = e.coluna(t, ["IdLinhaOrigemCopia", "IdLinhaOrigem"])
    if id_linha and origem:
        sel.append(f"o.[{m['IdCabecDoc']}] AS [IdCabecDocOrigem]")
        juntar = f"LEFT JOIN [{t.esquema}].[{t.nome}] o ON o.[{id_linha}] = l.[{origem}]"
    else:
        sel.append("NULL AS [IdCabecDocOrigem]")
        juntar = ""
        e.notas.append("LinhasDoc sem ligação à linha de origem: as notas de crédito não referem o documento original")
    ordem = f"l.[{m['IdCabecDoc']}]" + (f", l.[{m['NumLinha']}]" if "NumLinha" in m else "")
    sql = (f"SELECT {', '.join(sel)} FROM [{t.esquema}].[{t.nome}] l {juntar} "
           f"WHERE l.[{m['IdCabecDoc']}] IN ({{ids}}) AND l.[{m['Artigo']}] IS NOT NULL AND l.[{m['Artigo']}] <> '' "
           f"ORDER BY {ordem}")
    return Consulta(sql)


def clientes(e):
    t = e.exigir("clientes", ["Clientes"])
    sel, faltam, m = e.selecionar(t, "c", [
        ("Cliente", ["Cliente", "Codigo"], True),
        ("Nome", ["Nome", "Designacao"], True),
        ("NumContrib", ["NumContrib", "NumContribuinte", "Contribuinte", "NIF"], False),
        ("Fac_Mor", ["Fac_Mor", "Morada"], False),
        ("Fac_Local", ["Fac_Local", "Localidade"], False),
        ("Fac_Tel", ["Fac_Tel", "Telefone", "Tel"], False),
    ])
    e._faltam(t, faltam)
    return Consulta(f"SELECT {', '.join(sel)} FROM [{t.esquema}].[{t.nome}] c WHERE c.[{m['Cliente']}] IN ({{codigos}})")


# ---------------------------------------------------------------- artigos

def _preco(e, artigo_col):
    """(juntar, partes do SELECT, usa a moeda?) do preço de venda, que vem de ArtigoMoeda."""
    am = e.tabela(["ArtigoMoeda"])
    ligacao = e.coluna(am, ["Artigo"])
    pvp = e.coluna(am, ["PVP1", "PVP", "Preco1"])
    if not (am and ligacao and pvp):
        e.notas.append("sem tabela de preços (ArtigoMoeda/PVP1): os artigos vão sem preço de venda")
        return "", ["NULL", "NULL"], False
    iva = e.coluna(am, ["PVP1IvaIncluido", "PVP1IvaInc", "PVPIvaIncluido"])
    moeda = e.coluna(am, ["Moeda"])
    campos = [f"am.[{pvp}] AS [PVP]", (f"am.[{iva}] AS [PVPComIva]" if iva else "NULL AS [PVPComIva]")]
    filtro = f" AND am.[{moeda}] = ?" if moeda else ""
    juntar = (f"OUTER APPLY (SELECT TOP 1 {', '.join(campos)} FROM [{am.esquema}].[{am.nome}] am "
              f"WHERE am.[{ligacao}] = a.[{artigo_col}]{filtro}) m")
    return juntar, ["m.[PVP]", "m.[PVPComIva]"], bool(moeda)


def artigos_codigos(e):
    """Os artigos de um lote de documentos: descrição, tipo e preço."""
    t = e.exigir("artigos", ["Artigo", "Artigos"])
    sel, faltam, m = e.selecionar(t, "a", [
        ("Artigo", ["Artigo", "Codigo"], True),
        ("Descricao", ["Descricao", "Designacao"], False),
        ("TipoArtigo", ["TipoArtigo", "Tipo"], False),
        ("CodBarras", ["CodBarras", "CodigoBarras"], False),
    ])
    e._faltam(t, faltam)
    return Consulta(f"SELECT {', '.join(sel)} FROM [{t.esquema}].[{t.nome}] a WHERE a.[{m['Artigo']}] IN ({{codigos}})")


def catalogo(e):
    """O catálogo todo, com o preço na moeda dada (o ? é a moeda, se a tabela de preços a tiver)."""
    t = e.exigir("artigos", ["Artigo", "Artigos"])
    sel, faltam, m = e.selecionar(t, "a", [
        ("Artigo", ["Artigo", "Codigo"], True),
        ("Descricao", ["Descricao", "Designacao"], True),
        ("TipoArtigo", ["TipoArtigo", "Tipo"], False),
        ("CodBarras", ["CodBarras", "CodigoBarras"], False),
        ("Familia", ["Familia"], False),
        ("Anulado", ["ArtigoAnulado", "Anulado", "Inactivo"], False),
        ("Custo", ["PCMedio", "PrecoCustoMedio", "PCUltimo", "PCPadrao", "PrecoCusto"], False),
    ])
    e._faltam(t, faltam)
    juntar = []

    iva_t = e.tabela(["Iva", "TaxasIva"])
    iva_art, iva_chave, iva_taxa = (e.coluna(t, ["Iva", "CodIva", "IdIva"]), e.coluna(iva_t, ["Iva", "Codigo"]),
                                    e.coluna(iva_t, ["Taxa", "TaxaIva", "Percentagem"]))
    if iva_art and iva_chave and iva_taxa:
        juntar.append(f"LEFT JOIN [{iva_t.esquema}].[{iva_t.nome}] i ON i.[{iva_chave}] = a.[{iva_art}]")
        sel.append(f"i.[{iva_taxa}] AS [Taxa]")
    else:
        sel.append("NULL AS [Taxa]")
        e.notas.append("não encontrei as taxas de IVA dos artigos: vão sem IVA (a Lonntra pode recusá-los)")

    fam_t = e.tabela(["Familias", "Familia"]) if "Familia" in m else None
    fam_chave, fam_desc = e.coluna(fam_t, ["Familia", "Codigo"]), e.coluna(fam_t, ["Descricao", "Designacao", "Nome"])
    if fam_t is not None and fam_t is not t and fam_chave and fam_desc:
        juntar.append(f"LEFT JOIN [{fam_t.esquema}].[{fam_t.nome}] f ON f.[{fam_chave}] = a.[{m['Familia']}]")
        sel.append(f"f.[{fam_desc}] AS [FamiliaNome]")
    else:
        sel.append("NULL AS [FamiliaNome]")

    preco_juntar, preco_sel, _ = _preco(e, m["Artigo"])
    if preco_juntar:
        juntar.append(preco_juntar)
    sel += [f"{preco_sel[0]} AS [PVP]", f"{preco_sel[1]} AS [PVPComIva]"]
    return Consulta(f"SELECT {', '.join(sel)} FROM [{t.esquema}].[{t.nome}] a {' '.join(juntar)}")


BUILDERS = {
    "documentos": documentos,
    "linhas": linhas,
    "clientes": clientes,
    "artigos_codigos": artigos_codigos,
    "artigos": catalogo,
}
