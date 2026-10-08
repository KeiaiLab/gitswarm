# gitswarm — 설계 (서브프로젝트 B: Intent)

2026-10-08. 사용자 결정으로 확정된 내용만 담는다. A 스펙
(`2026-10-06-gitswarm-workspace-design.md`)의 층·검증·오류·게이트 관례를 그대로 잇는다.

## 0. 무엇인가

커밋마다 "왜"를 1급 기록으로 남겨 **다음 에이전트에게 맥락을 넘긴다**. 다음 에이전트는 파일에서
시작한다 — "이 경로를 최근에 왜 바꿨고, 남은 일은 무엇인가".

| 결정 | 값 | 근거 |
|---|---|---|
| 주 소비자 | 에이전트 간 맥락 전달 | 사용자 선택 |
| 조회 진입점 | 경로 기준(`intent for <path>`) | 사용자 선택 — 에이전트는 파일부터 연다 |
| 저장 | 커밋 트레일러 `Gitswarm-Intent: <id>` + meta 브랜치 `intent/<id>.json` | 사용자 선택 — §1 |
| 기록 | 불변. 고치지 않고 새 기록으로 대체 | 쓰기 경합 = create 뿐 |

**A 스펙 §0·§2 정정**: `refs/notes/gitswarm/intent` 는 쓰지 않는다. notes 는 커밋 oid 에 붙어
rebase·squash·cherry-pick 마다 떨어진다 — C(Landing)는 rebase 로 착지하므로 착지하는 순간 맥락을
잃는다. 트레일러는 메시지에 실려 rewrite 를 견딘다.

```
  agent ── intent record ──▶ meta: intent/<id>.json   (CAS, 이벤트 intent.recorded)
    │                 └────▶ worktree gitdir/gitswarm-intent = <id>
    │
    └── git commit ──▶ prepare-commit-msg 훅 ──▶ "Gitswarm-Intent: <id>" 트레일러
                                                       │ rebase·cherry-pick 에도 남는다
  next agent ── intent for <path> ─▶ git log -- <path> ─┴▶ 트레일러 id ─▶ meta 기록 배치 읽기
```

## 1. 상태 배치

```
<remote>
  refs/heads/gitswarm/meta     트리에 intent/<id>.json 추가(ws/<id>.json 옆). 기록 1건 = 커밋 1개
<host>
  ~/.gitswarm/hives/<…>/repo.git/hooks/prepare-commit-msg   hive 가 설치
  <worktree gitdir>/gitswarm-intent                         현재 intent id(worktree 마다)
```

- meta 를 공유하는 이유: 이벤트 로그·CAS·검증 경로가 하나로 남는다. 기록은 영구다 — `ws gc`·`ws drop` 은
  `intent/` 를 건드리지 않는다.
- 위험(수용): meta 트리가 기록 수만큼 자란다. `MetaStore.apply` 는 쓰기마다 전체 트리를 다시 짓는다.
  bench 에 기록 1만 건 픽스처를 넣어 쓰기 예산을 잰다(§6). 넘으면 샤딩(`intent/<yyyymm>/`)을 후속으로.

## 2. 기록 모델

```json
{ "id": "01JA…(ULID)", "ws_id": "01J9…|null",
  "instruction": "지시 원문", "rationale": "왜 이렇게", "next": "남은 일",
  "checks": [{"cmd": "uv run pytest", "rc": 0}],
  "agent": {"name": "implementer", "run": "…"},
  "created_at": "2026-10-08T00:00:00Z" }
```

| 필드 | 필수 | 상한 | 검증 |
|---|---|---|---|
| `instruction` | 예 | 8 KiB | 비어 있지 않은 UTF-8 |
| `rationale` · `next` | 아니오(`""`) | 각 4 KiB | UTF-8 |
| `checks` | 아니오(`[]`) | 20개, `cmd` 512자 | `rc` 는 0–255 정수 |
| `ws_id` | 아니오 | — | ULID, 같은 meta tip 에 `ws/<id>.json` 이 있어야 함 |
| `agent` | 아니오 | A 와 같음 | A 와 같음 |

- 텍스트 필드는 `\n`·`\t` 외 제어문자를 거절한다(`Usage`). 원격 텍스트가 에이전트 프롬프트로 그대로
  흐르므로 쓰는 쪽에서 막고, 읽는 쪽은 A 처럼 다시 검증해 깨진 기록을 `invalid` 로 격리한다.
- 레코드 전체 상한은 A 의 64 KiB 읽기 상한을 따른다(위 필드 상한 합으로 항상 그 안).
- `ws_id` 생략 시 cwd 가 hive worktree 면 그 id 를 쓴다(A discovery 규칙).

## 3. 동작

| 동작 | 하는 일 | 멱등 |
|---|---|---|
| `intent record --instruction … [--rationale] [--next] [--check "<cmd>=<rc>"]… [--ws <id>]` | 검증 → meta `intent/<id>.json` 쓰기(제목 `intent.recorded <id>`) → cwd 가 hive worktree 면 그 gitdir 에 `gitswarm-intent` 기록. 반환 `{id, trailer, remote}` — `trailer` = `Gitswarm-Intent: <id>` | 아니오(id 새로) |
| `intent get <id>` | 기록 조회. 없으면 `NotFound`, 깨졌으면 `InvalidState` | 예 |
| `intent for <path> [--ref <ref>] [--limit N]` | §4 | 예 |

