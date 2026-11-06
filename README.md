# AI DEV DIRECTOR

**세 개의 AI 코딩 CLI를 한 회사처럼 돌려, 목표 한 줄을 검증된 코드로 바꾸는 로컬 개발 감독관.**
A local orchestrator that runs three subscription AI CLIs (Claude Code, OpenAI Codex, Google Antigravity) as
HEAD / BUILDER / REVIEWER, and turns a one-line goal into tested, independently reviewed code — without a single
pay-per-use API call.

> 상태: 개인 운영 환경에서 매일 쓰고 있는 코드를 그대로 공개한 것입니다. 윈도우 우선, 한국어 UI.
> Status: the code we run daily, published as-is. Windows-first, Korean UI, English comments.

---

## 무엇을 하나 (What it does)

```
   goal ──▶ HEAD (plans an ORDER) ──▶ BUILDER (proposes whole files as JSON)
                 ▲                              │
                 │                              ▼
          HEAD decision ◀── REVIEWER ◀── Verifier (build / tests in an isolated worktree)
```

- **NORMAL 파이프라인** — HEAD가 목표를 ORDER(허용 경로·파일 수·diff 상한이 명시된 작업 명세)로 만들고, BUILDER가
  파일 전체를 JSON으로 제출하면 Director가 Scope Guard로 검사한 뒤 **격리 작업본(git worktree)** 에 반영하고 테스트를
  돌립니다. REVIEWER가 독립 검토하고 HEAD가 PASS/FAIL/수정 지시를 내립니다. 원본 프로젝트는 절대 직접 수정되지 않습니다.
- **미션(Mission)** — 채팅 한 줄이 미션이 됩니다. HEAD가 마일스톤 ORDER를 하나씩 계획하고, 각 단계는 직전 PASS 작업본의
  스냅샷에서 시작하며, 실제 증거(테스트·리뷰)를 보고 다시 계획합니다. 완료된 산출물은 설정에 따라 자동 반영되되
  사람이 그 사이 고친 파일은 덮어쓰지 않습니다.
- **HEAD 프록시 자율 모드** — 결제·실계좌 주문·실제 자격증명·되돌릴 수 없는 외부 행동만 사람에게 묻고, 나머지
  마찰(빌더 JSON 깨짐, 토큰 한도, 경로 범위, 리뷰 지적, 한도 초과)은 재시도·공급자 교체·재계획으로 스스로 풉니다.
- **TRIAD 연구 모드** — 세 AI가 독립 연구 → 상호 비판 → 종합 가설 → NORMAL 파이프라인으로 실험 구현 → 세 갈래
  해석 → 연구 메모리. 설계와 구현은 `docs/V3_TRIAD_RESEARCH_PLAN.md`.
- **TRINITY 콘솔(8081)** — 세 AI의 역할·호출·토큰·현재 단계를 보여주는 무대형 대시보드, 채팅, 미션 다시보기,
  역할 교환, **작업 정지 / 시스템 중지** 버튼. `/classic`은 표 중심의 작업실 화면입니다.

## 지키는 것 (Hard rules, enforced in code)

| 규칙 | 구현 |
|---|---|
| 종량제 API 금지 — 로그인된 구독 CLI만 사용 | `core/env_utils.py` 가 모든 `*_API_KEY` 를 환경에서 제거하고 CLI를 호출 |
| 원본 프로젝트 무수정 | 모든 빌드는 `.director_worktrees/` 안의 git worktree에서만 |
| Scope Guard | ORDER의 허용 경로·보호 경로·파일 수·diff 상한을 Director가 검사, 빌더는 완화 불가 (`core/safety.py`) |
| 리뷰어 fail-closed | 리뷰 응답이 없거나 깨지면 PASS가 아니라 FAIL |
| 돈·자격증명은 사람이 | `core/autopilot.py` 의 정지 조건 — 결제, 실계좌 주문 코드(`OrderSend` 등), 계정 생성, 외부 비가역 행동 |
| 프로세스 트리 타임아웃 | 멈춘 CLI는 자식까지 함께 종료 (`core/process_runner.py`) |
| 정지는 정지 | 채팅의 "중단/멈춰/stop", 콘솔의 작업 정지 버튼 → 돌던 CLI 즉시 종료, 미션 STOPPED 고정, 재시작해도 재개 안 됨 |

## 요구 사항 (Requirements)

