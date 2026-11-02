"""Recebimentos das faturas: o que o Primavera da empresa diz que já foi recebido, enviado à Lonntra para a fatura passar a paga
(ou parcial), em vez de ficar «por pagar» e «vencida» para sempre.

A Auto Style guarda os recebimentos numa tabela própria (CNO_recibimento): movimentos com o número do documento, os valores
(líquido, IVA, retenção, selo), o indicador `Recibido` e a `DataRecebimento`. Como não há uma regra publicada de como o
`NumeroDoc` se liga à fatura nem de que valores somam o total recebido, o sync NÃO as supõe: INFERE-as dos dados e confere-as.

  1. Casar:   o `NumeroDoc` de cada movimento com as faturas já enviadas à Lonntra (que têm o número «FA 2026/12»). Tenta, por
              esta ordem, o número completo, série/número, tipo+número e só o número (este só se for único). Chaves ambíguas
              nunca se usam.
  2. Valor:   testa as fórmulas (líquido+IVA, menos retenção, mais selo, só líquido) contra o TotalDocumento das faturas casadas e
              fica com a que bate certo na maioria. Se nenhuma bater em pelo menos 70 %, NÃO envia e diz porquê.
  3. Enviar:  um recebimento por fatura e data de recebimento (soma das linhas), com um id estável, para reenviar atualizar e não
              duplicar. Só envia quando está ligado (`[sincronizacao] recebimentos = sim`), depois de se ver o relatório
              (`--recebimentos` ou o comando remoto `recebimentos`).
"""
import re
from collections import Counter, defaultdict
from datetime import date, datetime

LIMIAR_CASAR = 0.8      # fração mínima de movimentos casados com uma fatura enviada
LIMIAR_VALOR = 0.7      # fração mínima de faturas em que a fórmula do valor bate com o total
TOLERANCIA = 0.015

FORMULAS = {
    "liquido+iva": lambda r: r["liquido"] + r["iva"],
    "liquido+iva-retencao": lambda r: r["liquido"] + r["iva"] - r["retencao"],
    "liquido+iva+selo": lambda r: r["liquido"] + r["iva"] + r["selo"],
    "liquido+iva-retencao+selo": lambda r: r["liquido"] + r["iva"] - r["retencao"] + r["selo"],
    "liquido": lambda r: r["liquido"],
}
ORDEM_CHAVES = ("completo", "serie/numero", "tipo+numero", "numero")


def _num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _data(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)[:10]


def normalizar(texto):
    """«FA 2026/0012» e «fa2026/12» dão a mesma chave: minúsculas, sem espaços nem pontuação (menos «/»), números sem zeros à esquerda."""
    t = re.sub(r"[^a-z0-9/]", "", str(texto or "").lower())
    return re.sub(r"\d+", lambda m: str(int(m.group())), t)


def chaves_do_documento(numero):
    """[(tipo de chave, chave)] de um número «FA 2026/12»."""
    m = re.fullmatch(r"\s*([A-Za-z]+)\s+(\S+?)/(\d+)\s*", str(numero or ""))
    if not m:
        return [("completo", normalizar(numero))]
    tipo, serie, num = m.groups()
    return [("completo", normalizar(f"{tipo}{serie}/{num}")), ("serie/numero", normalizar(f"{serie}/{num}")),
            ("tipo+numero", normalizar(f"{tipo}{num}")), ("numero", normalizar(num))]


class Casador:
    """Liga o NumeroDoc de um movimento a uma fatura já enviada. Só usa chaves que identificam UMA fatura."""

    def __init__(self, enviados):
        """enviados: [(Id do documento, 'FA 2026/12')]"""
        bruto = defaultdict(set)
        for doc_id, numero in enviados:
            for tipo, chave in chaves_do_documento(numero):
                bruto[(tipo, chave)].add(str(doc_id).upper())
        self.indice = {k: next(iter(v)) for k, v in bruto.items() if len(v) == 1}
        self.ambiguas = sum(1 for v in bruto.values() if len(v) > 1)

    def casar(self, numero_doc):
        """(Id do documento, tipo de chave) ou (None, None)."""
        chave = normalizar(numero_doc)
        for tipo in ORDEM_CHAVES:
            doc = self.indice.get((tipo, chave))
            if doc:
                return doc, tipo
        return None, None


def _linha(r):
    return {"id": str(r.get("id") or ""), "documento": str(r.get("documento") or "").strip(), "entidade": str(r.get("entidade") or "").strip(),
            "data": _data(r.get("data_recebimento")), "moeda": str(r.get("moeda") or "").strip(),
            "liquido": _num(r.get("liquido")), "iva": _num(r.get("iva")), "retencao": _num(r.get("retencao")), "selo": _num(r.get("selo"))}


