from before import render_rows


def test_renders_each_row(tmp_path):
    template = tmp_path / "t.txt"
    template.write_text("hello {name}", encoding="utf-8")
    rows = [{"name": "ana"}, {"name": "bo"}]
    assert render_rows(rows, str(template)) == ["hello ana", "hello bo"]


def test_no_rows_reads_nothing(tmp_path):
    template = tmp_path / "t.txt"
    template.write_text("x", encoding="utf-8")
    assert render_rows([], str(template)) == []
