import asyncio
from pathlib import Path

from fastmcp import Client

from gitswarm.surfaces.mcp import mcp


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