def analisar(linhas, enviados, totais, moeda="CVE", formula=None):
    """Relatório sobre os movimentos recebidos (sem enviar nada).

    linhas:   linhas em bruto (id, documento, data_recebimento, liquido, iva, retencao, selo, moeda…)
    enviados: [(Id do documento, número)] já enviados à Lonntra
    totais:   {Id do documento: TotalDocumento}, ou uma função que os devolve para uma lista de Ids
    formula:  fixa a fórmula do valor (senão escolhe a que melhor bate)
    """
    casador = Casador(enviados)
    movs = [_linha(r) for r in linhas]
    em_moeda = [m for m in movs if not m["moeda"] or m["moeda"].upper() == moeda.upper()]
    casados, por_chave, sem_casar = [], Counter(), []
    for m in em_moeda:
        doc, tipo = casador.casar(m["documento"])
        if doc:
            casados.append({**m, "doc": doc})
            por_chave[tipo] += 1
        else:
            sem_casar.append(m)
    por_doc = defaultdict(list)
    for m in casados:
        por_doc[m["doc"]].append(m)

    if callable(totais):  # só se vão buscar os totais das faturas casadas
        totais = totais(list(por_doc)) if por_doc else {}
    confere = {}
    for nome, f in FORMULAS.items():
        exatas = sum(1 for doc, ms in por_doc.items() if doc in totais and abs(sum(f(m) for m in ms) - totais[doc]) <= TOLERANCIA)
        confere[nome] = exatas
    avaliaveis = sum(1 for doc in por_doc if doc in totais)
    melhor = formula if formula in FORMULAS else (max(confere, key=lambda n: (confere[n], -list(FORMULAS).index(n))) if confere else None)
    taxa_casar = len(casados) / len(em_moeda) if em_moeda else 0.0
    taxa_valor = (confere.get(melhor, 0) / avaliaveis) if (melhor and avaliaveis) else 0.0
    fiavel = bool(em_moeda) and taxa_casar >= LIMIAR_CASAR and taxa_valor >= LIMIAR_VALOR
    motivo = None
    if not em_moeda:
        motivo = "não há movimentos recebidos nesta moeda"
    elif taxa_casar < LIMIAR_CASAR:
        motivo = f"só {taxa_casar:.0%} dos movimentos casam com uma fatura enviada (precisa de {LIMIAR_CASAR:.0%}); ver os exemplos sem par"
    elif taxa_valor < LIMIAR_VALOR:
        motivo = f"nenhuma fórmula do valor bate com o total em {LIMIAR_VALOR:.0%} das faturas (a melhor, {melhor}, em {taxa_valor:.0%})"
    return {"movimentos": len(movs), "na_moeda": len(em_moeda), "casados": len(casados), "documentos": len(por_doc),
            "por_chave": dict(por_chave), "ambiguas": casador.ambiguas, "sem_casar": sem_casar[:5], "n_sem_casar": len(sem_casar),
            "confere": confere, "avaliaveis": avaliaveis, "formula": melhor, "taxa_casar": taxa_casar, "taxa_valor": taxa_valor,
            "fiavel": fiavel, "motivo": motivo, "_por_doc": por_doc}


def recebimentos_a_enviar(analise):
    """[{id, documento_id, data, valor, numero}] — um por fatura e data de recebimento. O id não muda enquanto o par (fatura,
    data) for o mesmo: reenviar com outro valor atualiza o recebimento na Lonntra, não o duplica."""
    f = FORMULAS[analise["formula"]]
    out = []
    for doc, ms in analise["_por_doc"].items():
        por_data = defaultdict(float)
        for m in ms:
            if m["data"]:
                por_data[m["data"]] += f(m)
        for data, valor in sorted(por_data.items()):
            if valor > 0.005:
                out.append({"id": f"{doc}|{data.replace('-', '')}", "documento_id": doc, "data": data, "valor": round(valor, 2)})
    return out


def formatar(analise):
    """O relatório em texto, para o ecrã e para o comando remoto."""
    a = analise
    linhas = [f"Movimentos recebidos lidos: {a['movimentos']} ({a['na_moeda']} na moeda certa)",
              f"Casados com uma fatura já enviada: {a['casados']} ({a['taxa_casar']:.0%}) em {a['documentos']} faturas; "
              f"por tipo de chave: {a['por_chave'] or '—'}; chaves ambíguas ignoradas: {a['ambiguas']}"]
    if a["n_sem_casar"]:
        linhas.append(f"Sem fatura correspondente: {a['n_sem_casar']}. Exemplos (NumeroDoc / Entidade / data):")
        linhas += [f"    {m['documento']!r} / {m['entidade']} / {m['data']}" for m in a["sem_casar"]]
    if a["avaliaveis"]:
        linhas.append(f"Conferência do valor com o total das faturas ({a['avaliaveis']} faturas casadas com total):")
        for nome, n in sorted(a["confere"].items(), key=lambda kv: -kv[1]):
            linhas.append(f"    {nome:<28} bate em {n} ({n / a['avaliaveis']:.0%}){'   <- escolhida' if nome == a['formula'] else ''}")
    linhas.append(("PRONTO PARA ENVIAR (ligar com recebimentos = sim em [sincronizacao])" if a["fiavel"] else f"NÃO ENVIA: {a['motivo']}"))
    return "\n".join(linhas)
