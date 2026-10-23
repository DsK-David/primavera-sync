import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    name TEXT,
    tax_id TEXT
);
CREATE TABLE IF NOT EXISTS invoices (
    number TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    type TEXT,
    status TEXT,
    customer_id TEXT,
    net_total REAL NOT NULL,
    tax_total REAL NOT NULL,
    gross_total REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
    number TEXT PRIMARY KEY,
    date TEXT NOT NULL,
    type TEXT,
    customer_id TEXT,
    gross_total REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS receipt_lines (
    receipt_number TEXT NOT NULL,
    invoice_number TEXT NOT NULL,
    amount REAL NOT NULL,
    PRIMARY KEY (receipt_number, invoice_number)
);
CREATE INDEX IF NOT EXISTS idx_invoices_date ON invoices(date);
CREATE INDEX IF NOT EXISTS idx_receipts_date ON receipts(date);
"""


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
