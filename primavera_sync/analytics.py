"""Queries behind the dashboard."""
from datetime import date

OPEN_INVOICES_SQL = """
SELECT i.number, i.date, i.customer_id, COALESCE(c.name, i.customer_id) AS customer,
       i.gross_total,
       COALESCE(SUM(l.amount), 0) AS paid,
       i.gross_total - COALESCE(SUM(l.amount), 0) AS open_amount
FROM invoices i
LEFT JOIN customers c ON c.id = i.customer_id
LEFT JOIN receipt_lines l ON l.invoice_number = i.number
WHERE i.type IN ('FT', 'FR', 'FS', 'VD', 'ND')
GROUP BY i.number
"""


def _rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params)]


def summary(conn):
    inv = conn.execute(
        "SELECT COALESCE(SUM(net_total),0) n, COALESCE(SUM(tax_total),0) t, "
        "COALESCE(SUM(gross_total),0) g, COUNT(*) c FROM invoices").fetchone()
    rec = conn.execute(
        "SELECT COALESCE(SUM(gross_total),0) g, COUNT(*) c FROM receipts").fetchone()
    open_total = conn.execute(
        f"SELECT COALESCE(SUM(open_amount),0) FROM ({OPEN_INVOICES_SQL}) WHERE open_amount > 0.005"
    ).fetchone()[0]
    return {
        "invoices_count": inv["c"], "invoiced_net": inv["n"], "invoiced_tax": inv["t"],
        "invoiced_gross": inv["g"], "receipts_count": rec["c"], "received": rec["g"],
        "open_total": open_total,
    }


def monthly(conn):
    return _rows(conn, """
        SELECT m AS month, SUM(invoiced) AS invoiced, SUM(received) AS received FROM (
            SELECT substr(date,1,7) AS m, gross_total AS invoiced, 0 AS received FROM invoices
            UNION ALL
            SELECT substr(date,1,7), 0, gross_total FROM receipts
        ) GROUP BY m ORDER BY m""")


def by_customer(conn):
    return _rows(conn, f"""
        SELECT customer, SUM(gross_total) AS invoiced, SUM(paid) AS paid,
               SUM(open_amount) AS open_amount
        FROM ({OPEN_INVOICES_SQL}) GROUP BY customer_id ORDER BY invoiced DESC""")


def open_invoices(conn, today=None):
    today = today or date.today()
    rows = _rows(conn, f"SELECT * FROM ({OPEN_INVOICES_SQL}) WHERE open_amount > 0.005 ORDER BY date")
    for r in rows:
        r["days_open"] = (today - date.fromisoformat(r["date"])).days
    return rows
