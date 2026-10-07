import shlex
import stat
import subprocess
from pathlib import Path

import pytest

from gitswarm.constants import peek_ref, tracking_ref
from gitswarm.driver.git import Git
from gitswarm.errors import RemoteError
from tests.conftest import count_network, git


@pytest.fixture
def repo(tmp_path: Path, remote_url: str) -> Git:
    g = Git.init_bare(tmp_path / "hive.git")
    g.set_origin(remote_url)
    return g


def test_fetch_main_and_missing_ref(repo: Git):
    oid = repo.fetch("refs/heads/main")
    assert oid and len(oid) == 40
    assert repo.rev_parse(tracking_ref("refs/heads/main")) == oid
    assert repo.fetch("refs/heads/nope") is None


def test_ls_remote(repo: Git):
    assert repo.ls_remote("refs/heads/main") == repo.fetch("refs/heads/main")
    assert repo.ls_remote("refs/heads/nope") is None


def test_blob_tree_commit_roundtrip(repo: Git):
    a = repo.hash_object(b"A")
    b = repo.hash_object(b"B")
    tree = repo.build_tree({"ws/x.json": a, "ws/y.json": b, "top.txt": a})
    assert repo.ls_tree_recursive(tree) == {"ws/x.json": a, "ws/y.json": b, "top.txt": a}
    names = sorted(e.name for e in repo.ls_tree(tree))
    assert names == ["top.txt", "ws"]
    c1 = repo.commit_tree(tree, [], "ws.created x")
    c2 = repo.commit_tree(tree, [c1], "ws.created y")
    assert repo.cat_file(f"{c2}:ws/x.json") == b"A"
    log = repo.log(c2, since=None)
    assert [e.subject for e in log] == ["ws.created y", "ws.created x"]
    assert [e.oid for e in repo.log(c2, since=c1)] == [c2]


def test_push_lease_create_then_reject(repo: Git):
    base = repo.fetch("refs/heads/main")
    assert repo.push(base, "refs/heads/gitswarm/meta", expected=None) is True
    # 두 번째 "없어야 한다" lease 는 거절된다
    assert repo.push(base, "refs/heads/gitswarm/meta", expected=None) is False
    # 맞는 expected 로는 된다
    assert repo.push(base, "refs/heads/gitswarm/meta", expected=base) is True
    assert repo.rev_parse(tracking_ref("refs/heads/gitswarm/meta")) == base


def test_push_lease_different_oid_with_wrong_expected(repo: Git):
    # Remote at A, push B with expected=<wrong oid> → False (git's lease rejection)
    tree = repo.build_tree({})
    oid_a = repo.commit_tree(tree, [], "commit_a")
    # Push oid_a to create the ref
    assert repo.push(oid_a, "refs/heads/gitswarm/test", expected=None) is True
    # Create a different oid
    oid_b = repo.commit_tree(tree, [], "commit_b")
    # Try to push oid_b with wrong expected (not A, not B) → False
    wrong_oid = repo.fetch("refs/heads/main")
    assert repo.push(oid_b, "refs/heads/gitswarm/test", expected=wrong_oid) is False
    # Verify remote still has oid_a (not updated to oid_b)
    assert repo.ls_remote("refs/heads/gitswarm/test") == oid_a


def test_push_lease_new_oid_with_none_expected(repo: Git):
    # (b) remote at A, push B with expected=None → False (stale info)
    tree = repo.build_tree({})
    new_oid = repo.commit_tree(tree, [], "new")
    # Push new_oid to create the ref
    assert repo.push(new_oid, "refs/heads/gitswarm/test_b", expected=None) is True
    # Try to push a different oid with expected=None → False (stale info)
    newer_oid = repo.commit_tree(tree, [], "newer")
    assert repo.push(newer_oid, "refs/heads/gitswarm/test_b", expected=None) is False


@pytest.mark.parametrize("lease", ["none", "wrong", "held"])
def test_push_same_oid_honours_lease(repo: Git, monkeypatch: pytest.MonkeyPatch, lease: str):
    """원격이 이미 oid 를 들고 있으면 git 은 lease 를 보지 않고 "Everything up-to-date" 로 rc 0.

    그 구멍은 결과 해석으로 닫는다 — 사전 ls-remote 없이 push 한 번.
    """
    ref = "refs/heads/gitswarm/test_c"
    oid = repo.commit_tree(repo.build_tree({}), [], "commit")
    assert repo.push(oid, ref, expected=None) is True
    expected = {"none": None, "wrong": repo.fetch("refs/heads/main"), "held": oid}[lease]

    calls = count_network(monkeypatch)
    assert repo.push(oid, ref, expected=expected) is (lease == "held")
    assert calls == ["push"]
    assert repo.ls_remote(ref) == oid


