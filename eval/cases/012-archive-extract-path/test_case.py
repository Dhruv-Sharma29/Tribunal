import io
import zipfile

from before import extract_member


def make_archive(tmp_path, name, data=b"x"):
    path = tmp_path / "a.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(name, data)
    return zipfile.ZipFile(io.BytesIO(path.read_bytes()))


def test_writes_the_member_under_the_destination(tmp_path):
    archive = make_archive(tmp_path, "docs/readme.txt", b"hello")
    out = tmp_path / "out"
    written = extract_member(archive, "docs/readme.txt", str(out))
    assert (out / "docs" / "readme.txt").read_bytes() == b"hello"
    assert written.endswith("readme.txt")
