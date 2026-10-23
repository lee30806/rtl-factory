# 블록 이식 시험 (P1)

LLM 없이 기존 블록을 손으로 IR에 옮기고, 방출된 RTL을 원본 블록의 기존 테스트벤치로 판정합니다. 스키마의 표현력과 Emitter의 정확성을 보는 시험입니다. 설계 문서의 "블록 이식 시험 계획" 절차 2\~5와 7을 수행했습니다.

**사내 블록 대신 공개 블록을 썼습니다 (2026-10-11 결정).** 사내 자료를 받으면 같은 실행기로 다시 판정합니다. VC SpyGlass 판정(절차 6)은 이번 구현에서 제외했습니다.

## 대상과 결과

| 유형 | 블록 | 원본 | 고정한 설정 | 원본 테스트벤치 | 결과 |
| --- | --- | --- | --- | --- | --- |
| 계층 모듈 (제어) | `uart` = `uart_tx` + `uart_rx` | verilog-uart `1b867e5` | `DATA_WIDTH=8` | `test_uart_rx`, `test_uart_tx` | 2/2, 2/2 통과. top `uart`는 테스트벤치가 없어 E1만 봄 |
| FSM 중심 | `axis_cobs_decode` | verilog-axis `48ff7a7` | 없음 | `test_axis_cobs_decode` | 16/16 통과 |
| datapath 중심 | `axis_adapter` | verilog-axis `48ff7a7` | 32비트 → 8비트(downsize), ID/DEST/USER 사용 | `test_axis_adapter` | 9/9 통과 |
| 다중 클럭 | `axis_async_fifo` | verilog-axis `48ff7a7` | `DATA_WIDTH=8`, `RAM_PIPELINE=1`, `FRAME_FIFO=0`, pause 사용, 클럭 10ns/11ns | `test_axis_async_fifo` | 20/20 통과 |

모든 원본은 MIT 라이선스이고, 각 블록의 `upstream/COPYING`에 있습니다.

- **기준선.** 같은 테스트벤치를 원본 RTL에 돌려도 모두 통과합니다(`--baseline`). 테스트벤치 자체는 이 환경에서 믿을 수 있습니다.
- **테스트벤치 수정.** 하나뿐입니다. cocotb 1.9가 이 Mac에서 빌드되지 않아 cocotb 2.1을 썼습니다. 그래서 `test_axis_async_fifo.py`의 `depth = dut.DEPTH.value` 두 곳을 `int(...)`로 감쌌습니다. 검사 내용은 바뀌지 않습니다.

```bash
.venv/bin/python tests/golden/run_golden.py              # 전체 (Validator → 방출 → E1 → 원본 테스트벤치)
.venv/bin/python tests/golden/run_golden.py --baseline   # 원본 RTL로 같은 테스트벤치
.venv/bin/python -m pytest tests/sim/test_golden.py      # CI 회귀
```

cocotb 2.1, cocotbext-axi, cocotbext-uart, cocotb-test, Verilator가 필요합니다.

## 기록 항목

### 원시 블록 비중

방출 RTL에서 주석과 빈 줄을 뺀 줄 수에 대한, `comb`와 `seq` 블록 `body`의 줄 수 비율입니다.

| 블록 | body 줄 | 방출 RTL 줄 | 비중 | 원본 RTL 줄 |
| --- | --- | --- | --- | --- |
| `uart` | 65 | 167 | 0.39 | 196 |
| `axis_cobs_decode` | 119 | 213 | 0.56 | 220 |
| `axis_adapter` | 52 | 107 | 0.49 | 215 |
| `axis_async_fifo` | 131 | 305 | 0.43 | 662 |
| 합계 | 367 | 792 | 0.46 | 1293 |

`axis_cobs_decode`가 가장 높습니다. Mealy 출력을 `comb` 블록에 다시 쓰느라 그렇습니다(아래 우회 표현 4번).

### 표현 불가 구문 → 스키마 확장 (이번에 구현)

