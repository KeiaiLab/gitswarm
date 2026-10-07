# /// script
# requires-python = ">=3.11"
# dependencies = ["gitswarm"]
#
# [tool.uv.sources]
# gitswarm = { path = "..", editable = true }
# ///
"""실제 원격에 대한 왕복 측정. 명령마다 ssh 호출 수·새 SSH 핸드셰이크 수·지연을 찍는다.

uv run scripts/bench.py <remote-url> [--base stable] [--rounds 3]

PATH 앞에 `ssh` 대리자를 둔다 — gitswarm 이 거는 `ssh -o ControlMaster…` 가 그것을 부른다.
대리자는 호출마다 한 줄을 남기고, ControlPath 소켓이 이미 있으면 다중화(핸드셰이크 0)로 센다.
한 사이클(create→drop)을 rounds 번 돌려 명령별 중앙값 지연·최대 호출 수로 판정한다 — 공유
서버의 push 지연은 가끔 수 초씩 튄다. 예산을 넘는 명령이 있으면 rc 1.

    bench ──▶ gitswarm CLI ──▶ git ──▶ ssh(대리자: 기록) ──▶ 진짜 ssh
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

# 명령별 예산: (ssh 호출 수, 초). None = 지연 예산 없음
BUDGETS: dict[str, tuple[int, float | None]] = {
    "ws create --checkout": (4, 2.5),
    "ws get": (1, None),
    "ws list": (1, None),
    "ws read": (2, None),
    "ws tree": (2, None),
    "ws publish": (4, 2.5),
    "events tail": (1, None),
    "ws gc": (1, None),
    "ws drop": (4, None),
}
AGENT = "bench"
DEFAULT_ROUNDS = 3
TTL_S = 600
CONTROL_PATH_OPT = "ControlPath="
LOG_NEW = "new"
LOG_MUX = "mux"

WRAPPER = """#!{python}
import os, stat, sys
path = next((a.split("=", 1)[1].strip('"') for a in sys.argv if a.startswith("{opt}")), None)
try:
    mux = path is not None and stat.S_ISSOCK(os.stat(path).st_mode)
except OSError:
    mux = False
with open({log!r}, "a") as f:
    f.write("{mux}\\n" if mux else "{new}\\n")
