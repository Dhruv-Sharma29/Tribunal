"""CSV rendering by repeated string concatenation."""


def render(rows):
    text = ""
    for row in rows:
        text = text + ",".join(str(cell) for cell in row) + "\n"
    return text
