import asyncio
import base64
from pathlib import Path

from fastmcp import Client

from gitswarm.surfaces.mcp import mcp
from tests.conftest import git


def commit(wt: Path) -> None:
    (wt / "n.txt").write_text("n\n")
    git("add", "n.txt", cwd=wt)
    git("commit", "-q", "-m", "n", cwd=wt)


def call(name: str, **args):
    async def go():
        async with Client(mcp) as c:
            res = await c.call_tool(name, args)
            return res.data

    return asyncio.run(go())


def test_tools_listed():
    async def go():
        async with Client(mcp) as c:
            return {t.name for t in await c.list_tools()}

    names = asyncio.run(go())
    assert {
        "hive_init",
        "workspace_create",
        "workspace_get",
        "workspace_list",
        "workspace_read_file",
        "workspace_tree",
        "workspace_publish",
        "workspace_drop",
        "workspace_gc",
        "events_tail",
        "doctor",
        "stats",
    } <= names


def test_lifecycle_via_mcp(remote_url: str, home: Path):
    assert call("hive_init", url=remote_url)["ok"] is True
    r = call(
        "workspace_create", remote=remote_url, base_ref="main", agent_name="impl", checkout=True
    )
    assert r["ok"] is True and (Path(r["path"]) / "README.md").exists()
    got = call("workspace_get", remote=remote_url, ws_id=r["id"])
    assert got["state"] == "open"
    rd = call("workspace_read_file", remote=remote_url, ws_id=r["id"], path="README.md")
    assert rd["content"] == "seed\n"
    commit(Path(r["path"]))
    assert call("workspace_publish", remote=remote_url, ws_id=r["id"])["ok"] is True
    assert call("workspace_drop", remote=remote_url, ws_id=r["id"])["state"] == "dropped"


def test_errors_are_payloads_not_exceptions(remote_url: str, home: Path):
    call("hive_init", url=remote_url)
    out = call("workspace_get", remote=remote_url, ws_id="01J00000000000000000000000")
    assert out["ok"] is False and out["error"]["kind"] == "NotFound"


def _create(remote_url: str, **kw):
    return call("workspace_create", remote=remote_url, **kw)


def test_list_filters_and_rejects_bad_state(remote_url: str, home: Path):
    call("hive_init", url=remote_url)
    a = _create(remote_url, checkout=True)
    b = _create(remote_url)
    commit(Path(a["path"]))
    call("workspace_publish", remote=remote_url, ws_id=a["id"])
    got = call("workspace_list", remote=remote_url, state="published")
    assert [w["id"] for w in got["workspaces"]] == [a["id"]] and got["invalid"] == []
    assert b["id"] not in [w["id"] for w in got["workspaces"]]
    bad = call("workspace_list", remote=remote_url, state="bogus")
    assert bad["ok"] is False and bad["error"]["kind"] == "Usage"


def test_tree_and_gc(remote_url: str, home: Path):
    call("hive_init", url=remote_url)
    r = _create(remote_url)
    tree = call("workspace_tree", remote=remote_url, ws_id=r["id"])
    assert "README.md" in str(tree["entries"])
    assert call("workspace_gc", remote=remote_url) == {
        "ok": True,
        "expired": [],
        "invalid": [],
        "conflicted": [],
        "remote": remote_url,
    }


def test_read_binary_as_b64(remote_url: str, home: Path):
    call("hive_init", url=remote_url)
    r = _create(remote_url, checkout=True)
    blob = b"\xff\xfe\x00bin"
    wt = Path(r["path"])
    (wt / "b.bin").write_bytes(blob)
    git("add", "b.bin", cwd=wt)
    git("commit", "-q", "-m", "bin", cwd=wt)
    call("workspace_publish", remote=remote_url, ws_id=r["id"])
    rd = call("workspace_read_file", remote=remote_url, ws_id=r["id"], path="b.bin")
    assert base64.b64decode(rd["content_b64"]) == blob and "content" not in rd


def test_create_records_agent_labels_parent(remote_url: str, home: Path):
    call("hive_init", url=remote_url)
    p = _create(remote_url, agent_name="impl", agent_run="r1", labels={"k": "v"})
    got = call("workspace_get", remote=remote_url, ws_id=p["id"])
    assert got["labels"] == {"k": "v"}
    assert got["agent"] == {"name": "impl", "run": "r1"}
    c = _create(remote_url, from_ws=p["id"])
    child = call("workspace_get", remote=remote_url, ws_id=c["id"])
    assert child["parent"] == p["id"]


def test_publish_returns_full_oid(remote_url: str, home: Path):
    call("hive_init", url=remote_url)
    r = _create(remote_url, checkout=True)
    commit(Path(r["path"]))
    assert len(call("workspace_publish", remote=remote_url, ws_id=r["id"])["oid"]) == 40