- `--limit` 기본 `INTENT_FOR_LIMIT = 10`, 상한 `INTENT_FOR_MAX = 100`.
- `--ref` 기본 = 원격 HEAD 의 symref(A `ws create --base` 기본값과 같은 함수).

## 4. 경로 조회 (`intent for`)

1. hive 에서 `<ref>` 와 meta 를 한 번의 fetch 로 받는다(왕복 1).
2. `git log -n <limit> --format=%H%x00%s%x00%cI%x00%(trailers:key=Gitswarm-Intent,valueonly,separator=%x2C) <ref> -- <path>`.
3. 트레일러 값 중 ULID 형식만 모아 meta tip 에서 `read_many_at` 한 번으로 읽는다.
4. 반환:

```json
{ "remote": "…", "ref": "refs/heads/main", "path": "src/x.py",
  "entries": [ { "commit": "<40hex>", "subject": "…", "at": "…",
                 "intents": [ {…기록…} ] } ],
  "invalid": [ { "commit": "<40hex>", "detail": "…" } ] }
```

- 트레일러 없는 커밋도 `intents: []` 로 싣는다 — 다음 에이전트에게 "기록 없이 바뀌었다"도 맥락이다.
- 한 커밋에 트레일러가 여럿이면(squash) 전부 싣는다.
- 형식이 틀린 트레일러·meta 에 없는 id·깨진 기록은 `invalid` 로(detail 120자, A 관례). 예외 없음.
- 경로가 ref 에 한 번도 없으면 `entries: []`(오류 아님 — 지운 파일도 이력은 있다).
- `subject` 는 원격 텍스트다 — 제어문자를 지우고 200자로 자른다.

## 5. 트레일러 자동화 (훅)

- `Hive.init`·`Hive.open` 이 `repo.git/hooks/prepare-commit-msg` 를 설치한다(내용이 다르면 덮어쓴다 — gitswarm 소유 파일).
  hive 설정에 `core.hooksPath = <repo.git>/hooks` 를 넣는다 — 사용자의 전역 `core.hooksPath` 가 훅을 가리지 않게.
- 훅 동작: `$(git rev-parse --git-dir)/gitswarm-intent` 가 있고 그 값이 ULID 면
  `git interpret-trailers --in-place --if-exists addIfDifferent --trailer "Gitswarm-Intent: <id>" "$1"`. 없거나 틀리면 아무것도 하지 않고 0 으로 끝난다(커밋을 막지 않는다).
- 훅은 POSIX sh 다 — git 이 훅을 직접 실행하고 hive 는 Python 환경을 보장하지 않는다(전역 §5 의 예외 사유).
- 다른 호스트 clone: 훅이 없다. `intent record` 가 돌려준 `trailer` 를 에이전트가 커밋 메시지에 넣는다.
- `ws drop`·gc 의 worktree 제거가 `gitswarm-intent` 파일도 함께 지운다(gitdir 이 사라진다).

## 6. 표면·이벤트·검사

- CLI `gitswarm intent record|get|for` ↔ MCP `intent_record|intent_get|intent_for`. 인자·반환 동일, 모양 맞춤은 `surfaces/common.py`.
- 코드: `service/intent.py`(서비스) · `driver/git.py` 에 경로 log 함수 1개 · `store/hive.py` 에 훅 설치.
- 이벤트: `EVENT_KINDS` 에 `intent.recorded` 추가. `events tail` 은 kind 접두(`ws.`·`intent.`)로 레코드 경로를 고른다.
  `stats.by_kind` 에 자동 포함, `by_state` 는 ws 만.
- 게이트: A 와 같다 — 커버리지 100%(pragma 0) · vulture · ruff. 시험:
  - 계약: 필드 상한·제어문자·`ws_id` 존재·`rc` 범위의 경계값마다 "그것만 틀린" 격리 시험(A Q10 I2 교훈).
  - rewrite 생존: 트레일러 커밋을 rebase·cherry-pick 한 뒤 `intent for` 가 같은 기록을 돌려준다.
  - 훅: 전역 `core.hooksPath` 를 다른 곳으로 둔 환경에서도 트레일러가 붙는다. 파일 없음·깨진 값이면 커밋 성공·트레일러 없음.
  - hypothesis: 원격 트레일러·기록 검증기 속성 시험.
- bench: `intent record` 왕복 예산 = `ws create` 의 meta 쓰기와 같은 박자, `intent for` = 왕복 1. 기록 1만 건 meta 픽스처로 쓰기 시간을 잰다.
- RELEASING.md 실원격 smoke 에 record → 커밋 → push → `intent for` 단계 추가.

## 7. 슬라이스

1. B1 기록 모델·검증 + `intent record/get`(meta, 이벤트 kind)
2. B2 훅 설치·`gitswarm-intent` 파일·`core.hooksPath`
3. B3 `intent for`(driver log 함수, invalid 격리, rewrite 생존 시험)
4. B4 CLI·MCP 표면 · bench · smoke · 문서(README·CHANGELOG·A 스펙 §0·§2 정정 주석)

## 8. 범위 밖(B)

도구 호출 전체 로그(크기 폭발) · 기록 수정·삭제 · C 의 착지 판정 연동(C 스펙에서) · 경로가 아닌 심볼 단위 조회 ·
meta 샤딩(bench 가 요구할 때).
