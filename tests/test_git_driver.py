from pathlib import Path

import pytest

from gitswarm.constants import tracking_ref
from gitswarm.driver.git import Git
from gitswarm.errors import RemoteError


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
    repo.delete_remote("refs/heads/gitswarm/ws/a")
    repo.delete_remote("refs/heads/gitswarm/ws/a")
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
