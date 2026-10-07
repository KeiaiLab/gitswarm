from pathlib import Path

import pytest

from gitswarm.driver.git import Git
from gitswarm.errors import RemoteError
from gitswarm.service.doctor import diagnose
from gitswarm.service.workspace import Checkout, open_service
from tests.conftest import git

NAMES = ["git", "home", "config", "remote", "hive", "tokens", "ssh_mux"]


def _check(report: dict, name: str) -> dict:
    return next(c for c in report["checks"] if c["name"] == name)


def test_healthy_setup_passes(remote_url: str, home: Path):
    report = diagnose(remote_url, home)
    assert report["ok"] is True
    assert [c["name"] for c in report["checks"]] == NAMES
    assert "main" in _check(report, "remote")["detail"]
    assert "absent" in _check(report, "hive")["detail"]
    assert _check(report, "ssh_mux")["detail"] == "not an ssh remote"


def test_hive_reports_worktree_count(remote_url: str, home: Path):
    open_service(remote_url, home).create("main", {}, 0, None, Checkout.WORKTREE, {})
    assert "1 worktree" in _check(diagnose(remote_url, home), "hive")["detail"]


def test_tokens_check(remote_url: str, home: Path):
    assert _check(diagnose(remote_url, home), "tokens") == {
        "name": "tokens",
        "ok": True,
        "detail": "no hive; nothing recorded",
    }
    open_service(remote_url, home).create("main", {}, 0, None, Checkout.NONE, {})
    assert _check(diagnose(remote_url, home), "tokens")["detail"] == "every recorded token revoked"


def test_tokens_check_reports_unreachable_meta(remote_url: str, home: Path, monkeypatch):
    open_service(remote_url, home)

    def fail(self, ref):
        raise RemoteError("meta fetch failed")

    monkeypatch.setattr(Git, "fetch", fail)
    check = _check(diagnose(remote_url, home), "tokens")
    assert check == {"name": "tokens", "ok": False, "detail": "RemoteError: meta fetch failed"}


def test_no_remote_skips_remote_checks(home: Path):
    report = diagnose(None, home)
    assert report["ok"] is True
    assert [c["name"] for c in report["checks"]] == ["git", "home", "config", "remote"]


def test_missing_credential_file_fails(remote_url: str, home: Path):
    (home / "config.toml").write_text(
        '[remote."git.example.com"]\nadapter = "forgejo"\ncredential_file = "~/nope.cred"\n'
    )
    report = diagnose(remote_url, home)
    assert report["ok"] is False
    assert "git.example.com" in _check(report, "config")["detail"]


@pytest.mark.parametrize(
    "text",
    ["not toml [", '[remote."h"]\nbogus = 1\n', '[[sink]]\nkind = "smoke"\ntarget = "x"\n'],
    ids=["syntax", "unknown-key", "sink-kind"],
)
def test_broken_config_fails(home: Path, text: str):
    (home / "config.toml").write_text(text)
    report = diagnose(None, home)
    assert report["ok"] is False and _check(report, "config")["ok"] is False


def test_plain_adapter_needs_no_credential(home: Path):
    (home / "config.toml").write_text('[remote."h"]\nadapter = "plain"\n')
    assert _check(diagnose(None, home), "config")["ok"] is True


@pytest.mark.parametrize("version", ["2.38.5", "weird"])
def test_old_or_odd_git_fails(home: Path, monkeypatch, version: str):
    monkeypatch.setattr(Git, "version", staticmethod(lambda: version))
    check = _check(diagnose(None, home), "git")
    assert check["ok"] is False and version in check["detail"]


def test_missing_git_fails(home: Path, monkeypatch):
    def gone():
        raise RemoteError("git not runnable: FileNotFoundError")

    monkeypatch.setattr(Git, "version", staticmethod(gone))
    assert _check(diagnose(None, home), "git")["ok"] is False