@pytest.mark.parametrize("op", ["fetch", "peek"])
@pytest.mark.parametrize("present", [True, False])
def test_fetch_and_peek_are_one_round_trip(
    repo: Git, monkeypatch: pytest.MonkeyPatch, op: str, present: bool
):
    """없는 ref 는 fetch 의 "couldn't find remote ref" 로 판정한다 — 사전 ls-remote 없음."""
    ref = "refs/heads/main" if present else "refs/heads/nope"
    local = {"fetch": tracking_ref, "peek": peek_ref}[op](ref)
    stale = repo.commit_tree(repo.build_tree({}), [], "stale")
    repo.update_ref(local, stale)

    calls = count_network(monkeypatch)
    oid = getattr(repo, op)(ref)
    assert calls == ["fetch"]
    if present:
        assert oid == repo.rev_parse(local) != stale
        return
    assert oid is None
    assert repo.rev_parse(local) is None


def test_delete_remote_is_idempotent(repo: Git):
    base = repo.fetch("refs/heads/main")
    repo.push(base, "refs/heads/gitswarm/ws/a", expected=None)
    repo.delete_remote("refs/heads/gitswarm/ws/a", expected=None)
    repo.delete_remote("refs/heads/gitswarm/ws/a", expected=None)
    assert repo.ls_remote("refs/heads/gitswarm/ws/a") is None


def test_worktree_add_remove(repo: Git, tmp_path: Path):
    base = repo.fetch("refs/heads/main")
    repo.update_ref("refs/heads/gitswarm/ws/w", base)
    wt = tmp_path / "wt"
    repo.worktree_add(wt, "gitswarm/ws/w")
    assert (wt / "README.md").read_text() == "seed\n"
    repo.worktree_remove(wt)
    assert not wt.exists()
    repo.delete_ref("refs/heads/gitswarm/ws/w")
    assert repo.exists("refs/heads/gitswarm/ws/w") is False


def test_git_failure_is_remote_error(tmp_path: Path):
    g = Git.init_bare(tmp_path / "x.git")
    g.set_origin((tmp_path / "missing.git").as_uri())
    with pytest.raises(RemoteError):
        g.fetch("refs/heads/main")


def test_push_pre_receive_hook_decline_raises_error(tmp_path: Path):
    """Pre-receive hook decline is NOT a lease rejection; must raise RemoteError."""
    # Create a bare remote with a pre-receive hook that always declines
    remote_path = tmp_path / "remote_with_hook.git"
    Git.init_bare(remote_path)
    hook_path = remote_path / "hooks" / "pre-receive"
    hook_path.write_text("#!/bin/sh\nexit 1\n")
    hook_path.chmod(0o755)

    # Create a working repo pointing to it
    g = Git.init_bare(tmp_path / "work.git")
    g.set_origin(remote_path.as_uri())

    # Try to push; hook declines → RemoteError (not a lease rejection)
    tree = g.build_tree({})
    oid = g.commit_tree(tree, [], "test")
    with pytest.raises(RemoteError):
        g.push(oid, "refs/heads/test", expected=None)


def test_delete_remote_hook_decline_raises_error(tmp_path: Path):
    """훅이 삭제를 거절하면 lease 거절도 '이미 없음'도 아니다 — RemoteError."""
    remote_path = tmp_path / "remote_with_hook.git"
    Git.init_bare(remote_path)
    g = Git.init_bare(tmp_path / "work.git")
    g.set_origin(remote_path.as_uri())
    oid = g.commit_tree(g.build_tree({}), [], "test")
    assert g.push(oid, "refs/heads/test", expected=None)

    # 브랜치가 생긴 뒤에 거절 훅을 건다
    hook_path = remote_path / "hooks" / "pre-receive"
    hook_path.write_text("#!/bin/sh\nexit 1\n")
    hook_path.chmod(0o755)

    with pytest.raises(RemoteError):
        g.delete_remote("refs/heads/test", oid)
    assert g.ls_remote("refs/heads/test") == oid


