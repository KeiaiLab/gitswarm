# gitswarm

[English](https://github.com/KeiaiLab/gitswarm/blob/stable/README.md)

정본 레포: git.keiailab.com/keiailab-oss/gitswarm · 미러: github.com/KeiaiLab/gitswarm (이슈: github.com/KeiaiLab/gitswarm/issues)

gitswarm 은 여러 LLM 에이전트가 같은 레포를 동시에 다룰 때 쓰는, 임의 git 원격 위의 조율
계층이다. 에이전트 실행마다 격리된 workspace 브랜치를 주고, 체크아웃 없이 서로의 작업을
읽고, 발행하고, 거둔다. 상태는 전부 원격(`refs/heads/gitswarm/*`)에 있다 — 서버·데몬·DB
없음. CLI 명령 하나 = MCP 도구 하나, 인자·결과가 같다.

## 설치

```sh
uv tool install gitswarm      # 또는: uvx gitswarm --help
```

PyPI 발행 전이다. 그때까지는 git 에서 설치한다:

```sh
uvx --from git+https://github.com/KeiaiLab/gitswarm gitswarm --help
```

Claude Code(자세히: [docs/recipes/claude-code.md](https://github.com/KeiaiLab/gitswarm/blob/stable/docs/recipes/claude-code.md)):

```sh
claude mcp add gitswarm -- uvx gitswarm mcp
# PyPI 전까지:
claude mcp add gitswarm -- uvx --from git+https://github.com/KeiaiLab/gitswarm gitswarm mcp
```

플랫폼: POSIX(Linux, macOS), git >= 2.39, Python >= 3.11.

## 60초

클론 안에서 실행한다. gitswarm 이 원격(`origin`)을 찾고, 처음 쓸 때 로컬 미러를 만들고,
원격 HEAD 에서 분기한다. 아래 출력은 기본 브랜치가 `main` 인 `file://` 원격에서 실제로 낸
것이다.

```console
$ cd repo
$ gitswarm ws create --agent impl --checkout
{"ok": true, "id": "01M4B3NX30CF0XQFN7FGKTRSBC", "branch": "refs/heads/gitswarm/ws/01M4B3NX30CF0XQFN7FGKTRSBC", "base_oid": "bc876f294bbcf32c441f7e13bfef37700cc1c785", "path": "/tmp/demo/home/hives/c63648f6fa869444/wt/01M4B3NX30CF0XQFN7FGKTRSBC", "token": null, "clone": "git clone -b gitswarm/ws/01M4B3NX30CF0XQFN7FGKTRSBC file:///tmp/demo/remote.git", "branch_name": "gitswarm/ws/01M4B3NX30CF0XQFN7FGKTRSBC", "remote": "file:///tmp/demo/remote.git"}
```

`path` 에서 평범한 git 으로 일하고 발행한다. worktree 안에서는 hive 로 원격을 찾으므로
플래그가 필요 없다.

```console
$ cd /tmp/demo/home/hives/c63648f6fa869444/wt/01M4B3NX30CF0XQFN7FGKTRSBC
$ git add src/calc.py && git commit -qm "Add calc"
$ gitswarm ws publish 01M4B3NX30CF0XQFN7FGKTRSBC
{"ok": true, "id": "01M4B3NX30CF0XQFN7FGKTRSBC", "oid": "0212f996898c32b842cdc3068580f9ac019391a0", "remote": "file:///tmp/demo/remote.git"}
```

누구나 체크아웃 없이 읽는다:

```console
$ gitswarm ws tree 01M4B3NX30CF0XQFN7FGKTRSBC src
{"ok": true, "path": "src", "entries": [{"name": "app.py", "kind": "blob", "oid": "b80e3222ab264bd7cafb376749bd18814fd66776"}, {"name": "calc.py", "kind": "blob", "oid": "4693ad3cf8b0903b98497fb89b8b524fbf1b93f4"}], "remote": "file:///tmp/demo/remote.git"}
$ gitswarm ws read 01M4B3NX30CF0XQFN7FGKTRSBC src/calc.py
{"ok": true, "path": "src/calc.py", "content": "def add(a, b):\n    return a + b\n", "remote": "file:///tmp/demo/remote.git"}
```

끝나면 거둔다. 원격 브랜치·worktree·토큰을 지운다. 멱등이다.

```console
$ gitswarm ws drop 01M4B3NX30CF0XQFN7FGKTRSBC
{"ok": true, "id": "01M4B3NX30CF0XQFN7FGKTRSBC", "state": "dropped", "base_ref": "refs/heads/main", "base_oid": "bc876f294bbcf32c441f7e13bfef37700cc1c785", "branch": "refs/heads/gitswarm/ws/01M4B3NX30CF0XQFN7FGKTRSBC", "agent": {"name": "impl"}, "parent": null, "created_at": "2026-10-07T11:58:44Z", "ttl_s": 7200, "labels": {}, "token_id": null, "published_oid": "0212f996898c32b842cdc3068580f9ac019391a0", "remote": "file:///tmp/demo/remote.git"}
```

원격은 `--remote` → `$GITSWARM_REMOTE` → cwd(hive worktree 안이면 그 hive, git 레포
안이면 `origin`) 순으로 정한다. 성공 출력의 `remote` 가 실제로 쓴 원격이다. 셋 다 없으면:

```console
$ cd /tmp && gitswarm ws list
{"ok": false, "error": {"kind": "Usage", "detail": "no remote: pass --remote (MCP: remote), set $GITSWARM_REMOTE, or run inside a git repo with an origin"}}
```

`--base` 기본값은 원격 HEAD 가 가리키는 브랜치다(여기서는 `main`, 호스트에 따라 `stable`).
주면 기준을 고정하고 왕복 1회를 아낀다.

## 다른 호스트의 에이전트

다른 머신의 에이전트는 git 만 있으면 된다. create 결과의 `clone` 필드를 그대로 실행하고,
커밋하고, push 한 뒤 `ws publish` 로 결과를 기록한다. 평범한 `git push` 는 workspace 상태를
바꾸지 않는다 — `ws publish` 가 바꾼다.

```console
$ gitswarm ws create --agent reviewer
{"ok": true, "id": "01M4B3P6V3D549KPH4SC5BTJ74", "branch": "refs/heads/gitswarm/ws/01M4B3P6V3D549KPH4SC5BTJ74", "base_oid": "bc876f294bbcf32c441f7e13bfef37700cc1c785", "path": null, "token": null, "clone": "git clone -b gitswarm/ws/01M4B3P6V3D549KPH4SC5BTJ74 file:///tmp/demo/remote.git", "branch_name": "gitswarm/ws/01M4B3P6V3D549KPH4SC5BTJ74", "remote": "file:///tmp/demo/remote.git"}

# 다른 호스트에서
$ git clone -b gitswarm/ws/01M4B3P6V3D549KPH4SC5BTJ74 file:///tmp/demo/remote.git work
$ cd work && git add REVIEW.md && git commit -qm "Add review"
$ git push origin HEAD
To file:///tmp/demo/remote.git
   bc876f2..87d3743  HEAD -> gitswarm/ws/01M4B3P6V3D549KPH4SC5BTJ74
$ gitswarm ws publish 01M4B3P6V3D549KPH4SC5BTJ74
{"ok": true, "id": "01M4B3P6V3D549KPH4SC5BTJ74", "oid": "87d3743c1103ca653c4afe8db4d7a9e841649344", "remote": "file:///tmp/demo/remote.git"}
```

`git clone -b` 에는 `branch`(`refs/heads/…`)가 아니라 `branch_name`(`gitswarm/ws/<id>`)을
쓴다. 아무 커밋도 push 하지 않고 발행하면 거절한다:

```console
$ gitswarm ws publish 01M4B3PT92MMV3BST3Q0JDNBR6
{"ok": false, "error": {"kind": "InvalidState", "detail": "nothing published: branch is still at base; commit and push to the branch first"}}
```

실행을 조율하는 쪽이 `ws drop` 으로 거둔다. `ws gc` 는 `ttl_s` 가 지났고 이 호스트가 마지막으로
본 뒤 브랜치가 움직이지 않은 open workspace 만 거둔다. published workspace 는 건드리지 않고,
다른 호스트가 push 한 만료 workspace 는 거두지 않고 `conflicted` 로 보고한다. 둘 다 명시적
`ws drop <id>` 가 필요하다.
자세히: [docs/recipes/other-host.md](https://github.com/KeiaiLab/gitswarm/blob/stable/docs/recipes/other-host.md).

## publish 가 Conflict 를 내면

worktree 에서의 `ws publish` 는 lease 로 push 한다 — 원격 브랜치가 gitswarm 이 마지막으로 본
자리에 그대로 있을 때만 덮어쓴다. 그 사이 남이 push 했으면 아무것도 덮어쓰지 않고
`Conflict`(종료코드 3)다:

```console
$ gitswarm ws publish 01M4B3PF534F5ZZJDWM0YWK6TM
{"ok": false, "error": {"kind": "Conflict", "detail": "refs/heads/gitswarm/ws/01M4B3PF534F5ZZJDWM0YWK6TM moved on remote; run `git pull --rebase origin gitswarm/ws/01M4B3PF534F5ZZJDWM0YWK6TM` in the worktree"}}
```

그들의 커밋 위로 rebase 하고 다시 발행한다. HEAD 가 원격 tip 을 품었으니 잃을 것이 없음을
gitswarm 이 확인하고 push 한다:

```console
$ git pull --rebase origin gitswarm/ws/01M4B3PF534F5ZZJDWM0YWK6TM
$ gitswarm ws publish 01M4B3PF534F5ZZJDWM0YWK6TM
{"ok": true, "id": "01M4B3PF534F5ZZJDWM0YWK6TM", "oid": "efb41f844494ac27c759c925cad13257e1c037f7", "remote": "file:///tmp/demo/remote.git"}
$ gitswarm ws read 01M4B3PF534F5ZZJDWM0YWK6TM NOTES.md
{"ok": true, "path": "NOTES.md", "content": "notes\n", "remote": "file:///tmp/demo/remote.git"}
```

worktree 안의 `git fetch` 는 lease 기준(`refs/gitswarm/lease/…`)을 옮기지 않는다 —
gitswarm 자신의 push 만 옮긴다. rebase 없이 다시 발행하면 같은 `Conflict` 다.

## 명령과 MCP 도구

`--help` 를 뺀 모든 명령은 JSON 한 줄을 낸다. MCP 도구는 같은 객체를 돌려준다. `remote` 는
어디서나 선택이다 — MCP 는 서버의 cwd 로 찾는다.

| CLI | MCP 도구 | 반환 |
|---|---|---|
| `hive init <url>` | `hive_init` | `url, path` |
| `ws create [--base] [--agent] [--run] [--ttl] [--from-ws] [--checkout] [--label k=v]` | `workspace_create` | `id, branch, branch_name, base_oid, path, token, clone, remote` |
| `ws get <id>` | `workspace_get` | 레코드: `id, state, base_ref, base_oid, branch, agent, parent, created_at, ttl_s, labels, token_id, published_oid, remote` |
| `ws list [--state]` | `workspace_list` | `workspaces, invalid, remote` |
| `ws read <id> <path>` | `workspace_read_file` | `path, content`(UTF-8) 또는 `content_b64`, `remote` |
| `ws tree <id> [path]` | `workspace_tree` | `path, entries [{name, kind, oid}], remote` |
| `ws publish <id>` | `workspace_publish` | `id, oid, remote` |
| `ws drop <id>` | `workspace_drop` | 레코드, `state: dropped` |
| `ws gc` | `workspace_gc` | `expired, invalid, conflicted, remote` |
| `events tail [--since <oid>]` | `events_tail` | `events [{kind, id, oid, at, payload}], remote` |
| `doctor` | `doctor` | `ok, checks [{name, ok, detail}], remote` |
| `stats` | `stats` | `by_kind, by_state, open_oldest_age_s, total_events, invalid, unrevoked_tokens, remote` |
| `mcp` | — | stdio MCP 서버 |

- `invalid` 는 읽을 수 없는 레코드, `conflicted` 는 이 호스트가 마지막으로 본 뒤 남이 push 한
  만료 workspace 다. `gc` 는 둘 다 건너뛴다 — `ws drop <id>` 로 거둔다.
- `token` 은 `create` 가 한 번만 돌려주고 저장하지 않는다(`token_id` 만 저장). 토큰 어댑터가
  없으면 `null`.
- 이벤트 종류: `ws.created`, `ws.published`, `ws.dropped`, `ws.expired`, `ws.revoked`.

```console
$ gitswarm ws gc
{"ok": true, "expired": ["01M4B3PT92MMV3BST3Q0JDNBR6"], "invalid": [], "conflicted": [], "remote": "file:///tmp/demo/remote.git"}
$ gitswarm events tail --since 1ab2a0e68ea5680f66af0bc5c6e5662b81f7c49b
{"ok": true, "events": [{"kind": "ws.expired", "id": "01M4B3PT92MMV3BST3Q0JDNBR6", "oid": "d75a472ed8e037e0d4d1daf677112a1cb4873c13", "at": "2026-10-07T21:00:23+09:00", "payload": {"agent": {"name": "idle"}, "base_oid": "bc876f294bbcf32c441f7e13bfef37700cc1c785", "base_ref": "refs/heads/main", "branch": "refs/heads/gitswarm/ws/01M4B3PT92MMV3BST3Q0JDNBR6", "created_at": "2026-10-07T11:59:14Z", "id": "01M4B3PT92MMV3BST3Q0JDNBR6", "labels": {"task": "demo"}, "parent": null, "published_oid": null, "state": "dropped", "token_id": null, "ttl_s": 60}}], "remote": "file:///tmp/demo/remote.git"}
$ gitswarm stats
{"ok": true, "by_kind": {"ws.expired": 1, "ws.dropped": 4, "ws.published": 6, "ws.created": 7}, "by_state": {"dropped": 5, "published": 2}, "open_oldest_age_s": null, "total_events": 18, "invalid": 0, "unrevoked_tokens": 0, "remote": "file:///tmp/demo/remote.git"}
$ gitswarm doctor
{"ok": true, "checks": [{"name": "git", "ok": true, "detail": "git 2.55.0"}, {"name": "home", "ok": true, "detail": "/tmp/demo/home"}, {"name": "config", "ok": true, "detail": "0 remote(s), 0 sink(s)"}, {"name": "remote", "ok": true, "detail": "reachable; default branch main"}, {"name": "hive", "ok": true, "detail": "/tmp/demo/home/hives/c63648f6fa869444: 1 worktree(s)"}, {"name": "tokens", "ok": true, "detail": "every recorded token revoked"}, {"name": "ssh_mux", "ok": true, "detail": "not an ssh remote"}], "remote": "file:///tmp/demo/remote.git"}
```

SSH 원격이면 `doctor` 가 연결 다중화도 본다:

```console
$ gitswarm doctor --remote ssh://git@git.keiailab.com/keiailab-oss/gitswarm.git
{"ok": true, "checks": [{"name": "git", "ok": true, "detail": "git 2.55.0"}, {"name": "home", "ok": true, "detail": "/tmp/demo/homeR"}, {"name": "config", "ok": true, "detail": "0 remote(s), 0 sink(s)"}, {"name": "remote", "ok": true, "detail": "reachable; default branch stable"}, {"name": "hive", "ok": true, "detail": "absent; the first ws command creates it"}, {"name": "tokens", "ok": true, "detail": "no hive; nothing recorded"}, {"name": "ssh_mux", "ok": true, "detail": "connections are multiplexed"}], "remote": "ssh://git@git.keiailab.com/keiailab-oss/gitswarm.git"}
```

### 종료코드

실패는 `{"ok": false, "error": {"kind", "detail"}}`. 다음 행동이 있으면 `detail` 끝에 적는다.

| 종류 | 뜻 | 종료코드 |
|---|---|---|
| `Usage` | 잘못된 인자, 원격을 못 찾음(표면 전용) | 1 |
| `NotFound` | id·ref·경로 없음, 잘못된 id | 2 |
| `Conflict` | 남이 브랜치·meta 를 옮김, CAS 재시도 소진 | 3 |
| `InvalidState` | 허용되지 않는 전이, 망가진 레코드, 거절된 URL | 4 |
| `Unsupported` | 어댑터에 그 기능 없음 | 5 |
| `RemoteError` | git·호스팅 API 실패 | 6 |

`doctor` 는 검사가 하나라도 실패하면 `{"ok": false, "checks": […]}` 를 내고 종료코드 1이다.
거절된 `--remote` 는 그래도 `InvalidState`(종료코드 4)다.

```console
$ gitswarm doctor --remote file:///tmp/demo/missing.git
{"ok": false, "checks": [{"name": "git", "ok": true, "detail": "git 2.55.0"}, {"name": "home", "ok": true, "detail": "/tmp/demo/home"}, {"name": "config", "ok": true, "detail": "0 remote(s), 0 sink(s)"}, {"name": "remote", "ok": false, "detail": "file:///tmp/demo/missing.git unreachable: fatal: '/tmp/demo/missing.git' does not appear to be a git repository\nfatal: Could not read from remote repository.\n\nPlease make sure you have the correct access rights\nand the repository exists."}, {"name": "hive", "ok": true, "detail": "absent; the first ws command creates it"}, {"name": "tokens", "ok": true, "detail": "no hive; nothing recorded"}, {"name": "ssh_mux", "ok": true, "detail": "not an ssh remote"}], "remote": "file:///tmp/demo/missing.git"}
```

## 설정

`~/.gitswarm/config.toml`(홈 전체를 `GITSWARM_HOME` 으로 옮긴다). 없으면 모든 원격이 평범한
git 이고 이벤트를 보내지 않는다.

```toml
[remote."git.example.com"]
adapter = "forgejo"                 # workspace 마다 레포 한정 토큰 발급
api = "https://git.example.com"
user = "gitswarm-bot"
credential_file = "~/.config/gitswarm/forgejo.cred"

[remote."github.com"]
adapter = "github"                  # GitHub App installation token
api = "https://api.github.com"
user = "<app_id>/<installation_id>"
credential_file = "~/.config/gitswarm/github-app.pem"

[[sink]]
kind = "jsonl"                      # 또는 "webhook" + target = "https://…"
target = "~/.gitswarm/events.jsonl"
```

어댑터는 원격의 host 로 고른다 — 선언 안 된 host 는 평범한 git. sink 는 이벤트마다 한 번,
재시도 없이 보낸다 — `events tail --since` 로 로그에서 되감는다.

**자격 범위.** Forgejo `credential_file` 은 `user` 계정 전체의 비밀이다 — Forgejo 토큰
엔드포인트는 계정 인증만 받는다. 토큰 발급만 하는 전용 봇 계정을 쓰고, 대상 org 의 멤버로,
대상 레포에만 쓰기 권한을 준다. GitHub PEM 은 App 이 설치된 모든 레포의 토큰을 발급할 수
있다 — 필요한 레포에만 설치한다. GitHub installation token 은 1시간 유효하고 id 로 폐기할 수
없다 — `drop` 이 만료 시각을 stderr 에 적는다. 준비: [forgejo-tokens.md](https://github.com/KeiaiLab/gitswarm/blob/stable/docs/recipes/forgejo-tokens.md),
[github-app.md](https://github.com/KeiaiLab/gitswarm/blob/stable/docs/recipes/github-app.md).

**URL:** http(s)/git URL 안의 자격(userinfo)은 거절한다 — credential helper 를 쓴다. ssh 로그인
이름은 괜찮다. `https://user:token@host/…`·`https://token@host/…` 는 `InvalidState`(오류에는
`https://***@host/…` 로 보인다), `ssh://git@host/…`·`git@host:…` 는 받는다.

## 권한 경계

workspace 는 만든 에이전트에 묶이지 않는다. `refs/heads/gitswarm/*` 에 push 할 수 있는 누구나
어떤 workspace 든 발행·폐기하고 어떤 레코드든 고쳐 쓸 수 있다 — 경계는 원격의 push ACL 이다.
meta 브랜치에서 읽은 값은 전부 검증하고 fail-closed 한다. `ws drop` 은 그 브랜치의 발행 안 된
커밋(남의 것 포함)까지 지우고, `ws gc` 는 이 호스트가 본 적 없는 커밋을 절대 지우지 않는다.
자세히: [SECURITY.md](https://github.com/KeiaiLab/gitswarm/blob/stable/SECURITY.md).

## CI

workspace 브랜치는 평범한 브랜치다. CI 가 `gitswarm/**` 를 무시하게 하라 — 아니면 create·
publish·meta 쓰기마다 런이 뜬다:

```yaml
on:
  push:
    branches-ignore:
      - "gitswarm/**"
```

Forgejo Actions 와 GitHub Actions 가 같은 문법이다. 자세히: [docs/recipes/ci.md](https://github.com/KeiaiLab/gitswarm/blob/stable/docs/recipes/ci.md).

## 성능

명령 하나는 원격 왕복 1~4회다. SSH 연결은 hive 별로 다중화되어, 마스터가 사는 동안(60 초)
명령은 새 핸드셰이크를 열지 않는다. Forgejo(SSH, RTT 0.2 s) 실측, 3 라운드 중앙값:

| 명령 | ssh 연결 | 새 핸드셰이크 | 초(중앙값) | 예산 |
|---|---|---|---|---|
| hive init | 1 | 1 | 3.49 | - |
| ws create --checkout | 4 | 0 | 1.62 | ≤ 4 연결, ≤ 2.5 s |
| ws get | 1 | 0 | 0.29 | ≤ 1 연결 |
| ws list | 1 | 0 | 0.71 | ≤ 1 연결 |
| ws read | 2 | 0 | 0.54 | ≤ 2 연결 |
| ws tree | 2 | 0 | 0.49 | ≤ 2 연결 |
| ws publish | 4 | 0 | 1.68 | ≤ 4 연결, ≤ 2.5 s |
| events tail | 1 | 0 | 1.30 | ≤ 1 연결 |
| ws gc | 1 | 0 | 0.64 | ≤ 1 연결 |
| ws drop | 4 | 0 | 1.71 | ≤ 4 연결 |

재현: `uv run scripts/bench.py <remote-url> --base <branch>`(예산 초과면 종료코드 1). 연결 수
예산이 본 기준이다 — 2.5 s 예산은 공유 서버에서 빠듯하다(push 가 가끔 수 초 걸린다).

## 개발

[CONTRIBUTING.md](https://github.com/KeiaiLab/gitswarm/blob/stable/CONTRIBUTING.md). CI 와 같은 게이트:

```sh
uv sync --dev
uv run ruff check src scripts tests
uv run ruff format --check src scripts tests
uv run vulture src vulture_whitelist.py --min-confidence 60
uv run pytest -q --cov          # 커버리지 100% 필수
```

설계: [docs/ARCHITECTURE.md](https://github.com/KeiaiLab/gitswarm/blob/stable/docs/ARCHITECTURE.md). 설계 스펙·계획은 레포 안에 있다.
변경 이력: [CHANGELOG.md](https://github.com/KeiaiLab/gitswarm/blob/stable/CHANGELOG.md).

## 로드맵

A Workspace(이 판) → B Intent(커밋마다 왜: 지시·근거·검사 기록) → C Landing(동시 결과 병합:
직렬 rebase·기계 중재, 에이전트별 소유권).

## 라이선스

MIT. Copyright KeiaiLab.