| 구문 | 나온 곳 | 조치 |
| --- | --- | --- |
| 여러 비트 gray 포인터 동기화 | `axis_async_fifo` | `cdc_sync`에 `style: "gray"` 추가. src와 dst 타입이 같아야 함. 셀은 같은 표준 셀에 `WIDTH`만 줌. `2ff`는 1비트만 허용 |
| 두 도메인에 걸친 메모리 배열 | `axis_async_fifo` | `memory` 블록의 `impl: "inferred"` 방출(배열 선언). 쓰기 포트 도메인의 `seq`만 쓰고(V5) 읽기 포트 도메인의 `seq`만 읽음(V8). `macro`는 래퍼 규약을 받은 뒤 |
| `localparam` | `axis_adapter`, `axis_async_fifo` | 파라미터에 `local: true` 추가. 모듈 안의 `localparam`으로 방출하고, 인스턴스에서 바꾸면 V2 오류 |

### 표현 불가 구문 → v1에서는 설정 고정으로 대응 (스키마에 넣지 않음)

| 구문 | 나온 곳 | 조치 |
| --- | --- | --- |
| `generate if`로 파라미터마다 구조가 바뀌는 블록 | `axis_adapter`(bypass/upsize/downsize), `axis_async_fifo`(frame, output FIFO, pause) | 설정 하나를 정해 그 구조만 옮김. IR은 설정별로 따로 둠. 설정 값으로만 쓰이던 파라미터는 남겨 두었고, E1에서 `UNUSEDPARAM`으로 나옴(4건, 16건) |
| `function` (`bin2gray`, `gray2bin`) | `axis_async_fifo` | `comb` 블록에 풀어 씀(`for` 루프 포함) |

### 우회 표현

| # | 내용 | 나온 곳 | 비고 |
| --- | --- | --- | --- |
| 1 | 리셋이 있는 레지스터와 없는 레지스터가 한 `always`에 섞여 있어, V7 때문에 `seq` 블록을 나눔. 나뉜 블록마다 같은 조건문을 다시 씀 | `axis_cobs_decode`, `axis_adapter`, `axis_async_fifo` (5곳) | 조건이 두 곳에 있으면 한쪽만 고치는 실수가 생길 수 있음. Repair가 두 블록을 함께 봐야 함 |
| 2 | 리셋 없이 초기값(`reg x = 0`)만 있던 레지스터에 리셋을 추가함 (같은 블록의 다른 레지스터와 맞추려고) | `uart_rx`, `uart_tx`의 `data_reg` | 동작 차이는 리셋 중에 값이 지워지는 것뿐 |
| 3 | 암묵적인 폭 변환을 모두 명시함(`4'(DATA_WIDTH + 2)`, `19'd1`, `{prescale, 3'b000}`, `[M_DATA_WIDTH-1:0]`) | 전 블록 | 원본은 Verilator `-Wno-WIDTH`로 돌림. 파이프라인은 폭 경고를 오류로 다룸 |
| 4 | 다음 상태와 출력을 한 `case`에서 만드는 Mealy FSM은 `fsm` 전이와 `comb` 블록으로 나뉘어, 같은 조건이 양쪽에 있음 | `axis_cobs_decode` | 전이 9개. 원시 블록 비중이 가장 높은 원인 |
| 5 | `always` 안의 blocking 임시 변수(`wr_ptr_temp`)를 `comb` 신호로 뺌 | `axis_async_fifo` | V4 규칙(`seq`에는 non-blocking만) 그대로 |
| 6 | RAM 파이프라인 배열과 `for` 루프를 단계별 레지스터로 풂 | `axis_async_fifo` | `RAM_PIPELINE=1` 고정이라 두 단 |
| 7 | 비동기 리셋으로 세우는 리셋 동기화 플롭(`s_rst_sync1_reg`)을 동기 리셋 레지스터로 옮김 | `axis_async_fifo` | 한 도메인에 리셋 방식이 하나뿐이라서. 리셋 진입이 한 클럭 늦음 |
| 8 | 리셋이 없던 동기화 플롭이 리셋이 있는 표준 셀로 바뀜 | `axis_async_fifo` | **이 차이로 기능 버그가 생겼고, P2 생성 하니스가 찾음** (한쪽 도메인이 먼저 리셋에서 풀리면 입력을 받았다가 잃음). "상대 도메인 준비됨" 플래그(리셋 시 0)를 동기화하도록 고침. 원본 테스트벤치는 두 리셋을 동시에 풀어 놓침. 셀 규약은 사내 셀을 받으면 다시 봄 |
| 9 | 비동기 입력 `rxd`를 원본처럼 플롭 하나로 받으려고 동기 포트로 선언함 | `uart_rx` | 원본의 관행. `cdc_sync`를 쓰면 지연이 한 클럭 늘어 원본과 달라짐 |