def test_push_lease_stale_after_remote_moved_returns_false(tmp_path: Path):
    """Sequential stale info: remote moves between fetch and push → return False."""
    remote_path = tmp_path / "remote_race.git"
    Git.init_bare(remote_path)
    g = Git.init_bare(tmp_path / "work.git")
    g.set_origin(remote_path.as_uri())

    # 빈 원격 — 기준 커밋을 만들어 ref 를 연다
    base = g.commit_tree(g.build_tree({}), [], "base")
    assert g.push(base, "refs/heads/race-test", expected=None) is True

    # Simulate a race: another process updates the remote
    # We do this by directly updating the remote's ref (simulating a competing push)
    remote_git = Git(remote_path)
    tree = remote_git.build_tree({})
    competing_oid = remote_git.commit_tree(tree, [], "competing")
    remote_git.update_ref("refs/heads/race-test", competing_oid)

    # Now try to push with old expected value → lease fails with stale info
    tree = g.build_tree({})
    new_oid = g.commit_tree(tree, [], "our change")
    result = g.push(new_oid, "refs/heads/race-test", expected=base)
    # Should return False (lease rejection, not error)
    assert result is False


@pytest.mark.parametrize(
    "stderr,expected",
    [
        # Stale info rejection (sequential)
        (
            " ! [rejected]        e938593f -> x (stale info)\n"
            "error: failed to push some refs to '../remote.git'",
            True,
        ),
        # Incorrect old value rejection (concurrent race, measured output)
        (
            "remote: error: cannot lock ref 'refs/heads/x': "
            "is at e2e4c333 but expected 77358abf\n"
            " ! [remote rejected] 862aa4e2 -> x (incorrect old value provided)\n"
            "error: failed to push some refs to '../remote.git'",
            True,
        ),
        # Server-side race on create
        (
            "remote: error: cannot lock ref 'refs/heads/y': reference already exists\n"
            " ! [remote rejected] 862aa4e2 -> y (reference already exists)\n"
            "error: failed to push some refs to '../remote.git'",
            True,
        ),
        # Ref lock contention with a sibling writer
        (
            "remote: error: cannot lock ref 'refs/heads/gitswarm/meta': unable to create "
            "directory for '/tmp/r/remote.git/refs/heads/gitswarm/meta.lock'\n"
            " ! [remote rejected] 1a2b3c4d -> gitswarm/meta (failed to update ref)\n"
            "error: failed to push some refs to '../remote.git'",
            True,
        ),
        # Pre-receive hook decline (NOT a lease rejection)
        (
            "remote: error: hook declined\n"
            " ! [remote rejected] 862aa4e2 -> x (pre-receive hook declined)\n"
            "error: failed to push some refs to '../remote.git'",
            False,
        ),
    ],
)
def test_is_lease_rejection_classification(stderr: str, expected: bool) -> None:
    """Pure function: classify lease rejections vs. hook declines."""
    from gitswarm.driver.git import is_lease_rejection

    assert is_lease_rejection(stderr) is expected


def test_peek_does_not_move_tracking_ref(repo: Git, tmp_path: Path, remote_url: str):
    ref = "refs/heads/main"
    known = repo.fetch(ref)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", remote_url, str(other)], check=True)
    (other / "n.txt").write_text("n\n")
    git("add", "n.txt", cwd=other)
    git("commit", "-q", "-m", "n", cwd=other)
    git("push", "-q", "origin", "HEAD:main", cwd=other)
    moved = git("rev-parse", "HEAD", cwd=other)

    assert repo.peek(ref) == moved
    assert moved != known
    assert repo.rev_parse(tracking_ref(ref)) == known
    assert repo.peek("refs/heads/nope") is None


def _foreign_commit(repo: Git, ref: str, msg: str) -> str:
    """원격 ref 를 다른 쓰기 주체가 옮긴 상황(로컬 tracking 은 그대로)."""
    other = Git.init_bare(repo.repo.parent / f"other-{msg}.git")
    other.set_origin(repo.origin_url())
    oid = other.commit_tree(other.build_tree({}), [], msg)
    assert other.push(oid, ref, expected=repo.ls_remote(ref))
    return oid


def test_delete_remote_lease_matches(repo: Git):
    ref = "refs/heads/gitswarm/ws/l"
    base = repo.fetch("refs/heads/main")
    repo.push(base, ref, expected=None)
    assert repo.delete_remote(ref, expected=base) is True
    assert repo.ls_remote(ref) is None
    assert repo.rev_parse(tracking_ref(ref)) is None


