from before import generate_report


def test_returns_the_destination_path(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "before.subprocess.run", lambda *a, **k: calls.append((a, k))
    )
    assert generate_report("q3", "/tmp/out") == "/tmp/out/q3.pdf"
    assert calls, "the report command was never invoked"
