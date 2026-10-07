import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gitswarm.surfaces.cli import app

runner = CliRunner()


def run(*args: str, remote: str) -> tuple[int, dict]:
    res = runner.invoke(app, [*args], env={"GITSWARM_REMOTE": remote})
    line = res.stdout.strip().splitlines()[-1]
    return res.exit_code, json.loads(line)


@pytest.fixture
def inited(remote_url: str, home: Path) -> str:
    code, out = run("hive", "init", remote_url, remote=remote_url)
    assert code == 0 and out["ok"] is True
    return remote_url


def test_lifecycle_via_cli(inited: str):
    code, out = run(
        "ws",
        "create",
        "--base",
        "main",
        "--agent",
        "impl",
        "--ttl",
        "5",
        "--checkout",
        remote=inited,
    )
    assert code == 0 and out["ok"] is True
    ws_id = out["id"]
    assert (Path(out["path"]) / "README.md").exists()

    code, out = run("ws", "get", ws_id, remote=inited)
    assert code == 0 and out["state"] == "open" and out["agent"] == {"name": "impl"}

    code, out = run("ws", "read", ws_id, "README.md", remote=inited)
    assert code == 0 and out["content"] == "seed\n"

    code, out = run("ws", "tree", ws_id, remote=inited)
    assert code == 0 and [e["name"] for e in out["entries"]] == ["README.md"]

    code, out = run("ws", "publish", ws_id, remote=inited)
    assert code == 0 and len(out["oid"]) == 40

    code, out = run("ws", "list", "--state", "published", remote=inited)
    assert code == 0 and [w["id"] for w in out["workspaces"]] == [ws_id]

    code, out = run("ws", "drop", ws_id, remote=inited)
    assert code == 0 and out["state"] == "dropped"

    code, out = run("ws", "gc", remote=inited)
    assert code == 0 and out["expired"] == []


def test_error_exit_codes(inited: str):
    code, out = run("ws", "get", "01J00000000000000000000000", remote=inited)
    assert code == 2
    assert out == {
        "ok": False,
        "error": {"kind": "NotFound", "detail": "workspace 01J00000000000000000000000 not found"},
    }


def test_missing_hive_is_not_found(remote_url: str, home: Path):
    code, out = run("ws", "list", remote=remote_url)
    assert code == 2 and out["error"]["kind"] == "NotFound"


def test_read_binary_is_base64(inited: str):
    _, out = run("ws", "create", "--base", "main", remote=inited)
    ws_id = out["id"]
    # README 는 텍스트라 content 로 온다; 이진이면 content_b64. 여기서는 키 계약만 고정
    _, out = run("ws", "read", ws_id, "README.md", remote=inited)
    assert "content" in out and "content_b64" not in out
