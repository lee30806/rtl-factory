# IMPLEMENTATION

> 이 문서는 원본 저장소 [chip-design-department](https://github.com/lee30806/chip-design-department)에서 복사했습니다. rtl-factory에는 RTL 생성 파이프라인과 원본 RTL → IR·SPEC 변환만 있고, 부서 계층(gateway, department, API, EDA 래퍼)과 P0 실험, PROGRESS.md는 원본 저장소에 있습니다.

Oct 8, 2026 · @Seungjoon Lee

[ARCHITECTURE.md](./ARCHITECTURE.md)를 구현하는 순서와 단계별 완료 기준입니다.

계획은 두 가지 원칙을 따릅니다.

- **LLM 없이 먼저 검증합니다.** IR 스키마, Emitter, Validator, Verify를 LLM 없이 먼저 만들고, 기존 블록으로 검증합니다. 결정론적 툴이 틀리면 그 위에 올린 에이전트의 결과도 판정할 수 없기 때문입니다.
- **안쪽에서 바깥쪽으로 쌓습니다.** 실행 계층을 먼저 만들고, 그다음 부서 계층과 Web UI를 붙입니다. 각 단계는 앞 단계의 산출물만으로 시험할 수 있어야 합니다.

일정은 인원과 자료 입수 시점에 달려 있어서 적지 않았습니다. 순서와 완료 기준만 정합니다.

---

## 0. 전제와 가정

| 항목 | 가정 | 근거 |
| --- | --- | --- |
| 파이프라인 구현 언어 | Python | pyslang, cocotb, 게이트웨이가 모두 Python입니다. **설계 문서에서 확정된 사항은 아닙니다.** |
| IR 스키마 | JSON Schema (draft 2020-12) | V1 단계의 수단 |
| SV 파싱 | pyslang | V3, V4 단계의 수단 |
| 시뮬레이션 | Verilator + cocotb | E1, E2 단계의 수단 |
| 최종 lint | VC SpyGlass 사내 템플릿을 래퍼로 호출 | E3 단계의 수단 |
| 프런트엔드 | React + TypeScript | 확정 사항 |
| 저장소 | Git. Seat별 worktree | 확정 사항 |
| LLM 실행 | coding agent(Claude Code(기본), Pi, Codex, Antigravity(agy) 등). Seat는 OpenRig 장기 세션, 파이프라인 에이전트는 스케줄러가 띄우는 headless 세션. 모델 API를 직접 호출하는 코드는 없음 | 확정 사항 |
| 사람과 Seat의 대화 | Web UI → 게이트웨이 → OpenRig 데몬 → Seat | 확정 사항 |

## 1. 저장소 구조 (제안)

```
chip-design-depart/
├── ARCHITECTURE.md
├── IMPLEMENTATION.md
├── docs/                       # 상세 설계 문서 (Chip Design Department, RTL 생성 파이프라인)
├── examples/i3c_basic_controller/ # 공개 예시 SPEC (I3C Basic v1.2 controller, 10개 섹션)
├── experiments/p0/             # P0 실험 키트 (X1~X6 안내서, 실행 스크립트)
├── schema/                     # IR JSON Schema (L0~L3), patch 스키마, 환류 양식 스키마
├── pipeline/
│   ├── ir/                     # IR 로더, 심볼 테이블, reads/writes 그래프, Python 조각 번역기, 설계 모델 실행기
│   ├── validator/              # V1~V11
│   ├── emitter/                # SV 템플릿, 소스맵, SDC 골격
│   ├── verify/                 # E1~E3 래퍼, 리포트 파서, 번역기 검사(설계 모델 대 RTL)
│   ├── tb/                     # cocotb 하니스 생성기, 프로토콜 체커 라이브러리
│   ├── classifier/             # 실패 분류 11종, 외부 리포트 입력
│   ├── patch/                  # patch 적용 툴, 쓰기 범위 강제
│   ├── spec/                   # Word/PDF/엑셀/다이어그램 변환, SPEC 검사
│   ├── agents/                 # coding agent 실행기(런타임별 실행 명령), 역할별 TASK 템플릿과 도구 집합
│   ├── scheduler/              # 의존 그래프, 게이트, 대기열, 상태 저장
│   └── api/                    # 실행 제출, 상태 조회, 환류 (CLI + HTTP)
├── tools/                      # MCP 도구 서버 (Validator 사전 실행, 트레이스 조회 등)
├── eda/                        # EDA 표준 래퍼 스크립트
├── rig/                        # rig.yaml, Seat 기본 지침
├── gateway/                    # Python 게이트웨이
├── web/                        # React + TypeScript
└── tests/
    ├── fixtures/               # 설계 문서의 IR 예시 (pkt_parser, pkt_rx_top)
    ├── golden/                 # 이식 시험 블록의 IR, 기대 RTL
    └── ...
```

## 2. 단계 개요

```mermaid
flowchart LR
  P0[P0 · 선행 확인<br/>자료 수집 · 실험] --> P1[P1 · 이식 시험<br/>스키마 · Emitter · V1~V5]
  P1 --> P2[P2 · 결정론적 툴 완성<br/>V6~V11 · Verify · TB · Classifier]
  P1 --> P3[P3 · SPEC 변환과 검사]
  P2 --> P4[P4 · 에이전트와 스케줄러]
  P3 --> P4
  P4 --> P5[P5 · 파이프라인 API<br/>환류 입력]
  P5 --> P6[P6 · 부서 계층<br/>OpenRig · Seat]
  P5 --> P7[P7 · 게이트웨이와 Web UI 1차]
  P6 --> P8[P8 · 첫 Block Top<br/>End-to-End]
  P7 --> P8
  P8 --> P9[P9 · Web UI 2·3차<br/>운영 안정화]
```

P2와 P3은 병렬로 진행할 수 있고, P6과 P7도 병렬로 진행할 수 있습니다. P8a(Python 동작 조각)는 P8 뒤에 사용자 결정으로 더한 단계이고, P10(다중 클럭과 리셋), P11(원본 RTL에서 IR과 SPEC 만들기), P12(v1 제외 항목 확장)도 그 뒤에 더한 단계입니다.

---

## P0. 선행 확인

설계의 전제가 실제 환경에서 성립하는지 확인합니다. 결과에 따라 이후 단계의 선택지가 바뀝니다.

실행 절차, 통과 기준, 스크립트는 [experiments/p0/README.md](https://github.com/lee30806/chip-design-department/blob/main/experiments/p0/README.md)에 있습니다.

### 실험

| # | 실험 | 실패할 때의 대안 | 영향을 받는 단계 |
| --- | --- | --- | --- |
| X1 | coding agent(로컬 모델)가 스키마에 맞는 IR 파일을 쓰고 검사 도구를 쓰는 비율 (런타임과 모델 조합별) | 다른 조합으로 교체하거나 출력 단위를 작게 쪼갬 | P4 |
| X2 | 비전 모델을 쓰는 coding agent의 타이밍 다이어그램 → WaveJSON 정확도 | 사람이 WaveJSON을 작성하고 coding agent는 초안만 만듦 | P3 |
| X3 | coding agent 런타임이 로컬 모델로 동작하는지. OpenRig Seat(Pi 런타임의 Seat별 `models.json`)와 headless 실행 둘 다 | `terminal` 런타임에 다른 coding agent를 올림 | P4, P6 |
| X4 | rig.yaml edge 종류. **완료:** `handoff`, `can_message`는 없음. 있는 다섯 가지로 대응 관계를 정함 | 해당 없음 (격리는 원래 권한으로 강제함) | P6 |
| X5 | 폐쇄망 설치. npm 사내 미러, 데몬의 외부 호출(플러그인 자동 갱신 1건 확인. 실패해도 동작), coding agent 런타임의 외부 호출 | 설정이나 패치로 호출을 끔 | P6 |
| X6 | 게이트웨이 → OpenRig 데몬 API → Seat 대화 왕복. 보낸 사람 구분, 동시 전송, 응답을 메시지 단위로 읽기 | Seat가 정해진 형식의 응답 파일이나 큐 항목을 남기는 규약 | P7, P9 |

### 수집할 자료

- Word/PDF SPEC 양식과 예시, 엑셀 레지스터 맵, 블록 다이어그램 원본(Visio/SVG)
- VC SpyGlass 사내 템플릿의 호출 방식과 리포트 형식
- 표준 동기화 셀, 메모리 매크로 래퍼 규약, 표준 프로토콜 태그 목록, 사내 코딩 가이드
- 로컬 모델 목록(언어, 비전)과 계열
- 이식 시험 대상 블록 3개와 그 테스트벤치: FSM 중심, datapath 중심, 다중 클럭. 이 중 하나는 계층 모듈이어야 합니다.

### 완료 기준

2026-10-11 기준, 사내 자료를 공개 자료와 임시 표준으로 대체해 충족했습니다. 상세는 [experiments/p0/README.md](https://github.com/lee30806/chip-design-department/blob/main/experiments/p0/README.md)에 있습니다.

- [x] X1\~X6의 결과가 기록되어 있고, 실패한 항목은 대안이 정해져 있습니다. X1과 X2는 agy(클라우드 모델)로 통과했고, 로컬 모델 판정은 사용자가 따로 진행합니다
- [x] 이식 시험 블록 3개와 사내 템플릿을 받았습니다. 공개 블록 4개로 대체했고([tests/golden/](tests/golden/README.md)), SpyGlass 템플릿은 제외했습니다

---

## P1. 블록 이식 시험

LLM 없이 스키마의 표현력과 Emitter의 정확성을 확인합니다. 설계 문서의 절차 2\~7을 이 단계에서 수행합니다. 절차 1(SPEC 변환)은 P3에서 수행합니다.

### 작업

1. **스키마.** `schema/`에 L0\~L3의 JSON Schema를 작성합니다. 모듈 role 4종과 블록 종류 5종을 포함합니다. v0 초안은 `schema/ir.schema.json`에 있습니다.
2. **IR 코어.** `pipeline/ir/`에 로더, 이름 기반 심볼 테이블, reads/writes 그래프를 만듭니다.
3. **Validator V1\~V5.** 스키마, 참조, 표현식(pyslang), 조각, 드라이버 단계입니다. 모든 오류에 노드 이름이나 `id`를 붙입니다.
4. **최소 Emitter.** 블록 종류 5종의 방출, `domain_map` 기반 클럭과 리셋 배선, `// @ir <id>` 주석, 소스맵을 만듭니다.
5. **손 이식.** 블록 3개를 손으로 IR에 옮기고 `tests/golden/`에 둡니다. 막힌 지점은 기록 항목 표에 따라 남깁니다.
6. **판정.** 방출된 RTL이 기존 테스트벤치와 VC SpyGlass 사내 템플릿을 통과하는지 봅니다.

### 진행 상황 (2026-10-11)

| 작업 | 상태 | 위치 |
| --- | --- | --- |
| 1. 스키마 | v0. 이식 시험에서 `cdc_sync`의 `gray`, 파라미터의 `local`을 추가 | `schema/ir.schema.json` |
| 2. IR 코어 | 완료: 로더, 번들 펼치기, domains에서 클럭·리셋 포트 유도, FSM 상태 레지스터, 읽기·쓰기 이름 공간 | `pipeline/ir/model.py` |
| 3. Validator V1\~V5 | 완료. V3, V4는 IR 하나를 탐침 모듈로 만들어 pyslang으로 한 번에 컴파일하고, 진단 줄을 IR 노드로 되돌림. 조각의 실제 읽기·쓰기와 대입 종류는 구문 트리에서 뽑음 | `pipeline/validator/` |
| 4. 최소 Emitter | `comb`, `seq`(리셋 분기 포함), `fsm`, `cdc_sync`(`2ff`, `gray`), `memory`(`inferred`), 인스턴스(`domain_map` 배선, 번들 연결), localparam, `// @ir` 주석, 소스맵. 동기화 셀은 임시 표준 `sync_2ff #(WIDTH)` | `pipeline/emitter/sv.py` |
| 5\~6. 손 이식과 판정 | **완료 (2026-10-11), 공개 블록으로 대체.** 4개 블록(계층, FSM, datapath, 다중 클럭)의 방출 RTL이 원본 테스트벤치 49개를 모두 통과. 골격 코드의 E1 위반 0건. SpyGlass 판정은 제외. 기록 항목과 판정은 [tests/golden/README.md](tests/golden/README.md) | `tests/golden/`, `tests/sim/test_golden.py` |

**검증.**
- 설계 문서의 예시 IR 2개가 V1\~V5를 통과합니다.
- 일부러 넣은 오류 18종을 모두 맞는 단계와 노드로 잡습니다.
- X1 리허설에서 모델이 만든 실제 IR 2개도 원인 위치를 짚어 거부합니다.
- 방출한 RTL은 pyslang elaboration에서 진단 0건입니다.
- 시뮬레이션: Verilator 5.052와 cocotb 2.1.0으로, 방출한 `pkt_parser`를 SPEC 기반 Python 참조 모델과 사이클 단위로 비교합니다. 정상 패킷, 잡음, 간격, 연속 패킷, 무작위 입력 3종이 통과합니다. 조각에 버그를 하나 넣은 변이는 3종 모두 실패합니다(`tests/sim/`). 시뮬레이션은 저장소의 `.venv`에서 돌립니다.

```bash
uv venv --python 3.11 .venv && uv pip install --python .venv/bin/python -e ".[dev]"   # 의존성은 pyproject.toml
.venv/bin/python -m pytest tests            # 시뮬레이션 포함
```

```bash
python3 -m pipeline.emitter.sv tests/fixtures/*.ir.json -o build/rtl
```

### 완료 기준

공개 블록으로 대체해 판정했습니다 (2026-10-11). VC SpyGlass 항목은 제외했습니다.

- [x] 블록 3개의 방출 RTL이 기존 테스트벤치를 통과합니다. 4개 블록, 원본 테스트벤치 49개 모두 통과
- [x] ~~골격 코드에서 SpyGlass 위반이 0건입니다~~ (제외). 대신 E1 Verilator `-Wall`에서 골격 줄의 위반 0건
- [x] 기록 항목(표현 불가 구문, 우회 표현, 원시 블록 비중, Validator 오탐)을 정리하고, 판정 규칙에 따라 스키마 조정안을 확정했습니다. `gray` 동기화, 추론형 메모리, localparam을 추가했고 `generate`는 설정별 IR로 대응
- [x] 원시 블록 비중의 기준선(0.46)과 Repair 반복 상한의 초기값(모듈당 5)을 정했습니다

> **분기점.** 원시 블록 비중이 대부분이면 JSON 계층을 얇게 줄이는 방향으로 ARCHITECTURE.md를 개정한 뒤 P2로 넘어갑니다.

---

## P2. 결정론적 툴 완성

파이프라인에서 LLM이 아닌 부분을 모두 완성합니다.

| 영역 | 작업 | 완료 기준 |
| --- | --- | --- |
| Validator | V6 래치와 루프, V7 리셋, V8 도메인, V9 FSM, V10 계층, V11 동결 해시 | 규칙마다 통과 사례와 실패 사례의 단위 테스트가 있고, golden IR 3개는 오탐 없이 통과. **진행 (2026-10-10): V6\~V11 구현.** 단계별 실패 사례 10종과 V11 테스트가 있고, 예시 IR 2개는 오탐 없이 통과. **golden IR(공개 블록 4개, 7개 모듈)은 V1\~V10 오류·경고 0건, V11 변경 없음 (2026-10-11).** 조합 경로는 대입문 단위로 계산(`pipeline/validator/structure.py`) |
| Emitter | SDC 골격(클럭 정의, 비동기 클럭 그룹, CDC 경로), `external` 참조, 전력 도메인 L1 기록, `memory` 매크로 래퍼 | Emitter 대표 출력물의 SpyGlass 통과를 회귀 테스트로 CI에 등록. **진행 (2026-10-11):** SDC 골격(`pipeline/emitter/sdc.py`: 클럭 정의, `async_to`로 비동기 클럭 그룹, 계층 전체의 `cdc_sync`에 대한 CDC 경로 제약), `external`은 방출하지 않고 `rtl_files`를 `filelist.f`에 넣음. 전력 도메인은 스키마의 L1 필드와 동결 범위로 기록됨(UPF 형식이 정해지면 출력 추가). `memory`는 매크로 래퍼 규약을 받은 뒤 함. SpyGlass 회귀는 이번 구현에서 제외 |
| Verify | E1 Verilator lint, E2 Verilator sim, E3 SpyGlass 래퍼와 리포트 파서. 라이선스 대기열 | 세 단계 결과가 같은 형식(툴, 규칙, 파일, 줄, 메시지)으로 나옴. **진행 (2026-10-11): E1, E2 구현** (`pipeline/verify/run.py`). E2는 시드를 여러 개 주면 초기값을 무작위화해 실행하고(T1), 실패 목록이나 DUT 출력이 시드에 따라 달라지면 `T1_INIT`으로 냄. E1은 `-Wall` 경고도 실패로 보고, E2는 cocotb 결과 파일의 예외 타입을 규칙으로 냄(`AssertionError`는 기댓값 불일치). 빌드 실패와 결과 파일 누락도 같은 형식으로 냄. E3과 라이선스 대기열은 이번 구현에서 제외 |
| 테스트벤치 | L1에서 cocotb 하니스 생성. 번들 프로토콜별 드라이버, 모니터, 스코어보드 골격. 프로토콜 체커 라이브러리 | golden 블록에 생성된 하니스를 붙이고 손으로 쓴 참조 모델로 T1\~T5 실행. **진행 (2026-10-11):** 생성기(`pipeline/tb/gen.py`)는 L1만으로 하니스 설정을 만들고, 내부 신호 목록은 Verify만 쓰는 별도 파일로 둠(Verifier 격리). 라이브러리(`pipeline/tb/lib.py`): `valid_ready` 드라이버, 모니터, 프로토콜 체커(T2), 스코어보드(T3), 속성 등록(T4), 트레이스. FSM 전이 커버리지(T5 일부)는 `pipeline/tb/report.py`. 예시 블록 `pkt_parser`에 생성 하니스와 손으로 쓴 참조 모델(`tests/sim/tb_pkt_parser_h.py`)을 붙여 T1\~T4와 전이 커버리지 통과. **golden 블록 적용 (2026-10-11):** `axis_cobs_decode`, `axis_adapter`, `axis_async_fifo`에 생성 하니스와 손으로 쓴 참조 모델을 붙여 시드 3개로 T1\~T5 통과. T5에 Verilator line/toggle/branch 커버리지와 요구사항 ID 커버리지를 추가 (`tests/golden/run_golden.py --harness`). 남은 것: `valid_ready` 외 프로토콜(사내 태그 목록 대기) |
| Classifier | 실패 유형 11종. 소스맵으로 조각 내부와 골격 구분. 외부 리포트 입력 경로 | 유형마다 재현 사례가 있음. **진행 (2026-10-11): 툴 결과로 판별하는 7종 구현** (`pipeline/classifier/classify.py`): 스키마 위반, IR 의미 오류, lint 조각 내부, lint 골격, sim 불일치, TB 실행 오류, 인터페이스 변경 필요(V11, L1을 고치려던 patch, 동결된 L1 값에 걸린 lint). Emitter 소스맵에 IR 값이 그대로 놓인 줄(조각 본문, 전이 조건, 신호 타입, 리셋값, 포트 선언, 연결로 정해진 인스턴스 핀)을 `fragment` 항목으로 추가해 조각과 골격을 가르고, 그 노드를 role이 고칠 수 없으면 인터페이스 변경으로 보냄. 외부 리포트는 같은 결과 형식으로 넣음. 나머지 4종(기댓값 오류 의심, 자식 모듈 원인 의심, Verifier 간 불일치, 후보 간 불일치)은 루프 이력이나 에이전트 판정이 필요해 P4 스케줄러에서 붙임 |
| patch 툴 | `{op, target, field, value}` 적용. 쓰기 범위와 동결 범위를 강제하고 waiver 추가를 거부 | 범위를 벗어난 patch가 거부되는 테스트. **완료 (2026-10-11)** (`pipeline/patch/apply.py`). `set`, `add`, `remove`. 하나라도 거부되면 아무것도 적용하지 않음. 범위 규칙과 별개로 동결 범위 해시도 비교함. 진동 감지용 IR 해시 제공. 전처리 지시어(`` `ifdef ``, `` `line ``, `` `define ``)도 거부. CLI는 `-o` 없이 실행하면 Validator 검사만 함(Repair의 patch 사전 실행). `top`과 `block_top`은 신호와 타입까지 동결 범위에 넣어 patch할 수 없음 |
| 불일치 리포트 | 첫 불일치 트랜잭션, FSM 트레이스, 팬인 신호 값, SPEC 구절 묶음 | golden 블록에 버그를 일부러 넣었을 때 리포트가 원인 노드를 포함. **진행 (2026-10-11):** `pipeline/tb/report.py`. 첫 실패(T2, T3, T4 중 가장 이른 것), 전후 FSM 상태 트레이스, 팬인 노드와 신호 값, 요구사항 ID가 들어간 SPEC 줄을 묶음. 예시 블록에 FSM 전이 버그와 카운터 버그를 넣었을 때 리포트가 원인 노드를 포함함. **golden 블록 확인 (2026-10-11):** 세 블록(FSM 전이, datapath 시프트, 다중 클럭 출력 필드)에 버그를 넣으면 리포트가 원인 노드를 포함함 (`tests/sim/test_golden_harness.py`). 한계: 작은 블록에서는 팬인이 거의 모든 블록을 덮어 범위를 좁히는 힘이 약함 |

VC SpyGlass에서 반복해서 걸리는 규칙은 Validator 규칙으로 옮깁니다.

---

## P3. SPEC 변환과 검사

P1과 독립적으로 진행할 수 있습니다. 다만 템플릿 섹션의 대응은 P0에서 받은 Word 양식이 있어야 확정됩니다.

| 작업 | 내용 | 완료 기준 |
| --- | --- | --- |
| Word 변환 | 10개 섹션 템플릿으로 결정론적 변환 | 양식 목차와 10개 섹션의 대응표 확정. **대기: 사내 Word 양식** |
| PDF 변환 | 배치 기반으로 표와 제목 추정, 스캔본은 OCR | 결과 전체를 사람이 검토하는 흐름이 있음. **완료 (2026-10-11)** `pipeline/spec/pdf.py`: Markdown과 검토 리포트(목차 대조 일치율, 고르지 않은 표, 텍스트 없는 쪽, 템플릿 대응, 그림 목록). I3C PDF에서 제목 98.8% 일치. OCR은 하지 않고 대상 쪽만 표시 |
| 엑셀 레지스터 맵 | 직접 파싱. IP-XACT, 문서 내 표와 대조해 경고 | column 정의 확정. **완료 (2026-10-11)** `pipeline/spec/regmap.py`: 열 register, offset, field, bits, access, reset, description (임시 확정). 형식·겹침·정렬·주소 폭 검사, SPEC 인용과 문서 표 대조. IP-XACT 대조는 대기 |
| 블록 다이어그램 | Visio 연결 관계, SVG 좌표 추정, PDF 벡터 추출. 실패하면 작성자에게 반려 | Block Top 표시 규칙 확정. 경계 신호와 인터페이스 표의 교차 검사. **대기: 다이어그램 원본과 그림 작성 규칙** |
| 타이밍 다이어그램 | 비전 모델을 쓰는 coding agent로 WaveJSON 변환, 다시 렌더링, 태그와 설명 파싱, 원본 해시 캐시 | X2 결과를 반영. **흐름 완료 (2026-10-11)** `pipeline/spec/waveform.py`: PDF 그림 잘라 내기, agy 변환, 원본 해시 캐시, 원본과 다시 그린 그림을 나란히 둔 검토 페이지, 그림별 태그(규범/예시)와 요구사항·시나리오 연결 검사. I3C PDF 그림 2장을 변환했으나, 아날로그 타이밍 그림이라 WaveJSON에 담긴 정보가 적음. 실제 SPEC 그림으로 X2 재판정 필요 |
| Block Top L1 생성 | SPEC 2\~4번 섹션의 표에서 L1 생성 (전력 도메인 포함) | 생성된 L1이 Validator V1\~V3 통과. **완료 (2026-10-11)** `pipeline/spec/l1.py`: I3C `i3c_ctrl_top` L1이 V1\~V3 통과. IR 노드마다 SPEC 줄 번호를 냄 |
| SPEC 검사 | 툴 검사(필수 섹션, 포트 참조 등), LLM 검사(모호함, 모순, 빠진 코너 케이스) | 미해결 질문이 검토 항목으로 기록됨. **완료 (2026-10-11)** `pipeline/spec/check.py`(툴), `pipeline/spec/llm_check.py`(coding agent). I3C: 게이트 통과, 검토 항목 52개(미정의 3, 열린 질문 7, 경고 27, LLM 질문 15) |

**첫 입력.** 이식 시험 블록 3개의 SPEC을 변환하고 고정합니다. 이 3개가 P4에서 정답이 있는 첫 입력이 됩니다.

**공개 예시 SPEC.** MIPI I3C Basic v1.2의 controller를 예시로 씁니다. 사내 양식이 아닌 공개 PDF라서 PDF 변환 경로와 SPEC 검사 게이트를 시험하기에 좋고, 결과를 공개 저장소에서 다룰 수 있습니다. SPEC 원본은 `experiments/specs/`에 두고 저장소에는 올리지 않습니다. 10개 섹션으로 정리한 초안은 [examples/i3c_basic_controller](./examples/i3c_basic_controller/)에 있습니다. 기존 RTL과 테스트벤치가 없으므로 P1 이식 시험 대상으로는 쓰지 않습니다.

---

## P4. 에이전트와 스케줄러

결정론적 툴 위에 LLM 에이전트를 올립니다.

### 순서

1. **coding agent 실행기.** 작업마다 격리된 작업 디렉터리를 만들고, 과제 파일(`TASK.md`), 입력 산출물, 도구를 둔 뒤 headless coding agent를 실행합니다. 종료 후 출력 산출물을 회수하고, 실행 전후를 비교해 쓰기 범위 밖 변경을 거부합니다. 런타임(Claude Code(기본), Pi, Codex, Antigravity(agy) 등)별 실행 명령 차이만 어댑터로 흡수하고, 역할마다 런타임과 모델을 설정으로 바꿀 수 있게 합니다. P0 X1 키트의 `experiments/p0/common/agent.py`가 출발점입니다.
2. **도구.** Validator 사전 실행, patch 사전 실행, 팬인 조회, 트레이스 조회, SPEC 구절 조회, 하니스 생성, 모델 단독 실행입니다. 작업 디렉터리에서 실행하는 CLI로 만들고, 필요하면 MCP 서버로도 노출합니다.
3. **에이전트 하나씩 붙이기.** 매번 golden 블록으로 평가합니다.

| 순서 | 에이전트 | 단독 평가 방법 |
| --- | --- | --- |
| 1 | Author | golden L1을 고정한 채 L2, L3 생성. 기존 테스트벤치로 판정 |
| 2 | Repair | golden IR에 버그를 주입하고 실패 리포트로 수정. 범위 위반 0건 |
| 3 | Verifier | golden RTL(정답)에 대해 참조 모델과 테스트 통과. 버그 주입 RTL은 검출 |
| 4 | Architect | golden 모듈 트리와 비교. 가정 목록 출력 |
| 5 | Arbiter | 기댓값을 일부러 틀린 테스트를 SPEC 근거로 판정 |

4. **스케줄러.** 모듈 의존 그래프를 아래에서 위로, 같은 레벨은 병렬로 실행합니다. 주요 기능은 다음과 같습니다.
   - 후보 중복(Author 2, Verifier 2)과 모델 계열 중복 경고
   - 차분 검사 두 종류와 후보 선택 규칙
   - 반복 상한, 예산 상한, IR 해시 기반 진동 감지
   - 동결 해제의 전파(진행 중인 루프 취소)와 두 번째 동결 해제 시 중단
   - 기댓값 변경 승인 대기(해당 모듈만), 사람에게 넘기는 실패
   - 검토 항목 기록(5개 조건), 모듈별 상태 저장과 재개
   - 종료 게이트: Validator를 통과한 산출물만 받음
5. **통합 단계.** `block_top`과 `top`에 대해 E1\~E3을 실행하고 최종 보고서를 만듭니다.

### 완료 기준

- 이식 시험 블록 3개를 SPEC부터 끝까지 사람 개입 없이 실행합니다. 결과 RTL은 기존 테스트벤치를 통과합니다.
- 실행을 강제로 중단한 뒤 같은 상태에서 재개됩니다.
- 격리를 시험합니다. Verifier의 입력에 L2, L3, RTL이 없고, Author 후보끼리 서로의 산출물이 보이지 않습니다.

### 진행 상황 (2026-10-12)

자세한 기록은 [PROGRESS.md](https://github.com/lee30806/chip-design-department/blob/main/PROGRESS.md)에 있습니다.

- **실행기와 격리:** `pipeline/agents/runner.py`, `task.py`.
  - 작업 디렉터리는 저장소 밖(`~/.cache/chip-design-department/runs`)에 두고, macOS `sandbox-exec`로 저장소와 다른 작업을 막습니다.
  - 사용량 한도(429)를 만나면 `paused_quota`로 멈추고 이어서 실행합니다.
- **도구:** `pipeline/agents/pl.py` (validate, patch, emit, fanin, trace, spec, harness, model).
- **에이전트:** Author, Repair, Verifier(작성, 승인된 수정, 차분 보강), Architect, Arbiter. 모두 Claude Code CLI로 돌립니다 (2026-10-13 17시경부터 모든 역할 Sonnet, effort low, `experiments/p0/local/agents.claude.json`. 그전에는 agy로 모든 역할 `claude-sonnet-5-5-low`. 원래 Author `gemini-3.8-flash-high`, 나머지 `gemini-3.1-pro-high`였으나 3.1 Pro가 agy에서 빠지고 Gemini 한도가 차서 사용자 결정으로 교체했습니다. 같은 계열 경고).
- **스케줄러:** `pipeline/scheduler/run.py`.
  - 하위 모듈이 여럿이면 같은 번호 후보끼리 묶어 검증합니다.
  - Repair는 원인 후보 모듈로 보내고, 형제 모듈을 지목하면 그 모듈로 넘깁니다. 트레이스와 팬인은 인스턴스 경계를 넘습니다.
  - Repair들이 원인을 서로 넘기면(충돌) Design-lead 결정을 기다립니다 (`repair_conflict`). 고칠 모듈을 지정하거나, Architect 재구성을 요청해 모듈 트리를 다시 정하고 Author부터 다시 돌립니다 (2026-10-13, P8a 비교 실험에서 드러남).
  - Arbiter 판정, 반복 상한, 진동 감지, sim 시간 초과, 상태 저장과 재개를 갖췄습니다.
- **사람 승인 (P5 일부 앞당김):** `pipeline/scheduler/approve.py`. 기댓값 수정안을 승인하거나 거절하고, `decisions.json`에 기록합니다.
- **후보 간 차분 시뮬레이션:** `pipeline/tb/diffsim.py`.
  - 같은 무작위 자극(steady, reset)으로 후보를 비교하고, 차이를 유지하는 최소 자극으로 줄입니다.
  - Verifier가 그 상황의 테스트를 더합니다. 보강된 테스트는 후보를 갈라야 채택합니다.
- **E2E:** 이식 시험 블록 3개가 SPEC부터 원본 테스트벤치까지 통과했습니다 (9/9, 16/16, 20/20). 블록마다 기댓값 수정 승인이 한 번 있었습니다.
- **아직 안 한 것:**
  - 동결 해제의 전파 (L1 변경 요청은 검토 항목으로만 기록)
  - 두 단계 이상의 모듈 트리
  - 단독 평가 일부 (Author, Verifier, Architect 각 3블록, Repair cobs. 사용량 한도로 못 돌림)
  - Linux 서버 격리 (bubblewrap 등)

---

## P5. 파이프라인 API와 환류 입력

부서 계층이 호출할 세 인터페이스를 CLI와 HTTP로 노출합니다.

| 인터페이스 | 엔드포인트 (예시) | 비고 |
| --- | --- | --- |
| 실행 제출 | `POST /runs` → `run_id` | SPEC 버전, 모델, 후보 개수, 예산 |
| 상태와 결과 조회 | `GET /runs/{id}`, `/runs/{id}/modules`, `/runs/{id}/review-items`, `/runs/{id}/report` | 승인된 RTL은 읽기 전용 브랜치로 제공 |
| 환류 | `POST /runs/{id}/feedback` | 형태 4종 스키마 검증. 제출자는 Design-lead만 허용 |

- **검사 실패 리포트 입력.** Superlint, Xprop, DFT, DCLint, 합성, LEC 리포트의 파서입니다. 파서는 툴, 규칙, 파일, 줄, 메시지로 정규화한 뒤 Classifier로 넘깁니다.
- **영향 모듈만 재실행.** SPEC 변경 요청이 들어오면 바뀐 요구사항 ID에서 영향 모듈을 계산합니다.
- **교착 방지.** 같은 Block Top에서 재실행이 3회 반복되거나 24시간 이상 진행이 없으면 자동으로 멈추고 알립니다.
- **이력.** 모든 환류를 실행 ID, 요구사항 ID와 함께 기록합니다.

**완료 기준.** 외부 리포트 샘플을 넣으면 조각 내부 위반은 Repair로, 골격 위반은 생성기 버그로 분류됩니다.

### 진행 상황 (2026-10-12)

- 구현했습니다: 실행 제출과 조회 API(HTTP, CLI), 환류 네 형태(스키마 검증, Design-lead만 제출), 사람 결정(기댓값 수정, SPEC 변경 승인), 영향 모듈만 재실행(외부 리포트 Repair, L1이 같은 SPEC 개정), 교착 방지(재실행 3회, 24시간), 이력.
- 리포트 파서는 양식 파일(`config/report_formats/`)로 동작합니다. 지금은 Verilator와 iverilog 양식만 있습니다. 사내 툴(Superlint, Xprop, DFT, DCLint, 합성, LEC)은 샘플을 받으면 양식 파일만 추가합니다.
- 완료 기준은 Verilator 양식 리포트로 확인했습니다 (조각 내부 → Repair 재실행, 골격 → 생성기 버그).
- 남은 것:
  - 승인된 RTL의 읽기 전용 브랜치 (지금은 통과한 실행의 RTL을 API로 제공)
  - 인증 (P7 게이트웨이)

---

## P6. 부서 계층 (OpenRig)

P0의 X3\~X6 결과를 전제로 진행합니다.

| 작업 | 내용 |
| --- | --- |
| 폐쇄망 설치 | 사내 미러, 외부 호출 차단, 설치 절차 문서화 |
| rig.yaml | 4개 Pod, 15개 Seat, edge, 연속성 정책 |
| Seat 기본 지침 | 역할별 지침. 공통 규칙은 RTL/IR을 직접 수정하지 않고 수정 요구는 환류 양식으로만 내는 것 |
| 권한 분리 | Seat별 Git worktree. IR과 RTL 쓰기 권한은 파이프라인 계정에만 줌. DV Seat는 Verifier 산출물 경로에 접근 불가 |
| 스케줄러 배치 | OpenRig 서비스 묶음으로 배치. Seat 세션 초기화와 실행 상태 분리 |
| Seat 도구 | 파이프라인 API 클라이언트. 환류 제출은 Design-lead에게만 노출 |
| Sanity 연동 | E3 결과를 넘겨받음. DCLint는 SDC 골격과 UPF를 입력으로 받음. EPC 래퍼 |
| 복구 | OpenRig 스냅숏으로 토폴로지 복구 절차 |

**완료 기준.**
- 권한 위반 시도가 실패합니다. 예를 들어 DV Seat가 Verifier 테스트를 읽거나, Seat가 RTL에 쓰려 하면 거부됩니다.
- Seat 세션을 초기화해도 진행 중인 실행에 영향이 없습니다.

### 진행 상황 (2026-10-12)

자세한 내용은 [department/README.md](https://github.com/lee30806/chip-design-department/blob/main/department/README.md)와 [PROGRESS.md](https://github.com/lee30806/chip-design-department/blob/main/PROGRESS.md)에 있습니다.

- 구현했습니다:
  - rig.yaml (Pod 4개, Seat 15개, infra Pod, edge, 연속성 정책)
  - Seat 기본 지침 (공통과 역할별)
  - 권한 분리 (Seat별 worktree와 Seat별 macOS sandbox 프로필)
  - 스케줄러 배치 (infra Pod의 API 서버, 실행은 별도 세션)
  - Seat 도구 (`deptctl`, 환류는 Design-lead만)
  - 복구 (스냅숏, 안 되면 rig.yaml)
- 완료 기준 두 가지를 OpenRig로 올린 rig에서 시험했습니다: 권한 위반이 실패하고, Seat 초기화가 실행에 영향을 주지 않습니다.
- OpenRig에는 Docker Compose 서비스 묶음이 있지만, 이 맥에 Docker가 없어 API 서버를 terminal 노드로 배치했습니다.
- 남은 것:
  - 폐쇄망 설치
  - Linux 격리 (계정 분리, bubblewrap)
  - Sanity 연동 (E3 제외)
- agy Seat의 폴더 신뢰 확인은 Seat 진입점이 자기 worktree를 agy 신뢰 목록에 넣어 해결했습니다 (사용자 승인).

---

## P7. 게이트웨이와 Web UI 1차

P6과 병렬로 진행합니다. 게이트웨이는 파이프라인 API와 OpenRig 데몬 앞에 둡니다.

| 작업 | 내용 |
| --- | --- |
| 게이트웨이 | 자체 계정 인증. 인증 모듈은 SSO로 교체할 수 있게 분리. 5개 역할(관리자, 설계, 검증, 구현, 승인자)의 권한. 승인 기록(누가, 언제, SPEC 버전, 실행 ID). OpenRig 데몬 API 연결(Seat 상태 조회, 이후 대화 중계의 기반). 모델은 직접 호출하지 않음 |
| SPEC 작업대 | 원본과 변환 결과의 나란한 대조, 원본 파형과 WaveJSON 렌더링의 대조, 검사 질문에 답하기 |
| 승인함 | SPEC 고정, 최종 승인, RTL Freeze, 기댓값 변경의 승인과 반려 |
| 실행 상세 (읽기 전용) | 모듈 트리와 상태, 후보별 진행, 검토 항목, 실패 분류, RTL과 IR 노드의 대응(소스맵), 에이전트 작업별 출력과 transcript |
| 환류 양식 | 형태, 대상, 근거를 받는 양식. 자유 텍스트로 수정을 요청하는 경로는 두지 않음 |

**완료 기준.** 승인자 역할만 승인할 수 있고, 모든 승인이 기록에 남습니다.

### 진행 상황 (2026-10-12)

자세한 내용은 [gateway/README.md](https://github.com/lee30806/chip-design-department/blob/main/gateway/README.md)와 [PROGRESS.md](https://github.com/lee30806/chip-design-department/blob/main/PROGRESS.md)에 있습니다.

- 구현했습니다:
  - 게이트웨이: 자체 계정, 인증 제공자 분리, 역할 5개, 승인 기록, 파이프라인 API 토큰, OpenRig Seat 상태 조회
  - 승인 다섯 가지와 RTL Freeze 판정
  - Web UI 1차: SPEC 작업대, 승인함, 실행 상세, 환류 양식, Seat 상태
  - 부서 계층의 Seat를 게이트웨이 API 키로 연결
- 완료 기준을 시험으로 확인했습니다: 승인자만 승인하고, 모든 승인이 기록됩니다.
- 남은 것:
  - 사내 SSO
  - 대화 중계 (P9)
  - 승인된 RTL의 읽기 전용 브랜치
  - 2차, 3차 화면

---

## P8. 첫 Block Top End-to-End

이식 시험 블록이 아닌 실제 신규 Block Top 하나로 진행 순서 1\~6을 모두 거칩니다.

1. SPEC 작업대에서 SPEC을 고정합니다.
2. RTL Engineer가 실행을 제출합니다. 같은 시점에 DV Pod가 검증 환경을, PI Pod의 Power Intent Seat가 UPF를 준비합니다.
3. Sanity 검사를 실행합니다.
4. 승인함에서 최종 승인합니다.
5. DV가 독립 검증을, PI가 합성, LEC, STA, SDC 검사를 진행합니다. 환류가 생기면 2단계로 돌아갑니다.
6. RTL Freeze를 검토합니다.

**완료 기준.**
- RTL Freeze에 도달합니다.
- 모든 환류가 4가지 형태로 분류되어 기록되어 있습니다.
- 회고에서 검토 항목 조건, 반복 상한, 예산의 조정안을 냅니다.

---

## P8a. Python 동작 조각 (2026-10-13 사용자 결정)

IR 구조는 JSON 그대로 두고, 동작 조각을 SV 대신 Python(사이클 단위 기술)으로도 쓸 수 있게 합니다. 결정 사항은 다음과 같습니다.

- Python 조각은 IR 슬롯 안에 둡니다. 소스맵은 SV 줄 → (IR 노드, 조각 안의 Python 줄)로 한 번에 대응합니다.
- 설계 모델(Author의 Python 조각)과 참조 모델(Verifier)은 따로 씁니다. 같은 모델을 RTL 원천과 기댓값에 함께 쓰지 않습니다.
- 설계 모델과 RTL의 비교는 번역기 검사로만 씁니다. 판정은 RTL 시뮬레이션입니다.
- 전면 전환이 아니라 후보별 선택으로 시작해, 같은 블록에서 SV 후보와 비교한 뒤 넓힙니다.

### 작업

1. 스키마에 모듈 `lang`(`sv`, `py`)을 더합니다.
2. 조각 번역기(`pipeline/ir/pyfrag.py`)를 만듭니다. Python 부분집합을 SV로 옮기고, reads/writes를 계산하고, 문장별 줄 대응을 냅니다. 폭 변환은 파라미터 식 캐스트로 명시합니다.
3. 모듈을 읽을 때 `py` 조각을 낮춰 Validator, Emitter, 하니스가 같은 경로를 타게 합니다. 번역 오류는 V4로 냅니다.
4. Emitter 소스맵에 `py_line`을 넣고, Classifier 메시지에 Python 줄을 붙입니다.
5. 설계 모델 실행기(`pipeline/ir/pysim.py`)를 만듭니다. 계층(인스턴스)과 파라미터 대입을 지원합니다.
6. 번역기 검사(`pipeline/verify/pyequiv.py`)를 만들고 스케줄러의 검증에 넣습니다. 불일치는 생성기 버그(`PY_EQUIV`)로 처리합니다.
7. Author 후보마다 조각 언어를 정합니다 (`roles.author.langs`, 설정 변경 `author_langs`). Author 종료 게이트가 언어를 확인합니다.
8. 에이전트 안내서에 Python 조각 절을, 에이전트 도구에 `pl.py pysim`을 더합니다.
9. 실제 블록에서 SV 후보와 Python 후보를 비교합니다 (`axis_crc_append`와 golden 블록). 에이전트 실행 수, Repair 수, lint, 후보 간 차분, PI 결과를 봅니다.

### 완료 기준

- golden `axis_adapter`를 Python 조각으로 옮긴 IR이 기본값이 아닌 파라미터에서도 번역기 검사를 통과하고, 손으로 쓴 SV 판의 RTL과도 같은 출력을 냅니다.
- 스케줄러에서 Python 후보가 번역기 검사를 거쳐 SV 후보와 함께 통과합니다 (가짜 에이전트 시험).
- 작업 9의 비교 결과로 Python 조각의 범위(기본 언어로 할지, 후보 하나만 둘지)를 정합니다.

**결정 (2026-10-14, 사용자):** 후보 하나만 Python으로 둡니다 (`roles.author.langs` = `["sv", "py"]`). 비교한 4블록(adapter, cobs_decode, crc_append, async_fifo) 모두 두 언어 후보가 통과했고, Python 후보는 Repair가 같거나 조금 많았습니다.

---

## P9. 운영 안정화

- **Web UI 2차.** 대시보드, 환류 관리, 리포트
- **Web UI 3차.** 조직과 Seat(Seat 상태, 대화, lead에게 작업 지시. 지시 입력은 lead에게만 노출. 대화는 게이트웨이가 OpenRig 데몬 API로 Seat에 전달하고 transcript로 응답을 보여 줌. X6 결과를 따름), 설정(역할별 모델, 후보 개수, 예산, 라이선스 대기열)
- **SSO 연동**
- **v1 제외 항목 확장.** 이식 시험과 P8에서 실제로 필요하다고 드러난 것만 추가합니다. 후보는 generate, function/task, inout, 다중 비트 CDC, SVA입니다. (inout은 P11, 다중 비트 CDC는 P10, function/task와 SVA는 P12에서 추가)
- **EPC 스위칭 활동 정보.** 출처를 정하고 연동합니다.

---

## P10. 다중 클럭과 리셋 확장 (2026-10-16 사용자 결정, 2026-10-16 구현)

지금도 비동기 도메인 여러 개, `cdc_sync`(`2ff`, `gray`), 도메인별 리셋 하나는 됩니다 (P8a `axis_async_fifo`). 이 단계는 남은 빈틈 네 가지를 메웁니다. 대상 블록은 아직 없으므로 시험용 IR과 SPEC으로 검증합니다.

### 결정 사항 (설계안)

**1. 관련된 동기 클럭.** 같은 원천에서 나온 정수 분주 클럭입니다. 블록 안에서 클럭을 만들지는 않습니다 (생성 클럭 금지는 그대로). 클럭은 여전히 포트로 들어옵니다.

| 위치 | 바뀌는 것 |
| --- | --- |
| SPEC 2번 섹션 | "도메인 간 관계" 칸에 `<도메인>에서 N분주`(이 클럭 = 기준 / N, 상승 에지 정렬)를 씁니다. 클럭 포트가 같은 두 도메인은 `N = 1`인 동기 관계로 봅니다 |
| IR `domains` | `sync_to: {"domain", "div"}`를 더합니다. 기준 도메인은 `sync_to`가 없어야 합니다 (사슬 금지). `freq_mhz`는 기준 / div와 같아야 합니다 |
| Validator V8, V10 | 같은 클럭 그룹(기준과 그 분주 도메인) 안의 신호는 동기화 셀 없이 읽고 연결할 수 있습니다. 그룹 안에서 `cdc_sync`를 쓰면 경고합니다 |
| SDC | 분주 도메인은 기준 주기 × N으로 `create_clock`하고, 같은 원천임을 주석으로 남깁니다. 비동기 클럭 그룹에 넣지 않습니다 (기본 동기 해석) |
| 하니스 | 분주 도메인의 주기를 기준 주기(ps) × N으로 정확히 맞춰 상승 에지를 정렬합니다 |

**2. 리셋 구조.**

| 항목 | 설계 |
| --- | --- |
| 도메인당 추가 동기 리셋 | `reset.extra: [{"port", "active"}]`(동기만). 비동기 리셋은 주 리셋 하나뿐입니다. Emitter는 `if (주 리셋) ... else if (추가 리셋) ... else 본문`으로 방출합니다 (합성기가 비동기 리셋을 추론하는 형태). SPEC에서는 리셋 포트와 극성 칸에 쉼표로 나열합니다 |
| 같은 클럭, 다른 리셋 | 클럭 포트가 같은 도메인 둘(N = 1 동기 관계)로 씁니다. 리셋 도메인이 다른 것입니다 |
| 리셋 동기화 셀 | `reset.synchronize: true`(비동기 assert, 블록 안에서 동기 deassert). `top`과 `block_top`에만 둘 수 있고, Emitter가 표준 셀 `rst_sync`를 넣고 동기화한 리셋을 자기 블록과 하위 인스턴스에 연결합니다. SPEC의 "리셋 동기" 칸 `비동기 (블록 안에서 동기화)`. 지금의 `비동기 assert, 동기 deassert`는 바깥에서 이미 동기화했다는 뜻으로 그대로 둡니다 |
| 조각에서 리셋 읽기 | `comb` 블록이 자기 도메인의 리셋 포트(주, 추가)를 읽을 수 있습니다 (예: 리셋 중 `tready = 0`). `seq`와 `fsm`은 읽지 않습니다. AFIFO-E02처럼 "리셋이 걸린 그 사이클" 요구를 만족시킬 수 있습니다 |
| 리셋 도메인 교차 검사 | 같은 클럭 그룹에서 비동기 리셋이 다른 도메인의 레지스터를 읽으면 경고합니다 (V8, RDC). 오류로 할지는 실제 블록에서 본 뒤 정합니다 |

**3. CDC 셀.** 모두 임시 표준 셀(`pipeline/emitter/stubs/`)로 방출하고, 사내 셀 이름과 포트를 받으면 바꿉니다 (`sync_2ff`와 같음).

| 셀 | IR | 용도 |
| --- | --- | --- |
| `sync_pulse` | `cdc_sync`의 `style: "pulse"`. `src_domain`이 있어야 함 | 보내는 쪽 1사이클 펄스 → 받는 쪽 1사이클 펄스 (토글 방식). 펄스 간격은 양쪽 느린 클럭 3주기 이상이어야 함 |
| `cdc_fifo` | 새 블록 `cdc_fifo`: `src_domain`, `dst_domain`, `depth`(2~256의 2의 거듭제곱), `src: {valid, ready, data}`, `dst: {valid, ready, data}`. 폭은 `data` 타입에서 정함 | 여러 비트 값을 valid/ready로 넘기는 비동기 FIFO (gray 포인터). 깊이 2면 핸드셰이크 동기화로 씁니다 (별도 핸드셰이크 셀은 두지 않음) |
| `rst_sync` | 위의 `reset.synchronize` | 리셋 동기화 |

Validator는 셀의 읽기·쓰기와 도메인을 알고(V3, V5, V8), SDC는 셀의 동기화 핀까지 `set_max_delay`(비동기 그룹이면 주석)를 냅니다.

**4. 설계 모델 실행기 다중 도메인.** `PySim`이 도메인마다 주기를 받아 시간 순서로 에지를 처리합니다. 같은 시각에 오르는 도메인은 한꺼번에 갱신합니다 (RTL의 non-blocking과 같음). `cdc_sync`(`2ff`, `gray`, `pulse`), `cdc_fifo`, `rst_sync`, 추가 리셋을 표준 셀 RTL과 같은 의미로 실행합니다. 번역기 검사(`pyequiv_tb`)는 클럭을 직접 움직여 RTL과 실행기의 에지 시각을 맞추고, 클럭 그룹마다 기준 클럭의 하강 에지에서 입력을 바꾸고, 사건마다 모든 출력을 비교합니다. 그룹마다 기준 클럭의 반주기를 서로 다른 홀수 ns로 두어(예: 5ns, 7ns) 입력을 바꾸는 시각이 어느 그룹의 상승 에지와도 겹치지 않게 하고, 조합 세 가지로 위상을 바꿔 가며 봅니다.

**하지 않는 것:** 하강 에지 도메인(SPEC에서 L1로 옮길 때 지금처럼 상승 에지 고정. P12에서 추가), 블록 안 클럭 생성과 클럭 게이팅, 분주 관계의 사슬, 비동기 추가 리셋.

### 작업

1. 스키마와 모델: `domains`의 `sync_to`, `reset.extra`, `reset.synchronize`, `cdc_sync`의 `pulse`, 새 블록 `cdc_fifo`. 클럭 그룹 계산을 `Module`에 둡니다.
2. SPEC → L1과 SPEC 검사: 분주 관계, 같은 클럭 포트, 리셋 목록, 블록 안 동기화. 관계가 가리키는 도메인, 분주비, 주파수 일치를 검사합니다.
3. Validator: 클럭 그룹 안 읽기와 연결 허용, 그룹 안 `cdc_sync` 경고, 리셋 포트 읽기(`comb`만), RDC 경고, `cdc_fifo`의 구동과 도메인, `synchronize`는 `top`/`block_top`만.
4. Emitter와 SDC: 추가 리셋 분기, `rst_sync` 삽입과 하위 연결, `sync_pulse`, `cdc_fifo`. 임시 표준 셀 세 개를 쓰고 셀마다 시뮬레이션 시험을 둡니다.
5. 하니스: 분주 도메인 주기 정렬, 추가 리셋 포트를 입력으로 다루고 리셋 중 큐 비우기에 넣습니다.
6. 실행기와 번역기 검사: 다중 도메인 시간 진행, 셀 의미, `pl.py pysim`의 다중 도메인 입력.
7. 에이전트 안내서(`ir_guide.md`, Architect 과제)와 SPEC 작성 템플릿, 설계 문서의 SPEC 규격.

구현하면서 정한 것:
- 하위 모듈의 추가 리셋은 연결한 부모 도메인 추가 리셋의 앞에서부터 같은 극성이어야 합니다 (하위 모듈은 일부만 받을 수 있음).
- 하위 모듈의 두 도메인이 클럭이나 리셋 포트를 함께 쓰면 부모도 같은 넷을 줘야 합니다 (V2). Emitter는 그 핀을 한 번만 냅니다. 블록 안 리셋 동기화는 도메인마다 동기화한 리셋이 따로 나오므로, 하위 모듈은 도메인마다 리셋 포트를 따로 둡니다.
- Block Top의 내부 신호를 인스턴스 입력으로 잇는 연결(동기화 셀 출력 → 인스턴스 입력)을 V5가 신호의 구동으로 잘못 세던 것을 고쳤습니다.
- 표준 셀은 `pipeline/emitter/stubs/`의 모든 `.sv`를 파일 목록에 넣습니다.

### 완료 기준

- 시험용 Block Top(비동기 도메인 둘 + 분주 도메인 하나, 추가 동기 리셋, 블록 안 리셋 동기화, `sync_pulse`, `cdc_fifo`)의 IR이 Validator를 통과하고, verilator lint가 깨끗하고, 방출한 SDC가 SDC 검사를 통과합니다.
- 같은 블록을 Python 조각으로 쓴 판이 번역기 검사를 건너뛰지 않고 통과합니다 (서로 다른 주기 조합 세 가지 이상).
- 기존 시험이 모두 통과합니다 (단일 도메인과 `axis_async_fifo` 결과가 바뀌지 않음).
- 시험용 SPEC에서 L1을 만들면 위 IR의 L1과 같습니다.

결과 (2026-10-16): 시험용 블록은 `tests/fixtures/multiclock`(`mc_top`, `mc_src`, SPEC)입니다. Validator 통과(분주 도메인이 리셋이 다른 sys 레지스터를 읽는 곳에 리셋 도메인 교차 경고 하나), verilator lint 깨끗, SPEC 검사 게이트 통과, SPEC의 L1이 IR의 L1과 같음. 번역기 검사는 반주기 조합 (5, 7), (11, 13), (17, 19)ns에서 통과하고, 셀 RTL이나 분주 클럭을 일부러 틀리게 바꾼 네 경우를 잡습니다. 생성한 하니스로 RTL을 돌린 시험(분주 클럭 에지 정렬, 소프트 리셋 중 `in_ready = 0`)도 통과합니다. SDC는 문자열로만 확인했고 SDC 검사 도구에는 아직 넣지 않았습니다.

---

## P11. 원본 RTL에서 IR과 SPEC 만들기 (2026-10-16 사용자 결정, 2026-10-16 구현)

원본 RTL이 있는 블록을 IR(JSON + SV 또는 Python 조각)로 옮기고 SPEC도 씁니다. P1에서 손으로 한 이식을 흐름으로 만드는 단계입니다. 결과는 golden 블록과 같은 묶음(`ir/*.ir.json`, `spec.md`, 생성 하니스, `golden.json`)이고, 이후 일반 파이프라인에 그대로 넣을 수 있습니다 (예: SPEC을 바꿔 다시 생성). 기존의 차분 시뮬레이션(`run_diffsim`, `minimize_diff`), L1 기반 하니스 생성기, 일반 파이프라인(SPEC → IR → RTL)을 그대로 씁니다.

**입력.** 순서 있는 원본 RTL 파일 목록(Verilog `.v`와 SystemVerilog `.sv`를 섞어도 됨, `-f filelist.f` 형식 허용), top 모듈 이름, 고정할 파라미터 값, include 경로(`-I`), define 값(`-D`). 언어는 확장자로 정하고 `--lang verilog|sv`로 직접 정할 수도 있습니다. 선택으로 원본 테스트벤치와 참고 문서.

### 결정 사항 (설계안)

**1. 단계.**

| 단계 | 하는 일 | 방식 | 통과 조건 |
| --- | --- | --- | --- |
| R1 구조 추출 | pyslang으로 고정한 파라미터에서 elaborate해 모듈 트리, 포트, 파라미터를 뽑습니다. `always_ff`와 Verilog `always @(...)`의 sensitivity와 리셋 분기에서 도메인 후보(클럭, 리셋, 극성, 비동기 여부)를, 이름 규칙에서 valid/ready 번들 후보를 찾습니다. 모듈마다 L1과 Block Top의 instances/connections 초안을 냅니다. Verilog, 헤더와 매크로, `generate`, inout은 아래 2~5번대로 처리합니다 | 결정론 | 초안 L1이 V1~V3 통과. 표현할 수 없는 구문은 목록으로 거부 |
| R2 L1 확정 | 도구로 알 수 없는 것(주파수, 도메인 관계·분주, 프로토콜 태그)과 R1의 검토 항목을 사용자에게 요청합니다. 실행은 이 단계에서 멈추고, 사용자가 답을 넣으면(`port.py answer`) L1에 반영해 이어 갑니다. 에이전트가 추정하지 않습니다 | 사람 | 모든 요청 항목에 답이 있고, 반영한 L1이 V1~V3 통과 |
| R3 동작 이식 | 모듈마다 원본 RTL의 동작을 L2/L3(SV 또는 Python 조각)로 옮깁니다. 모듈 구조(모듈 이름, 계층, 인스턴스 이름, 포트)는 원본과 1:1로 같아야 하고 Architect는 거치지 않습니다. 모듈을 나누거나 합치지 않습니다 | 에이전트 (새 Porter 과제) | Validator 통과, 모듈 트리와 L1이 R2 결과와 같음, R4 |
| R4 이식 등가 판정 | 아래 하위 모듈부터 원본 RTL과 방출 RTL에 생성 하니스로 같은 자극을 넣고 모든 출력 포트를 사이클 단위로 비교합니다. 자극은 DUT 응답과 무관하게 시드로 정해지므로(`diffsim`의 자극 계획) 두 RTL을 따로 돌려도 같은 입력을 받습니다. `diffsim`에 사이클별 출력 기록을 더합니다 (지금은 트랜잭션 순서만 기록). 시드 여럿, `steady`와 `reset` 모드. 불일치는 `minimize_diff`로 줄여 Repair로 넘깁니다. 원본 테스트벤치가 있으면 그것도 돌립니다 | 결정론 + Repair | 모든 모듈의 출력 일치 |
| R5 SPEC 작성 | 2~4절(클럭, 인터페이스, 파라미터)은 L1에서 결정론적으로 만듭니다 (`l1.py`의 역방향). 1·5~10절(목적, 기능 요구사항, 타이밍, 코너 케이스, 시나리오, 구현 제약, 열린 질문)은 에이전트가 원본 RTL과 IR을 읽고 템플릿대로 씁니다 | 결정론 + 에이전트 (새 SpecWriter 과제) | SPEC 검사 게이트 통과, SPEC에서 만든 L1 = IR의 L1 |
| R6 SPEC 충실도 검사 | R5의 SPEC만 입력으로 일반 파이프라인을 돌려 IR을 다시 만들고(재생성 IR), R3의 이식 IR과 비교합니다. 비교 방법은 아래 6번 | 일반 파이프라인 + 결정론 + SpecWriter | Block Top L1이 같고, 재생성 RTL과 원본 RTL의 트랜잭션 차분이 없음 |

**2. Verilog.** pyslang이 Verilog-2001/2005도 읽습니다 (golden 원본 6개 파일이 모두 `.v`이고 오류 없이 elaborate됨, 2026-10-16 확인).

| 항목 | 처리 |
| --- | --- |
| SV 예약어 충돌 (`logic`, `bit`, `int` 등을 이름으로 씀) | `.v` 파일은 `` `begin_keywords "1364-2005" ``로 감싸 읽습니다. IR에서 SV 예약어가 되는 이름은 바꾸고 원래 이름과의 대응을 남깁니다 (V1) |
| `always @(posedge clk or negedge rst_n)`, `always @*`, `always @(a or b)` | `always_ff`/`always_comb`와 같게 다룹니다. 목록이 불완전한 조합 `always @(a)`는 원본과 시뮬레이션이 달라질 수 있어 검토 항목으로 냅니다 |
| `reg`/`wire`/`integer`, non-ANSI 포트 선언 | elaborate 결과의 타입과 포트 목록을 쓰므로 선언 형태와 상관없습니다 |
| `function`/`task` | 조각 안에 풀어 씁니다 (Porter). 지연(`#`)이 있는 `task`는 거부합니다 |
| `defparam` | elaborate 결과에 반영되고, 인스턴스 파라미터 대입으로 옮겼음을 검토 항목으로 표시합니다 |
| 암묵적 넷 | pyslang 경고를 검토 항목으로 냅니다 (오타일 가능성이 높음) |
| R4 원본 시뮬레이션 | 원본 `.v`를 Verilator로 돌립니다 (golden `--baseline`과 같음). 원본의 폭 경고는 원본에만 `-Wno-WIDTH` 같은 옵션으로 완화합니다 |

**3. 헤더와 매크로.** 파일을 두 종류로 나눕니다.

| 종류 | 판단 기준 | 처리 |
| --- | --- | --- |
| 헤더 | 확장자 `.vh`, `.svh`, `.h`, 또는 다른 파일이 `` `include``하는 파일 (확장자가 `.v`여도) | 따로 컴파일하지 않고 include 경로로만 찾습니다. `-I`가 없으면 포함하는 파일의 폴더에서 찾습니다 |
| 컴파일 단위 | 그 밖의 `.v`, `.sv` (매크로만 있는 `.v` 포함) | 받은 목록 순서대로 컴파일 단위 하나로 읽습니다 (pyslang `--single-unit`. 파일마다 따로 읽으면 다른 파일의 매크로를 못 찾음). 상용 시뮬레이터의 기본 동작과 같습니다. 정의보다 먼저 쓰인 매크로가 있으면 옮길 파일을 알려 주고 거부합니다 |

| 원본 | IR 처리 |
| --- | --- |
| 숫자 상수 매크로 (`` `define DW 8``) | 쓰는 모듈마다 같은 이름의 `localparam`(`local: true`)으로 둡니다. 여러 모듈에 생긴 같은 이름의 값이 모두 같은지 R1이 검사합니다 |
| 식이나 코드로 풀리는 매크로 (인자 있는 매크로 포함) | 풀어 쓴 결과로 옮깁니다. Porter에게는 원본과 전처리한 코드를 함께 줍니다 |
| `` `ifdef``/`` `ifndef`` | `generate if`처럼 받은 define 값으로 갈래를 고르고, IR은 define 조합마다 따로 둡니다 |
| 헤더 안의 `parameter`, `localparam` (모듈 안에서 include) | 그 모듈의 파라미터로 옮깁니다. 바꿀 수 있는지는 elaborate 결과를 따릅니다 (SV에서 `#(...)`이 있는 모듈의 본문 `parameter`는 localparam) |
| 파일 맨 위(`$unit`)의 `parameter`, SV `package` 상수 | 쓰는 모듈마다 `localparam`으로 복사하고 값 일치를 검사합니다 (IR에는 package가 없음) |
| `package`/`.svh`의 `typedef enum` | FSM 상태 타입이면 `fsm`으로 옮깁니다 |
| 다른 `typedef` (struct, union) | 포트에 쓰이면 거부합니다. 내부 신호면 같은 폭의 비트 벡터로 풀고 검토 항목으로 남깁니다 |
| `` `timescale``, `` `default_nettype``, `` `celldefine`` 등 | 무시합니다 |

SPEC 4절에는 바꿀 수 있는 top 파라미터만 넣고, 헤더에서 온 상수는 9절 "구현 제약"에 이름과 값을 적습니다. define 조합은 1절의 범위에 적습니다 (예: "`` `USE_PARITY`` 정의 설정"). `golden.json`에는 파일 목록과 순서, 헤더, include 경로, define 값, 파라미터 값을 남겨 R4와 이후 재현에서 원본을 같은 조건으로 돌립니다 (Verilator `+incdir+`, `+define+`).

**4. `generate`.** R1이 고정한 파라미터로 펼칩니다. 스키마는 바꾸지 않습니다.

| 원본 형태 | 처리 |
| --- | --- |
| `generate if` (파라미터에 따라 구조가 바뀜) | elaborate에서 고른 갈래만 IR로 냅니다. 설정마다 IR을 따로 둡니다 (P1 방식). 설정으로만 쓰이던 파라미터는 펼치면서 지웁니다 |
| `generate for`로 인스턴스 복제 | 인스턴스 N개(`u_x_0` … `u_x_{N-1}`)와 연결로 펼칩니다 |
| `generate for`로 로직 복제 | 조각 안의 `for` 루프로 옮깁니다 (SV, Python 조각 모두 지원) |

파라미터가 살아 있는 IR이 필요해지면 인스턴스에 `"if": "<파라미터 식>"`과 `"array": {"var", "count"}`를 더하는 스키마 확장을 따로 정합니다. 그때는 Validator, pysim, 번역기 검사가 파라미터 범위에서 나열한 설정마다 돌아야 합니다.

**5. inout.** 하위 모듈 안쪽은 세 신호로 나누고, Block Top 경계에서만 inout을 허용합니다.

| 위치 | 바뀌는 것 |
| --- | --- |
| R1 | 원본의 `assign x = oe ? o : 'z;` 형태를 알아보고, 하위 모듈 포트를 `x_o`, `x_oe`, `x_i`로 나눕니다 |
| 스키마 | 포트 방향에 `inout`을 더하되 role `block_top`에서만 허용합니다. Block Top에 둘 수 있는 블록에 `tristate {pad, o, oe, i, style: "push_pull" \| "open_drain"}`를 더합니다 |
| Validator | inout 포트는 `tristate` 블록 하나만 구동하고 읽습니다 (V5, V8). `o`와 `oe`는 같은 도메인입니다. `i`는 비동기 입력으로 보고 `cdc_sync`를 거쳐 읽습니다. 원본이 플롭 하나로 바로 받으면 동기 입력 선언을 허용합니다 (P1 `uart_rx` 우회) |
| Emitter | `assign pad = oe ? o : 'z; assign i = pad;` (open drain은 0만 구동). 외부 인터페이스가 원본과 같아 그 자리에 바꿔 끼울 수 있습니다 |
| pysim | `tristate`를 `i = oe ? o : 외부값`(open drain은 풀업 포함)으로 실행합니다 |
| 하니스와 R4 | 하니스 쪽에 패드 모델(외부 드라이버, 외부 enable, 풀업)을 두고 패드의 해석값을 비교합니다. 양쪽이 동시에 구동하는 충돌(X)도 차이로 셉니다. 패드 모델을 감싼 SV 래퍼를 top으로 둡니다. Verilator의 3상태 처리를 먼저 확인하고, 안 되면 이 비교만 iverilog로 돌립니다 |
| SPEC | 인터페이스 표의 방향에 `inout`을 허용합니다 (`l1.py`, SPEC 검사) |

하위 모듈 사이를 오가는 3상태 버스는 넣지 않습니다. 구동 하나 규칙(V5)이 깨지고 pysim이 Z와 X를 해석해야 하기 때문입니다.

**6. R6 SPEC 충실도 검사 (재생성 IR과 이식 IR 비교).** SPEC이 원본의 동작을 빠짐없이 정했다면, SPEC만으로 다시 만든 설계가 원본과 같은 일을 해야 합니다.

| 비교 | 방법 | 차이가 나면 |
| --- | --- | --- |
| Block Top L1 | 재생성 IR과 이식 IR의 L1(포트, 파라미터, 도메인, 번들)이 같아야 합니다. R5 게이트로 이미 같으므로 확인만 합니다 | 생성기 버그 |
| 동작 | 재생성 RTL과 원본 RTL에 `diffsim`(트랜잭션 순서 비교, `steady`와 `reset`, 시드 여럿)을 돌립니다. 지연이 달라도 SPEC을 지키는 설계는 같은 출력 순서를 내므로 트랜잭션 단위로 봅니다. 차이는 `minimize_diff`로 줄입니다 | 아래 판정 |
| 타이밍 | 같은 실행에서 트랜잭션별 지연과 처리율을 양쪽에서 재어 표로 냅니다 | SPEC 6절에 적힌 값과 어긋나면 차이로 셈. 적히지 않은 차이는 SpecWriter에 검토 항목으로 넘깁니다 (원본 지연이 요구사항이면 6절에 적게) |
| 구조 | 재생성 IR의 모듈 트리는 Architect가 정하므로 원본과 다를 수 있습니다. 모듈 수, 인스턴스, 하위 모듈 경계를 리포트로만 냅니다 | 판정하지 않음 |
| 덤 | 재생성 실행에서 Verifier가 SPEC만 보고 쓴 테스트를 원본 RTL에도 돌립니다 | 실패하면 동작 차이와 같은 판정 |

동작 차이의 판정은 Arbiter가 합니다. 줄인 자극, 양쪽 출력, SPEC을 보고 둘 중 하나로 정합니다.
- **SPEC 누락·모호:** SPEC이 그 동작을 정하지 않았거나 두 해석을 허용합니다. SpecWriter에 줄인 자극과 원본 출력을 근거로 넘겨 SPEC을 고치고, R5 게이트부터 다시 돕니다.
- **재생성 결함:** SPEC이 분명히 정했는데 재생성 설계가 어겼습니다. SPEC은 그대로 두고 일반 파이프라인 결함으로 리포트합니다 (P11의 판정에는 넣지 않음).

SPEC 수정은 최대 3회(설정 가능)까지 돌고, 남은 차이는 사람이 검토합니다. 재생성은 일반 파이프라인 한 번(Architect, Author 후보, Verifier, Repair)만큼 비용이 듭니다.

**7. 이식 IR과 재생성 IR의 쓰임.** 묶음의 정본은 원본과 1:1인 이식 IR입니다. 재생성 IR은 R6 판정에만 쓰고 묶음에는 리포트로만 남깁니다.

**R1 거부 목록.** SV interface와 struct·union 포트, 래치, 하위 모듈 사이의 3상태 버스, 펼친 뒤에도 남는 구문(예: 파라미터가 아닌 값에 따른 구조), 지연이 있는 `task`, 정의보다 먼저 쓰인 매크로(파일 순서 오류).

**원본과 IR 규칙이 맞지 않는 곳.** 리셋 없는 플롭, 한 `always`에 리셋 있는 레지스터와 없는 레지스터가 섞인 경우, 비표준 동기화 플롭은 P1처럼 동작이 조금 바뀔 수 있습니다 (P1 우회 표현 2, 7, 8). R4에 리셋 직후 사이클의 차이를 허용하는 규칙을 두고, 바뀐 점은 가정 목록에 남겨 사람이 검토합니다.

**하지 않는 것:** 형식 등가 검사(yosys는 SV 지원이 부족하고 다중 클럭이 어려워 시뮬레이션 차분으로 대신함), 인스턴스 `if`/`array` 스키마 확장, 하위 모듈의 inout, 부서와 Web UI 연결(1차는 CLI만).

### 결정 (2026-10-16, 사용자)

1. **모듈 구조는 원본과 1:1로 같게 만듭니다.** R3 종료 게이트가 모듈 이름, 계층, 인스턴스 이름, 포트를 R2 결과와 대조합니다.
2. **R2는 사용자에게 요청합니다.** 에이전트가 추정하지 않습니다.
3. **R6은 SpecWriter가 쓴 SPEC으로 IR을 다시 만들어 이식 IR과 비교합니다** (위 6번).

### 작업

1. R1 추출기 (`pipeline/importer/`): 파일 목록(`-f`, `-I`, `-D`, `--lang`), 헤더 판별, 컴파일 단위 하나로 읽기, Verilog 예약어 처리, elaborate, 모듈 트리, 포트, 파라미터, 도메인 후보(`always_ff`와 `always @`), 번들 후보, 매크로·`$unit`·package 상수를 `localparam`으로 옮기고 모듈 사이 값 일치 검사, `` `ifdef``와 `generate` 펼치기, typedef 처리, 3상태 분리, 거부 목록, 검토 항목, `golden.json`의 원본 재현 정보.
2. R2 요청과 답: 요청 항목 파일(`r2_request.json`), `port.py answer`로 답 넣기, 답을 L1에 반영하고 V1~V3 재검사.
3. 스키마와 툴의 inout: 포트 방향 `inout`(`block_top`만), `tristate` 블록, Validator, Emitter, pysim, 하니스 패드 모델, SPEC 인터페이스 표.
4. Porter 과제 (`pipeline/agents/porter.py`): 원본 모듈과 확정한 L1을 받아 L2/L3을 씁니다. 종료 게이트는 Validator 통과, L1 불변, 모듈 트리가 원본과 1:1.
5. R4 이식 등가 판정: `diffsim`에 사이클별 출력 기록 추가, 원본 RTL과 방출 RTL의 사이클 비교, 모듈별 bottom-up, 리셋 직후 차이 허용 규칙, 불일치를 Repair로 넘기기.
6. R5 SPEC: L1 → SPEC 2~4절 생성기, SpecWriter 과제 (`pipeline/agents/spec_writer.py`), SPEC 검사 게이트와 L1 왕복 검사.
7. R6: SPEC으로 일반 파이프라인 실행, 재생성 RTL과 원본 RTL의 트랜잭션 차분과 지연·처리율 표, 구조 리포트, Verifier 테스트를 원본에 돌리기, Arbiter 판정(SPEC 누락 / 재생성 결함), SpecWriter 수정 과제와 반복 상한.
8. 실행기 (`pipeline/scheduler/port.py`): 단계 진행, R2에서 멈추고 이어 가기, 재시도, 예산(기존 `Run`의 Repair와 예산 관리 재사용), 리포트, golden 묶음 쓰기.
9. 에이전트 안내서, 설계 문서, SPEC 작성 템플릿.

### 완료 기준

- golden 4블록(`uart`, `axis_cobs_decode`, `axis_adapter`, `axis_async_fifo`)을 이 흐름으로 다시 만들고, 결과 IR의 모듈 트리가 원본과 같고, 원본과 R4 등가이고, 원본 테스트벤치 49개를 통과합니다.
- 만든 SPEC이 SPEC 검사 게이트를 통과하고, SPEC에서 만든 L1이 IR의 L1과 같습니다. golden 4블록 모두 R6에서 재생성 RTL과 원본 RTL의 트랜잭션 차분이 없습니다 (SPEC 수정 반복 상한 안에서).
- SPEC에서 요구사항 하나를 일부러 지우면 R6이 차이를 찾고 Arbiter가 SPEC 누락으로 판정해 SpecWriter가 되살립니다.
- `generate if`/`generate for`가 있는 원본(golden `axis_adapter` 원본의 다른 설정 포함)과 Block Top 경계에 inout이 있는 시험용 원본(push-pull, open drain 각각)이 R1을 통과하고 R4에서 원본과 같습니다.
- 시험용 Verilog·SV 혼합 원본(`.vh` 헤더의 parameter와 localparam, 매크로만 있는 `.v`, `` `ifdef``, package 상수와 enum, SV 예약어를 이름으로 쓴 Verilog, non-ANSI 포트, `always @(...)`, `defparam`)이 define 조합 두 가지에서 R1을 통과하고 R4에서 원본과 같습니다. golden 4블록이 모두 Verilog 원본이므로 첫 기준도 Verilog 경로를 확인합니다.
- 가짜 에이전트 시험으로 단계 전환, R2에서 멈추고 이어 가기, 거부 목록, R4 불일치에서 Repair로, R6 차이에서 SpecWriter로 넘기는 경로를 확인합니다.

### 진행 상황 (2026-10-16)

| 작업 | 상태 | 위치 |
| --- | --- | --- |
| 1. R1 추출기 | 완료 | `pipeline/importer/sources.py`(파일 목록, 헤더, 컴파일 단위, 매크로, 원문), `extract.py`(L1·L0 초안, 거부, 검토), `r1.py`(CLI, 결과 파일) |
| 2. R2 요청과 답 | 완료 | `pipeline/importer/r2.py` |
| 3. inout | 완료 (pysim 제외) | 스키마 `block_tristate`, `model.py`, Validator V2/V5/V8/V11, Emitter, `tb/gen.py`의 패드 모델 |
| 4. Porter 과제 | 완료 | `pipeline/agents/porter.py`, 도구 `pl.py porteq` |
| 5. R4 | 완료 | `pipeline/importer/r4.py`, `diffsim`의 `cycle_trace`/`fixed` |
| 6. R5 | 완료 | `pipeline/importer/spec_tables.py`, `pipeline/agents/spec_writer.py`, 도구 `pl.py spec_gate` |
| 7. R6 | 완료 | `pipeline/importer/r6.py`, `arbiter.make_r6_task`, `minimize_diff`의 `sim_fn` |
| 8. 실행기 | 완료 | `pipeline/scheduler/port.py` (`start`, `answer`, `resume`, `status`) |
| 9. 문서 | 완료 | `ir_guide.md`, 설계 문서(role, 블록 종류, v1 제외 항목), SPEC 작성 템플릿, 역할 설정(`porter`, `spec_writer`, `porter_fix`, `spec_revise`) |

```bash
python3 -m pipeline.scheduler.port start <파일...> --top <모듈> [-f files.f] [-D N=V] [-G N=V] --run-dir <dir>
python3 -m pipeline.scheduler.port answer --run-dir <dir> freq.sys=100 proto.<모듈>.<번들>=valid_ready review.1=ok ...
python3 -m pipeline.scheduler.port resume --run-dir <dir> [--until r4|r5|r6]
```

구현하면서 정한 것:
- **Verilog 키워드.** Verilog 파일은 먼저 그대로(SV 문법으로) 읽고, 오류가 날 때만 1364-2005 키워드로 감쌉니다. 감싸면 Verilator가 `$error` 같은 SV 시스템 태스크를 거부하기 때문입니다. 감싼 파일은 `import.json`의 `wrapped`에 남기고 R4/R6의 원본 시뮬레이션도 같게 감쌉니다.
- **리셋 찾기.** 동기 리셋은 always 블록의 첫 문장 `if (rst) 상수 else ...`뿐 아니라 끝 문장 `if (rst) 상수`(끝에서 덮어쓰는 Verilog 관행, verilog-axis 전부)도 봅니다. 같은 리셋 포트를 동기와 비동기로 함께 쓰면 많이 쓴 쪽으로 합치고 검토 항목으로 남깁니다 (`axis_async_fifo`: 리셋 동기화 플롭만 비동기).
- **하위 모듈 파라미터 기본값.** 인스턴스에서 elaborate한 값(고정한 설정)을 기본값으로 둡니다. 펼친 `generate` 구조와 맞고, 모듈 단위 R4에서 원본에 같은 값을 줄 수 있습니다. 인스턴스 대입은 원문 식(부모 파라미터)을 쓰고, genvar나 `defparam`으로 정해진 값은 숫자로 씁니다.
- **R1이 더 거부하는 것.** 블록 안에서 만든 클럭, 하위 모듈 리셋에 내부 신호를 연결한 것(아래 리셋 동기화기 패턴 제외), 하강 에지 클럭(P12부터는 하강 에지 도메인으로 옮김), 인스턴스 배열. 처음에는 조합 모듈과 리셋 없는 도메인도 거부했으나 아래 추가 작업으로 옮길 수 있게 함.
- **원문.** pyslang 위치는 바이트 단위라 원문을 바이트로 잘라 읽습니다 (한글 주석이 있으면 문자열 인덱스와 다름).
- **R4 비교 범위.** DUT가 내는 번들의 data는 valid=1인 사이클만 비교합니다 (`strict`면 모두). 리셋 구간과 풀린 뒤 4사이클 안의 차이만 있으면 `reset_only`로 통과시키고 검토 항목으로 남깁니다. R4 불일치는 Repair 대신 Porter 고치기 과제(`prev.ir.json`과 첫 차이)로 넘깁니다. Porter는 SPEC 없이 원본만 보므로 기존 Repair 과제(SPEC과 실패 리포트 기반)가 맞지 않습니다.
- **inout 패드 모델.** 하니스는 inout 포트 P를 `P_pad_o`, `P_pad_oe`(바깥 장치), `P_pad_val`(패드 값) 세 포트로 바꾼 래퍼를 top으로 씁니다. 바깥 장치는 약한 구동(weak)이라 DUT가 구동하면 DUT 값이 이기고, 패드마다 풀업이 있습니다 (Verilator의 세기 해석으로 확인). 그래서 동시 구동 충돌은 따로 세지 않습니다. 포트를 나눈 하위 모듈은 이식 IR을 다시 inout으로 합치는 래퍼로 감싸 원본과 모듈 단위로 비교합니다.
- **`tristate`의 i**는 `domain`의 신호로 봅니다 (원본이 패드 입력을 동기 입력으로 쓰는 관행). pysim은 `tristate`를 실행하지 않습니다. `tristate`는 Block Top(SV)에만 있어 Python 조각 모듈의 번역기 검사와 관계없습니다.
- **R5 왕복 비교**에서 role(이식 IR의 top은 role module일 수 있음)과 파라미터 허용 범위(SpecWriter가 채움)는 뺍니다.
- **R6 판정**은 Arbiter의 새 과제(`spec_gap` / `regen_defect`)이고, 재생성 실행의 Verifier 테스트를 원본 RTL에도 돌려 실패하면 같은 판정에 넘깁니다.
- 래퍼 모듈 이름은 `<모듈>_ref_top`, `<모듈>_pad_top`입니다 (Verilator가 `__`를 바꿔 씀). 원본 포트 이름이 SV 예약어면 래퍼에서 이스케이프 식별자로 잇습니다.
- **남은 것.** 하강 에지 클럭, 패드 동시 구동 충돌 검출(4-state 시뮬레이터 필요), pysim의 `tristate` 실행은 보류했습니다 (2026-10-16 사용자 결정). 세 가지 모두 P12에서 구현했습니다 (충돌 검출은 4-state 시뮬레이터 없이).

**추가 작업: 제약 해소 (2026-10-16 사용자 결정, 같은 날 구현).**

| 항목 | 방법 | 위치 |
| --- | --- | --- |
| 클럭이 없는 조합 모듈 | `domains: []` 모듈을 허용. 그 모듈의 null 도메인 입력은 비동기가 아니라 부모가 정하는 도메인(V8). 부모에서 출력 넷은 연결한 입력 넷의 도메인을 물려받고(V8 고정점), 연결 도메인 검사(V10)는 이 핀을 보지 않음. R1은 부모의 포트 도메인 계산에서 조합 하위 모듈을 조합 경로로 봄. 모듈 단위 R4는 가상 클럭(`tb_comb_clk`, 리셋 없음) 하나를 둔 하니스 뷰(`comb_view`)와 래퍼로 입력을 바꿔 가며 비교 | `validator/structure.py`, `importer/extract.py`, `tb/gen.py`, `importer/r4.py` |
| 리셋이 없는 도메인 | 도메인의 `reset`을 뺄 수 있음(스키마). 레지스터 리셋값과 `fsm` 금지, 리셋 있는 자식 도메인을 리셋 없는 부모 도메인에 잇기 금지(V2, V7), 받는 쪽 리셋이 없는 동기화 셀은 경고. Emitter는 리셋 분기 없이, 셀 리셋은 1로. 하니스, pysim, 번역기 검사, SPEC 2절("없음") 왕복 | 스키마, `model.py`(`reset_of`), Validator, Emitter, SDC, `tb/gen.py`, `tb/lib.py`, `pysim.py`, `pyequiv_tb.py`, `spec/l1.py`, `spec_tables.py` |
| top의 리셋 동기화기 | R1이 top 안에서 입력 포트 리셋으로 비동기 리셋되고 리셋 아닌 갈래가 자기 레지스터와 상수만 읽는(시프트 체인) 블록을 찾고, 그 출력(또는 그대로 옮긴 wire)이 하위 모듈 리셋으로만 쓰이면 `reset.synchronize` 도메인으로 옮김. 2단이 아니면 검토 항목. top이 아닌 모듈에 있으면 거부 | `importer/extract.py` |
| generate 배열 안 인스턴스의 `defparam` | R1이 원본 시뮬레이션 사본(`r1/sim_src/`)을 만듦: `defparam`을 지우고 대상 인스턴스의 파라미터를 배열 원소마다 elaborate한 값을 고르는 조건식으로 바꿈. R4, R6, `porteq`가 사본을 씀. IR 추출에는 쓰지 않음 | `importer/simcopy.py` |
| 일반 파이프라인의 inout Block Top | 하니스 설정과 신호 폭이 inout을 패드 모델 포트로 바꿔 읽고, E2와 차분 시뮬레이션이 패드 래퍼(`pad_top`)를 top으로 씀. 트레이스는 래퍼 포트만. Verifier 과제에 패드 포트 안내, Architect 과제에 `tristate`와 하위 모듈 포트 나누기 규칙. 이제 inout 블록도 R6을 돌릴 수 있음 | `tb/gen.py`, `scheduler/run.py`, `agents/verifier.py`, `agents/architect.py` |

결과: 시험용 원본 `tests/fixtures/port/comb`(조합 모듈 + 리셋 없는 파이프라인)와 `rsync`(top의 2단 리셋 동기화기)가 R1, Validator, R4 `equal`을 통과하고, 조합 로직을 틀리게 바꾸면 R4가 잡습니다. 혼합 원본에 generate 배열 안 `defparam`을 다시 넣었고 두 define 조합 모두 R4 `equal`입니다. 생성 하니스와 패드 래퍼로 inout Block Top(`od_top`)을 cocotb 테스트와 `run_diffsim`으로 돌렸습니다.

결과 (2026-10-16, 에이전트 없이):
- **R1:** golden 4블록 원본에서 뽑은 L1이 P1에서 손으로 옮긴 golden IR의 L1과 같습니다. 다른 점은 R2에서 채우는 주파수와 도메인 관계, 쓰이지 않아 지운 `PAUSE_ENABLE`, 원문 그대로 둔 폭 식(`$clog2(DEPTH)`)뿐입니다. `axis_adapter` 원본의 upsize 설정(8 → 32비트, 다른 `generate` 갈래)도 R1과 V1~V3를 통과합니다.
- **R4:** golden IR을 이식 IR 자리에 두면 `axis_adapter`와 `axis_cobs_decode`는 사이클 단위로 같고, `uart`는 리셋 구간에서만 다릅니다(`txd` 초기값). `axis_async_fifo`는 중간 리셋 모드에서 `m_status_depth`가 다릅니다. P1 우회 표현 7, 8(리셋 동기화 플롭을 바꿈) 때문이고, 트랜잭션 순서는 같습니다.
- **시험용 원본:** Verilog·SV 혼합 원본(`tests/fixtures/port/mixed`)은 define 조합 두 가지 모두에서 손으로 옮긴 IR이 R4 `equal`입니다. 다른 조합의 IR을 넣으면 다르다고 판정합니다. inout 원본(`tests/fixtures/port/inout`)은 push-pull(하위 모듈에서 구동)과 open-drain(top에서 구동) 모두 R4 `equal`입니다. 일부러 틀리게 바꾼 IR은 R4에서 잡힙니다.
- **R6:** golden IR을 재생성 IR 자리에 두면 같고, 틀리게 바꾸면 차이를 찾아 자극을 줄이고 Arbiter에 넘길 입력을 만듭니다.
- **실행기:** 가짜 에이전트로 start → R2 대기 → answer → Porter(첫 시도 틀림 → R4 차이 → 고치기) → SpecWriter → R6(1차 차이 → spec_gap → SPEC 고치기 → 2차 같음) → 묶음을 확인했습니다.
- 시험: `tests/test_port.py` 17개, `tests/sim/test_port_sim.py` 8개.
- **아직 하지 않은 것:** 실제 에이전트로 golden 4블록을 다시 만드는 완료 기준 1~3과, upsize 설정의 R4 확인입니다. 일반 파이프라인을 블록마다 한 번 더 돌리므로 비용이 큽니다.

### 실제 에이전트 실행 (2026-10-17, `axis_cobs_decode`)

Claude Code CLI, 모든 역할 `claude-haiku-5-5`, effort medium (`config/roles.port-haiku.json`, `experiments/p0/local/agents.claude-haiku.json`). 블록 하나씩 진행하기로 하고 `axis_cobs_decode`부터 돌렸습니다. R2 답(주파수 100 MHz, 두 번들 모두 valid_ready, 검토 항목 ok)과 R6 재생성 중 Design-lead 결정(매번 "Architect 재구성"), 예산(60 → 120 → 160회)은 사용자가 정했습니다.

| 단계 | 결과 |
| --- | --- |
| R1, R2 | 통과 |
| R3, R4 | Porter 2회(첫 시도 뒤 고치기 1회)로 원본과 사이클 단위로 같음. 이식 IR이 원본 테스트벤치 16/16 통과 |
| R5 | SpecWriter가 SPEC v0을 쓰고 게이트 통과 (1회) |
| R6 | 4회차(SPEC v3) 재생성이 Design-lead 결정 대기에서 멈춘 상태로 중단. 원본과 같아지지 않음 (아래 "보강 뒤 실제 에이전트 실행"에서 이어 가 회차 5에 원본과 같아짐) |

| R6 회차 | SPEC | 재생성 | 원본과 비교 | 판정 |
| --- | --- | --- | --- | --- |
| 0 | v0 | 모듈 경계 충돌 → 재구성 → Repair 진동으로 실패 | Verifier 테스트 2벌이 원본에서도 실패 (`tlast` 끝 항목 시점, 빈 사이클 뒤 `s_axis_tready` 변화, 255 코드 뒤 종료 플래그) | spec_gap |
| 1 | v1 | 계약 충돌 → 재구성 → 통과 (하위 모듈 하나) | 출력 644개와 시점은 같음. 보관 바이트 없는 오류 출력의 `tdata`만 다름 (원본 133, 재생성 0) | spec_gap |
| 2 | v2 | 재구성 뒤 Repair 실패와 진동 | Verifier가 코드 자리 바이트의 `tlast` 오류를 원본과 다르게 읽음 | spec_gap |
| 3 | v3 | 하위 모듈 둘 사이 계약 충돌(1사이클 늦음, `s_axis_tready`)로 결정 대기 | – | (중단) |

에이전트 실행 147회 중 R3~R5는 3회, R6 판정과 SPEC 고치기는 6회이고 나머지 약 138회가 일반 파이프라인 재생성입니다.

판단:
- **R6은 의도대로 동작합니다.** SPEC이 원본과 어긋난 곳을 회차마다 찾았고, SpecWriter가 이를 받아 SPEC을 고쳤습니다. v1부터 정상 경로의 동작과 타이밍은 원본과 같습니다.
- **비용은 거의 재생성의 Architect 분할에서 나왔습니다.** Architect가 작은 블록을 하위 모듈 둘로 나눌 때마다 모듈 사이 계약(스트림의 의미, 레지스터 단 수)이 어긋나 사람 결정과 재구성이 필요했습니다 (4회 중 4회). 하위 모듈 하나로 둔 1회차는 통과했습니다.
- **Haiku의 SPEC은 오류 경로에서 약했습니다.** 정상 경로는 v1에서 맞았지만, 오류로 끝나는 프레임의 세부는 회차마다 한 군데씩 드러났습니다.

실행하면서 고친 것:
- **실행기 재개.** R6 재생성이 결정 대기(`waiting_approval`), 한도(`paused_quota`), 예산 초과로 멈추면 실패로 보지 않고 `waiting_regen`으로 두었다가 `resume`에서 같은 재생성 실행을 이어 갑니다. 판정 직전에 멈췄으면 재생성을 다시 돌리지 않습니다.
- **재생성 실패도 판정으로.** 재생성이 실패해도 그 실행의 Verifier 테스트가 원본에서 실패하면 SPEC이 원본과 다르게 읽힌다는 증거이므로 Arbiter 판정에 넘깁니다.
- `approve list`가 묶음(list의 list) 요구사항에서 멈추던 버그, `porteq` 길이를 R4와 같게(1500사이클).

이 결과로 정한 보강 (2026-10-17 사용자 결정, 같은 날 구현. 4회차는 진행하지 않음):

| 보강 | 방법 | 위치 |
| --- | --- | --- |
| 1. R6 재생성에서 분할 막기 | R6 재생성 실행은 원본 모듈 수(top 빼고, 적어도 1)를 하위 모듈 수 상한(`max_modules`)으로 받습니다. Architect 과제에 상한 규칙을 넣고 게이트가 넘는 것을 거부합니다. 상한은 실행 상태에 남아 재구성과 이어 가기에도 같습니다. 원본이 모듈 하나면 재생성도 하위 모듈 하나입니다 | `scheduler/port.py`(`_regen`), `scheduler/run.py`, `agents/architect.py` |
| 2. 모듈 사이 계약 | Architect가 하위 모듈끼리 잇는 연결마다 `out/contracts.json`에 계약을 씁니다: `carries`(필드마다 실어 나르는 값의 뜻, 끝 표시와 오류 플래그가 언제 1인지)와 `timing`(Block Top 입력에서 몇 사이클 뒤인지, ready의 조합 경로, 보관 개수). 계약이 없거나 비면 게이트가 거부합니다. 스케줄러가 그 연결의 양쪽 모듈 Author(`contracts.json`)와 Repair(`block.json`의 `contracts`)에게 주고, 재구성 과제는 지금 계약을 받아 다시 씁니다. 지금까지 Author는 형제 모듈의 계약을 전혀 보지 못하고 SPEC만으로 경계를 따로 해석했습니다 | `agents/architect.py`, `agents/author.py`, `agents/repair.py`, `scheduler/run.py` |
| 3. SpecWriter가 원본으로 확인 | SpecWriter 작업 디렉터리에 원본 묶음(`ref/`)을 두고 도구 `pl.py refsim`을 줍니다. 정한 입력 트랜잭션(선택으로 사이클별 valid, ready, 단독 입력)을 원본 RTL에 넣고 출력 트랜잭션과 사이클(리셋이 풀린 뒤 0부터)을 냅니다. 과제는 7절의 오류 입력과 8절의 코너 시나리오 기대 출력을 `refsim`으로 원본에서 얻은 값으로 쓰게 하고, R6 고치기에서는 판정 사례와 비슷한 경우를 더 돌려 규칙인지 확인하게 합니다. 처음 제안(R4 무작위 자극 결과를 입력으로)보다 원하는 오류 경우를 바로 넣어 볼 수 있어 이쪽으로 했습니다 | `agents/pl.py`(`refsim`), `agents/spec_writer.py`, `scheduler/port.py` |

확인: `refsim`으로 원본에 1회차 사례(코드 자리가 아닌 곳의 `tlast`)를 넣으면 원본이 직전 바이트(133)를 오류 출력의 data로 내는 것이 보입니다. 이 사례와 R6 실행기 경로(재생성이 결정을 기다리다 멈춘 뒤 이어 가기, 재생성이 실패했지만 Verifier 테스트가 원본에서 실패해 판정으로 넘어가기), Architect 계약과 상한 게이트를 시험으로 더했습니다. 보강한 흐름으로 실제 에이전트를 다시 돌리지는 않았습니다.

### 보강 뒤 실제 에이전트 실행 (2026-10-17~18, `axis_cobs_decode` 이어서)

같은 실행 디렉터리에서 R6을 이어 갔습니다. 모든 역할을 `claude-haiku-5-5` effort high로 바꿨습니다 (`config/roles.port-haiku-high.json`, `experiments/p0/local/agents.claude-haiku-high.json`, 예산 220회). SPEC 고치기 상한(3 → 4 → 5)과 회차 진행은 사용자가 정했습니다.

| R6 회차 | SPEC | 재생성 | 원본과 비교 | 판정 |
| --- | --- | --- | --- | --- |
| 3 (이어 감) | v3 | 하위 모듈 상한 1을 실행 상태에 넣고 재구성 → 하위 모듈 하나로 합쳐짐 → 두 후보 Repair 진동 | Verifier 테스트가 원본에서 실패: SPEC이 "리셋은 R을 바꾸지 않는다"고 원본과 반대로 적음, 코드 자리 바이트 뒤 오류 출력 | spec_gap (상한 3에 걸려 멈춤) |
| 4 | v4 (처음으로 `refsim` 사용) | 하위 모듈 하나, 두 후보 Repair 2회 뒤 진동 | 리셋과 R 규칙은 맞았지만, 원본이 내는 `tdata=0, tlast=1, tuser=1` 출력을 테스트가 0 데이터로 기대 (3·4회차와 같은 자리) | spec_gap |
| 5 | v5 (실패한 테스트의 자극을 원본에 다시 넣음) | Repair 없이 통과, 두 후보 차분 시뮬레이션 차이 없음 | Verifier 테스트 2벌 모두 원본에서 통과. 출력 644개, 첫 출력 사이클 69, 지연 차이 0으로 같음 | – (`done / pass`) |

에이전트 실행은 모두 193회입니다 (회차 3 이어 가기 29회, 회차 4 11회, 회차 5 6회).

판단:
- **SpecWriter가 실패한 테스트의 자극을 그대로 받자 끝났습니다.** 판정자와 SpecWriter는 "사이클 21에서 다름" 같은 메시지만 받아 그 출력을 낸 입력을 알 수 없었고, `refsim`이 있어도 같은 경우를 원본에 넣어 보지 못했습니다. v5는 이 자극을 돌려 대기 0 바이트가 있을 때 오류가 나면 0 바이트가 `tdata=0, tlast=1, tuser=1` 오류 출력 하나가 된다는 규칙(EX-14)을 찾았고, 리셋 사이클의 출력 전달(FR-RST-03/04)도 정했습니다.
- **하위 모듈 상한은 효과가 있었습니다.** 4회차부터 모듈 경계 충돌과 사람 결정이 없었습니다. 남은 실패(Repair 진동)는 SPEC이 원본과 다를 때 Verifier와 Author가 서로 다르게 읽어서 생겼고, SPEC이 맞자 사라졌습니다.
- Haiku high는 에이전트 한 번에 10분 안팎으로 medium보다 꽤 느립니다.

이번에 고친 것:
- **고치기 상한을 올려 이어 가기.** spec_gap 판정 뒤 SPEC을 고치기 전에 멈춘 실행(상한, SpecWriter 실패)을 이어 가면, 다시 만들기 전에 그 판정으로 SPEC부터 고칩니다. 전에는 고치지 않은 SPEC으로 다시 만들었습니다 (`scheduler/port.py` `_revise_spec`).
- **테스트 자극 기록.** 하니스가 테스트마다 넣은 자극(받힌 입력 트랜잭션, 사이클별 valid/ready/단독 입력, 도메인별 리셋)을 `refsim` 형식으로 결과 파일(`stimulus`)에 남깁니다. 리셋으로 버려져 받히지 않은 전송의 valid는 0으로 고칩니다 (`tb/lib.py`).
- **`refsim` 도중 리셋.** 자극에 `reset`(도메인별 사이클 값)을 넣을 수 있고, 리셋 사이클에도 기록된 valid를 냅니다 (`agents/pl.py`, `tb/diffsim.py`의 `valid_in_reset`). 5회차 Verifier 테스트 26개의 기록을 `refsim`으로 다시 넣으면 원본이 그 테스트에서 낸 출력이 모두 그대로 나옵니다.
- **SPEC 고치기에 자극 넘기기.** `verifier_on_ref`가 원본에서 실패한 테스트마다 자극 파일을 남기고, SpecWriter 고치기 과제가 `r6_stim/`으로 받아 `refsim`으로 돌려 보게 합니다 (`importer/r6.py`, `agents/spec_writer.py`).

---

## P12. v1 제외 항목 확장 (2026-10-19 사용자 지시, 같은 날 구현)

사용자 결정으로 보류·제외했던 항목 가운데 VC SpyGlass(E3)를 뺀 다섯 가지를 구현했습니다. generate, 하위 모듈 사이의 3상태 버스, 의도적 래치는 이번 범위가 아닙니다.

| 항목 | 방법 | 위치 |
| --- | --- | --- |
| 하강 에지 도메인 | 스키마에 있던 `edge: neg`를 끝까지 씁니다. V2: 분주 관계(`div` > 1), 동기화 셀, `reset.synchronize`는 상승 에지 도메인에만 두고, `domain_map`은 에지가 같은 도메인끼리 잇습니다. 하니스와 차분 시뮬레이션은 그 도메인의 입력을 상승 에지에서 바꾸고 표본을 읽습니다(설정 `neg_edge`). 설계 모델 실행기는 하강 에지 시각에 그 도메인을 진행합니다(`schedule`의 `falling`). R1은 `negedge clk`를 하강 에지 도메인(`<클럭>_neg`)으로 옮기고, 클럭 포트가 같은 도메인을 `sync_to` div 1로 묶습니다. SPEC 2절에 선택 열 "클럭 에지"를 두었습니다 | `validator/checks.py`, `tb/gen.py`, `tb/lib.py`, `tb/diffsim.py`, `ir/pysim.py`, `verify/pyequiv_tb.py`, `importer/extract.py`, `spec/l1.py`, `importer/spec_tables.py`, SPEC 작성 템플릿 |
| 패드 동시 구동 충돌 | 하니스의 패드 체커가 바깥 장치가 구동하는 표본(`P_pad_oe=1`)에서 패드 값이 바깥 장치의 값과 다르면 T2로 실패시킵니다. 바깥 구동이 약한 구동이라 충돌하면 블록의 값이 보이므로, 보류 사유였던 4-state 시뮬레이터 없이 잡힙니다 | `tb/lib.py`(`PadChecker`), `tb/gen.py`(`pads`) |
| 실행기의 `tristate` | 최상위 모듈의 `tristate`를 하니스와 같은 패드 모델 포트(`P_pad_o`, `P_pad_oe` → `P_pad_val`)로 실행합니다. 비트마다 블록 구동 > 바깥 구동 > 풀업입니다. 번역기 검사는 inout top을 패드 래퍼로 감싸 RTL과 비교합니다 | `ir/pysim.py`, `verify/pyequiv.py` |
| function/task | IR `functions`(module role만)를 automatic function/task로 방출합니다. 본문은 인자, 지역 변수(`vars`), 상수, 앞에 정의한 function만 읽고 씁니다(모듈 신호는 인자로). blocking만 쓰고 타이밍 제어와 재귀는 금지합니다(V4). task는 `comb`에서만 부르고, 출력 인자로 넘긴 신호가 그 조각의 쓰기입니다(V4 읽기·쓰기, V6 래치, 조합 경로). Python 조각에서는 `return e`, 식의 `f(a)`, `comb` 문장 `t(a, out)`로 쓰고, 실행기도 같은 의미로 실행합니다. Repair가 고칠 수 있습니다 | 스키마, `ir/model.py`, `validator/`, `emitter/sv.py`, `ir/pyfrag.py`, `ir/pysim.py`, `patch/apply.py` |
| SVA | IR `assertions`(assert, assume, cover. module role만)를 Emitter가 도메인 에지와 `disable iff`로 씌우고 `` `ifndef SYNTHESIS ``로 감쌉니다. 리셋은 `<도메인>_sva_rst` wire로 옮겨 씁니다(비동기 리셋 포트를 바로 쓰면 SYNCASYNCNET 경고). 실패하면 `$stop` 대신 `[SVA] FAIL` 줄을 냅니다. E2는 SVA가 있는 RTL만 `--assert`로 빌드하고, 로그에서 실패를 읽어 그 테스트의 결과로 냅니다. assert 실패는 Repair(노드 `assertions/<id>`)로, assume 실패는 Verifier로 갑니다. V2/V4/V8에서 읽기 목록, 대입 금지, 같은 클럭 그룹을 봅니다 | 스키마, `emitter/sv.py`, `validator/`, `verify/run.py`, `classifier/classify.py`, `scheduler/run.py` |

구현하면서 정한 것:
- **SVA 실패 보고.** 기본 동작(`$error` → `$stop`)은 시뮬레이션을 끝내 남은 테스트까지 모두 실패로 만들었습니다(실험으로 확인). 그래서 실패 줄을 `$display`로 내고 `$fflush`로 cocotb 로그와 순서를 맞춘 뒤, 줄 앞의 `running <테스트>`로 테스트를 정합니다.
- **`--assert`는 SVA가 있는 RTL에만 씁니다.** 원본 RTL의 `unique case` 같은 검사가 새로 켜지지 않게 하려는 것입니다.
- **`cover`는 방출만 합니다.** Verilator에서 action 블록이 없어 결과를 읽지 않습니다.
- **번역기 검사의 입력 시각.** 반주기를 2h ns(h는 홀수)로 두어 모든 에지가 짝수 ns에 오고, 입력은 기준 클럭 하강 에지에서 h ns 뒤(홀수 ns)에 바꿉니다. 하강 에지 도메인이 있어도 입력 변경이 어느 에지와도 겹치지 않습니다.
- **SPEC 표의 클럭 에지 열**은 하강 에지 도메인이 있을 때만 냅니다 (이전 SPEC과 표가 같게).
- **패드 체커는 Verifier 테스트에만 적용됩니다.** R4/R6의 차분 시뮬레이션은 패드 값을 출력으로 비교할 뿐 충돌로 실패시키지 않습니다.

검증 (에이전트 없이):
- 하강 에지: 시험용 원본 `tests/fixtures/negedge/ne.v`가 R1을 통과하고, 손으로 옮긴 IR이 R4 `equal`입니다. 하강 에지 단을 상승 에지로 옮긴 IR은 `differ`입니다. 생성 하니스 테스트가 통과하고, 하니스에서 하강 에지 처리를 빼면 실패합니다. Python 판의 번역기 검사가 통과하고, RTL의 하강 에지 단을 상승 에지로 바꾸면 잡습니다.
- 패드: `pp_top`, `od_top`의 Python 판 번역기 검사가 통과하고, 패드 구동을 바꾸면 잡습니다. `pp_top`에서 바깥 장치 0, 블록 1이면 충돌을 보고합니다. 기존 `od_top` 하니스 테스트는 그대로 통과합니다(오탐 없음).
- function/task와 SVA: 시험용 모듈 `tests/fixtures/v1ext/fn_mod`(SV, Python 판)가 Validator와 lint를 통과하고, 번역기 검사도 통과합니다(함수 본문을 바꾸면 잡음). 가정을 지키는 테스트는 깨끗하고, 어기는 테스트에만 assume 실패가 붙습니다. task 본문을 틀리게 바꾸면 두 테스트 모두 `assertions/p_z` 실패가 되고, Classifier가 모듈과 노드를 짚어 Repair로 보냅니다.
- 시험: `tests/test_v1_ext.py` 24개, `tests/sim/test_v1_ext_sim.py` 8개.

**아직 하지 않은 것.** 실제 에이전트 실행으로는 확인하지 않았습니다. Author가 `functions`, `assertions`를 쓰는지, assume 실패를 받은 Verifier가 어떻게 판단하는지는 다음 실제 실행에서 봅니다.

### 단일 리셋 방식 (2026-10-21 사용자 지시)

`axis_async_fifo` 이식에서 R6가 SPEC 고치기 5회 뒤에도 수렴하지 않았습니다. 원인은 리셋 동기화 플롭만 비동기이고 나머지는 동기인 원본이었습니다. IR은 이를 동기 도메인과 우회 로직으로 옮겼고, 리셋 직후 동작이 두 클럭 위상에 따라 달라져 SPEC 문장으로 정할 수 없었습니다. 그래서 한 설계의 리셋을 한 방식으로 제한했습니다.

| 항목 | 방법 | 위치 |
| --- | --- | --- |
| 규칙 | 모듈 안 도메인끼리, `domain_map`으로 잇는 부모와 자식 도메인끼리 `reset.async`가 같아야 합니다(V2). `reset.synchronize`는 비동기로 셉니다 | `validator/checks.py`, `ir/model.py`(`reset_async`) |
| 추가 리셋 | `reset.extra`는 동기 리셋이라 동기 설계에만 둡니다(스키마). 비동기 설계의 소프트 리셋은 `seq` 본문의 조건으로 씁니다. R1도 비동기 리셋 블록의 소프트 리셋은 추가 리셋으로 올리지 않습니다 | 스키마, `importer/extract.py` |
| 표준 셀 | `sync_2ff`, `sync_pulse`, `cdc_fifo`에 `ASYNC_RST` 파라미터(기본 1)를 두었습니다. 동기 리셋 설계면 Emitter가 `ASYNC_RST(1'b0)`를 주고, 실행기도 셀 리셋을 에지에서 넣습니다 | `emitter/stubs/`, `emitter/sv.py`, `ir/pysim.py` |
| R1 | 리셋 방식이 섞인 원본(모듈 안이나 모듈 사이)을 `mixed_reset`으로 거부합니다. 이전에는 많이 쓴 쪽으로 합치고 검토 항목으로 냈습니다 | `importer/extract.py` |
| 문서와 안내 | ARCHITECTURE 5절 "리셋 방식", SPEC 작성 템플릿 2절, `ir_guide.md`, Architect와 Porter 안내 | |

결정:
- **추가 리셋은 동기 설계에만 둡니다** (사용자 결정). 시험용 Block Top `mc_top`(비동기 설계)의 소프트 리셋은 입력 포트와 동작으로 바꾸고, 추가 리셋 시험은 같은 픽스처를 동기 설계로 바꾼 것으로 합니다.
- **`axis_async_fifo` 원본은 모두 동기로 고쳤습니다** (사용자 결정). 리셋 동기화 플롭 두 블록의 `posedge rst`를 뺐습니다. 또 리셋이 없던 동기화 2·3단(`s_rst_sync2/3`, `m_rst_sync2/3`)에 자기 도메인 리셋 때 1을 넣었습니다 (사용자 결정). 표준 셀은 받는 쪽 리셋에서 늘 0이 되므로, 리셋 없는 동기화 플롭은 IR로 같게 옮길 수 없었습니다. 이 2·3단 리셋을 넣으면 Porter의 IR이 R4 `reset_only`가 되는 것을 확인했습니다. 리셋을 푼 뒤 자기 클럭 2사이클 동안 리셋이 이어지므로, 리셋 직후 동작이 상대 클럭 위상이 아니라 자기 도메인 사이클로 정해집니다.

검증:
- 고친 원본과 손으로 옮긴 golden IR이 모두 원본 테스트벤치 20/20을 통과합니다.
- 셀을 비동기로 되돌려도 R4 첫 차이가 같았습니다. 그래서 차이의 원인은 셀이 아니라 리셋 없는 동기화 플롭으로 보았습니다.
- 시험: `tests/test_multiclock.py`(단일 방식, 동기 설계의 추가 리셋, 셀 `ASYNC_RST`, 실행기의 셀 동기 리셋), `tests/test_port.py`(`mixed_reset` 거부). 전체 시험 통과 (시뮬레이션 제외 298개, 시뮬레이션 103개).
- 이식 재실행 (`port-axis_async_fifo-20261021b`, Claude Code Haiku 5.5 effort high, 에이전트 실행 77회): R3 Porter 첫 시도 통과, R4 `reset_only` (첫 차이 m 사이클 183, 리셋 창 안). 이전 원본에서는 R4 `differ`였습니다. R6는 회차 0\~5 모두 `spec_gap`으로 끝나 다시 `R6_SPEC_GAP`입니다. 매 회차 판정은 재생성 설계가 SPEC을 어기지 않았고, SPEC 문장이 원본과 다르다는 것이었습니다. 남은 요구사항은 리셋 직후 동작입니다: 리셋 전달 지연 사이클(EX-02, EX-05), 단독 `m_rst`(EX-07), s_rst 뒤 종료 항목의 출현 조건과 값(EX-06, EX-08). SpecWriter가 이 값을 원본 refsim에서 재지 않고 추정해 적은 것이 원인으로 보입니다. 리셋 방식 통일로 R4는 좋아졌지만 R6 수렴에는 SpecWriter 쪽 보강이 더 필요합니다.

#### 다중 클럭 리셋을 사용 조건으로 (2026-10-22 사용자 결정)

위 실패를 다시 보니 마지막 두 회차의 SPEC은 원본과 맞았습니다. refsim으로 SC-07, SC-10을 돌리면 SPEC 기대와 같았습니다. 실패의 원인은 SPEC이 아니라 아래 세 가지였습니다.
- **하니스:** `start()`가 s 도메인 리셋을 먼저 풀고 m 에지를 기다려, `start` 뒤의 `send(idle=N)`이 s 도메인에서만 사이클 N+1에 나갔습니다. SPEC 사이클로 쓴 테스트가 원본에서 실패했습니다.
- **SPEC 범위:** 두 클럭 위상에 따라 달라지는 사이클(예: TR-06의 1 ns, 2 ns 경계)과 한쪽 리셋 동작까지 정확한 값으로 정하려 했습니다.
- **R6 비교:** 도메인마다 따로 무작위 리셋을 걸어, 리셋 때 살아남는 항목이 두 클럭 사이 지연에 따라 달랐습니다.

| 항목 | 방법 | 위치 |
| --- | --- | --- |
| 하니스 시작 | 리셋 풀기와 표본 시작을 `start()` 뒤로 미룸. `start` 바로 뒤의 `idle=N`은 모든 도메인에서 사이클 N | `tb/lib.py`, `tb_guide.md` |
| 속성 표본 | 모든 도메인의 리셋 포트를 넣음 (두 리셋을 함께 보는 속성) | `tb/lib.py` |
| R6 함께 리셋 | 리셋 도메인이 둘 이상이면 모든 도메인에 같은 시각 구간(가장 느린 클럭 4\~8사이클)으로 리셋. 마지막 리셋 뒤 출력만 비교. 리셋마다 입력 순서를 처음부터. 앞부분이 같고 더 받은 쪽이 더 냈으면 끝 개수 차이는 무시. 축소기는 함께 리셋을 통째로 지움 | `tb/diffsim.py`(`joint_reset`), `importer/r6.py`(`plan_opts`), `scheduler/run.py`(`minimize_diff`) |
| SpecWriter | 다중 클럭이면 리셋을 사용 조건("함께, 가장 느린 클럭 4사이클 이상, 한쪽 리셋은 미정의")으로, 두 클럭 위상에 따른 사이클은 범위와 "구현에 따름(검증하지 않음)"으로 씀 | `agents/spec_writer.py` |
| 사용량 한도 | "disabled Claude subscription access"도 한도로 보고 멈춤 (실패로 기록되어 회차 하나를 잃음) | `agents/task.py` |

결과 (`port-axis_async_fifo-20261022`, R1\~R4는 `20261021b`를 이어 받음, Claude Code Haiku 5.5 effort medium, 에이전트 실행 62회):
- 회차 0, 1: 재생성 통과, 비교는 입력 받은 개수 차이로 `differ`. 고친 비교로 다시 보면 두 회차 모두 4개 시드에서 같았습니다. 남은 차이는 원본이 리셋 직후 `s_status_depth`를 2046, 2047(음수 감싸기)로 내는데 SPEC이 0\~1024로 정한 것이었습니다.
- 회차 2: Verifier 테스트가 다른 도메인 리셋 포트를 표본에서 찾지 못해(`KeyError: 's_rst'`) 재생성 실패. 판정은 spec_gap(FR-STAT-01).
- 회차 3: 구독 접근 차단으로 에이전트가 돌지 않아 지우고 다시 돌림.
- **회차 3(SPEC v3): 재생성 통과, 원본과 4개 시드 모두 같음, 두 Verifier 테스트가 원본에서 통과. R6 통과.** 첫 출력 사이클과 출력 수도 원본과 같았습니다(1926개, 지연 차 0).
- 사람 승인 2회(기댓값 수정): TR-01 지연 하한 완화, sc03_full 대기 시간 수정.
- 시험: `tests/test_port.py`(함께 리셋 계획, 사건 묶음, 마지막 리셋 뒤 비교, 끝 개수 차이). 시뮬레이션 제외 303개, 시뮬레이션 103개 통과 (끝 개수 차이와 입력 순서 재시작 뒤에는 관련 시뮬레이션 33개를 다시 돌림).

### 실패 교훈 반영 점검과 남은 항목 실행 (2026-10-22\~23 사용자 지시)

DV/PI Seat 모델 선정을 뺀 남은 항목을 진행하고, 지난 실패에서 얻은 교훈(Architecture·SPEC 작성 요구 등)이 하네스에 들어 있는지 점검했습니다. 빠진 것은 아래처럼 넣었습니다.

| 교훈 | 반영 | 위치 |
| --- | --- | --- |
| 합성 RTL에는 task와 PLI를 쓰지 않음 (사내 규칙, 사용자 확인) | V4: `task`는 오류. 합성 가능한 상수 시스템 함수(`$clog2`, `$bits`, `$signed` 등 `SYNTH_SYSFUNCS`) 밖의 시스템 함수·PLI는 블록, FSM 조건, 함수 본문에서 오류. SVA는 `ifndef SYNTHESIS` 안이라 제외. Porter는 task를 comb나 function으로 풀고 `$display`는 옮기지 않음. P12의 task 기능은 이 규칙으로 대체 | `validator/checks.py`, `agents/porter.py`, `ir_guide.md`, 픽스처 `fn_mod`, `fn.v` |
| RTL 코딩 규칙과 용어를 한 곳에 | 규칙마다 강제하는 곳(Validator 규칙, Emitter, 안내)을 짝지은 `docs/RTL 코딩 규칙.md`, 용어 약 110개의 `docs/용어집.md` | `docs/` |
| 동기 리셋은 "다음 사이클부터" | SPEC 검사 경고 `RESET_SAME_CYCLE`(동기 리셋 문장에 "rst=1인 동안"만 있고 다음 사이클·클럭이 없음). golden SPEC 두 곳(ADP-E01, COBS-E01) 수정 | `spec/check.py`, SPEC 작성 템플릿 |
| 다중 클럭 리셋은 사용 조건 | SPEC 검사 경고 `MULTI_RESET_USAGE`(리셋 도메인이 둘 이상인데 함께 거는 사용 조건 문장이 없음) | `spec/check.py`, 템플릿, ARCHITECTURE R6 |
| 범위와 "구현에 따름" | 상태 출력의 단위와 범위, 위상에 따른 값은 범위와 "구현에 따름(검증하지 않음)". Verifier는 그런 값을 검사하지 않고 범위는 범위로 검사 | 템플릿, `agents/verifier.py`, `tb_guide.md` |
| 사람이 승인한 테스트 결함 | `tb_guide.md` "자주 틀린 것": 고정 사이클 뒤 개수 검사, `set_ready` 되풀이, 범위 값을 한 값으로, 동기 리셋 그 사이클 출력, 리셋 뒤 보내던 프레임, 다른 도메인 사이클 번호(대기 클럭 예), 직전 사이클 값으로 같은 사이클 규칙 검사 | `tb_guide.md` |
| refsim으로 사이클을 재고 씀 | refsim이 단일 출력 값 변화(`singles`)도 냄. 자극 배열과 번호가 같다는 설명 | `agents/pl.py`, `agents/spec_writer.py` |
| 고정 파라미터로 지운 원본 갈래 | R1이 지운 파라미터(`removed_params`)를 기록하고, 참조 빌드에 그 고정 값을 줌 (아래 `PAUSE_ENABLE`) | `importer/extract.py`, `importer/r1.py`, `importer/r4.py`(`ref_params`) |
| `$clog2` 파라미터 식 | Python 조각의 상수 식에서 `$clog2`를 기호로 다룸 | `ir/pyfrag.py` |
| DV 핸드셰이크 표본 | 핸드셰이크는 에지 전에 표본, 조건 대기, SVA는 `--assert`와 `$display`(멈추지 않음)와 음성 검사 | `department/guidance/dv.tb.md`, `dv.checker.md` |

이 과정에서 찾아 고친 결함:
- **`PAUSE_ENABLE` 제거로 틀린 참조.** R1이 고정 파라미터 `PAUSE_ENABLE=1`로 갈래를 고르고 파라미터를 지웠는데, R4/R6 참조 빌드는 원본 기본값 0으로 빌드했습니다. 그래서 전에 통과한 `axis_async_fifo` R6(커밋 7e8f5fa)는 일시정지가 꺼진 원본과 비교한 것이었습니다. `removed_params`로 고친 뒤 R4가 `m_pause_ack` 차이를 잡았습니다.
- **원본의 리셋 없는 일시정지 동기화 플롭.** 고친 참조로 R4가 `s_pause_ack`에서 `differ`였습니다. 원본 RTL의 일시정지 동기화 플롭(`s_pause_req_sync1`, `s_pause_ack_sync2/3`는 s_rst, `s_pause_req_sync2/3`, `s_pause_ack_sync1`은 m_rst)에 동기 리셋 0을 넣었습니다 (사용자 결정). 원본 테스트벤치 20/20 통과.
- **하니스 시작 순서.** 리셋 풀기를 `_release()`로 미뤄 `idle=N`이 모든 도메인에서 사이클 N.
- **diffsim:** 처음 리셋 구간(`INIT_RESET` 전) 출력은 기록하지 않음(재생성의 사이클 0 valid로 `regen_defect` 오판 2회). 다중 리셋 도메인이면 끝 개수 허용을 steady 모드에도 적용하고(처음 리셋 뒤 ready 지연도 미정), 더 받은 쪽이 중간 한 곳에 연속으로 낸 항목(받은 입력 차이 이하)도 건너뜀(프로브 앞 조용한 구간에서 입력 순서가 다른 위치에서 끊김). 프로브 전환 때 내던 트랜잭션은 받힐 때까지 유지(valid를 받히기 전에 내리지 않음).
- **SPEC 게이트:** 블록이 없는 L1(SPEC 표에서 만든 것)에 V2의 "inout은 tristate 블록 하나가 맡아야" 규칙을 적용해 inout 블록이 늘 실패. 게이트에서 이 규칙을 뺌 (`od_top`).
- **`pl.py model`:** 입력 번들이 없는 블록(단독 포트만, inout 패드만)에서 빈 목록에서 고르다 예외. Model 생성만 확인하도록 고치고, 테스트가 import하는 `harness/`를 경로에 넣음 (`fn_top`, `od_top`).

실제 에이전트 실행 (Claude Code Haiku 5.5 effort medium):

| 실행 | 결과 | 비고 |
| --- | --- | --- |
| `port-axis_adapter-upsize-20261022` | R4 `equal` (Porter 첫 시도) | 업사이즈 설정 |
| `port-ne_top-20261022` (하강 에지) | R6 통과 (회차 4, 에이전트 92회) | 사람 승인 2회 |
| `port-od_top-20261022` (open-drain 패드) | R6 통과 (회차 0, 13회) | SPEC 게이트와 `pl.py model` 수정 뒤 |
| `port-fn_top-20261022` (function) | R6 통과 (회차 0, 14회) | `pl.py model` 수정 뒤 |
| `port-axis_async_fifo-20261022c` | R6 통과 (회차 5, 139회) | 사람 승인 4회 (2회는 버린 첫 회차 5). 아래 |
| `port-axis_adapter-deltest-20261022`, `deltest2` | 삭제를 잡지 못함 | 아래 |

- **`axis_async_fifo`:** 회차 2, 3은 리셋 뒤 `s_axis_tready`가 처음 1이 되는 사이클을 정확한 값으로 써서 실패했고, 회차 4(SPEC v4)에서 범위(사이클 2\~4, 구현에 따름)가 되었습니다. 그런데 회차 4를 비교 결함(위 diffsim 두 가지)이 `differ`로 내, SpecWriter가 v5에서 다시 "사이클 4"로 좁혀 회차 5가 실패하고 고치기 상한에 걸렸습니다. 비교를 고친 뒤 회차 4로 되돌려(회차 5 결과는 `.abandoned`로 보관) 다시 판정하니 재생성은 원본과 같았고, 남은 것은 원본이 리셋 구간에 `s_status_depth=2046`을 내는 진짜 SPEC 공백이었습니다. 새 v5(리셋 창 인덱스 1\~2의 상태는 0\~2047, 구현에 따름)로 회차 5가 원본과 같고 두 Verifier 테스트가 원본에서 통과. 가져온 IR로 원본 테스트벤치 20/20 통과.
- **삭제 시험:** SPEC 요구사항을 지워도 R6가 잡는지 보았습니다. 1차(FR-DATA-03 하나)는 SC-02 표가 같은 동작을 정하고 있어 남았고, 2차는 FR-DATA-03(tkeep=0 바이트도 입력 값 그대로)과 EX-02, SC-02의 해당 값까지 지웠습니다. 재생성 설계는 그래도 원본과 같았습니다. 시프터의 가장 단순한 구현이 마스크하지 않으므로 원본과 같은 동작이 기본으로 나온 것입니다. R6는 지운 요구사항이 없을 때 설계가 원본과 다르게 나올 수 있는 동작만 잡습니다. 검출을 시험하려면 사이클 수, 조건부 값, 우선순위처럼 추측할 수 없는 동작을 지워야 합니다.
- **CRC DV 공백 (P8 대기 항목):** `experiments/p8/crc_dv/`에 SVA(bind: 유지, 상태, 리셋, 16비트 롤오버 cover)와 0xFFFF→0→1 롤오버, stall, 중간 리셋 테스트. 통과. SPEC 결정(사용자, 권장안): CRC-E03은 도중 리셋된 프레임을 마무리하지 않음(남은 바이트와 CRC를 내지 않고 받는 쪽이 처리), CRC-E04는 리셋 뒤 첫 프레임이 CRC-01\~12를 따름, CRC-E01은 "rst=1을 본 다음 사이클부터" 한 문장(REQ_MULTI, RESET_SAME_CYCLE 경고 해소), Q-01은 상위 바이트 먼저로 고정하고 범위 밖 항목에 넣음. 동결 RTL(674c)의 동작 그대로라 RTL은 바꾸지 않았고, `first_frame_after_mid_reset` 테스트(끊긴 프레임 3바이트 뒤 리셋, 다음 "123456789"가 정확히 `29 B1`로 끝나고 `stat_frames=1`)를 더해 3/3 통과, SVA 실패 0. 남은 SPEC 경고는 기존 CRC-T01의 REQ_MULTI 하나.
- **golden 블록 Verifier 체커의 리셋 표본 가정:** 다시 보았고 가정한 곳이 없었습니다.

시험: 시뮬레이션 제외 309개(3개 건너뜀), 시뮬레이션 103개 통과.

---

## 3. 횡단 관심사

| 항목 | 방침 |
| --- | --- |
| 테스트 | golden 블록 3개의 IR → RTL → 기존 TB 통과를 CI 회귀로 둡니다. Emitter 대표 출력의 SpyGlass 통과도 CI에 넣습니다. |
| 재현성 | 실행마다 SPEC 버전, 모델 설정, 시드, 툴 버전을 기록합니다. 에이전트 입출력 산출물은 모두 저장합니다. |
| 관측 | 모듈별 상태 전이, 반복 횟수, 토큰과 시간 사용량, 라이선스 대기 시간을 기록합니다. headless 에이전트의 출력, transcript, 작업 디렉터리를 실행 ID별로 보관합니다. |
| 추적성 | 요구사항 ID에서 검사, 불일치 리포트, 환류, 승인까지 연결합니다. |
| 보안과 격리 | 격리는 지침이 아니라 파일시스템, 저장소 권한, 스케줄러 입력 구성으로 강제하고, 이를 테스트로 확인합니다. |

## 4. 위험과 대응

| 위험 | 신호 | 대응 |
| --- | --- | --- |
| coding agent의 구조화 출력 품질 부족 | X1에서 통과율이 낮음 | 런타임과 모델 조합 교체, 출력 단위 축소, 재시도 정책 |
| 스키마 표현력 부족 | P1에서 원시 블록 비중이 높거나 우회 표현이 많음 | 판정 규칙에 따라 스키마 조정 또는 SV 소스 중심으로 전환 |
| 테스트 약화 방향으로 수렴 | Author 후보 간 차분이 자주 발생, 커버리지 정체 | Verifier 격리를 확인하고 테스트 보강 루프와 사람 검토를 강화 |
| 공통 오독 | DV에서 기능 버그 발견 | 예시 시나리오 보강, 참조 모델의 사람 검토 비중 확대 |
| Python 조각 번역 오류 | 번역기 검사 불일치(`PY_EQUIV`) | 생성기 버그로 멈추고 번역기와 실행기를 고침. 판정은 늘 RTL 시뮬레이션이므로 잘못된 RTL이 통과하지는 않음 |
| 설계 모델과 참조 모델의 공통 오독 | Python 후보에서만 DV 기능 버그 | 두 모델을 다른 에이전트가 쓰는 격리 확인, Author와 Verifier 모델 계열 분리 |
| OpenRig 폐쇄망 비호환 | X3, X5, X6 실패 | 자체 Seat 런타임으로 대체. 부서 계층의 설계 원칙은 그대로 유지 |
| EDA 라이선스 병목 | E3 대기 시간 증가 | 후보 선택 뒤에만 E3 실행(설계 사항). 대기열 우선순위 조정 |
| 자료 입수 지연 | P0 자료 미수령 | P1은 기존 블록 RTL과 TB만으로 시작하고, SPEC 변환은 P3로 미룸 |