def test_events_tail_via_mcp(remote_url: str, home: Path):
    call("hive_init", url=remote_url)
    r = _create(remote_url)
    call("workspace_drop", remote=remote_url, ws_id=r["id"])
    out = call("events_tail", remote=remote_url)
    assert out["ok"] is True
    assert [e["kind"] for e in out["events"]] == ["ws.dropped", "ws.created"]


def test_events_tail_bad_since_is_payload(remote_url: str, home: Path):
    call("hive_init", url=remote_url)
    out = call("events_tail", remote=remote_url, since="bogus")
    assert out["ok"] is False and out["error"]["kind"] == "NotFound"


def test_remote_is_optional_and_discovered(
    remote_url: str, home: Path, tmp_path: Path, monkeypatch
):
    call("hive_init", url=remote_url)
    clone = tmp_path / "clone"
    git("clone", "-q", remote_url, str(clone), cwd=tmp_path)
    monkeypatch.chdir(clone)
    r = call("workspace_create", base_ref="main")
    assert r["ok"] is True and r["remote"] == remote_url
    got = call("workspace_get", ws_id=r["id"])
    assert got["ok"] is True and got["remote"] == remote_url


def test_no_remote_outside_repo_is_usage_payload(home: Path):
    out = call("workspace_list")
    assert out["ok"] is False and out["error"]["kind"] == "Usage"


def test_every_tool_names_the_remote(remote_url: str, home: Path):
    call("hive_init", url=remote_url)
    r = _create(remote_url, checkout=True)
    assert r["remote"] == remote_url
    commit(Path(r["path"]))
    ws = {"ws_id": r["id"]}
    for name, args in (
        ("workspace_get", ws),
        ("workspace_list", {}),
        ("workspace_read_file", {**ws, "path": "README.md"}),
        ("workspace_tree", ws),
        ("workspace_publish", ws),
        ("events_tail", {}),
        ("workspace_drop", ws),
        ("workspace_gc", {}),
    ):
        out = call(name, remote=remote_url, **args)
        assert out["ok"] is True and out["remote"] == remote_url, name


def test_mcp_creates_hive_and_defaults_base(remote_url: str, home: Path):
    r = call("workspace_create", remote=remote_url)
    assert r["ok"] is True
    got = call("workspace_get", remote=remote_url, ws_id=r["id"])
    assert got["base_ref"] == "refs/heads/main"


def test_mcp_remote_without_head_needs_base(remote_url: str, home: Path, tmp_path: Path):
    git("symbolic-ref", "HEAD", "refs/heads/gone", cwd=tmp_path / "remote.git")
    out = call("workspace_create", remote=remote_url)
    assert out["ok"] is False and out["error"]["kind"] == "Usage"
    assert call("workspace_create", remote=remote_url, base_ref="main")["ok"] is True


def test_mcp_from_ws_skips_base_lookup(remote_url: str, home: Path, tmp_path: Path):
    p = call("workspace_create", remote=remote_url, base_ref="main")
    git("symbolic-ref", "HEAD", "refs/heads/gone", cwd=tmp_path / "remote.git")
    c = call("workspace_create", remote=remote_url, from_ws=p["id"])
    assert c["ok"] is True


def test_mcp_create_gives_clone_command(remote_url: str, home: Path):
    r = call("workspace_create", remote=remote_url)
    assert r["branch_name"] == f"gitswarm/ws/{r['id']}"
    assert r["clone"] == f"git clone -b gitswarm/ws/{r['id']} {remote_url}"


def test_doctor_via_mcp(remote_url: str, home: Path):
    out = call("doctor", remote=remote_url)
    assert out["ok"] is True and out["remote"] == remote_url
    (home / "config.toml").write_text(
        '[remote."h"]\nadapter = "github"\nuser = "1/2"\ncredential_file = "/nonexistent"\n'
    )
    out = call("doctor", remote=remote_url)
    assert out["ok"] is False


def test_doctor_via_mcp_without_remote(home: Path):
    out = call("doctor")
    assert out["ok"] is True and "remote" not in out


def test_stats_via_mcp(remote_url: str, home: Path):
    r = _create(remote_url)
    out = call("stats", remote=remote_url)
    assert out["ok"] is True and out["remote"] == remote_url
    assert out["by_state"] == {"open": 1} and out["total_events"] == 1
    assert isinstance(out["open_oldest_age_s"], int)
    assert r["ok"] is True


def test_mcp_refuses_option_shaped_remote(tmp_path: Path, home: Path):
    pwned = tmp_path / "pwned-mcp"
    poc = f"--upload-pack=touch {pwned};"
    for name in ("doctor", "workspace_list", "stats"):
        out = call(name, remote=poc)
        assert out["ok"] is False and out["error"]["kind"] == "InvalidState", name
    assert call("hive_init", url=poc)["error"]["kind"] == "InvalidState"
    assert not pwned.exists()


def test_tool_descriptions_are_english():
    async def go():
        async with Client(mcp) as c:
            return {t.name: t.description or "" for t in await c.list_tools()}

    bad = [name for name, text in asyncio.run(go()).items() if not text or not text.isascii()]
    assert bad == []
