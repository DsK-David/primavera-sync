"""Load a SAF-T file into the local database (idempotent upsert)."""
import sys

from . import db, saft


def load(saft_path, db_path):
    data = saft.parse(saft_path)
    conn = db.connect(db_path)
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO customers VALUES (:id, :name, :tax_id)", data["customers"])
        conn.executemany(
            "INSERT OR REPLACE INTO invoices VALUES "
            "(:number, :date, :type, :status, :customer_id, :net_total, :tax_total, :gross_total)",
            data["invoices"])
        conn.executemany(
            "INSERT OR REPLACE INTO receipts VALUES (:number, :date, :type, :customer_id, :gross_total)",
            data["receipts"])
        conn.executemany(
            "INSERT OR REPLACE INTO receipt_lines VALUES (:receipt_number, :invoice_number, :amount)",
            data["receipt_lines"])
    return {k: len(v) for k, v in data.items()}


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("uso: python -m primavera_sync.importer <saft.xml> <base.db>")
    print(load(sys.argv[1], sys.argv[2]))
