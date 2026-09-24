import sqlite3

from sql_injection import export_report, fetch_many, fetch_user


def _db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, email TEXT)")
    conn.executemany(
        "INSERT INTO users VALUES (?, ?, ?)",
        [(1, "ada", "ada@example.com"), (2, "grace", "grace@example.com")],
    )
    return conn


def test_fetch_user_returns_row():
    assert fetch_user(_db(), 1) == (1, "ada", "ada@example.com")


def test_fetch_many_skips_missing():
    assert len(fetch_many(_db(), [1, 2, 99])) == 2


def test_export_report_writes_file(tmp_path):
    dest = tmp_path / "out.csv"
    assert export_report(_db(), [1, 2], str(dest)) == str(dest)
