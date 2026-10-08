# /// script
# requires-python = ">=3.11"
# dependencies = ["gitswarm"]
#
# [tool.uv.sources]
# gitswarm = { path = "..", editable = true }
# ///
"""실제 원격에 대한 생명주기 점검. CI 밖에서 사람이 돌린다(네트워크 필요).

uv run --with-editable . scripts/smoke.py <remote-url> [base-branch=main]
"""

import subprocess
import sys
import tempfile
from pathlib import Path

from gitswarm.service.workspace import Checkout, WsState, open_service
from gitswarm.store.hive import Hive


def commit_marker(wt: Path) -> None:
    """publish 는 base 그대로인 worktree 를 거절한다 — 커밋 하나를 만든다."""
    (wt / "SMOKE.md").write_text("smoke\n")
    git = ["git", "-C", str(wt), "-c", "user.name=smoke", "-c", "user.email=smoke@gitswarm"]
    subprocess.run([*git, "add", "SMOKE.md"], check=True)
    subprocess.run([*git, "commit", "-qm", "Add smoke marker"], check=True)


def main(url: str, base: str = "main") -> int:
    home = Path(tempfile.mkdtemp(prefix="gitswarm-smoke-"))
    Hive.init(url, home)
    svc = open_service(url, home)

    r = svc.create(base, {"name": "smoke"}, 300, None, Checkout.WORKTREE, {"smoke": "1"})
    print("created", r.id, r.branch)
    assert svc.get(r.id).state is WsState.OPEN
    print("tree", [e["name"] for e in svc.tree(r.id)][:5])
    commit_marker(Path(r.path))
    print("publish", svc.publish(r.id))
    assert svc.get(r.id).state is WsState.PUBLISHED
    print("events", [e.kind for e in svc.events(None)][:3])
    print("drop", svc.drop(r.id).state)
    assert svc.hive.git.ls_remote(r.branch) is None
    print("OK — home", home)
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
