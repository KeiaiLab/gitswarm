# gitswarm — 설계 (서브프로젝트 A: Workspace)

2026-10-06. 사용자 결정으로 확정된 내용만 담는다.

## 0. 무엇인가

LLM 에이전트 다수가 같은 코드베이스를 동시에 다루기 위한 **git 위 조율 계층**.
참조: Cloudflare Artifacts(https://blog.cloudflare.com/next-git-platform-on-cloudflare/)가
저장 계층을 제공하고 그 위의 "coordination layer" 를 공모한 자리와 같다. gitswarm 은
저장소를 새로 짓지 않고 **임의 git 원격** 위에 얹힌다.

| 결정 | 값 |
|---|---|
| 층 | git 위 조율 계층(저장 서버·새 VCS 아님) |
| 저장 백엔드 | 임의 git 원격 + 선택 어댑터(첫 어댑터 Forgejo) |
| 표면 | MCP 서버 + CLI, 한 코어 공유 |
| 첫 소비자 | 일반 OSS 사용자(keiailab 특수성 배제) |
| 조율 상태 | git-native(refs·notes·트레일러), 서비스 무상태 |
| 이름 | `gitswarm`(PyPI `gitswarm`·`git-swarm` 2026-10-06 비어 있음) |
| 스택 | Python ≥3.11 · uv · hatchling · typer · fastmcp · subprocess git(pygit2 없음) |
| 라이선스 | MIT(keiai-sans 와 같음) |

세 서브프로젝트, 순서 **A → B → C**. 각각 스펙·계획·구현을 따로 돈다. 이 문서는 A.

```
                 ┌──────────────────────────────┐
  agent ──MCP──▶ │ (A) Workspace  생명주기        │ ─▶ 임의 git 원격
  agent ──CLI──▶ │     create / fork / token / drop│    (Forgejo·GitHub 어댑터 선택)
                 ├──────────────────────────────┤
                 │ (B) Intent     원장            │ 커밋 ↔ 지시·근거·도구 호출·검사 결과
                 ├──────────────────────────────┤
                 │ (C) Landing    큐              │ 동시 결과를 직렬 rebase + 기계 중재
                 └──────────────────────────────┘
```

- A: 에이전트 1실행 = 격리 작업 공간 1개. B·C 의 토대.
- B: 커밋마다 "왜"를 1급 기록으로(`refs/notes/gitswarm/intent`). A 끝난 뒤 별도 스펙.
- C: 겹치지 않는 변경 자동, 겹치면 기계 중재(rebase 재시도 → 파일 3-way → LLM 중재).
  게이트는 기계 검사만. B 끝난 뒤 별도 스펙.

## 1. 레포·층 구조

```
gitswarm/
  src/gitswarm/
    surfaces/   cli.py(typer) · mcp.py(fastmcp, stdio)   — service 만 부른다
    service/    workspace.py · (B) intent.py · (C) landing.py
    store/      meta.py   — git-native 상태 읽기/쓰기(CAS)
    driver/     git.py    — subprocess plumbing 캡슐화
    adapters/   remote.py(프로토콜) · plain.py · forgejo.py
  tests/  docs/  .forgejo/workflows/build.yml
```

규칙:
- 층은 바로 아래 이웃만 부른다. `git` 서브프로세스는 `driver/git.py` 에서만 띄운다.
- CLI 명령 하나 = MCP 도구 하나(`gitswarm ws create` ↔ `workspace_create`). 인자·반환 동일.
- 형제 레포 keiai-sans 관례를 따른다: hatchling · ruff `>=0.15,<0.16` · pytest · pyrefly ·
  `.forgejo/workflows/build.yml`.

## 2. git-native 상태 배치

```
<remote>
  refs/heads/gitswarm/meta        고아 브랜치. 트리 = ws/<id>.json …  상태 변경 1건 = 커밋 1개
  refs/heads/gitswarm/ws/<id>     workspace 브랜치(base 에서 분기)
  refs/notes/gitswarm/intent      (B) 가 쓴다
<host>
  ~/.gitswarm/hives/<sha256(remote)[:16]>/repo.git   bare 미러
  ~/.gitswarm/hives/<…>/wt/<id>                       worktree(--checkout 때만)
  ~/.gitswarm/config.toml                             어댑터·sink 선언. GITSWARM_HOME 으로 이동 가능
```

- `refs/heads/` 아래에 두는 이유: 모든 호스팅이 push 를 받는 유일한 네임스페이스.
- 접두 `gitswarm/` 은 상수 `REF_PREFIX` 하나. meta 브랜치 커밋 이력이 곧 이벤트 로그(§6).
- hive = 원격 하나에 대한 bare 미러. 원격 URL 정규화(끝 `.git`·`/` 제거, scheme 소문자) 뒤
  해시.

## 3. Workspace 모델과 동작

```json
{ "id": "01J9…(ULID, 26자)", "state": "open|published|dropped",
  "base_ref": "refs/heads/main", "base_oid": "<40hex>",
  "branch": "refs/heads/gitswarm/ws/01J9…",
  "agent": {"name": "implementer", "run": "…"}, "parent": null,
  "created_at": "2026-10-06T00:00:00Z", "ttl_s": 7200, "labels": {} }
```

상태 전이: `open → published → dropped`, `open → dropped`. 같은 상태로의 재전이(`published`
에서 다시 publish)는 허용. 그 밖은 `InvalidState`.

| 동작 | 하는 일 | 멱등 |
|---|---|---|
| `hive init <url>` | bare 미러 생성·fetch | 예 |
| `ws create --base <ref> [--agent] [--ttl] [--from-ws <id>] [--checkout]` | base oid 고정 → 브랜치 생성(from-ws 면 그 브랜치 tip 에서, parent 기록) → 원격 push → meta 기록. `--checkout` 이면 worktree 경로 반환 | 아니오(id 새로) |
| `ws get <id>` · `ws list [--state]` | meta 조회. list 는 `{workspaces, invalid:[{id, detail}]}` — 깨진 레코드는 건너뛰고 보고한다 | 예 |
| `ws read <id> <path>` · `ws tree <id> [path]` | 체크아웃 없이 블롭·트리 읽기 | 예 |
| `ws publish <id>` | worktree 의 커밋을 원격 브랜치로 push(lease 기준 = §4.1 전용 ref), state=published | 예 |
| `ws drop <id>` | 원격·로컬 브랜치, worktree 삭제, state=dropped, 토큰 revoke | 예 |
| `ws gc` | `created_at + ttl_s < now` 인 open 을 drop. `{expired, invalid}` 반환 | 예 |

- `ttl_s` 기본 상수 `DEFAULT_TTL_S = 7200`. `0` 은 무기한. create 가 `0 ≤ ttl_s ≤ MAX_TTL_S(1년)` 를 검증한다.
- 원격 meta 에서 읽은 값은 전부 검증하고, 깨진 레코드는 `InvalidState`(get) 또는 `invalid` 보고(list·gc)다 — 절대 traceback 이 아니다.
- 격리 두 방식: 같은 호스트 = worktree 경로, 다른 호스트 = 브랜치 clone. 같은 `id` 로 다룬다.
- `ws create` 반환: `{id, branch, base_oid, path|null, token|null}`. 토큰은 어댑터가 TOKEN
  capability 를 가질 때만 발급하고, 없으면 `null`(오류 아님).

## 4. 동시성 — CAS 로만 쓴다

meta 쓰기 = **읽고-검증하고-쓰는 변환**(`Change(path, transform, subject)`,
`transform: bytes | None → bytes`). CAS 는 원격 push 의 `--force-with-lease` 하나다. 로컬
`refs/heads/gitswarm/meta` 는 두지 않는다 — 로컬 bare 에는 `refs/remotes/origin/gitswarm/meta`
(원격 캐시)만 있고, 새 커밋은 oid 로 직접 push 한다.

```
loop ≤ META_CAS_RETRIES(8):
  old  = fetch origin meta → refs/remotes/origin/gitswarm/meta 의 oid (없으면 빈 값)
  prev = old 에서 path 의 현재 내용(없으면 None)
  new_content = transform(prev)        ← 여기서 상태 전이를 다시 검증한다(§3 전이표)
  tree = old.tree + (path, new_content)
  new  = commit-tree(tree, parent=old, msg="<종류> <id>")
  git push origin new:refs/heads/gitswarm/meta --force-with-lease=refs/heads/gitswarm/meta:old
                                                      ← 거절이면 지터 지수 백오프 뒤 continue
  return
raise Conflict
```

- **재적용은 덮어쓰기가 아니다.** 거절 뒤 재시도는 새 tip 의 레코드를 다시 읽어 전이를 다시
  판정한다. 다른 호스트가 그 사이 `dropped` 로 바꿨으면 `publish` 는 `InvalidState` 로 끝난다.
- 백오프: `CAS_BACKOFF_BASE_S = 0.05`, `CAS_BACKOFF_MAX_S = 1.0`, 지터 ×[0.5, 1.5]. 동시
  작성자 N 이 상한보다 많아도 결정적으로 실패하지 않는다.
- `create` 는 브랜치 push 뒤의 어떤 실패(meta `Conflict` 포함)에도 **보상**한다 — 만든 원격
  브랜치·로컬 ref·토큰을 거두고 원래 예외를 올린다. 레코드 없는 고아 브랜치는 남지 않는다.
- **삭제도 lease 로.** `drop`·`gc` 의 원격 브랜치 삭제는 마지막으로 본 oid 를 기대값으로 건다
  (`--force-with-lease=<ref>:<seen> :<ref>`). 본 적 없는 커밋은 지우지 않는다(`Conflict`).
- 잠금 파일·데몬 없음. 같은 호스트 형제 프로세스가 로컬 tracking ref 디렉터리를 두고 다투는
  `cannot lock ref` 는 fetch 쪽에서 유한 재시도한다.

### 4.1 publish 의 lease 기준 — 전용 ref

`publish` 의 기대값은 `refs/gitswarm/lease/<id>` 다. 이 ref 는 **gitswarm 자신의 성공한
push(create·publish)만** 갱신한다 — worktree 안의 `git fetch` 는 `refs/remotes/origin/*` 만
움직이므로 기준을 흔들지 못한다. 거절되면 원격 oid 를 peek 해서 그것이 HEAD 의 조상이면(에이전트가
`git pull --rebase` 를 했다는 뜻, 손실 없음이 증명됨) 그 oid 를 기대값으로 한 번 더 민다.
아니면 `Conflict`.

### 4.2 읽기는 기준을 건드리지 않는다

`read`·`tree`·`--from-ws` 의 원격 조회는 `refs/gitswarm/peek/<short>` 로 가져온다. 어떤 읽기도
lease 기준(§4.1)이나 tracking ref 를 움직이지 않는다.

## 5. 원격 어댑터와 토큰

```python
class RemoteAdapter(Protocol):
    def capabilities(self) -> set[Capability]            # TOKEN · EVENTS · PR
    def issue_token(self, ws: Workspace, scope: Scope) -> Token   # 없으면 Unsupported
    def revoke_token(self, token: Token) -> None
```

- **plain**(A 첫 어댑터): 임의 git URL. TOKEN·EVENTS·PR 전부 `Unsupported` 로 명시 반환.
- **forgejo**(A 마지막 슬라이스): 레포 한정 PAT 발급(Forgejo 16 `repositories` 필드).
  scope = `read` | `write`(workspace 브랜치 push). drop 시 revoke. 관리 자격은
  `config.toml` 이 가리키는 파일 경로에서 읽고 로그·출력에 내지 않는다.
- 선택: `config.toml` `[remote."git.example.com"] adapter = "forgejo"`. 미선언 = plain.

## 6. 이벤트

- 이벤트 로그 = `git log refs/heads/gitswarm/meta`. 커밋 제목 고정 형식 `<종류> <id>`.
  종류(A): `ws.created` · `ws.published` · `ws.dropped` · `ws.expired`.
- `gitswarm events tail [--since <oid>]` → JSON lines `{kind, id, oid, at, payload}`.
- 구독: `config.toml` `[[sink]]` — `kind = "webhook", url = …` 또는 `kind = "jsonl", path = …`.
  발행 시점 1회 전송, 재시도 없음. 놓친 쪽은 `--since` 로 되감는다(로그가 git 에 있어 손실 0).

## 7. 오류

| 종류 | 뜻 | CLI 종료코드 |
|---|---|---|
| `NotFound` | id·ref 없음 | 2 |
| `Conflict` | CAS 상한 초과 | 3 |
| `InvalidState` | 허용되지 않는 전이 | 4 |
| `Unsupported` | 어댑터 기능 없음 | 5 |
| `RemoteError` | git 종료코드 ≠ 0 | 6 |

MCP 는 `{ok:false, error:{kind, detail}}`. 모호하면 오류 — 추측해서 진행하는 경로 없음.

## 8. 테스트·CI

- pytest, 네트워크 0: 임시 bare 레포를 `file://` 원격으로.
- CAS: 두 프로세스가 같은 meta 를 동시에 쓰는 경합 시험 — 한쪽 재적용, 둘 다 반영.
- 표면: CLI 는 typer runner, MCP 는 fastmcp 인프로세스 클라이언트.
- 어댑터: 공용 계약 시험을 plain·forgejo(httpx mock) 둘 다 통과.
- CI: `build.yml`(ruff·pytest). 착지는 land-direct.

## 9. A 슬라이스 순서

1. A1 `driver/git.py` + `store/meta.py`(CAS)
2. A2 `hive init` · `ws create/get/list/drop`
3. A3 `ws read/tree/publish/gc`
4. A4 CLI·MCP 표면
5. A5 events tail · sink
6. A6 forgejo 어댑터 · 토큰

## 10. 범위 밖(A)

B intent · C landing · GitHub 어댑터 · 이벤트 재시도 큐 · 로컬 조회 캐시(필요해지면 §2 위에 얹는다).

**권한 경계(명시)**: A 는 workspace 를 만든 에이전트에게 소유권을 묶지 않는다.
`refs/heads/gitswarm/*` 에 push 할 수 있는 주체는 누구나 어느 workspace 든 publish·drop 할
수 있다. 경계는 **원격의 push ACL** 이고, Forgejo 어댑터의 레포 한정 토큰이 호스트 단위로
그것을 좁힌다. 에이전트별 소유·승인은 C(landing)의 주제다. 원격 meta 에서 읽은 값은 전부
검증한다(id 알파벳·branch 재계산·created_at/ttl_s 형식·since oid) — 통과 못 하면
`InvalidState`/`NotFound` 로 fail-closed.
