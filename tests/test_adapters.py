from pathlib import Path

import pytest

from gitswarm.adapters.plain import PlainAdapter
from gitswarm.adapters.remote import Capability, Scope
from gitswarm.adapters.select import adapter_for, host_of, repo_name
from gitswarm.config import Config, RemoteSpec, SinkSpec, load_config
from gitswarm.errors import Unsupported


def test_plain_adapter_declares_nothing():
    a = PlainAdapter()
    assert a.capabilities() == frozenset()
    with pytest.raises(Unsupported):
        a.issue_token("o/r", "01J", Scope.WRITE)
    with pytest.raises(Unsupported):
        a.revoke_token("1")
    assert Capability.TOKEN not in a.capabilities()


@pytest.mark.parametrize(
    ("url", "host", "name"),
    [
        ("ssh://git@git.example.com/org/repo.git", "git.example.com", "org/repo"),
        ("https://git.example.com/org/repo", "git.example.com", "org/repo"),
        ("git@git.example.com:org/repo.git", "git.example.com", "org/repo"),
        ("file:///tmp/x/remote.git", "", "x/remote"),
    ],
)
def test_host_and_repo_name(url, host, name):
    assert host_of(url) == host
    assert repo_name(url) == name


def test_load_config_missing_and_present(home: Path):
    assert load_config(home) == Config(remotes={}, sinks=[])
    (home / "config.toml").write_text(
        '[remote."git.example.com"]\n'
        'adapter = "forgejo"\napi = "https://git.example.com"\nuser = "bot"\n'
        'credential_file = "/tmp/cred"\n\n'
        '[[sink]]\nkind = "jsonl"\ntarget = "/tmp/events.jsonl"\n'
    )
    cfg = load_config(home)
    assert cfg.remotes["git.example.com"] == RemoteSpec(
        "forgejo", "https://git.example.com", "bot", "/tmp/cred"
    )
    assert cfg.sinks == [SinkSpec("jsonl", "/tmp/events.jsonl")]


def test_adapter_for_defaults_to_plain(home: Path):
    cfg = load_config(home)
    assert isinstance(adapter_for("ssh://git@h/o/r.git", cfg), PlainAdapter)