def test_unwritable_home_fails(tmp_path: Path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    assert _check(diagnose(None, blocker), "home")["ok"] is False


def test_unreachable_remote_fails(tmp_path: Path, home: Path):
    report = diagnose((tmp_path / "nope.git").as_uri(), home)
    assert report["ok"] is False and _check(report, "remote")["ok"] is False


def test_remote_without_head_still_passes(remote_url: str, home: Path, tmp_path: Path):
    git("symbolic-ref", "HEAD", "refs/heads/gone", cwd=tmp_path / "remote.git")
    check = _check(diagnose(remote_url, home), "remote")
    assert check["ok"] is True and "--base" in check["detail"]


@pytest.mark.parametrize("url", ["ssh://git@example.invalid/org/repo.git", "git@h:org/repo"])
def test_ssh_mux_path_limit(home: Path, monkeypatch, url: str):
    monkeypatch.setattr(Git, "default_branch", lambda self, remote="origin": "main")
    monkeypatch.delenv("GIT_SSH", raising=False)
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    # tmp_path 아래 홈은 소켓 한도를 넘는다
    check = _check(diagnose(url, home), "ssh_mux")
    assert check["ok"] is False and "too long" in check["detail"]

    monkeypatch.setattr("gitswarm.driver.git.SOCKET_PATH_MAX", 4096)
    assert _check(diagnose(url, home), "ssh_mux")["ok"] is True


def test_git_version_is_parsed_from_git():
    assert Git.version()[0].isdigit()


def test_git_version_without_git_is_remote_error(monkeypatch):
    monkeypatch.setenv("PATH", "")
    with pytest.raises(RemoteError):
        Git.version()


def test_mux_problem_names_quote(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("gitswarm.driver.git.SOCKET_PATH_MAX", 4096)
    assert "quote" in Git(tmp_path / 'q"x').mux_problem()
    assert Git(tmp_path / "ok").mux_problem() is None


def test_diagnose_refuses_option_shaped_remote(tmp_path: Path, home: Path):
    pwned = tmp_path / "pwned-diag"
    report = diagnose(f"--upload-pack=touch {pwned};", home)
    assert report["ok"] is False
    assert "invalid remote url" in _check(report, "remote")["detail"]
    assert not pwned.exists()


def test_corrupt_hive_toml_is_a_failed_check(remote_url: str, home: Path):
    from gitswarm.store.hive import Hive

    hive = Hive.init(remote_url, home)
    (hive.path / "hive.toml").write_text("url = [")
    check = _check(diagnose(remote_url, home), "hive")
    assert check["ok"] is False and "hive.toml is corrupt" in check["detail"]


def test_unexpected_exception_is_a_failed_check(home: Path, monkeypatch):
    def boom():
        raise RuntimeError("surprise")

    monkeypatch.setattr(Git, "version", staticmethod(boom))
    report = diagnose(None, home)
    assert report["ok"] is False
    assert _check(report, "git") == {"name": "git", "ok": False, "detail": "RuntimeError"}


SSH_URL = "ssh://git@example.invalid/org/repo.git"


def _no_network(monkeypatch):
    monkeypatch.setattr(Git, "default_branch", lambda self, remote="origin": "main")
    monkeypatch.setattr("gitswarm.driver.git.SOCKET_PATH_MAX", 4096)
    monkeypatch.delenv("GIT_SSH", raising=False)
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)


def test_ssh_mux_reports_caller_env(home: Path, monkeypatch):
    _no_network(monkeypatch)
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /k")
    check = _check(diagnose(SSH_URL, home), "ssh_mux")
    assert check == {
        "name": "ssh_mux",
        "ok": True,
        "detail": "disabled: caller set GIT_SSH_COMMAND",
    }


def test_ssh_mux_reports_core_ssh_command(home: Path, monkeypatch):
    _no_network(monkeypatch)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.sshCommand")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "ssh -i /k")
    check = _check(diagnose(SSH_URL, home), "ssh_mux")
    assert check["detail"] == "disabled: caller set core.sshCommand"


def test_ssh_mux_ignores_repo_config_around_home(tmp_path: Path, monkeypatch):
    """hive 가 없을 때 홈을 감싼 레포의 core.sshCommand 는 hive(bare)에 상속되지 않는다."""
    _no_network(monkeypatch)
    outer = tmp_path / "outer"
    git("init", "-q", str(outer), cwd=tmp_path)
    git("config", "core.sshCommand", "ssh -i /k", cwd=outer)
    home = outer / "gs-home"
    check = _check(diagnose(SSH_URL, home), "ssh_mux")
    assert check == {"name": "ssh_mux", "ok": True, "detail": "connections are multiplexed"}


def test_ssh_mux_reads_the_hive_repo_config(home: Path, monkeypatch):
    from gitswarm.store.hive import hive_path

    _no_network(monkeypatch)
    repo = Git.init_bare(hive_path(SSH_URL, home) / "repo.git")
    git("config", "core.sshCommand", "ssh -i /k", cwd=repo.repo)
    check = _check(diagnose(SSH_URL, home), "ssh_mux")
    assert check["detail"] == "disabled: caller set core.sshCommand"


def test_git_floor_admits_debian_bookworm(home: Path, monkeypatch):
    """CI 파드(bookworm)는 git 2.39.5 다 — 하한이 그것을 막으면 doctor 가 거짓으로 붉다."""
    from gitswarm.service.doctor import GIT_MIN_VERSION

    assert GIT_MIN_VERSION <= (2, 39, 0)
    monkeypatch.setattr(Git, "version", staticmethod(lambda: "2.39.5"))
    assert _check(diagnose(None, home), "git")["ok"] is True
