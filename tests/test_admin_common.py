import pytest

from app.admin import common

pytestmark = pytest.mark.unit


@pytest.mark.unit
def test_short_date():
    assert common.short_date("2026-06-18 12:00:00") == "Jun 18"
    assert common.short_date("2026-06-18") == "Jun 18"
    assert common.short_date("") == ""
    assert common.short_date(None) == ""


@pytest.mark.unit
def test_dir_size_and_fmt(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    (d / "a.txt").write_text("hello")
    (d / "b.bin").write_bytes(b"\x00" * 2048)
    sz = common.dir_size(d)
    assert sz == 5 + 2048
    assert common.fmt_size(0) == "—"
    assert common.fmt_size(1500) == "2 KB"
    assert common.fmt_size(2_000_000) == "2 MB"


@pytest.mark.unit
def test_gallery_card():
    g = {
        "id": 1,
        "title": "Test",
        "client_name": "Client",
        "cover_asset_id": None,
        "pin": "1234",
        "published": True,
        "expires_at": "2026-07-01",
        "n_proof": 0,
        "n_proof_pending": 0,
        "n_assets": 5,
        "n_fav": 2,
        "created_at": "2026-06-18 12:00:00",
    }
    card = common.gallery_card(g, "2026-06-20", "2026-06-27")
    assert card["status"] == "Delivered"
    assert card["photos"] == "5 photos"


@pytest.mark.unit
def test_clients_with_hints_moved():
    from app.admin import common

    assert hasattr(common, "_clients_with_hints")
    # basic callable
    assert callable(common._clients_with_hints)


@pytest.mark.unit
def test_save_upload_writes_every_chunk(tmp_path):
    """Reads stay on the loop; the bytes still have to land on disk."""
    import asyncio
    from io import BytesIO

    from starlette.datastructures import UploadFile

    payload = b"x" * (1 << 20) + b"tail"
    upload = UploadFile(filename="shot.bin", file=BytesIO(payload))
    dest = tmp_path / "shot.bin"
    written = asyncio.run(common.save_upload(upload, dest))
    assert written == len(payload)
    assert dest.read_bytes() == payload
