from collections.abc import Iterable


def normalise(name: str) -> str:
    """Return a name stripped of surrounding whitespace and lowercased."""
    return name.strip().lower()


def normalise_all(names: Iterable[str]) -> list[str]:
    """Normalise every name, dropping any that is empty once stripped."""
    return [cleaned for name in names if (cleaned := normalise(name))]