def test_delete_remote_lease_rejected_keeps_branch(repo: Git):
    ref = "refs/heads/gitswarm/ws/m"
    base = repo.fetch("refs/heads/main")
    repo.push(base, ref, expected=None)
    moved = _foreign_commit(repo, ref, "moved")
    assert repo.delete_remote(ref, expected=base) is False
    assert repo.ls_remote(ref) == moved


def test_delete_remote_lease_on_missing_is_idempotent(repo: Git):
    base = repo.fetch("refs/heads/main")
    assert repo.delete_remote("refs/heads/gitswarm/ws/gone", expected=base) is True
    assert repo.delete_remote("refs/heads/gitswarm/ws/gone", expected=None) is True


LOCAL_LOCK_ERR = (
    "error: cannot lock ref 'refs/remotes/origin/gitswarm/meta': unable to create "
    "directory for '/h/repo.git/refs/remotes/origin/gitswarm/meta.lock'"
)


def _flaky_fetch(monkeypatch: pytest.MonkeyPatch, failures: int, err: str) -> list[int]:
    """`git fetch` 가 처음 failures 번 err 로 실패한다. 반환 = 시도 횟수 칸."""
    real = Git._run
    calls = [0]

    def run(self, *args, **kw):
        if args[0] == "fetch":
            calls[0] += 1
            if calls[0] <= failures:
                raise RemoteError(err)
        return real(self, *args, **kw)

    monkeypatch.setattr(Git, "_run", run)
    monkeypatch.setattr("gitswarm.driver.git.time.sleep", lambda s: None)
    return calls


@pytest.mark.parametrize("op", ["fetch", "peek"])
def test_local_ref_lock_race_is_retried(repo: Git, monkeypatch: pytest.MonkeyPatch, op: str):
    calls = _flaky_fetch(monkeypatch, 2, LOCAL_LOCK_ERR)
    oid = getattr(repo, op)("refs/heads/main")
    assert oid and len(oid) == 40
    assert calls[0] == 3


def test_local_ref_lock_race_gives_up(repo: Git, monkeypatch: pytest.MonkeyPatch):
    from gitswarm.constants import FETCH_LOCK_RETRIES

    calls = _flaky_fetch(monkeypatch, 99, LOCAL_LOCK_ERR)
    with pytest.raises(RemoteError):
        repo.fetch("refs/heads/main")
    assert calls[0] == FETCH_LOCK_RETRIES


def test_other_fetch_errors_are_not_retried(repo: Git, monkeypatch: pytest.MonkeyPatch):
    calls = _flaky_fetch(monkeypatch, 1, "fatal: repository not found")
    with pytest.raises(RemoteError):
        repo.fetch("refs/heads/main")
    assert calls[0] == 1


def test_is_ancestor(repo: Git):
    base = repo.fetch("refs/heads/main")
    child = repo.commit_tree(repo.build_tree({}), [base], "child")
    assert repo.is_ancestor(base, child) is True
    assert repo.is_ancestor(child, base) is False


def _captured_env(monkeypatch: pytest.MonkeyPatch, repo: Git, *args: str) -> dict:
    """repo._run(*args) 가 git 에 넘기는 env. `git config` 조회와 git 밖 명령은 진짜로 돈다."""
    real = subprocess.run
    seen: dict = {}

    def run(cmd, **kw):
        if cmd[0] != "git" or cmd[3] == "config":
            return real(cmd, **kw)
        seen.update(kw["env"])
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr("gitswarm.driver.git.subprocess.run", run)
    repo._run(*args)
    return seen


