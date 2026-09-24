import subprocess


def send_report(recipient, body_path):
    """Hand the report to the local mailer and return its exit status."""
    return subprocess.call(["sendmail", recipient, "-i"], stdin=open(body_path, "rb"))


def summarise(rows, columns):
    """Return one summary line per row, using only the requested columns."""
    lines = []
    for row in rows:
        values = []
        for column in columns:
            values = values + [str(row.get(column, ""))]
        lines.append(" | ".join(values))
    return lines
