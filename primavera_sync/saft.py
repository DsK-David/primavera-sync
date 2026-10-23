"""Parser for SAF-T (PT) files exported from Primavera."""
import xml.etree.ElementTree as ET


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _child(el, name):
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _text(el, name, default=""):
    c = _child(el, name)
    return (c.text or "").strip() if c is not None and c.text else default


def _num(el, name):
    t = _text(el, name)
    return float(t) if t else 0.0


def _iter(el, name):
    return (c for c in el if _local(c.tag) == name) if el is not None else iter(())


def parse(path):
    """Return dict with customers, invoices, receipts, receipt_lines lists."""
    root = ET.parse(path).getroot()
    out = {"customers": [], "invoices": [], "receipts": [], "receipt_lines": []}

    master = _child(root, "MasterFiles")
    for c in _iter(master, "Customer"):
        out["customers"].append({
            "id": _text(c, "CustomerID"),
            "name": _text(c, "CompanyName"),
            "tax_id": _text(c, "CustomerTaxID"),
        })

    docs = _child(root, "SourceDocuments")

    sales = _child(docs, "SalesInvoices") if docs is not None else None
    for inv in _iter(sales, "Invoice"):
        status = _text(_child(inv, "DocumentStatus"), "InvoiceStatus") if _child(inv, "DocumentStatus") is not None else ""
        if status == "A":  # anulada
            continue
        totals = _child(inv, "DocumentTotals")
        out["invoices"].append({
            "number": _text(inv, "InvoiceNo"),
            "date": _text(inv, "InvoiceDate"),
            "type": _text(inv, "InvoiceType"),
            "status": status,
            "customer_id": _text(inv, "CustomerID"),
            "net_total": _num(totals, "NetTotal"),
            "tax_total": _num(totals, "TaxPayable"),
            "gross_total": _num(totals, "GrossTotal"),
        })

    payments = _child(docs, "Payments") if docs is not None else None
    for p in _iter(payments, "Payment"):
        number = _text(p, "PaymentRefNo")
        totals = _child(p, "DocumentTotals")
        out["receipts"].append({
            "number": number,
            "date": _text(p, "TransactionDate"),
            "type": _text(p, "PaymentType"),
            "customer_id": _text(p, "CustomerID"),
            "gross_total": _num(totals, "GrossTotal"),
        })
        for line in _iter(p, "Line"):
            src = _child(line, "SourceDocumentID")
            if src is None:
                continue
            out["receipt_lines"].append({
                "receipt_number": number,
                "invoice_number": _text(src, "OriginatingON"),
                "amount": _num(line, "CreditAmount") or _num(line, "DebitAmount"),
            })
    return out