def test_network_calls_multiplex_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """ssh 가 실제로 읽는 ControlPath 가 소켓 경로 그대로다(공백 포함). ssh -G 는 네트워크를 안 탄다."""
    from gitswarm.driver.git import SSH_CONTROL_DIR, SSH_CONTROL_PERSIST_S, SSH_CONTROL_SOCKET

    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    monkeypatch.setattr("gitswarm.driver.git.SOCKET_PATH_MAX", 4096)  # tmp_path 는 길다
    repo = Git.init_bare(tmp_path / "a b.git")

    cmd = _captured_env(monkeypatch, repo, "fetch")["GIT_SSH_COMMAND"]
    control = repo.repo / SSH_CONTROL_DIR
    opts = shlex.split(cmd)
    assert opts[0] == "ssh"
    cfg = subprocess.run(
        [*opts, "-G", "example.com"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    assert f"controlpath {control / SSH_CONTROL_SOCKET}" in cfg
    assert "controlmaster auto" in cfg
    assert f"controlpersist {SSH_CONTROL_PERSIST_S}" in cfg
    assert stat.S_IMODE(control.stat().st_mode) == 0o700


def test_double_quote_in_path_skips_multiplexing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    monkeypatch.setattr("gitswarm.driver.git.SOCKET_PATH_MAX", 4096)
    repo = Git.init_bare(tmp_path / 'q"x.git')
    assert "GIT_SSH_COMMAND" not in _captured_env(monkeypatch, repo, "fetch")


def test_core_ssh_command_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    monkeypatch.setattr("gitswarm.driver.git.SOCKET_PATH_MAX", 4096)
    repo = Git.init_bare(tmp_path / "r.git")
    git("config", "core.sshCommand", "ssh -i /k", cwd=repo.repo)
    assert "GIT_SSH_COMMAND" not in _captured_env(monkeypatch, repo, "push")


def test_git_messages_are_untranslated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LC_ALL", "ko_KR.UTF-8")
    repo = Git(tmp_path)
    assert _captured_env(monkeypatch, repo, "rev-parse")["LC_ALL"] == "C"


def test_up_to_date_echoed_by_hook_is_not_the_marker(repo: Git, monkeypatch: pytest.MonkeyPatch):
    """서버 훅이 그 문구를 찍어도 실제 갱신은 성공이다 — 문구는 한 줄 전체로만 맞춘다."""
    err = b"remote: Everything up-to-date\nTo /r.git\n   1111111..2222222  2222222 -> x\n"

    def run(self, *args, **kw):
        return subprocess.CompletedProcess(args, 0, b"", err)

    monkeypatch.setattr(Git, "_run", run)
    assert repo.push("2" * 40, "refs/heads/x", expected="1" * 40) is True


def test_local_calls_do_not_touch_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from gitswarm.driver.git import SSH_CONTROL_DIR

    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.setattr("gitswarm.driver.git.SOCKET_PATH_MAX", 4096)
    repo = Git(tmp_path / "w")
    repo.repo.mkdir()
    assert "GIT_SSH_COMMAND" not in _captured_env(monkeypatch, repo, "rev-parse")
    assert not (repo.repo / SSH_CONTROL_DIR).exists()


@pytest.mark.parametrize("var", ["GIT_SSH_COMMAND", "GIT_SSH"])
def test_caller_ssh_setting_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, var: str):
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.setenv(var, "/opt/counting-ssh")
    monkeypatch.setattr("gitswarm.driver.git.SOCKET_PATH_MAX", 4096)
    repo = Git(tmp_path / "r.git")
    repo.repo.mkdir()
    env = _captured_env(monkeypatch, repo, "push")
    assert env[var] == "/opt/counting-ssh"
    assert "ControlMaster" not in env.get("GIT_SSH_COMMAND", "")


def test_long_hive_path_skips_multiplexing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """제어 소켓 경로가 sun_path 한도를 넘으면 ssh 가 죽는다 — 다중화 없이 간다."""
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    repo = Git(tmp_path / ("d" * 120))
    repo.repo.mkdir()
    assert "GIT_SSH_COMMAND" not in _captured_env(monkeypatch, repo, "ls-remote")


def test_default_branch_reads_remote_head(repo: Git, remote_url: str, tmp_path: Path):
    assert repo.default_branch() == "main"
    # 레포 밖에서 URL 로도 읽는다(doctor 는 hive 를 만들지 않는다)
    assert Git(tmp_path).default_branch(remote_url) == "main"


def test_default_branch_none_without_symref(repo: Git, tmp_path: Path):
    git("symbolic-ref", "HEAD", "refs/heads/gone", cwd=tmp_path / "remote.git")
    assert repo.default_branch() is None


def test_default_branch_unreachable_is_remote_error(tmp_path: Path):
    with pytest.raises(RemoteError):
        Git(tmp_path).default_branch((tmp_path / "nope.git").as_uri())


def test_option_shaped_url_is_never_an_option(tmp_path: Path):
    """검증을 건너뛰어도 `--` 뒤의 값은 옵션이 아니다 — upload-pack 이 돌지 않는다."""
    pwned = tmp_path / "pwned-driver"
    poc = f"--upload-pack=touch {pwned};"
    with pytest.raises(RemoteError):
        Git(tmp_path).default_branch(poc)
    bare = Git.init_bare(tmp_path / "h.git")
    bare.set_origin(poc)
    assert bare.origin_url() == poc
    for op in (lambda: bare.ls_remote("HEAD"), lambda: bare.fetch("refs/heads/main")):
        with pytest.raises(RemoteError):
            op()
    assert not pwned.exists()


def _record_runs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], object]]:
    seen: list[tuple[list[str], object]] = []

    def run(cmd, **kw):
        seen.append((cmd, kw.get("timeout")))
        if "ls-remote" in cmd:
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr("gitswarm.driver.git.subprocess.run", run)
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh")  # 다중화 판정의 git config 호출을 건너뛴다
    return seen


