# AI DEV DIRECTOR

**세 개의 AI 코딩 CLI를 한 회사처럼 돌려, 목표 한 줄을 검증된 코드로 바꾸는 로컬 개발 감독관.**
A local orchestrator that runs subscription AI CLIs (Claude Code, OpenAI Codex, optionally Google Antigravity) as
HEAD / BUILDER / REVIEWER, and turns a one-line goal into tested, independently reviewed code through signed-in CLI
sessions instead of pay-per-use API keys.

> **완벽한 시스템이 아닙니다.** 저희가 쓰려고 만든 도구를 공개한 것입니다. AI가 쓴 코드와 테스트를 이 PC에서
> 그대로 실행합니다. 처음에는 **분리된 가상 PC나 버려도 되는 계정에서 작은 작업으로** 써 보시길 권합니다.
> 아래 [알려진 한계](#알려진-한계-known-limits)를 먼저 읽어 주세요.
> **This is not a hardened product.** It runs AI-written code and tests on your machine. Try it in a VM first.

> 상태: 개인 운영 환경에서 매일 쓰고 있는 코드를 그대로 공개한 것입니다. 윈도우 우선, 한국어 UI.
> **macOS / Linux: 코드는 준비돼 있지만 미검증입니다** (v3.1.8, 아래 참고). 맥에서 돌려 보신 분은 이슈로 알려 주시면 고맙겠습니다.
> Status: the code we run daily, published as-is. Windows-first, Korean UI, English comments.
> **macOS / Linux: supported in code since v3.1.8 but not yet verified on a real Mac** — reports welcome.

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
  마찰(빌더 JSON 깨짐, 토큰 한도, 경로 범위, 리뷰 지적)은 재시도·공급자 교체·재계획으로 스스로 풉니다.
  파일 수·diff 상한 초과와 의존성·빌드·권한 설정 변경은 기본값에서 통과시키지 않습니다(`autonomy.waive_scope_caps`).
- **TRIAD 연구 모드** — 세 AI가 독립 연구 → 상호 비판 → 종합 가설 → NORMAL 파이프라인으로 실험 구현 → 세 갈래
  해석 → 연구 메모리. 설계와 구현은 `docs/V3_TRIAD_RESEARCH_PLAN.md`.
- **TRINITY 콘솔(8081)** — 세 AI의 역할·호출·토큰·현재 단계를 보여주는 무대형 대시보드, 채팅, 미션 다시보기,
  역할 교환, **작업 정지 / 시스템 중지** 버튼. `/classic`은 표 중심의 작업실 화면입니다.

## 지키는 것 (Hard rules, enforced in code)

| 규칙 | 구현 |
|---|---|
| 종량제 API 차단(환경 변수 범위) | `core/env_utils.py` 가 모든 `*_API_KEY`, 인증 토큰, 엔드포인트·클라우드 과금 전환 변수를 지우고 CLI와 검증 명령을 실행. CLI 자체를 API 키로 로그인해 둔 경우는 막지 못함 |
| Claude 역할은 읽기 도구만 | HEAD·REVIEWER·연구·빌더의 Claude 호출은 `--tools Read,Glob,Grep --permission-mode default`, MCP 서버 없이 실행 |
| 원본 프로젝트 무수정 | 모든 빌드는 `.director_worktrees/` 안의 git worktree에서만 |
| Scope Guard | ORDER의 허용 경로·보호 경로·파일 수·diff 상한을 Director가 검사, 빌더는 완화 불가 (`core/safety.py`). 상한을 넘으면 정지 — 운영자가 `waive_scope_caps: true` 로 직접 풀었을 때만 통과 |
| 프로젝트 밖 문서 | ORDER에 적힌 절대 경로 문서는 프로젝트 안이거나 `safety.reference_roots` 에 있을 때만 빌더에 전달 |
| 콘솔 명령은 연결된 브라우저만 | 상태를 바꾸는 모든 요청은 로컬 출처 검사 + 세션(연결 주소로 한 번 접속) 또는 브리지 토큰 필요 |
| 리뷰어 fail-closed | 리뷰 응답이 없거나 깨지면 PASS가 아니라 FAIL |
| 돈·자격증명은 사람이 | `core/autopilot.py` 의 정지 조건 — 결제, 실계좌 주문 코드(`OrderSend` 등), 계정 생성, 외부 비가역 행동 |
| 프로세스 트리 타임아웃 | 멈춘 CLI는 자식까지 함께 종료 (`core/process_runner.py`) |
| 정지는 정지 | 채팅의 "중단/멈춰/stop", 콘솔의 작업 정지 버튼 → 돌던 CLI 즉시 종료, 미션 STOPPED 고정, 재시작해도 재개 안 됨 |

## 요구 사항 (Requirements)

- Windows 10/11 (매일 쓰는 환경). **macOS / Linux는 미검증**: 프로세스 트리 종료(psutil), 작업본 폴더 열기(`open` / `xdg-open`),
  CLI 탐색(`~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`), 셸 런처(`start-companion.sh`)까지 코드는 갖춰져 있으나
  실제 맥에서 돌려 보지 못했습니다. 폴더 선택 창은 tkinter라 Homebrew 파이썬은 `brew install python-tk`가 필요하고,
  없으면 콘솔에 경로를 직접 입력하면 됩니다.
- Python 3.9+ 와 `pip install -r requirements.txt`
- Git
- 로그인된 CLI 두 개: **Claude Code**(`claude`, `--tools` 옵션을 지원하는 최근 버전), **OpenAI Codex**(`codex`).
  구독 로그인 세션을 그대로 씁니다. API 키는 필요 없습니다. 환경 변수의 키는 지우고 실행하지만,
  **CLI를 API 키로 로그인해 두었다면 그 키로 과금됩니다** — 구독 로그인인지 먼저 확인하세요.
- **Google Antigravity(`agy`)는 기본 꺼짐입니다.** 구글 Antigravity FAQ는 `agy`를 다른 애플리케이션 안에 감싸 쓰는
  것을 소비자 플랜에서 지원하지 않으며 계정 정지로 이어질 수 있다고 적고 있습니다
  ([FAQ](https://antigravity.google/docs/faq/)). 본인 플랜이 허용하고 위험을 감수할 때만 `config.yaml` 의
  `antigravity.enabled: true` 로 켜세요. 켜야 세 AI를 쓰는 TRIAD 연구 모드가 동작합니다.

## 빠른 시작 (Quick start)

```bash
pip install -r requirements.txt
python -m pytest -q            # 630개 테스트, CLI 없이 스텁으로 돕니다 (약 25~35분)
```

2026-11-28 기준 625/628 통과, 3개 실패 (v3.2.1에서 낭비 호출 테스트 5개 추가, 윈도우에서 통과). 실패 3개는 알려진 드리프트입니다: 두 개는 "저장소가 아닌 폴더는 거부"를
기대하는 옛 UI 테스트인데 v3.0부터 빈 폴더도 자동 준비(greenfield)하도록 바뀌었고, 하나는 스냅샷 제외 목록 픽스처
드리프트입니다. 안전 게이트·정책·미션 테스트는 모두 통과합니다.

콘솔 켜기 (윈도우):

```powershell
.\start-companion.ps1          # 연결 주소(http://127.0.0.1:8081/?token=...)를 출력합니다
.\stop-companion.ps1
```

처음 한 번은 **출력된 연결 주소**로 접속하세요. 그 브라우저는 이후 `http://127.0.0.1:8081` 로 바로 열립니다
(`/classic` 은 작업실 화면). 연결하지 않은 브라우저와 프로그램은 명령을 보낼 수 없습니다.

콘솔 켜기 (macOS / Linux, 미검증):

```bash
chmod +x start-companion.sh stop-companion.sh
./start-companion.sh           # python3 companion_server.py 를 백그라운드로, PID는 data/companion.pid
./stop-companion.sh
```

기본 역할은 HEAD=Claude, BUILDER=Codex, REVIEWER=Claude 입니다. 콘솔의 역할 버튼이나 `config.yaml` 로 바꿀 수 있습니다.

CLI로 바로 한 번 돌리기:

```bash
python director.py run -p D:/work/my_project -g "src/calc.py에 add(a, b)를 만들고 테스트를 추가해"
python director.py mission -p D:/work/my_project -g "회원가입 흐름을 끝까지 구현"
python director.py do -g "로그인 버그 고쳐" --profile quick     # 미션 + 자동 반영을 한 번에
python director.py status
python director.py clean-worktrees -p D:/work/my_project --dry-run
```

## 호출 수와 토큰 (Measured cost, v3.2.1)

"AI끼리 계속 돌려서 토큰이 무섭다"는 말씀을 듣고, 같은 과제(빈 폴더에 가계부 명령줄 도구, 세 단계 미션)를 실제 CLI로
여러 번 돌려 호출 하나하나가 왜 일어났는지 따져 봤습니다.

| 실행 | 시간 | AI 호출 | 실패한 호출 | 토큰(추정) |
|---|---|---|---|---|
| 수정 전 | 61분 | 37 | 15 | 약 55만 |
| v3.2.1 | 20분 | 10 | 0 | 약 28만 |

**같은 조건의 비교가 아닙니다.** 수정 전은 HEAD가 Codex, 수정 후는 HEAD가 Claude였습니다(측정 도중 Codex 주간 한도가
끝났습니다). BUILDER는 Antigravity, REVIEWER는 Claude로 같습니다. 과제 하나로 잰 값이라 다른 작업에서는 달라집니다.
10회는 이 구조의 최소치입니다: 첫 기획 1회 + 단계마다 구현·검수·판정 3회.

없앤 낭비:

- 검수 메모에 "delete"/"remove"가 있으면 통과한 단계를 실패 처리하던 것 → 검수자가 P1로 매긴 지적만 검사
- CLI 오류 문구가 잘려 한도가 끝난 공급자를 단계마다 다시 부르던 것 → 실제 오류 줄을 남겨 건너뜀
- 미션의 첫 기획 호출만 장애 조치가 없던 것 → 다른 공급자가 대신 기획
- 단계마다 HEAD를 같은 증거로 두 번 부르던 것(판정, 다음 계획) → PASS 판정에 다음 계획을 함께 받음
- 답을 준비하느라 출력이 없는 `agy`를 멈춘 것으로 보고 끊던 것 → 실측(131~163초)에 맞춰 무응답 제한 360초
- 최악의 비용을 알 수 없던 것 → 미션당 호출 상한(quick 12 / standard 36 / deep 72)과 종료 시 사용량 표시

아직 남은 것: HEAD의 판정 호출이 전체 토큰의 약 64%를 씁니다. 단계가 쌓일수록 판정 자료가 커지기 때문입니다.

## 설정 (Configuration)

`config.yaml` 한 파일입니다. 역할별 공급자(`head/builder/reviewer`), 타임아웃, 안전 상한, 검증 명령,
자율 모드(`autonomy`)와 프로필(`quick / standard / deep`), 프로젝트별 검증 명령(`projects`)을 둡니다.
프로젝트별 검증 명령을 프로젝트 안이 아니라 여기에 두는 이유는 빌더가 그것을 약화시킬 수 없게 하기 위해서입니다.

`companion.json` 은 콘솔 포트와(선택) 허용 프로젝트 목록입니다. 콘솔은 항상 `127.0.0.1` 에만 바인딩되며,
상태를 바꾸는 요청은 로컬 출처 검사와 브리지 토큰(`data/solomon-bridge.token`, 첫 실행 때 생성)을 요구합니다.

## 구조 (Layout)

```
director.py            CLI 진입점 (run / mission / do / status / resume / clean-worktrees)
companion_server.py    8081 콘솔 서버 (정적 파일 허용 목록 + /ops/* API)
dashboard_server.py    작업실(/classic)·채팅·미션 정지·상태 요약
agents/                HEAD / BUILDER / REVIEWER 어댑터 (claude, codex, antigravity, google 역할, mock)
core/                  orchestrator, mission, autopilot(정책), safety(Scope Guard), verifier, git_manager,
                       process_runner, provider_manager(장애 조치), research_*(TRIAD), usage(토큰 집계)
schemas/               ORDER / BuilderResult / ReviewResult / Decision / Provenance (pydantic)
console/               TRINITY 무대와 작업실 프런트엔드 (의존성 없는 정적 파일)
scripts/               폴더 선택 도우미, CLI 진단, 실패 run 재시도, 역할 smoke
tests/                 630 tests (스텁 CLI로 격리, 로컬 설정 파일에 의존하지 않음; 실제 CLI 통합은 -m integration_cli 로 opt-in)
docs/                  설계·마일스톤 기록
```

## 알려진 한계 (Known limits)

**안전 관련 — 먼저 읽어 주세요**

- **AI가 쓴 테스트와 빌드 명령은 격리 없이, 당신의 OS 계정 권한 그대로 실행됩니다.** git worktree는 파일 변경을
  원본과 분리할 뿐 샌드박스가 아닙니다. 그 코드는 홈 폴더의 파일을 읽을 수 있고 네트워크에 접근할 수 있습니다.
  중요한 계정이 로그인된 PC보다 가상 PC·컨테이너·별도 OS 계정에서 쓰는 것이 안전합니다.
- **"구독만 사용" 표시는 측정값이 아니라 선언값입니다.** 기록의 `api_key_used: false` 는 Director가 환경 변수의
  키를 지우고 호출했다는 뜻일 뿐, CLI가 실제로 무엇으로 과금됐는지 확인한 값이 아닙니다.
- **사용량 한도는 호출 횟수 기준입니다 (v3.2.1).** 미션마다 AI 호출 상한(`autonomy.max_ai_calls`, 기본 36)이 있고
  끝날 때 호출 수와 토큰 추정치를 알려 줍니다. 토큰이나 금액 기준의 한도는 없으며, 각 CLI의 구독 한도는 그대로 적용됩니다.
- **같은 OS 계정으로 도는 다른 프로그램은 막지 못합니다.** 그런 프로그램은 `data/solomon-bridge.token` 을 읽을 수
  있습니다. 세션·토큰은 포트에만 닿을 수 있는 프로세스와 다른 웹 페이지를 막습니다.
- **Claude 역할은 프로젝트 폴더에서 읽기 도구로 돕니다.** 쓰기·셸·MCP는 꺼 두었지만 프로젝트 안의 파일은 읽습니다.
- **Codex는 `--sandbox read-only` 로 부릅니다.** 읽기 범위는 Codex 샌드박스 정책을 따릅니다.
- `autonomy.waive_scope_caps`, `resume_on_start`, `auto_apply` 를 켜면 사람 확인 없이 진행되는 범위가 넓어집니다.
  기본값은 모두 꺼짐입니다.

**그 밖에**

- 큰 변경도 "파일 전체 JSON 제안" 방식이라 CLI 출력 한도에 걸릴 수 있습니다. 한 번은 압축 재시도, 그 다음은 공급자 교체.
- 각 CLI의 구독 한도(쿼터)에 그대로 종속됩니다. 한도가 끝나면 장애 조치 순서대로 다른 CLI로 넘어갑니다.
- 리뷰어와 HEAD가 같은 공급자로 몰리면 `REDUCED_INDEPENDENCE` 표시와 함께 통과시킵니다(숨기지 않음).
- 테스트는 로컬 스텁 CLI로 돕니다. 실제 세 CLI를 끝까지 태우는 E2E는 opt-in 마커로만 돌아갑니다.
- macOS / Linux는 미검증입니다. 일부 테스트는 윈도우 드라이브 문자 경로(`C:/...`)를 전제로 해서 그 플랫폼에서는 실패할 수 있으며, 제품 동작과는 무관합니다.

## 라이선스 (License)

MIT License — `LICENSE` 파일을 보세요. 자유롭게 쓰고, 고치고, 재배포할 수 있습니다. 저작권 표시와 라이선스 문구만 남겨 주세요.