os.execv({ssh!r}, [{ssh!r}, *sys.argv[1:]])
"""


@dataclass(frozen=True)
class Row:
    name: str
    calls: int
    handshakes: int
    seconds: float


def _write_wrapper(bin_dir: Path, log: Path) -> None:
    """PATH 의 첫 `ssh` = 호출을 세는 대리자."""
    real = shutil.which("ssh")
    if real is None:
        sys.exit("ssh not found on PATH")
    src = WRAPPER.format(
        python=sys.executable,
        opt=CONTROL_PATH_OPT,
        log=str(log),
        ssh=real,
        mux=LOG_MUX,
        new=LOG_NEW,
    )
    wrapper = bin_dir / "ssh"
    wrapper.write_text(src)
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)


def _cli() -> list[str]:
    exe = Path(sys.executable).parent / "gitswarm"
    return [str(exe)] if exe.exists() else [sys.executable, "-m", "gitswarm.surfaces.cli"]


def _run(name: str, args: list[str], env: dict, log: Path, rows: list[Row]) -> dict:
    """한 명령을 재고 마지막 JSON 줄을 돌려준다. 실패하면 멈춘다."""
    log.write_text("")
    start = time.monotonic()
    p = subprocess.run([*_cli(), *args], env=env, capture_output=True, text=True)
    seconds = time.monotonic() - start
    lines = log.read_text().split()
    rows.append(Row(name, len(lines), lines.count(LOG_NEW), seconds))
    if p.returncode != 0:
        sys.exit(f"{name} failed rc={p.returncode}: {p.stdout}{p.stderr}")
    out = p.stdout.strip().splitlines()
    return json.loads(out[-1]) if out else {}


def _commit(worktree: str) -> None:
    """publish 가 밀 커밋 하나(측정 밖)."""
    Path(worktree, "BENCH.txt").write_text("bench\n")
    for args in (["add", "BENCH.txt"], ["commit", "-q", "-m", "bench"]):
        subprocess.run(["git", "-C", worktree, *args], check=True, capture_output=True)


def _summary(rows: list[Row]) -> list[Row]:
    """명령별로 묶는다: 호출·핸드셰이크는 최대, 지연은 중앙값."""
    names = list(dict.fromkeys(r.name for r in rows))
    out = []
    for name in names:
        same = [r for r in rows if r.name == name]
        out.append(
            Row(
                name,
                max(r.calls for r in same),
                max(r.handshakes for r in same),
                statistics.median(r.seconds for r in same),
            )
        )
    return out


def _over(row: Row) -> bool:
    budget = BUDGETS.get(row.name)
    if budget is None:
        return False
    calls, seconds = budget
    return row.calls > calls or (seconds is not None and row.seconds > seconds)


def _table(rows: list[Row]) -> str:
    head = (
        "| command | ssh connections | new handshakes | seconds (median) | budget |\n"
        "|---|---|---|---|---|"
    )
    lines = [head]
    for r in rows:
        budget = BUDGETS.get(r.name)
        mark = "-" if budget is None else ("OVER" if _over(r) else "ok")
        lines.append(f"| {r.name} | {r.calls} | {r.handshakes} | {r.seconds:.2f} | {mark} |")
    return "\n".join(lines)


def _cycle(base: str, env: dict, log: Path, rows: list[Row]) -> None:
    """에이전트 한 사이클. 만든 workspace 는 실패해도 drop 한다."""
    created = _run(
        "ws create --checkout",
        ["ws", "create", "--base", base, "--agent", AGENT, "--ttl", str(TTL_S), "--checkout"],
        env,
        log,
        rows,
    )
    ws_id = created["id"]
    try:
        _run("ws get", ["ws", "get", ws_id], env, log, rows)
        _run("ws list", ["ws", "list"], env, log, rows)
        _run("ws read", ["ws", "read", ws_id, "README.md"], env, log, rows)
        _run("ws tree", ["ws", "tree", ws_id], env, log, rows)
        _commit(created["path"])
        _run("ws publish", ["ws", "publish", ws_id], env, log, rows)
        _run("events tail", ["events", "tail"], env, log, rows)
        _run("ws gc", ["ws", "gc"], env, log, rows)
    except BaseException:
        # 원래 실패를 지킨다 — 정리용 drop 의 실패는 stderr 로만
        try:
            _run("ws drop", ["ws", "drop", ws_id], env, log, rows)
        except SystemExit as e:
            print(e, file=sys.stderr)
        raise
    _run("ws drop", ["ws", "drop", ws_id], env, log, rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("remote")
    ap.add_argument("--base", default="stable")
    ap.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    a = ap.parse_args()

    # 짧은 경로 — ssh 제어 소켓 경로 한도(104 바이트) 안에 든다
    tmp = Path(tempfile.mkdtemp(prefix="gsb-", dir="/tmp"))
    try:
        return _bench(a.remote, a.base, a.rounds, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _bench(remote: str, base: str, rounds: int, tmp: Path) -> int:
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    log = tmp / "ssh.log"
    _write_wrapper(bin_dir, log)
    env = {k: v for k, v in os.environ.items() if k not in ("GIT_SSH_COMMAND", "GIT_SSH")}
    env |= {
        "GITSWARM_HOME": str(tmp / "home"),
        "GITSWARM_REMOTE": remote,
        "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
    }

    rows: list[Row] = []
    _run("hive init", ["hive", "init", remote], env, log, rows)
    for _ in range(rounds):
        _cycle(base, env, log, rows)

    summary = _summary(rows)
    print(_table(summary))
    return 1 if any(_over(r) for r in summary) else 0


if __name__ == "__main__":
    sys.exit(main())
