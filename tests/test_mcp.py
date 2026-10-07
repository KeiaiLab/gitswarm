import asyncio
import base64
from pathlib import Path

from fastmcp import Client

from gitswarm.surfaces.mcp import mcp
from tests.conftest import git


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
