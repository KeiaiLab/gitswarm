import subprocess
from pathlib import Path

import pytest

from gitswarm.constants import tracking_ref
from gitswarm.driver.git import Git
from gitswarm.errors import RemoteError
from tests.conftest import git


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


def test_push_lease_same_oid_with_wrong_expected(repo: Git):
    # (c) remote at A, push A with expected=<wrong oid> → False
    tree = repo.build_tree({})
    oid = repo.commit_tree(tree, [], "commit")
    # Push oid to create the ref
    assert repo.push(oid, "refs/heads/gitswarm/test_c", expected=None) is True
    # Try to push the same oid with wrong expected (pre-check should catch it) → False
    wrong_oid = repo.fetch("refs/heads/main")
    assert repo.push(oid, "refs/heads/gitswarm/test_c", expected=wrong_oid) is False


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


def test_push_lease_stale_after_remote_moved_returns_false(tmp_path: Path):
    """Sequential stale info: remote moves between fetch and push → return False."""
    remote_path = tmp_path / "remote_race.git"
    Git.init_bare(remote_path)
    g = Git.init_bare(tmp_path / "work.git")
    g.set_origin(remote_path.as_uri())

    # Get base oid
    base = g.fetch("refs/heads/main")
    # Push to create the ref
    g.push(base, "refs/heads/race-test", expected=None)

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
