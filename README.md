# gitswarm

LLM 에이전트 다수를 위한 **git 위 조율 계층**. 임의 git 원격 위에 에이전트마다 격리
workspace 를 만들고, 체크아웃 없이 읽고, 발행하고, 거둔다. 상태는 전부 원격 git 에
있다(`refs/heads/gitswarm/*`) — 데몬·DB 없음.

## 설치

    uv tool install gitswarm        # 또는 uvx gitswarm --help

## 30초

    gitswarm hive init ssh://git@host/org/repo.git
    export GITSWARM_REMOTE=ssh://git@host/org/repo.git
    gitswarm ws create --base main --agent impl --checkout
    #  → {"ok": true, "id": "01J…", "branch": "refs/heads/gitswarm/ws/01J…", "path": "…/wt/01J…", …}
    gitswarm ws read 01J… README.md
    gitswarm ws publish 01J…
    gitswarm ws drop 01J…
    gitswarm events tail

`--help` 를 뺀 모든 명령의 출력은 JSON 한 줄. 오류는 `{"ok": false, "error": {"kind", "detail"}}` + 종료코드
(NotFound 2 · Conflict 3 · InvalidState 4 · Unsupported 5 · RemoteError 6).
사용법 오류는 `{"ok": false, "error": {"kind": "Usage", …}}` + 종료코드 1.

`ws list` 는 `{"workspaces": […], "invalid": [{"id", "detail"}]}`, `ws gc` 는
`{"expired": [id…], "invalid": [{"id", "detail"}], "conflicted": [id…]}` 를 낸다.
`invalid` 는 읽을 수 없는 meta 레코드, `conflicted` 는 이 호스트가 본 뒤 남이 push 해
gc 가 건너뛴 workspace 다 — 둘 다 보고만 하며, `ws drop <id>` 로 거둔다.

## Claude Code 에 MCP 로 붙이기

    claude mcp add gitswarm -- uvx gitswarm mcp

도구: `hive_init` · `workspace_create/get/list/read_file/tree/publish/drop/gc` · `events_tail`.
MCP 도구는 원격을 호출마다 `remote` 인자로 받는다(`GITSWARM_REMOTE` 는 CLI 전용).

## 다른 호스트의 에이전트

worktree 없이 브랜치만 받은 에이전트는 평범한 git 으로 일한다:

    git clone -b gitswarm/ws/01J… ssh://git@host/org/repo.git
    … commit …
    git push origin HEAD

## 설정 `~/.gitswarm/config.toml`

    [remote."git.example.com"]
    adapter = "forgejo"                 # 레포 한정 토큰 발급
    api = "https://git.example.com"
    user = "bot"
    credential_file = "~/.config/gitswarm/forgejo.cred"

    [[sink]]
    kind = "jsonl"                      # 또는 webhook
    target = "~/.gitswarm/events.jsonl"

**자격 범위**: `credential_file` 의 비밀은 `user` 계정 전체의 비밀이다(Forgejo 토큰
엔드포인트는 계정 인증만 받는다). 따라서 `user` 는 토큰 발급만을 위한 전용 봇
계정이어야 하고, 대상 org 의 멤버이며, 대상 레포에만 쓰기 권한을 가진다.

## 상태 배치

    refs/heads/gitswarm/meta        상태(ws/<id>.json) + 이벤트 로그(커밋 이력)
    refs/heads/gitswarm/ws/<id>     workspace 브랜치

쓰기는 `push --force-with-lease` 하나로 CAS. 설계: `docs/superpowers/specs/`.

`ws publish` 의 lease 기준은 로컬 `refs/gitswarm/lease/…` 다 — worktree 에서 `git fetch`
해도 옮겨지지 않는다. 남이 먼저 push 했으면 Conflict 이고, `git pull --rebase origin
gitswarm/ws/<id>` 뒤 다시 publish 하면 된다.

## 권한 경계

gitswarm A 는 workspace 를 만든 에이전트에 묶지 않는다. `refs/heads/gitswarm/*` 에
push 할 수 있는 누구나 어떤 workspace 든 발행하거나 거둘 수 있다 — 경계는 원격의
push ACL 이다(호스트별 레포 한정 토큰으로 좁힌다). 에이전트별 소유권은 C(Landing)의
몫이다. 원격 meta 브랜치에서 읽은 모든 값(id 문자 집합, 브랜치 재계산,
created_at/ttl_s, token_id, since oid)은 검증하며, 어긋나면 InvalidState/NotFound 로
fail-closed 한다. `ws drop` 은 그 workspace 브랜치의 발행 안 된 원격 커밋(남이 push 한
것 포함)까지 지우는 파괴적 동작이고, `ws gc` 는 이 호스트가 본 적 없는 커밋을 절대
지우지 않는다.

## 개발

```
uv sync --dev
uv run ruff check src scripts tests
uv run vulture src --min-confidence 60 --ignore-names "hive_init,ws_*,workspace_*,events_tail,mcp_serve,main"
uv run pytest -q --cov
```

죽은 코드 0 · 커버리지 100%(`fail_under = 100`)가 CI 게이트다. vulture 허용 이름은 typer·fastmcp
데코레이터가 등록하는 진입점과 console script `main` 뿐이다.

## 로드맵

A Workspace(이 판) → B Intent(커밋 ↔ 지시·근거 기록) → C Landing(동시 결과 기계 병합).

MIT · KeiaiLab
