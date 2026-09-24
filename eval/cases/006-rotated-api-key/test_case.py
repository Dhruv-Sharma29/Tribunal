import before


def test_sends_the_bearer_header(monkeypatch):
    seen = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(request):
        seen["headers"] = request.headers
        seen["url"] = request.full_url
        return FakeResponse()

    monkeypatch.setattr(before.urllib.request, "urlopen", fake_urlopen)
    assert before.fetch_invoice("inv-1") == b"{}"
    assert seen["url"].endswith("/invoices/inv-1")
    assert any("Bearer " in str(v) for v in seen["headers"].values())
