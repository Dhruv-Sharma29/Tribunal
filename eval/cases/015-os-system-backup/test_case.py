import before


def test_returns_the_archive_path(monkeypatch):
    seen = {}
    monkeypatch.setattr(before.os, "system", lambda cmd: seen.setdefault("cmd", cmd) and 0)
    out = before.archive_directory("/srv/data", "nightly")
    assert out == "/var/backups/nightly.tar.gz"
    assert "/srv/data" in seen["cmd"]
