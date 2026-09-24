"""User lookup. The query is built with % formatting."""


def fetch_user(conn, user_id):
    query = "SELECT id, name FROM users WHERE id = '%s'" % user_id
    return conn.execute(query).fetchone()
