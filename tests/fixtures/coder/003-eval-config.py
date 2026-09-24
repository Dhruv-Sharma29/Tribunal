"""Config loader that evaluates its input."""


def load_config(blob):
    return eval(blob)


def get(blob, key):
    return load_config(blob).get(key)
