"""Known-vulnerable fixture: every finding here should be detected by a static tool."""

import os
import subprocess


def run_report(name):
    subprocess.run("generate " + name, shell=True)


def load_config(blob):
    return eval(blob)


def lookup(conn, user_id):
    query = "SELECT * FROM users WHERE id = '%s'" % user_id
    return conn.execute(query).fetchall()


def cleanup(path):
    os.system("rm -rf " + path)
