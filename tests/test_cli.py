import base64
import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gitswarm.surfaces import cli
from gitswarm.surfaces.cli import app, main
from tests.conftest import git

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
    ws_id, wt = out["id"], Path(out["path"])
    assert (wt / "README.md").exists()

    code, out = run("ws", "get", ws_id, remote=inited)
    assert code == 0 and out["state"] == "open" and out["agent"] == {"name": "impl"}

    code, out = run("ws", "read", ws_id, "README.md", remote=inited)
    assert code == 0 and out["content"] == "seed\n"

    code, out = run("ws", "tree", ws_id, remote=inited)
    assert code == 0 and [e["name"] for e in out["entries"]] == ["README.md"]

    (wt / "n.txt").write_text("n\n")
    git("add", "n.txt", cwd=wt)
    git("commit", "-q", "-m", "n", cwd=wt)
    code, out = run("ws", "publish", ws_id, remote=inited)
    assert code == 0 and len(out["oid"]) == 40

    code, out = run("ws", "list", "--state", "published", remote=inited)
    assert code == 0 and [w["id"] for w in out["workspaces"]] == [ws_id]
    assert out["invalid"] == []

    code, out = run("ws", "drop", ws_id, remote=inited)
    assert code == 0 and out["state"] == "dropped"

    code, out = run("ws", "gc", remote=inited)
    assert code == 0
    assert out == {
        "ok": True,
        "expired": [],
        "invalid": [],
        "conflicted": [],
        "remote": inited,
    }


def test_error_exit_codes(inited: str):
    code, out = run("ws", "get", "01J00000000000000000000000", remote=inited)
    assert code == 2
    assert out == {
        "ok": False,
        "error": {"kind": "NotFound", "detail": "workspace 01J00000000000000000000000 not found"},
    }


def test_missing_hive_is_created_on_demand(remote_url: str, home: Path):
    code, out = run("ws", "list", remote=remote_url)
    assert code == 0 and out["workspaces"] == []
    code, out = run("ws", "create", remote=remote_url)
    assert code == 0 and out["remote"] == remote_url


def test_unreachable_remote_is_remote_error(tmp_path: Path, home: Path):
    code, out = run("ws", "create", remote=(tmp_path / "nope.git").as_uri())
    assert code == 6 and out["error"]["kind"] == "RemoteError"
    assert list(home.glob("hives/*")) == []


def test_base_defaults_to_remote_head(inited: str):
    code, out = run("ws", "create", remote=inited)
    assert code == 0
    _, got = run("ws", "get", out["id"], remote=inited)
    assert got["base_ref"] == "refs/heads/main"


def test_remote_without_head_needs_base(inited: str, tmp_path: Path):
    git("symbolic-ref", "HEAD", "refs/heads/gone", cwd=tmp_path / "remote.git")
    code, out = run("ws", "create", remote=inited)
    assert code == 1 and out["error"]["kind"] == "Usage"
    assert "--base" in out["error"]["detail"]
    code, _ = run("ws", "create", "--base", "main", remote=inited)
    assert code == 0


BINARY = bytes([0xFF, 0xFE, 0x00, 0x80])


def test_read_binary_is_base64(inited: str):
    _, out = run("ws", "create", "--base", "main", "--checkout", remote=inited)
    ws_id, wt = out["id"], Path(out["path"])
    (wt / "blob.bin").write_bytes(BINARY)
    git("add", "blob.bin", cwd=wt)
    git("commit", "-m", "Add blob", cwd=wt)
    code, _ = run("ws", "publish", ws_id, remote=inited)
    assert code == 0

    code, out = run("ws", "read", ws_id, "blob.bin", remote=inited)
    assert code == 0 and "content" not in out
    assert base64.b64decode(out["content_b64"]) == BINARY


def main_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", ["gitswarm", *argv])
    monkeypatch.delenv("GITSWARM_REMOTE", raising=False)
    code = 0
    try:
        main()
    except SystemExit as exc:
        code = int(exc.code or 0)
    return code, capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ("ws", "list"),
        ("ws", "list", "--remote", "x", "--state", "bogus"),
        ("ws", "create", "--remote", "x", "--label", "foo"),
        ("ws", "list", "--nope"),
    ],
)
def test_usage_errors_are_one_json_line(monkeypatch, capsys, argv):
    code, stdout = main_cli(monkeypatch, capsys, *argv)
    lines = stdout.strip().splitlines()
    assert code == 1 and len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["ok"] is False and payload["error"]["kind"] == "Usage"


def test_help_exits_zero(monkeypatch, capsys):
    code, _ = main_cli(monkeypatch, capsys, "--help")
    assert code == 0


