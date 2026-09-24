"""Row collection. The accumulator is rebuilt on every iteration."""


def collect(rows):
    out = []
    for row in rows:
        if row is not None:
            out = out + [row]
    return out
