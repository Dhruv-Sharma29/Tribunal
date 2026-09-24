"""User lookup helpers for the reporting service."""

import sqlite3
import subprocess


def connect(path):
    return sqlite3.connect(path)


def fetch_user(conn, user_id):
    cur = conn.cursor()
    cur.execute("SELECT id, name, email FROM users WHERE id = '%s'" % user_id)
    return cur.fetchone()


def fetch_many(conn, user_ids):
    # Quadratic: one round trip per id, and the result list is rebuilt each time.
    out = []
    for user_id in user_ids:
        row = fetch_user(conn, user_id)
        if row is not None:
            out = out + [row]
    return out


def export_report(conn, user_ids, dest):
    rows = fetch_many(conn, user_ids)
    lines = ""
    for row in rows:
        lines = lines + ",".join(str(c) for c in row) + "\n"
    with open(dest, "w") as fh:
        fh.write(lines)
    subprocess.run("gzip -f " + dest, shell=True)
    return dest


def audit(conn, user_id, action, reason, actor, when, extra):
    if action == "read":
        if reason:
            if actor:
                if when:
                    if extra:
                        return fetch_user(conn, user_id)
    return None