def test_only_probes_have_a_wall_clock_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """큰 base 의 첫 fetch 는 60 초를 넘을 수 있다 — 전송은 멈춤만 끊고, ls-remote 만 벽시계로."""
    from gitswarm.driver.git import GIT_PROBE_TIMEOUT_S

    seen = _record_runs(monkeypatch)
    with pytest.raises(RemoteError, match="git ls-remote timed out"):
        Git(tmp_path).ls_remote("HEAD")
    for args in (
        ("fetch", "-q", "--", "origin", "x"),
        ("push", "--", "origin", "x"),
        ("rev-parse",),
    ):
        Git(tmp_path)._run(*args)
    assert [t for _, t in seen] == [GIT_PROBE_TIMEOUT_S, None, None, None]


def test_transfers_bound_http_stalls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from gitswarm.driver.git import HTTP_LOW_SPEED_LIMIT, HTTP_LOW_SPEED_TIME_S

    seen = _record_runs(monkeypatch)
    Git(tmp_path)._run("fetch", "-q")
    Git(tmp_path)._run("rev-parse")
    stall = [
        "-c",
        f"http.lowSpeedLimit={HTTP_LOW_SPEED_LIMIT}",
        "-c",
        f"http.lowSpeedTime={HTTP_LOW_SPEED_TIME_S}",
    ]
    fetch_cmd, local_cmd = seen[0][0], seen[1][0]
    assert fetch_cmd[3 : 3 + len(stall)] == stall and fetch_cmd[3 + len(stall)] == "fetch"
    assert "-c" not in local_cmd


def test_ssh_command_keeps_connections_alive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from gitswarm.driver.git import (
        SSH_ALIVE_COUNT_MAX,
        SSH_ALIVE_INTERVAL_S,
        SSH_CONNECT_TIMEOUT_S,
    )

    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    monkeypatch.setattr("gitswarm.driver.git.SOCKET_PATH_MAX", 4096)
    repo = Git.init_bare(tmp_path / "r.git")
    opts = shlex.split(_captured_env(monkeypatch, repo, "fetch")["GIT_SSH_COMMAND"])
    for opt in (
        f"ServerAliveInterval={SSH_ALIVE_INTERVAL_S}",
        f"ServerAliveCountMax={SSH_ALIVE_COUNT_MAX}",
        f"ConnectTimeout={SSH_CONNECT_TIMEOUT_S}",
    ):
        assert opt in opts


def _argv_of(monkeypatch: pytest.MonkeyPatch, repo: Git, op) -> list[tuple[str, ...]]:
    """op 가 _run 에 넘긴 인자들. 원격은 진짜로 탄다."""
    real = Git._run
    seen: list[tuple[str, ...]] = []

    def run(self, *args, **kw):
        seen.append(args)
        return real(self, *args, **kw)

    monkeypatch.setattr(Git, "_run", run)
    op()
    return [a for a in seen if a[0] in ("fetch", "push", "ls-remote")]


@pytest.mark.parametrize(
    "op",
    [
        lambda g: g.peek("refs/heads/main"),
        lambda g: g.push(g.fetch("refs/heads/main"), "refs/heads/dd", expected=None),
        lambda g: g.delete_remote("refs/heads/nope", None),
    ],
    ids=["peek", "push", "delete"],
)
def test_url_and_refs_follow_double_dash(repo: Git, monkeypatch: pytest.MonkeyPatch, op):
    """원격·ref 자리 앞에는 항상 `--` — 옵션 모양 값이 와도 옵션이 아니다."""
    calls = _argv_of(monkeypatch, repo, lambda: op(repo))
    assert calls
    for args in calls:
        dd = args.index("--")
        assert all(not a.startswith("-") for a in args[dd + 1 :]), args