def test_main_propagates_notfound_exit_code(monkeypatch, capsys, remote_url: str, home: Path):
    argv = ("ws", "get", "01J00000000000000000000000", "--remote", remote_url)
    monkeypatch.setattr(sys, "argv", ["gitswarm", *argv])
    code = 0
    try:
        main()
    except SystemExit as exc:
        code = int(exc.code or 0)
    lines = capsys.readouterr().out.strip().splitlines()
    assert code == 2 and len(lines) == 1
    assert json.loads(lines[0])["error"]["kind"] == "NotFound"


def test_main_success_exits_zero(monkeypatch, capsys, remote_url: str, home: Path):
    monkeypatch.setattr(sys, "argv", ["gitswarm", "hive", "init", remote_url])
    try:
        main()
    except SystemExit as exc:
        assert not exc.code
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out["ok"] is True


def test_events_tail_via_cli(inited: str):
    code, out = run("ws", "create", "--base", "main", remote=inited)
    assert code == 0
    run("ws", "drop", out["id"], remote=inited)

    code, tail = run("events", "tail", remote=inited)
    assert code == 0 and tail["ok"] is True
    assert [e["kind"] for e in tail["events"]] == ["ws.dropped", "ws.created"]


def test_events_tail_bad_since_exits_2(inited: str):
    code, out = run("events", "tail", "--since", "bogus", remote=inited)
    assert code == 2 and out["error"]["kind"] == "NotFound"


def test_label_pairs_are_recorded(inited: str):
    code, out = run("ws", "create", "--label", "a=b", "--label", "c=", remote=inited)
    assert code == 0
    code, got = run("ws", "get", out["id"], remote=inited)
    assert got["labels"] == {"a": "b", "c": ""}


def test_mcp_command_serves(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr("gitswarm.surfaces.mcp.serve", lambda: calls.append(1))
    res = runner.invoke(app, ["mcp"])
    assert res.exit_code == 0 and calls == [1]


def test_main_turns_abort_into_usage_json(monkeypatch, capsys):
    def abort(*args, **kwargs):
        raise cli.typer.Abort()

    monkeypatch.setattr(cli, "app", abort)
    code, stdout = main_cli(monkeypatch, capsys, "ws", "list")
    payload = json.loads(stdout.strip())
    assert code == 1
    assert payload["error"] == {"kind": "Usage", "detail": "aborted"}


def test_tree_takes_path_as_positional_argument(inited: str):
    _, out = run("ws", "create", "--base", "main", "--checkout", remote=inited)
    ws_id, wt = out["id"], Path(out["path"])
    (wt / "dogfood").mkdir()
    (wt / "dogfood" / "a.md").write_text("a\n")
    git("add", "dogfood", cwd=wt)
    git("commit", "-q", "-m", "Add dogfood", cwd=wt)
    run("ws", "publish", ws_id, remote=inited)

    code, out = run("ws", "tree", ws_id, "dogfood", remote=inited)
    assert code == 0 and out["path"] == "dogfood"
    assert [e["name"] for e in out["entries"]] == ["a.md"]


def run_here(*args: str) -> tuple[int, dict]:
    """--remote·env 없이 — cwd 로 원격을 찾게 한다."""
    res = runner.invoke(app, [*args], env={"GITSWARM_REMOTE": None})
    return res.exit_code, json.loads(res.stdout.strip().splitlines()[-1])


def test_every_success_line_names_the_remote(inited: str):
    _, out = run("ws", "create", "--base", "main", "--checkout", remote=inited)
    ws_id, wt = out["id"], Path(out["path"])
    assert out["remote"] == inited
    git("commit", "-q", "--allow-empty", "-m", "e", cwd=wt)
    for argv in (
        ("ws", "get", ws_id),
        ("ws", "list"),
        ("ws", "read", ws_id, "README.md"),
        ("ws", "tree", ws_id),
        ("ws", "publish", ws_id),
        ("events", "tail"),
        ("ws", "drop", ws_id),
        ("ws", "gc"),
    ):
        code, out = run(*argv, remote=inited)
        assert code == 0 and out["remote"] == inited, argv


def test_remote_discovered_in_clone(inited: str, tmp_path: Path, monkeypatch):
    clone = tmp_path / "clone"
    git("clone", "-q", inited, str(clone), cwd=tmp_path)
    monkeypatch.chdir(clone)
    code, out = run_here("ws", "create", "--base", "main")
    assert code == 0 and out["remote"] == inited


def test_remote_discovered_in_worktree(inited: str, monkeypatch):
    _, out = run("ws", "create", "--base", "main", "--checkout", remote=inited)
    monkeypatch.chdir(out["path"])
    code, got = run_here("ws", "get", out["id"])
    assert code == 0 and got["id"] == out["id"] and got["remote"] == inited


def test_no_remote_outside_repo_is_usage(monkeypatch, capsys, home: Path):
    code, stdout = main_cli(monkeypatch, capsys, "ws", "list")
    payload = json.loads(stdout.strip())
    assert code == 1 and payload["error"]["kind"] == "Usage"
    assert "--remote" in payload["error"]["detail"]