- Windows 10/11 (경로 처리·폴더 선택 창·PowerShell 런처가 윈도우 기준. 핵심 로직은 POSIX에서도 동작하지만 미검증)
- Python 3.9+ 와 `pip install -r requirements.txt`
- Git
- 로그인된 CLI 중 최소 두 개: **Claude Code**(`claude`), **OpenAI Codex**(`codex`), **Google Antigravity**(`agy`).
  구독 로그인 세션을 그대로 씁니다. API 키는 필요 없고, 있어도 무시됩니다.

## 빠른 시작 (Quick start)

```bash
pip install -r requirements.txt
python -m pytest -q            # CLI 없이 스텁으로 돕니다 (약 12~25분)
```

`TEMP`나 `TMPDIR`이 긴 경로를 가리키면 일부 TRIAD 테스트가 Windows 경로 길이 제한(260자)에 걸립니다.
그럴 때는 짧은 임시 경로로 실행하세요 (`--basetemp=D:\t\p`, `TMPDIR=D:\t`).

콘솔 켜기 (윈도우):

```powershell
.\start-companion.ps1          # http://127.0.0.1:8081  (TRINITY 무대)  ·  /classic (작업실)
.\stop-companion.ps1
```

CLI로 바로 한 번 돌리기:

```bash
python director.py run -p D:/work/my_project -g "src/calc.py에 add(a, b)를 만들고 테스트를 추가해"
python director.py mission -p D:/work/my_project -g "회원가입 흐름을 끝까지 구현"
python director.py do -g "로그인 버그 고쳐" --profile quick     # 미션 + 자동 반영을 한 번에
python director.py research -p D:/work/new_idea --new-project -g "연구 질문"   # TRIAD 1사이클, CLI 호출 약 15~20회
python director.py status
python director.py clean-worktrees -p D:/work/my_project --dry-run
```

## 설정 (Configuration)

`config.yaml` 한 파일입니다. 역할별 공급자(`head/builder/reviewer`), 타임아웃, 안전 상한, 검증 명령,
자율 모드(`autonomy`)와 프로필(`quick / standard / deep`), 프로젝트별 검증 명령(`projects`)을 둡니다.
프로젝트별 검증 명령을 프로젝트 안이 아니라 여기에 두는 이유는 빌더가 그것을 약화시킬 수 없게 하기 위해서입니다.

`companion.json` 은 콘솔 포트와(선택) 허용 프로젝트 목록입니다. 콘솔은 항상 `127.0.0.1` 에만 바인딩되며,
상태를 바꾸는 요청은 로컬 출처 검사와 브리지 토큰(`data/solomon-bridge.token`, 첫 실행 때 생성)을 요구합니다.

## 구조 (Layout)

```
director.py            CLI 진입점 (run / mission / do / research / status / resume / clean-worktrees)
companion_server.py    8081 콘솔 서버 (정적 파일 허용 목록 + /ops/* API)
dashboard_server.py    작업실(/classic)·채팅·미션 정지·상태 요약
agents/                HEAD / BUILDER / REVIEWER 어댑터 (claude, codex, antigravity, google 역할, mock)
core/                  orchestrator, mission, autopilot(정책), safety(Scope Guard), verifier, git_manager,
                       process_runner, provider_manager(장애 조치), research_*(TRIAD), usage(토큰 집계)
schemas/               ORDER / BuilderResult / ReviewResult / Decision / Provenance (pydantic)
console/               TRINITY 무대와 작업실 프런트엔드 (의존성 없는 정적 파일)
scripts/               폴더 선택 도우미, CLI 진단, 실패 run 재시도, 역할 smoke
tests/                 스텁 CLI로 도는 테스트 (격리, 로컬 설정 파일에 의존하지 않음; 실제 CLI 통합은 -m integration_cli 로 opt-in)
docs/                  설계·마일스톤 기록
```

## 알려진 한계 (Known limits)

- 큰 변경도 "파일 전체 JSON 제안" 방식이라 CLI 출력 한도에 걸릴 수 있습니다. 한 번은 압축 재시도, 그 다음은 공급자 교체.
- 각 CLI의 구독 한도(쿼터)에 그대로 종속됩니다. 한도가 끝나면 장애 조치 순서대로 다른 CLI로 넘어갑니다.
- 리뷰어와 HEAD가 같은 공급자로 몰리면 `REDUCED_INDEPENDENCE` 표시와 함께 통과시킵니다(숨기지 않음).
- 테스트는 로컬 스텁 CLI로 돕니다. 실제 세 CLI를 끝까지 태우는 E2E는 opt-in 마커로만 돌아갑니다.

## 라이선스 (License)

MIT License — `LICENSE` 파일을 보세요. 자유롭게 쓰고, 고치고, 재배포할 수 있습니다. 저작권 표시와 라이선스 문구만 남겨 주세요.
