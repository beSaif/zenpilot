"""file_upload only sends files from the upload folders (default + upload_dirs)."""

import contextlib

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from zenpilot import server
from zenpilot.safety import Config


class FakeUpload:
    def __init__(self):
        self.calls = []

    @contextlib.asynccontextmanager
    async def session(self, port):
        yield

    async def tab(self, tab_id):
        return {"id": "t1", "context": "c1", "url": "http://localhost/", "active": True, "title": ""}

    async def ref_url(self, key):
        return "http://localhost/"

    def top_context(self, ref):
        return "c1"

    def ref(self, key):
        return key

    async def file_upload(self, key, paths):
        self.calls.append(paths)
        return "ok"


@pytest.fixture
def upload(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cfg = Config(
        allowlist=["localhost"],
        read_only=False,
        allow_all_sites=False,
        port=9222,
        audit_log=tmp_path / "audit.jsonl",
        auto_launch=False,
    )
    monkeypatch.setattr(server, "load_config", lambda *a, **k: cfg)

    async def ok_ref(key):
        return None

    monkeypatch.setattr(server, "_check_ref", ok_ref)
    fake = FakeUpload()
    monkeypatch.setattr(server, "browser", fake)
    return {"fake": fake, "cfg": cfg, "home": home, "outside": tmp_path / "outside"}


def default_root(env):
    root = env["home"] / ".local/state/zenpilot/uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root


async def test_file_inside_default_root_is_accepted(upload):
    f = default_root(upload) / "a.txt"
    f.write_text("x")
    await server.file_upload(ref="r1", paths=[str(f)])
    assert upload["fake"].calls == [[str(f.resolve())]]


async def test_file_outside_roots_is_rejected(upload):
    upload["outside"].mkdir()
    f = upload["outside"] / "id_rsa"
    f.write_text("SECRET-KEY-BODY")
    with pytest.raises(ToolError, match="upload_dirs") as err:
        await server.file_upload(ref="r1", paths=[str(f)])
    assert "SECRET-KEY-BODY" not in str(err.value)
    assert upload["fake"].calls == []


async def test_symlink_inside_root_pointing_outside_is_rejected(upload):
    f = upload["outside"] / "id_rsa"
    upload["outside"].mkdir()
    f.write_text("k")
    link = default_root(upload) / "link.txt"
    link.symlink_to(f)
    with pytest.raises(ToolError, match="upload_dirs"):
        await server.file_upload(ref="r1", paths=[str(link)])
    assert upload["fake"].calls == []


async def test_dot_dot_traversal_out_of_root_is_rejected(upload):
    upload["outside"].mkdir()
    secret = upload["outside"] / "id_rsa"
    secret.write_text("k")
    traversal = str(default_root(upload) / ".." / ".." / ".." / "outside" / "id_rsa")
    with pytest.raises(ToolError, match="upload_dirs"):
        await server.file_upload(ref="r1", paths=[traversal])
    assert upload["fake"].calls == []


async def test_file_inside_configured_upload_dir_is_accepted(upload):
    extra = upload["outside"]
    extra.mkdir()
    f = extra / "doc.pdf"
    f.write_text("x")
    upload["cfg"].upload_dirs = [str(extra)]
    await server.file_upload(ref="r1", paths=[str(f)])
    assert upload["fake"].calls == [[str(f.resolve())]]


async def test_temp_dir_is_not_an_upload_root_by_default(upload, tmp_path):
    f = tmp_path / "scratch.txt"
    f.write_text("x")
    with pytest.raises(ToolError, match="upload_dirs"):
        await server.file_upload(ref="r1", paths=[str(f)])
    assert upload["fake"].calls == []