### Validator 오탐과 누락

| 종류 | 내용 | 조치 |
| --- | --- | --- |
| 오탐 | 대입 왼쪽이 연결(`{data_reg, txd} <= ...`)이면 원소를 읽기로 셈 | 고침. 왼쪽의 원소는 쓰기, 왼쪽 인덱스 안의 이름은 읽기 |
| 누락 | `bit_cnt > DATA_WIDTH+1`(4비트 대 32비트 비교)의 폭 차이를 pyslang 탐침이 보고하지 않음. Verilator 빌드(`WIDTHEXPAND`)에서 잡힘 | E1이 E2보다 먼저 돌아 같은 결과를 냄. Classifier가 조각 노드로 되돌림 |

`comb` 블록 안에서 먼저 쓰고 다시 읽는 중간 신호도 `reads`에 적어야 합니다. 오탐은 아니지만 규칙으로 문서화했습니다.

### Emitter와 Classifier 결함 (고침)

- 동기화 셀의 리셋을 도착 도메인의 리셋 포트에 극성과 관계없이 연결했습니다. active-high 리셋이면 반전해 연결하도록 고쳤습니다.
- 파라미터 선언 줄이 소스맵에 없어서, 미사용 파라미터 경고가 골격(생성기 버그)으로 분류됐습니다. `params/<이름>` 노드로 대응시켰습니다.
- 회귀 테스트는 E1 결과가 모두 IR 노드로 돌아가고 골격을 가리키지 않는지 확인합니다.

### 골격 코드의 lint 위반

E1(Verilator `-Wall`)에서 골격 줄의 위반은 0건입니다. 나온 21건은 모두 L1 선언 줄입니다.
- 미사용 파라미터 20건: `axis_adapter` 4건, `axis_async_fifo` 16건. 설정을 고정한 결과입니다.
- 미사용 입력 1건: `KEEP_ENABLE=0`인 `s_axis_tkeep`. 원본에서도 쓰이지 않습니다.

VC SpyGlass 판정은 제외했습니다.

## 판정

| 판정 규칙 | 결과 | 결정 |
| --- | --- | --- |
| 원시 블록 비중이 대부분인가 | 0.39\~0.56, 합계 0.46 | **JSON 계층을 유지합니다.** ARCHITECTURE.md는 개정하지 않습니다 |
| 우회 표현이 많은가 | 9종 | 세 가지는 스키마 확장으로 해결했습니다(gray, inferred memory, localparam). 나머지는 v1에서 받아들이고 규칙으로 문서화합니다. Mealy FSM 출력(4번)과 `generate`는 사내 블록으로 다시 판정한 뒤 확장 여부를 정합니다 |
| Validator 오탐이 많은가 | 1건 | 고쳤습니다. 규칙을 경고로 내릴 필요는 없습니다 |

### 정한 값

| 항목 | 값 | 근거 |
| --- | --- | --- |
| 원시 블록 비중 기준선 | **0.46** | 이식 시험 4개 블록의 합계 |
| 경고 기준 | 모듈 비중 0.6 이상 | 이식 시험의 최댓값(0.56)보다 위. 넘으면 실행 기록에 남기고 스키마 확장 후보로 봄 |
| "대부분"으로 보는 기준 | 0.7 이상 | 판정 규칙의 JSON 계층 축소 분기. 이식 시험에서는 나오지 않음 |
| Repair 반복 상한 (모듈당) | **5** | 손 이식에서 Validator와 E1을 다시 통과하는 데 블록당 많아야 2번 걸림. LLM은 여유를 두어 그 두 배 남짓. 같은 IR 해시가 다시 나오면 상한 전에 멈춤(진동 감지) |

두 값 모두 사내 블록으로 다시 판정할 때와 P4 Repair 단독 평가 뒤에 조정합니다.
