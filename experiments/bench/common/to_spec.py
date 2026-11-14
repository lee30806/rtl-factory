"""벤치마크 입력 변환기 (BENCHMARK_PLAN 4.2절): 모듈 헤더 SV + 자연어 프롬프트 → L1 추출 → 2~4절 표 → 10개 섹션 spec.md."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pyslang

from experiments.bench.common.schema import BenchItem
from pipeline.agents import roles as roles_mod, spec_writer
from pipeline.agents.task import ROOT, Task, TaskResult, run_task
from pipeline.importer import r1 as r1_mod, r2 as r2_mod, spec_tables
from pipeline.importer.sources import Design, Inputs

TEMPLATE = ROOT / "docs" / "SPEC 작성 템플릿.md"
OUT = "out/spec.md"

CLK_RE = re.compile(r"(?:^|_|w|r)(clk|clock)(?:$|_|\d|[a-z])", re.I)
RST_RE = re.compile(r"(?:^|_|w|r|a|b)(a?rst_?n?|a?reset_?n?|clr|clear)(?:$|_|\d)", re.I)

BENCH_SPEC_TASK = """\
# 과제: 벤치마크 문제 {top}의 10개 섹션 SPEC 쓰기

너는 RTL 생성 파이프라인의 SpecWriter 에이전트다. 벤치마크 자연어 명세(`prompt.txt`)와 모듈 헤더(`header.sv`)를 읽고
파이프라인 표준 10개 섹션 SPEC(`{out}`)을 작성한다.
이 SPEC만 보고 Architect, Author, Verifier 에이전트들이 설계를 처음부터 구현하고 검증한다.

## 입력
- `template.md`: SPEC 작성 템플릿. 형식 규칙(제목, 10개 섹션 이름과 순서, 요구사항 ID 줄, 시나리오 표, 열린 질문)을 그대로 따른다
- `tables.md`: 모듈 헤더에서 결정론적으로 생성한 2~4절 표. **글자 하나 바꾸지 않고** 그대로 2, 3, 4절에 넣는다
- `prompt.txt`: 벤치마크 원본 자연어 명세 전문
- `header.sv`: 포트와 파라미터가 선언된 모듈 헤더
- `notes.json`: 고정 파라미터, 매크로 상수, 재사용 하위 셀 정보, 제공 include에 정의되지 않은 구성 매크로(`config_macros`),
  원문이 이름을 붙여 설명한 내부 레지스터·단(`named_registers`)
- `figures/`: `prompt.txt`가 `![](./figures/...)`로 참조하는 그림 (있을 때만). 블록도, FSM 상태 전이도, 타이밍도다. Read 도구로 열어 보고
  상태 이름, 전이 조건, 신호 순서처럼 그림에만 있는 정보를 5~8절에 글로 옮긴다. 그림을 보지 않고 그 부분을 비워 두지 않는다.

## 할 일
`{out}`에 10개 섹션 SPEC을 쓴다.
- **원문은 요약본이 아니라 계약의 근거다:** 외부 채점은 `prompt.txt`를 보고 만든 정답과 모든 출력을 사이클 단위 또는 형식 등가로
  비교한다. 원문의 레지스터 비트 맵, 주소·명령 디코드, 상태 비트, 내부 레지스터 이름과 단 구성, 기본값은 하나도 빼지 않고 5·7·9절에
  옮긴다. 파이프라인은 완성된 SPEC 뒤에 원문 전문을 `## 부록 A. 원문 명세`로 자동으로 붙이지만, 5~9절에 없는 요구사항은
  검증되지 않으므로 부록이 있다는 것을 생략의 근거로 삼지 않는다.
- 작성 후 벤치 검사(`PROMPT_COVERAGE`)가 `prompt.txt`의 식별자·비트 범위·수치 리터럴 중 SPEC에 없는 것을 알려 주면, 그 항목을
  5절 디코드 표, 7절 경계 조건, 9절 필수 구조 가운데 맞는 곳에 넣는다.
- 제목은 반드시 `# {top} SPEC`으로 쓴다.
- **2절, 3절, 4절은 `tables.md`의 내용을 글자 하나 바꾸지 않고 그대로 복사해 넣는다.**
- 1절 (`## 1. 목적과 범위`): 모듈의 목적을 한 문단으로 설명하고, 범위 밖 항목을 명시한다.
  - **동작 흐름 소절:** `prompt.txt`에 처리 순서 서술(Processing Flow, 동작 개요, 단계별 설명)이 있으면 1절 끝에 `### 동작 흐름`
    소절을 두고 그 서술을 요약하지 않고 옮긴다 (문장 수 제한 없음). 5~7절 요구사항의 근거 문맥이 되어 Author가 해석할 여지를 줄인다.
  - **파라미터 값이 본문 수치보다 우선한다:** `header.sv`의 파라미터 기본값(예: DEPTH = 16)과 `prompt.txt` 본문의 구체 수치(예: "깊이 8",
    "4비트 포인터", "하위 3비트 주소")가 어긋나면 파라미터 값을 따른다. 테스트벤치는 헤더로 인스턴스하므로 파라미터가 실제 계약이고,
    본문 수치는 다른 설정의 예시로 읽는다. 포인터 폭·주소 폭처럼 파라미터에서 유도되는 수는 파라미터 값으로 다시 계산해 5·7·9절에 쓴다
    (`notes.json`의 `param_conflicts`가 찾은 충돌이다). 충돌 사실과 택한 값은 10절에 `- Q-xx [LESSON] ...`으로 남긴다.
  - **구성 매크로는 제공 include의 정의 여부를 따른다:** `prompt.txt`가 "X가 정의되면(활성화되면) ... 정의되지 않으면 ..."처럼
    매크로로 갈리는 두 구성을 설명하고 `notes.json`의 `config_macros.undefined`에 그 매크로가 있으면, 정의되지 않은 쪽 구성을 구현
    대상으로 삼는다. 채점은 제공 include로 컴파일한 정답과 비교하므로 정의된 쪽을 고르면 맞는 설계가 실패한다 (or1200_sb:
    OR1200_SB_IMPLEMENTED이 주석 처리되어 정답은 FIFO 없는 통과 경로다). 정의되지 않은 쪽이 단순 통과(pass-through)라도 그대로 쓰고,
    정의된 쪽 동작은 1절 범위 밖에 적으며 10절에 `- Q-xx [LESSON] ...`으로 남긴다. 두 구성 중 무엇을 골랐는지 9절 필수 구조에도 적는다.
- 5절 (`## 5. 기능 요구사항`): `- REQ-01 (필수) ...` 형식으로 모든 출력 포트와 입력 포트가 최소 한 번 이상 등장하도록
  동작을 구체적인 수치·조건·타이밍으로 기술한다.
  - **중요 (`spec_gate` 규칙):** 3절 인터페이스 표에 있는 모든 포트 이름(번들인 경우 번들 이름 또는 하위 신호 이름, 클럭·리셋 제외)이
    5~7절 요구사항 문장에 최소 한 번 이상 그대로 적혀야 하며, 모든 출력 포트의 구동 조건과 값이 정의되어야 한다.
  - **한 문장 규칙:** 각 요구사항 ID 줄(`- ID (필수) ...`)에는 마침표로 끝나는 **문장 하나만** 쓴다 (두 문장 이상 쓰면 `REQ_MULTI` 경고가 발생함).
  - **디코드 표 허용:** 주소 맵, 레지스터 비트 맵, 명령·상태 비트, 연산 코드 분기, FSM 상태 전이처럼 조건이 여럿인 내용은 한 문장으로
    뭉개지 않는다. `- FR-RD-02 (필수) wb_adr_i에 따른 wb_dat_o는 아래 표를 따른다.` 한 줄을 쓰고 바로 아래에 `| 조건 | 출력 |` 마크다운 표를
    둔다. 표의 행은 그 요구사항의 일부이며, 원문의 모든 경우(값, 비트 위치, 기본 분기)를 행 하나씩 그대로 옮긴다. 표 칸의 포트 이름도
    `spec_gate` 포트 참조로 인정된다. 원문이 표로 준 것은 표로 옮긴다.
  - **출력 미정의 금지 (벤치 검사 `UNDEFINED_OUTPUT`):** 출력 포트의 값을 `- ID 미정의: ...`로 두지 않는다. 채점은 모든 출력을 비교하므로
    미정의는 실패와 같다. 원문이 어떤 경우의 출력을 말하지 않으면 원문이 암시하는 기본값을 (필수)로 정한다:
    여러 소스 중 하나를 고르는 읽기 다중화에서 아무것도 선택되지 않으면 0 (예: "MTSR이면 0, 그 외 선택된 그룹 값"이면 미사용 그룹도 0);
    데이터를 고르거나 확장하는 변환 경로(바이트·반워드 선택, 부호 확장, 정렬)에서 어떤 변환도 해당하지 않으면 데이터를 그대로 통과
    (가장 넓은 접근과 같은 결과, or1200_mem2reg: 적재가 아닌 lsu_op에서 regdata=memdata), 0이 아니다; 쓰기 조건이 아닌 사이클의 레지스터는
    이전 값 유지; 예약 비트는 0 읽힘. 원문이 연산 코드의 일부 비트만 쓴다고 하거나(`lsu_op[2:0]`) 코드 표의 상위 비트가 구분에 쓰이지 않으면
    디코드도 그 비트만 본다. 정한 값과 근거는 10절에 `- Q-xx [LESSON] ...`으로 남긴다. 예외는 리셋이 없는 도메인의 첫 값 전
    출력(`INIT_NO_RESET`)과 한 도메인만 리셋했을 때(`MULTI_RESET_USAGE`)뿐이다.
  - **프롬프트 충실성 규칙:** `prompt.txt`에 적히지 않은 리셋 해제 직후 첫 사이클 특수 동작(예: 리셋 중의 값을 첫 입력 비트로 간주할지 여부 등)을 임의로 지어내어 필수 요구사항으로 강제하지 않는다. 리셋 해제 후에는 명시적으로 인가된 입력 시퀀스에 대해서만 출력을 규정한다.
  - **명시된 구조는 표준과 달라도 지키고 재검토 알람을 남긴다:** `prompt.txt`가 레지스터 단 수, 동기화 단 수, 포인터나 플래그를
    만드는 레지스터 구성, 카운터 값에 따른 출력 토글 시점처럼 출력 타이밍을 정하는 구조를 명시하면, 표준 관행과 달라도 그 단 수와
    지연, 위상을 5절, 6절, 9절 필수 요구사항에 그대로 쓴다. 이런 설계는 사이클 단위 기대값으로 검증되므로 "더 나은" 구조로 바꾸면
    맞는 설계가 실패한다. 그리고 10절에 `- Q-01 [SPEC 재검토 알람] ...` 형식으로 표준과 다른 점과 그 영향을 적어 사람이 다시 보게 한다.
  - **구조가 정하는 플래그 지연은 7절 수치와 맞춘다 (템플릿 6절 규칙):** `prompt.txt`가 명시한 구조(예: 그레이 포인터를 이진 포인터보다
    한 에지 늦게 래치, 플래그는 포인터의 조합 비교)에서 플래그가 실제로 서는 시점과 그때까지 받히는 개수를 계산해 6절과 7절에 그대로 쓴다:
    "wfull은 16번째 쓰기의 다음 wclk 에지에 1이 되고 그 사이 쓰기 한 번이 더 받힐 수 있다"처럼. "DEPTH개가 차면 wfull=1이고 그때 쓰기는
    무시된다"처럼 구조와 맞지 않는 이상적 수치를 7절에 쓰지 않는다. 두 문장이 서로 맞지 않으면 구조를 따르고 수치를 고친 뒤 10절에
    `- Q-xx [SPEC 재검토 알람] ...`으로 남긴다. 외부 기준 TB는 명시된 구조의 사이클 동작을 기대값으로 쓴다.
  - **비동기 FIFO / CDC 포인터 및 상태 플래그 규칙:** 비동기 FIFO(`asyn_fifo`)나 복수 클럭 도메인 간의 CDC(Clock Domain Crossing) 설계에서, 2단 동기화기(2-stage synchronizer, 예: `rptr_syn`, `wptr_syn`)를 통과한 신호는 이미 해당 수신 클럭 도메인에 안전하게 동기화된 신호이다.
    따라서 수신 도메인의 로컬 포인터(`wptr`, `rptr`)와 2단 동기화된 상대 포인터를 비교하여 가득 참(`wfull`) 및 비어 있음(`rempty`) 플래그를 생성할 때는 추가 플립플롭 없이 **순수 조합논리 비교(`output wire wfull; assign wfull = ...;`)**로 구동하는 것이 표준이다. 동기화된 신호 간의 조합논리 플래그 생성을 "비동기 직접 비교 글리치 유발"로 간주하여 임의로 금지하거나 플래그에 불필요한 레지스터(`wfull_reg`, `rempty_reg`)를 추가하지 않는다.
- 6절 (`## 6. 타이밍과 성능`): `- TIM-01 (필수) ...` 형식으로 클럭 에지 기준 출력 갱신 지연과 처리율을 한 줄에 한 문장씩 명시한다.
  - **지연을 지어내지 않으며 파이프라인 단 수를 보존한다:** `prompt.txt`에 없는 정확한 지연 사이클 수, 핸드셰이크 응답 사이클, 사이클 번호를 정하지 않는다. 단, `prompt.txt`가 구체적인 파이프라인/포인터 레지스터 구성을 명시해 지연 사이클 수가 결정되는 경우(예: 바이너리 주소 레지스터 갱신 후 그레이 레지스터 래치)에는 그 구성을 그대로 반영하며, 임의로 선행 계산(lookahead next-state)을 도입하여 파이프라인 지연을 1사이클 앞당기지 않는다.
    벤치마크 테스트벤치는 사이클 단위(cycle-accurate)로 기대값 벡터와 엄격하게 대조 검증하므로, 프롬프트가 구체적 레지스터 구성을 명시한 경우 "전달 지연은 구현에 따름(검증하지 않음)"과 같은 비검증 면책 문구를 임의로 남발하지 않는다.
    결과를 알리는 출력 플래그(예: `o_en`, `valid`, `done`, `rdy`)가 있고 `prompt.txt`가 지연 단 수를 강제하지 않으면 "o_en=1인 사이클의 result는 그 전에 i_en=1로 받은 입력들의
    결과를 받은 순서대로 낸다"처럼 플래그 기준으로 쓰고, 지연은 "구현에 따름(검증하지 않음)"으로 둔다. "여러 단의 레지스터"처럼
    수를 정하지 않은 표현을 특정 사이클 수로 바꾸지 않는다: 지어낸 값은 그와 다른 지연을 가진 맞는 설계를 실패시킨다.
  - **고정 지연이라고 말한 설계는 지연을 정한다 (템플릿 6절 규칙):** `prompt.txt`가 고정 지연(fixed latency, fixed-latency pipeline)이라고
    말하면 "구현에 따름"으로 두지 않는다. 지연 수가 적혀 있으면 그 값을, 없으면 단 번호가 붙은 레지스터 나열(예: sign_1 ~ sign_20)이나
    "N단 파이프라인"에서 가장 큰 단 번호를 지연 사이클 수로 쓴다 (`notes.json`의 `stage_depth_hint`가 이 값이다). 구조에서 읽어낸 값은
    추정이므로 10절에 `- Q-xx [LESSON] ...`으로 근거와 함께 남긴다.
- 7절 (`## 7. 예외와 코너 케이스`): `- EXC-01 (필수) ...` 형식으로 리셋 인가 시 모든 출력 포트의 초기값과 경계 조건 동작을 한 줄에 한 문장씩 명시한다.
  - 아래 시뮬레이션 모델이 아닐 때, `tables.md` 2절에서 리셋 포트가 `없음`인 도메인의 출력에는 초기값(시뮬레이션 시작 값)을 쓰지 않는다. `prompt.txt`가 initial 문이나
    초기값을 말해도 그렇다: 파이프라인은 initial 문을 쓸 수 없어 보장할 수 없다. 첫 값이 정해지기 전의 출력은 `- EXC-02 미정의: ...` 형식으로 쓴다
    (spec_gate `INIT_NO_RESET` 오류).
  - `tables.md` 2절에서 리셋 동기가 `동기`인 도메인은 "리셋이 걸린 그 사이클"이 아니라 "리셋 신호를 본 다음 사이클부터 출력이 초기화된다"라고 쓴다. 반면 리셋 동기가 `비동기`이거나 리셋 포트가 `wrstn`, `rrstn`, `arstn` 또는 `~wrstn`처럼 비동기 리셋인 도메인은 "리셋 신호가 인가되는 즉시(비동기적으로) 해당 출력/레지스터가 초기화된다"로 명세하며, "다음 사이클부터"라고 왜곡하지 않는다.
  - `tables.md` 2절에 서로 다른 클럭의 리셋 포트가 2개 이상(예: `wrstn`, `rrstn` 또는 `arstn`, `brstn`) 있으면, 모든 리셋 포트 이름을 한 문장에 넣어 "모든 리셋(<포트 이름들>)을 함께, 가장 느린 클럭 4사이클 이상 건다."라는 필수 요구사항 문장과 한쪽만 리셋했을 때의 미정의 문장(`- EXC-02 미정의: ...` 형식)을 반드시 포함한다.
- **조합 블록:** `tables.md` 2절이 도메인 "없음", 도메인 간 관계 "조합 블록"인 행 하나뿐이면 클럭과 리셋이 없는 조합 블록이다.
  6절에는 클럭 지연 대신 "출력은 레지스터를 거치지 않는 입력의 조합 함수다." 같은 문장을 쓰고, 7절에는 리셋 문장 없이
  경계 입력(최댓값, 0, 넘침, 0으로 나누기 등)의 출력만 쓴다. 사이클, 클럭 에지, 리셋, 초기값이라는 말을 쓰지 않는다.
- **시뮬레이션 모델:** 설계가 합성할 수 없는 시간 기반 동작(입력 없이 시간 지연만으로 바뀌는 출력, `#` 지연이나 initial 문에 의존하는 동작)을
  요구하면 9절 첫머리에 `- 구현 종류: 시뮬레이션 모델`을 쓴다. 파이프라인은 이 설계를 합성 없이 동작 모델로 만든다. 사이클 대신 시간 단위로
  초기값, 첫 변화 시각, 변화 간격을 필수 요구사항으로 쓰고, `prompt.txt`가 말한 initial 값과 지연을 그대로 옮긴다. 조합 블록 규칙의 금지어는 적용하지 않는다.
  입력 포트가 없는데 출력이 상수인 합성 회로면 9절에 `- 구현 종류: 합성`을 쓴다 (spec_gate `NO_INPUT_KIND` 오류).
- 8절 (`## 8. 예시 시나리오`): `### SC-01 정상: ...`와 `### SC-02 코너: ...` 두 개 이상의 하위 제목(`###`)과 마크다운 표(`| 입력 | 기대 출력 |`)를 작성한다. 특정 사이클 번호 배열 길이를 강제하기보다 입력 시퀀스에 따른 기대 출력 시퀀스로 기술한다.
- 9절 (`## 9. 구현 제약`): `### 필수 구조`와 `### 금지 사항`을 적는다 (`notes.json`에 상수나 하위 셀이 있으면 명시).
  - **원문 명시 레지스터와 단 구성 (필수):** `notes.json`의 `named_registers`가 비어 있지 않으면 `### 필수 구조` 안에
    `#### 원문 명시 레지스터와 단 구성` 소절을 두고, 원문이 이름 붙인 내부 레지스터·중간 신호·파이프라인 단을 **하나도 빼지 않고**
    `- <이름>: <원문이 말한 역할과 갱신 조건, 속한 단>` 형식으로 옮긴다. 등가 채점은 이 구성이 만드는 사이클 동작을 그대로 기대하므로
    "내부 구현은 자유"로 두면 맞는 설계가 실패한다. 이름을 모두 옮긴 뒤 단 번호가 가장 큰 것을 6절 지연과 맞춘다.
  - **헤더/매크로/인클루드 보존 (필수):** `prompt.txt`, `header.sv`, 또는 `notes.json`에 명시된 매크로 정의(`define), 상수, 헤더 인클루드(`include "..."`), 전처리기 지시문이 있는 경우, `### 필수 구조`에 이를 반드시 명시하여 RTL 구현 시 최상단에 누락 없이 선언 및 인클루드하도록 강제한다.
  - **디폴트 값 및 미매핑 분기 명세 (V 조건 - 상태 유지 vs 초기화):** 주소 디코더, 레지스터 맵, 멀티플렉서, case 문에서 미매핑 주소(unmapped address)나 지정되지 않은 디폴트 조건(default condition), 비활성 사이클(idle)에 대해 이전 상태 값을 그대로 유지할지(`hold`, 예: `wb_dat_o <= wb_dat_o`), 특정 디폴트 값으로 초기화할지(`clear`, 예: `wb_dat_o <= 32'd0`)를 5절 기능 요구사항 및 9절 필수 구조에 명확히 명시한다. 프롬프트가 미매핑 주소 접근 시 이전 값 유지를 명시하거나 요구하는 경우 불필요한 `default: wb_dat_o <= 0`을 삽입하지 않도록 명시한다.
  - **비동기 FIFO 필수 구조:** 이중 포트 RAM(`dual_port_RAM`) 하위 인스턴스화, 쓰기/읽기 포인터 바이너리 레지스터 갱신 및 그레이 코드 변환 레지스터 래치, 2단 플립플롭 동기화기, 동기화된 그레이 포인터와의 순수 조합논리 플래그 비교(`assign wfull = ...; assign rempty = ...;`)를 필수 구조로 명시한다. 동기화기 출력을 사용한 플래그 비교는 이미 동기화된 신호이므로 글리치 금지 대상이 아님을 명확히 한다.
- 10절 (`## 10. 열린 질문`): 없으면 `- 없음`으로 적는다.
  - **LESSON 옮겨 적기 (필수):** `notes.json`의 `lessons`에 항목이 있으면 하나도 빼지 않고 `- Q-xx [LESSON] <내용>` 형식으로 10절에 그대로 적는다
    (헤더 지시문 결함, 리셋 판정 충돌처럼 툴이 찾은 것으로, 사람이 확인해야 한다). 이때 10절에 `- 없음`은 쓰지 않는다.
- **프롬프트 우선:** 위 지침의 예시(비동기 FIFO 구조 등)와 `prompt.txt`가 다르면 `prompt.txt`를 따르고, 다른 점을 10절에 `- Q-xx [LESSON] ...`으로 적는다.

작성 후 반드시 아래 명령을 실행해 `"ok": true`가 나오는지 확인하고, 오류가 있으면 모두 고친 뒤 종료한다:
```bash
python3 tools/pl.py spec_gate {out} --tables tables.md --top {top}
```

## 규칙
- `{out}` 외의 파일은 만들거나 고치지 않는다.
- 요구사항 줄은 반드시 `- <ID> (필수) <문장>` 형식을 지킨다 (괄호 안 의무 수준 필수). 미정의는 `- <ID> 미정의: <문장>`으로 쓴다.
"""


def _read_header_text(header_sv: str) -> str:
    if "\n" not in header_sv and len(header_sv) < 255:
        p = Path(header_sv)
        if p.exists() and p.is_file():
            return p.read_text(encoding="utf-8")
    return header_sv


COND_DIRECTIVES = ("`ifdef", "`ifndef", "`elsif", "`else", "`endif")
PLAIN_DIRECTIVES = ("`define", "`include", "`timescale", "`default_nettype")


def strip_to_header(sv_text: str, top: str, lessons: list[str] | None = None) -> str:
    """모듈 선언 및 선행 컴파일러 지시문(`define, `include 등)부터 포트 목록 끝(`);`)까지만 남기고 본문은 비운 헤더 SV를 만든다.
    조건부 지시문(`ifdef ~ `endif)은 중복을 지우지 않고 순서대로 둔다. 짝이 맞지 않는 지시문은 고쳐 넣고 그 사실을 lessons에 남긴다."""
    pattern = re.compile(rf"\bmodule\s+{re.escape(top)}\b", re.M)
    m = pattern.search(sv_text)
    if m:
        pre_text = sv_text[: m.start()]
        pre_lines: list[str] = []
        cond_depth = 0
        for line in pre_text.splitlines():
            ls = line.strip()
            if ls.startswith(COND_DIRECTIVES):
                if ls.startswith(("`ifdef", "`ifndef")):
                    cond_depth += 1
                elif ls.startswith("`endif"):
                    if cond_depth == 0:
                        if lessons is not None:
                            lessons.append(f"헤더 지시문 결함: 모듈 {top} 앞에 짝이 없는 `endif가 있어 헤더에서 뺐다. 원본 헤더의 조건부 지시문을 확인해야 한다.")
                        continue
                    cond_depth -= 1
                pre_lines.append(ls)
            elif ls.startswith(PLAIN_DIRECTIVES) and ls not in pre_lines:
                pre_lines.append(ls)
        directives = ("\n".join(pre_lines) + "\n\n") if pre_lines else ""
        # 모듈 선언이 `ifdef 블록 안에 있으면 닫는 `endif가 모듈 뒤에 있어 잘린다: 헤더 끝에 닫아 준다
        closing = "`endif\n" * cond_depth
        if cond_depth and lessons is not None:
            lessons.append(f"헤더 지시문 결함: 모듈 {top} 선언이 닫히지 않은 조건부 지시문(`ifdef/`ifndef) {cond_depth}개 안에 있다. "
                           "헤더 끝에 `endif를 붙여 닫았다. 어떤 매크로 조건에서 이 모듈이 정의되는지 사용자가 확인해야 한다.")

        depth = 0
        seen_paren = False
        i = m.end()
        n = len(sv_text)
        while i < n:
            # 주석 건너뛰기
            if sv_text[i : i + 2] == "//":
                nl = sv_text.find("\n", i + 2)
                i = n if nl == -1 else nl + 1
                continue
            if sv_text[i : i + 2] == "/*":
                endc = sv_text.find("*/", i + 2)
                i = n if endc == -1 else endc + 2
                continue
            ch = sv_text[i]
            if ch == "(":
                depth += 1
                seen_paren = True
            elif ch == ")":
                depth -= 1
            elif ch == ";" and seen_paren and depth == 0:
                return directives + sv_text[m.start() : i + 1].rstrip() + "\nendmodule\n" + closing
            i += 1
    if "endmodule" not in sv_text:
        return sv_text.rstrip() + "\nendmodule\n"
    return sv_text


def infer_reset_and_clock_traits(prompt_text: str, clk_port: str, rst_port: str | None, ref_hint: str = "", top: str = "",
                                 lessons: list[str] | None = None) -> tuple[str, str, bool]:
    """프롬프트·포트 이름·R2 힌트에서 (clock_edge: 'pos'|'neg'|'both', reset_active: 'high'|'low', reset_async: bool)을 추론한다.
    리셋 동기/비동기는 프롬프트에 적힌 것을 따르고, 모듈·포트 이름으로 한 추정은 프롬프트가 말하지 않을 때만 쓴다.
    둘이 어긋나면 lessons에 남긴다."""
    combined = (prompt_text + "\n" + ref_hint).lower()
    text_low = prompt_text.lower()
    hint_low = ref_hint.lower()
    ck_l = re.escape(clk_port.lower())
    # 클럭 에지 (양쪽 에지 사용 여부 포함)
    has_pos_hint = bool(re.search(rf"posedge\s+{ck_l}", hint_low))
    has_neg_hint = bool(re.search(rf"negedge\s+{ck_l}", hint_low))
    dual_in_prompt = bool(re.search(r"(both\s+(the\s+)?(rising|positive)\s+and\s+(falling|negative)\s+edges|(rising|positive)\s+and\s+(falling|negative)\s+edges\s+of\s+(the\s+)?clock)", text_low))
    if (has_pos_hint and has_neg_hint) or dual_in_prompt:
        clk_edge = "both"
    elif (has_neg_hint and not has_pos_hint) or re.search(rf"negedge\s+{ck_l}|(falling|negative)\s+edge\s+of\s+.*{ck_l}|하강\s*에지", text_low):
        clk_edge = "neg"
    else:
        clk_edge = "pos"

    if rst_port is None:
        return clk_edge, "high", False

    rl = re.escape(rst_port.lower())
    # else if (rst == 0) 같은 비활성 분기 문구는 제거한 뒤 극성 판별
    cleaned_combined = re.sub(rf"else\s+if\s*\(\s*{rl}\s*==\s*(?:1'b)?0\s*\)", "", combined)
    # 리셋 극성
    rst_low_name = bool(re.search(r"(_n|n)$", rst_port, re.I)) and not rst_port.lower().endswith("en")
    explicit_low = bool(
        re.search(
            rf"active[- ]low|low[- ]active|[!~]\s*{rl}\b|negedge\s+{rl}\b|\bif\s*\(\s*{rl}\s*==\s*(?:1'b)?0\s*\)|극성\s*\|\s*low|저활성",
            cleaned_combined,
        )
    )
    explicit_high = bool(
        re.search(
            rf"active[- ]high|high[- ]active|posedge\s+{rl}\b|\bif\s*\(\s*{rl}\s*(?:==\s*(?:1'b)?1)?\s*\)|(?:reset|rst)\s*\({rl}\)\s*(?:signal\s*)?is\s*high|극성\s*\|\s*high|고활성",
            cleaned_combined,
        )
    )
    if explicit_high and not explicit_low:
        active = "high"
    elif explicit_low and not explicit_high:
        active = "low"
    elif rst_low_name:
        active = "low"
    else:
        active = "high"

    # 동기 / 비동기 리셋
    async_name = bool(re.match(r"^a(rst|reset)", rst_port, re.I))
    is_async_module = bool(re.search(r"(?:asyn|async|cdc)", top.lower())) if top else False
    explicit_async = bool(
        re.search(
            rf"async(hronous|hronously)?\s*(?:reset|fifo|circuit|cdc)|비동기\s*(?:fifo|리셋)|(?:or|,)\s*(?:posedge|negedge)?\s*{rl}\b|negedge\s+{rl}\b"
            rf"|(?:falling|rising|negative|positive)\s+edge\s+of\s+(?:the\s+)?(?:[\w-]+\s+){{0,4}}\(?{rl}\b",
            combined,
        )
    )
    # "synchronous reset" 외에 "rst (..., Synchronous to clk)", "reset synchronized to the clock" 어순도 동기 명시로 본다
    explicit_sync = bool(re.search(
        rf"\bsync(?:hronous|hronously)?\s+reset|\b(?:reset|rst|{rl})\b[^.\n]{{0,40}}\bsynchronous(?:ly)?\s+(?:to|with)\b"
        rf"|\b(?:reset|rst|{rl})\b[^.\n]{{0,40}}\bsynchroni[sz]ed\s+(?:to|with)\b|동기\s*리셋|리셋\s*동기\s*\|\s*동기", text_low))
    if hint_low and not re.search(rf"(?:or|,)\s*(?:posedge|negedge)?\s*{rl}\b", hint_low) and re.search(rf"always\s*@\s*\(\s*(?:posedge|negedge)\s+{ck_l}\s*\)", hint_low):
        is_async = False
    elif explicit_sync and not explicit_async:
        is_async = False
        if (is_async_module or async_name) and lessons is not None:
            lessons.append(f"리셋 판정 충돌: 리셋 {rst_port}는 이름({top or rst_port})으로는 비동기로 보이지만 prompt.txt가 동기 리셋을 명시해 "
                           "프롬프트대로 동기로 썼다. 의도가 맞는지 사용자가 확인해야 한다.")
    elif explicit_async or async_name or is_async_module:
        is_async = True
        if not explicit_async and lessons is not None:
            lessons.append(f"리셋 판정 추정: prompt.txt에 리셋 {rst_port}의 동기/비동기가 없어 이름({top if is_async_module else rst_port})으로 "
                           "비동기로 추정했다. 사용자가 확인해야 한다.")
    else:
        is_async = False
        if not explicit_sync and lessons is not None:
            lessons.append(f"리셋 판정 기본값: prompt.txt에 리셋 {rst_port}의 동기/비동기가 없어 동기로 정했다. 사용자가 확인해야 한다.")
    return clk_edge, active, is_async


def _inject_dummy_seq_if_empty(sv_text: str, top: str, prompt_text: str, incdirs: list[Path], defines: dict[str, str],
                               params: dict[str, str], tmp_dir: Path, ref_hint: str = "", lessons: list[str] | None = None) -> Path:
    """빈 모듈 헤더(`module top(...); endmodule`)에 클럭/리셋 더미 순차 블록과 파라미터 참조를 넣어 r1이 도메인·포트·파라미터를 추출하게 한다."""
    raw_file = tmp_dir / f"{top}_raw.sv"
    if "endmodule" not in sv_text:
        sv_text = sv_text.rstrip() + "\nendmodule\n"
    raw_file.write_text(sv_text, encoding="utf-8")
    design = Design(Inputs([raw_file], top, params, incdirs, defines))
    body = design.top.body
    SK = pyslang.SymbolKind
    has_seq = any(m.kind == SK.ProceduralBlock for m in body)
    if has_seq:
        return raw_file

    in_ports = [p.name for p in body.portList if p.kind == SK.Port and p.direction == pyslang.ArgumentDirection.In]
    out_ports = [p.name for p in body.portList if p.kind == SK.Port and p.direction == pyslang.ArgumentDirection.Out]
    param_names = [m.name for m in body if m.kind == SK.Parameter and not m.isLocalParam]

    clks = [p for p in in_ports if CLK_RE.search(p)]
    rsts = [p for p in in_ports if RST_RE.search(p) and p not in clks]
    data_ins = [p for p in in_ports if p not in clks and p not in rsts]

    if not clks:
        if param_names:
            expr = " + ".join(f"int'({p})" for p in param_names)
            idx = sv_text.rfind("endmodule")
            augmented = sv_text[:idx] + f"\n  wire [31:0] __bench_p = 32'({expr});\nendmodule\n"
            aug_file = tmp_dir / f"{top}_aug.sv"
            aug_file.write_text(augmented, encoding="utf-8")
            return aug_file
        return raw_file

    # 클럭-리셋 짝짓기
    pairs: list[tuple[str, str | None]] = []
    if len(clks) == 1:
        pairs.append((clks[0], rsts[0] if rsts else None))
    else:
        used_rsts: set[str] = set()
        for ck in clks:
            prefix = re.sub(r"^(clk|clock)_?|_?(clk|clock)$", "", ck, flags=re.I).strip("_")
            match_r = next((r for r in rsts if r not in used_rsts and prefix and r.lower().startswith(prefix.lower())), None)
            if match_r is None and len(rsts) == 1:
                match_r = rsts[0]
            if match_r:
                used_rsts.add(match_r)
            pairs.append((ck, match_r))

    lines = []
    if param_names:
        expr = " + ".join(f"int'({p})" for p in param_names)
        lines.append(f"  wire [31:0] __bench_p = 32'({expr});")
    else:
        lines.append("  wire [31:0] __bench_p = 32'd0;")

    any_prefix_out = any(
        any(op.lower().startswith(re.sub(r"^(clk|clock)_?|_?(clk|clock)$", "", ck, flags=re.I).strip("_").lower()) for op in out_ports)
        for ck, _ in pairs
        if re.sub(r"^(clk|clock)_?|_?(clk|clock)$", "", ck, flags=re.I).strip("_")
    )

    for idx, (ck, rst) in enumerate(pairs):
        edge, active, is_async = infer_reset_and_clock_traits(prompt_text, ck, rst, ref_hint=ref_hint, top=top, lessons=lessons)
        edges = ["posedge", "negedge"] if edge == "both" else (["negedge"] if edge == "neg" else ["posedge"])
        prefix = re.sub(r"^(clk|clock)_?|_?(clk|clock)$", "", ck, flags=re.I).strip("_").lower()
        if len(pairs) > 1 and prefix:
            dom_ins = [p for p in data_ins if p.lower().startswith(prefix)] or (data_ins if idx == 0 else [])
            if any_prefix_out:
                dom_outs = [p for p in out_ports if p.lower().startswith(prefix)]
            else:
                dom_outs = out_ports if idx == len(pairs) - 1 else []
        else:
            dom_ins = data_ins
            dom_outs = out_ports
        read_terms = ["__bench_p", *dom_ins]
        rhs = "|{" + ", ".join(read_terms) + "}" if read_terms else "1'b0"
        dregs: list[str] = []
        for e_idx, ck_kw in enumerate(edges):
            dreg = f"__bench_d{idx}_{e_idx}"
            dregs.append(dreg)
            lines.append(f"  reg {dreg};")
            erhs = rhs if e_idx == 0 else dregs[0]
            if rst:
                rst_kw = "posedge" if active == "high" else "negedge"
                sens = f"{ck_kw} {ck} or {rst_kw} {rst}" if is_async else f"{ck_kw} {ck}"
                cond = rst if active == "high" else f"!{rst}"
                lines.append(f"  always @({sens}) if ({cond}) {dreg} <= 1'b0; else {dreg} <= {erhs};")
            else:
                lines.append(f"  always @({ck_kw} {ck}) {dreg} <= {erhs};")
        comb_dreg = " | ".join(dregs)
        for op in dom_outs:
            p_sym = body.findPort(op)
            is_var = getattr(getattr(p_sym, "internalSymbol", None), "kind", None) == SK.Variable
            if not is_var:
                lines.append(f"  assign {op} = ({comb_dreg}) ? '0 : '0;")
            else:
                lines.insert(-len(edges), f"  always @(posedge {ck}) {op} <= '0;") if False else None

    idx = sv_text.rfind("endmodule")
    augmented = sv_text[:idx] + "\n" + "\n".join(lines) + "\nendmodule\n"
    aug_file = tmp_dir / f"{top}_aug.sv"
    aug_file.write_text(augmented, encoding="utf-8")
    return aug_file


_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
              "sixteen": 16, "thirty-two": 32, "sixty-four": 64}


def prompt_param_conflicts(prompt_text: str, params: list[dict]) -> list[str]:
    """헤더 파라미터 기본값과 프롬프트 본문의 같은 이름 수치가 어긋나는 것을 찾는다 (asyn_fifo: DEPTH = 16 대 "depth 8").
    파라미터 이름(대소문자 무시)이 숫자와 함께 나오는 자리만 본다. 틀려도 LESSON만 내고 결정은 작성자가 한다."""
    out: list[str] = []
    for p in params:
        name, default = str(p.get("name", "")), str(p.get("default", "")).strip()
        if not name or not re.fullmatch(r"\d+", default):
            continue
        vals: list[str] = []
        pat = rf"\b{re.escape(name)}\b\s*(?:=|:|is|of|to|for|with)?\s*(?:a\s+)?(\d+|{'|'.join(_NUM_WORDS)})\b"
        for m in re.finditer(pat, prompt_text, re.I):
            v = m.group(1).lower()
            v = str(_NUM_WORDS.get(v, v))
            if v != default and v not in vals:
                vals.append(v)
        if vals:
            out.append(f"파라미터 충돌: header.sv의 {name}={default}인데 prompt.txt 본문은 {name} {', '.join(vals)}을(를) 말한다. "
                       f"SPEC은 파라미터 값 {default}을(를) 따르고 본문 수치는 예시로 본다. 사용자가 확인해야 한다.")
    return out


def prompt_stage_depth(prompt_text: str) -> tuple[int | None, str | None]:
    """고정 지연 설계에서 단 번호 레지스터 나열(sign_1 ~ sign_20)이나 "N-stage pipeline"으로 파이프라인 깊이를 추정한다.
    고정 지연(fixed latency)이나 파이프라인이라는 말이 없으면 (None, None)."""
    if not re.search(r"fixed[- ]latency|pipelin|고정\s*지연|파이프라인", prompt_text, re.I):
        return None, None
    best, why = 0, None
    for m in re.finditer(r"\b(\d+)[- ]stage\s+pipeline|\bpipeline\s+(?:of|with)\s+(\d+)\s+stages?|(\d+)\s*단\s*파이프라인", prompt_text, re.I):
        n = int(next(g for g in m.groups() if g))
        if n > best:
            best, why = n, f'prompt.txt의 "{m.group(0)}"'
    stems: dict[str, set[int]] = {}
    for m in re.finditer(r"\b([A-Za-z]\w*?)_(\d{1,3})\b", prompt_text):
        stems.setdefault(m.group(1), set()).add(int(m.group(2)))
    for stem, nums in stems.items():
        if len(nums) >= 3 and max(nums) > best:
            best, why = max(nums), f"prompt.txt의 단 번호 레지스터 {stem}_{min(nums)} ~ {stem}_{max(nums)}"
    return (best, why) if best else (None, None)


_UPPER_ID_RE = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b")


def config_macros(prompt_text: str, incdirs: list[Path], defines: dict[str, str], header_text: str) -> dict[str, list[str]]:
    """프롬프트가 말하는 구성 매크로 가운데 제공 include·헤더가 아는 것(`ifdef 검사나 주석 처리된 `define)을 정의 여부로 나눈다.
    채점은 제공 include로 정답을 컴파일하므로 정의되지 않은 매크로의 구성이 정답이다 (ChipVerilog or1200_sb 등 30문제)."""
    texts = [header_text]
    for d in incdirs:
        for f in sorted(Path(d).glob("*")):
            if f.is_file() and f.suffix in (".v", ".vh", ".sv", ".svh"):
                texts.append(f.read_text(encoding="utf-8", errors="replace"))
    blob = "\n".join(texts)
    defined = set(re.findall(r"^\s*`define\s+(\w+)", blob, re.M)) | set(defines)
    # 구성 매크로로 볼 근거: include가 `ifdef로 검사하거나 주석 처리한 이름, 또는 프롬프트가 "X is (not) defined/enabled/disabled"라고 말한 이름
    known = set(re.findall(r"`(?:ifdef|ifndef|elsif)\s+(\w+)", blob)) | set(re.findall(r"^\s*//\s*`define\s+(\w+)", blob, re.M)) | defined
    known |= set(re.findall(r"\b([A-Z][A-Z0-9_]+)\b[^.\n]{0,40}?\b(?:is|are|be|when|if|unless)\b[^.\n]{0,20}?"
                            r"\b(?:defined|undefined|enabled|disabled|commented out|not set|set)\b", prompt_text))
    mentioned = [m for m in dict.fromkeys(_UPPER_ID_RE.findall(prompt_text)) if m in known]
    return {"undefined": [m for m in mentioned if m not in defined], "defined": [m for m in mentioned if m in defined]}



# ---- 원문 보존 (SPEC 압축 손실 보상) --------------------------------------------------------------------------------
# ChipVerilog haiku 63건에서 SPEC은 원문보다 중간값 27% 짧고 비트 필드 표기의 절반 이상이 사라졌으며, 원문이 이름 붙인
# 내부 레지스터(fpu_addsub_pipeline 208개 중 3개 남음)와 "미정의"로 둔 출력(or1200_sprs to_wbmux)이 등가 채점 실패로 이어졌다.
# 보상: (1) 원문 전문을 SPEC 뒤에 부록으로 붙여 모든 에이전트가 보게 하고, (2) 출력의 미정의를 금지하고,
# (3) 원문 식별자·비트·수치의 SPEC 잔존율을 재서 첫 작성 뒤 한 번 다시 쓰게 하며, (4) 이름 붙은 레지스터 목록을 notes에 준다.
APPENDIX_HEAD = "## 부록 A. 원문 명세"
APPENDIX_NOTE = """\
이 부록은 벤치마크 원문 명세(`prompt.txt`)를 그대로 옮긴 것이다. 1~10절 SPEC이 계약이며, 두 글이 어긋나면 SPEC 5~9절이 우선한다.
SPEC이 요약했거나 빠뜨린 세부(레지스터 비트, 주소·명령 디코드, 내부 레지스터와 단 구성, 기본값, 동작 흐름)는 이 원문을 따라
구현하고 검증한다. 외부 채점은 이 원문으로 만든 정답과 모든 출력을 비교한다. 부록은 SPEC 검사기가 섹션·요구사항으로 읽지 않는다.
"""
_IDENT_RE = re.compile(r"(?<![\w`.])[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+\b")
_BITS_RE = re.compile(r"\b\w+\[\d+(?::\d+)?\]")
_LIT_RE = re.compile(r"\b\d+'[hbdoHBDO][0-9a-fA-F_xXzZ?]+\b|\b0x[0-9a-fA-F]+\b")
_NAMED_REG_RE = re.compile(r"(?<![\w`.])[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*_\d+\b")
_UNDEF_EXEMPT_RE = re.compile(r"리셋|초기|시뮬레이션\s*시작|첫\s*값|전원|reset|initial", re.I)
COVERAGE_MIN = 0.8
COVERAGE_MIN_TOKENS = 10


def with_prompt_appendix(spec_text: str, prompt_text: str) -> str:
    """SPEC 뒤에 원문 명세를 부록으로 붙인다 (이미 있으면 바꿔 넣는다: 멱등)."""
    body = spec_text.split("\n" + APPENDIX_HEAD, 1)[0].rstrip()
    if body.startswith(APPENDIX_HEAD):
        body = ""
    return f"{body}\n\n{APPENDIX_HEAD}\n\n{APPENDIX_NOTE}\n{prompt_text.rstrip()}\n"


def strip_prompt_appendix(spec_text: str) -> str:
    return spec_text.split("\n" + APPENDIX_HEAD, 1)[0]


def prompt_tokens(prompt_text: str, top: str = "") -> set[str]:
    """원문에서 SPEC에 남아야 할 토큰: 밑줄 식별자, 비트 범위 표기, 크기 있는 수치 리터럴(1비트 제외)·16진수."""
    toks = set(_IDENT_RE.findall(prompt_text)) | set(_BITS_RE.findall(prompt_text)) | set(_LIT_RE.findall(prompt_text))
    toks -= {top}
    return {t for t in toks if len(t) >= 3 and not re.fullmatch(r"[A-Za-z]_[A-Za-z]", t) and not t.endswith((".v", ".sv"))
            and not re.fullmatch(r"1'[bB][01xXzZ?]", t)}


def _token_present(tok: str, body: str) -> bool:
    if tok in body:
        return True
    # `name[N:0]`는 포트 전체 폭 표기다. 3절 표가 폭을 `logic [N:0]`으로 따로 적으므로 이름이 있으면 남은 것으로 본다.
    # 부분 범위(`doutb[19:1]`, `TTCR[31:28]`)는 그대로 있어야 한다
    m = re.fullmatch(r"(\w+)\[(\d+):0\]", tok)
    return bool(m) and re.search(rf"(?<![A-Za-z0-9_]){re.escape(m.group(1))}(?![A-Za-z0-9_])", body) is not None


def prompt_coverage(prompt_text: str, spec_text: str, top: str = "") -> dict[str, Any]:
    """원문 토큰 가운데 SPEC(부록 제외)에 그대로 남은 비율과 빠진 목록."""
    body = strip_prompt_appendix(spec_text)
    toks = prompt_tokens(prompt_text, top)
    missing = sorted(t for t in toks if not _token_present(t, body))
    total = len(toks)
    return {"total": total, "found": total - len(missing), "ratio": (total - len(missing)) / total if total else 1.0, "missing": missing}


def named_registers(prompt_text: str, top: str = "") -> list[str]:
    """원문이 이름 붙여 설명한 내부 레지스터·단 (숫자 접미 식별자: sum_2, exponent_3, sign_20). 포트 이름은 헤더가 따로 준다."""
    return sorted({t for t in _NAMED_REG_RE.findall(prompt_text) if t != top and len(t) >= 3})


def undefined_output_errors(spec_text: str, top_l1: dict) -> list[str]:
    """5~7절에서 출력 포트 값을 미정의로 둔 요구사항. 리셋 없는 도메인의 첫 값 전 출력과 한쪽만 리셋했을 때는 예외다."""
    from pipeline.spec.parse import parse

    outs: list[str] = []
    for p in top_l1.get("ports", []):
        for sgl in (p["signals"] if "bundle" in p else [p]):
            if sgl.get("dir") == "output":
                outs.append(sgl["name"])
    errs = []
    for r in parse(spec_text).requirements:
        if r.section not in (5, 6, 7) or r.level != "미정의" or _UNDEF_EXEMPT_RE.search(r.text):
            continue
        hit = [o for o in outs if re.search(rf"(?<![A-Za-z0-9_]){re.escape(o)}(?![A-Za-z0-9_])", r.text)]
        if hit:
            errs.append(f"[벤치 검사] UNDEFINED_OUTPUT {r.id}: 출력 {', '.join(hit)}의 값을 미정의로 둠. 채점은 모든 출력을 비교하므로 "
                        "원문이 암시하는 기본값(읽기 다중화의 선택 없음·미매핑은 0, 데이터 변환 경로의 해당 없음은 그대로 통과, 쓰기 조건 아니면 "
                        "이전 값 유지, 예약 비트 0)을 (필수)로 정하고 근거를 10절 [LESSON]에 남긴다")
    return errs


def coverage_errors(prompt_text: str, spec_text: str, top: str, minimum: float = COVERAGE_MIN) -> list[str]:
    cov = prompt_coverage(prompt_text, spec_text, top)
    if cov["total"] < COVERAGE_MIN_TOKENS or cov["ratio"] >= minimum:
        return []
    shown = cov["missing"][:40]
    more = f" 외 {len(cov['missing']) - len(shown)}개" if len(cov["missing"]) > len(shown) else ""
    return [f"[벤치 검사] PROMPT_COVERAGE 원문 식별자·비트 범위·수치 {cov['found']}/{cov['total']} ({cov['ratio']:.0%})가 SPEC에 남음 "
            f"(기준 {minimum:.0%}). 빠진 항목을 5절 디코드 표, 7절 경계 조건, 9절 필수 구조에 그대로 옮긴다: {', '.join(shown)}{more}"]


def bench_spec_check(top_l1: dict, prompt_text: str, top: str):
    """SpecWriter 종료 검사: 기존 spec_gate + 출력 미정의 금지(항상) + 원문 커버리지(첫 작성 뒤 한 번만 다시 쓰게 한다)."""
    base = spec_writer.check(top_l1)
    calls = [0]

    def _check(work: Path) -> list[str]:
        calls[0] += 1
        errs = base(work)
        p = work / OUT
        if not p.exists():
            return errs
        text = p.read_text(encoding="utf-8")
        errs += undefined_output_errors(text, top_l1)
        if calls[0] == 1:
            errs += coverage_errors(prompt_text, text, top)
        return errs
    return _check


HEADER_OUT = "out/header.sv"
HEADER_TASK = """\
# 과제: 벤치마크 문제 {top}의 모듈 헤더 쓰기

너는 RTL 생성 파이프라인의 HeaderWriter 에이전트다. 벤치마크 자연어 명세(`prompt.txt`)만 보고 모듈 `{top}`의 헤더
(포트와 파라미터 선언만 있는 빈 모듈)를 `{out}`에 쓴다. 이 헤더가 테스트벤치가 인스턴스하는 인터페이스 계약이 된다.

## 입력
- `prompt.txt`: 벤치마크 원본 자연어 명세 (제공 문서가 있으면 뒤에 붙어 있다)
- `cells/`: 제공된 재사용 하위 모듈 RTL (있을 때만). 상위 모듈의 포트는 명세가 말하는 대로 쓰고 하위 모듈 포트를 그대로 올리지 않는다

## 할 일
- `{out}`에 `module {top} #(파라미터) (포트); endmodule`만 쓴다 (ANSI 스타일, 본문 없음, `timescale·`include 없음).
- 포트 이름, 방향, 폭, 순서와 파라미터 이름, 기본값은 명세의 인터페이스 표, 포트 목록, 신호 설명을 그대로 따른다.
  이름을 바꾸거나 줄이거나 더하지 않는다. 명세가 말하지 않는 포트(디버그, 선택 사항)는 더하지 않는다.
- 폭이 파라미터로 주어지면 `[WIDTH-1:0]`처럼 파라미터 식으로 쓴다. 명세가 폭을 말하지 않는 1비트 신호는 폭 없이 쓴다.
- `input [7:0] a`, `output reg [7:0] b`처럼 Verilog-2001 문법으로 쓴다 (채점기는 Icarus Verilog다). `logic`은 쓰지 않는다.
- 두 가지로 읽히는 부분은 `endmodule` 뒤 주석 `// Q: ...`으로 남긴다.

## 규칙
- `{out}` 외의 파일은 만들거나 고치지 않는다.
"""


def header_check(top: str):
    def _check(work: Path) -> list[str]:
        text = (work / HEADER_OUT).read_text(encoding="utf-8", errors="replace")
        errs: list[str] = []
        if re.search(r"^\s*(always|assign|initial)\b", text, re.M):
            errs.append("헤더에 본문(always/assign/initial)이 있음: 포트와 파라미터 선언만 쓴다")
        tree = pyslang.SyntaxTree.fromText(text)
        comp = pyslang.Compilation()
        comp.addSyntaxTree(tree)
        diags = [d for d in comp.getAllDiagnostics() if d.isError()]
        if diags:
            errs.append("헤더가 파싱되지 않음: " + "; ".join(str(d.code) for d in diags[:3]))
        tops = [i for i in comp.getRoot().topInstances if i.name == top]
        if not tops:
            errs.append(f"모듈 이름이 {top!r}가 아님")
        elif not [p for p in tops[0].body.portList if p.kind == pyslang.SymbolKind.Port]:
            errs.append("포트가 하나도 없음")
        return errs
    return _check


def ensure_header(item: BenchItem, work_dir: Path, roles: roles_mod.Roles, isolate: bool = True) -> TaskResult | None:
    """헤더가 없는 문제(CVDP)는 HeaderWriter 에이전트가 프롬프트만 보고 헤더를 쓴다. 한 번 쓴 헤더는 work_dir에 남겨 다시 쓰지 않는다.
    테스트벤치(채점 기준)는 공식 설정대로 헤더 작성에 쓰지 않는다."""
    if item.header_sv.strip():
        return None
    out = Path(work_dir).resolve() / "header_writer" / "header.sv"
    if out.exists():
        item.header_sv = str(out)
        return None
    inputs: dict[str, str | Path] = {"prompt.txt": item.prompt_text}
    for c in item.cells:
        if Path(c).exists():
            inputs[f"cells/{Path(c).name}"] = Path(c)
    task = Task("spec_writer", f"bench_header/{item.id}", HEADER_TASK.format(top=item.top, out=HEADER_OUT), inputs=inputs,
                outputs=[HEADER_OUT], write_scope=["out/"], tools=False)
    task.isolate = isolate
    cfg = roles.agents.get("spec_writer") or roles.agents.get("verifier")
    if isolate:
        from pipeline.agents.task import RUNS
        import hashlib
        key = hashlib.sha256(str(out).encode()).hexdigest()[:8]
        agent_work, run_root = RUNS / f"bench-header-{item.id}-{key}", RUNS
    else:
        agent_work, run_root = out.parent / "agent_work", None
    res = run_task(cfg, task, agent_work, check=header_check(item.top), retries=roles.limits.get("agent_retries", 2), run_root=run_root)
    if not res.ok:
        raise RuntimeError(f"헤더 작성 실패: {res.errors[:3]}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(Path(res.outputs[HEADER_OUT]).read_text(encoding="utf-8"), encoding="utf-8")
    item.header_sv = str(out)
    return res


def extract_header_l1(item: BenchItem, work_dir: Path) -> tuple[dict, str, dict]:
    """모듈 헤더 SV와 프롬프트에서 Block Top L1과 2~4절 마크다운 표(`tables.md`), `notes.json`을 결정론적으로 추출한다."""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    raw_text = _read_header_text(item.header_sv)
    lessons: list[str] = []
    clean_hdr = strip_to_header(raw_text, item.top, lessons)
    incdirs = [Path(p).resolve() for p in item.incdirs]
    cells = [Path(p).resolve() for p in item.cells if Path(p).exists()]
    # 제공 셀에 `timescale이 있는데 헤더에 없으면 slang이 MissingTimeScale로 거부한다 (CVDP gcd): 셀의 지시문을 헤더 앞에 둔다
    if "`timescale" not in clean_hdr:
        for c in cells:
            if m := re.search(r"^\s*`timescale\b.*$", c.read_text(encoding="utf-8", errors="replace"), re.M):
                clean_hdr = m.group(0).strip() + "\n" + clean_hdr
                break
    (work_dir / "header.sv").write_text(clean_hdr, encoding="utf-8")

    # 클럭 에지·리셋 극성/동기는 프롬프트와 헤더로만 정한다. 정답 RTL(reference_rtl)에서 힌트를 뽑지 않는다 (엔진 간 공정 비교)
    aug_file = _inject_dummy_seq_if_empty(clean_hdr, item.top, item.prompt_text, incdirs, item.defines, item.params, work_dir, lessons=lessons)

    inp = Inputs([*cells, aug_file], item.top, dict(item.params), incdirs, dict(item.defines))
    r1_res = r1_mod.run(inp, work_dir)
    if r1_res.get("rejects") or not r1_res.get("ok"):
        raise RuntimeError(f"R1 헤더 추출 실패: {r1_res.get('rejects') or r1_res.get('validate')}")

    irs = {p.name.removesuffix(".ir.json"): json.loads(p.read_text(encoding="utf-8"))
           for p in sorted((work_dir / "r1" / "ir").glob("*.ir.json"))}
    req = r2_mod.make_request(r1_res, irs)

    # R2 무인 자동 응답
    answers: dict[str, str] = {}
    top_doms = irs[item.top].get("domains", [])
    default_dom = top_doms[0]["name"] if top_doms else "async"
    for q in req["items"]:
        qid, kind = q["id"], q["kind"]
        if kind == "freq":
            answers[qid] = "100"
        elif kind == "relation":
            answers[qid] = "async"
        elif kind == "protocol":
            sigs = q.get("signals", [])
            answers[qid] = "valid_ready" if len(sigs) >= 3 else "none"
        elif kind == "review":
            if q.get("port"):
                answers[qid] = default_dom
            else:
                answers[qid] = "ok"

    errs = r2_mod.set_answers(req, answers)
    if errs:
        raise RuntimeError(f"R2 응답 설정 오류: {errs}")
    applied_irs, apply_errs = r2_mod.apply(req, r1_res, irs)
    if apply_errs:
        raise RuntimeError(f"R2 적용 오류: {apply_errs}")

    top_ir = applied_irs[item.top]
    # 헤더 `define 매크로는 R1에서 local 파라미터가 된다. SPEC 표(4절)는 local 파라미터를 담지 않으므로
    # 포트 타입의 매크로 이름을 값으로 바꾼다 (ChipVerilog cordic `XY_BITS, or1200 `OR1200_OPERAND_WIDTH)
    local_vals = {p["name"]: str(p["default"]) for p in top_ir.get("params", []) if p.get("local")}

    def _resolve(t: str) -> str:
        for name, val in local_vals.items():
            if re.search(rf"\b{re.escape(name)}\b", t):
                t = re.sub(rf"\b{re.escape(name)}\b", val if re.fullmatch(r"\d+", val) else f"({val})", t)
                lessons.append(f"헤더 매크로 폭: 포트 타입의 {name}를 값 {val}로 바꿔 SPEC에 썼다 (매크로를 바꾸면 SPEC도 바꿔야 한다).")
        return t

    def _port(p: dict) -> dict:
        q = {k: v for k, v in p.items() if k not in ("kind", "reset")}
        if "type" in q:
            q["type"] = _resolve(q["type"])
        return q

    top_l1 = {
        "module": top_ir["module"],
        "role": "block_top",
        "params": [p for p in top_ir.get("params", []) if not p.get("local")],
        "domains": top_ir.get("domains", []),
        "ports": [
            _port(p) if "bundle" not in p else {**p, "signals": [_port(s) for s in p["signals"]]}
            for p in top_ir.get("ports", [])
        ],
    }
    tables = spec_tables.tables_md(top_l1)
    # 프롬프트 본문과 헤더 파라미터의 충돌, 고정 지연 설계의 단 수 힌트는 결정론적으로 찾아 작성자가 놓치지 못하게 LESSON으로 둔다
    conflicts = prompt_param_conflicts(item.prompt_text, top_l1["params"])
    lessons.extend(conflicts)
    depth, depth_why = prompt_stage_depth(item.prompt_text)
    fixed_latency = bool(re.search(r"fixed[- ]latency|고정\s*지연", item.prompt_text, re.I))
    if depth and fixed_latency:
        lessons.append(f"지연 추정: prompt.txt가 고정 지연이라 하지만 지연 수가 없어 {depth_why}로 파이프라인 깊이를 최소 {depth}단으로 보았다. "
                       f"6절 지연은 이 값을 기준으로 쓰되 사용자가 확인해야 한다.")
    cfg_macros = config_macros(item.prompt_text, incdirs, item.defines, raw_text)
    if cfg_macros["undefined"]:
        lessons.append(f"구성 매크로: prompt.txt가 말하는 {', '.join(cfg_macros['undefined'])}은(는) 제공 include에 정의되어 있지 않다"
                       "(주석 처리됨). 채점 정답은 이 매크로 없이 컴파일되므로 정의되지 않은 쪽 구성을 구현 대상으로 삼고 "
                       "정의된 쪽 동작은 범위 밖에 둔다.")
    notes = {
        "benchmark": item.benchmark,
        "id": item.id,
        "category": item.category,
        "params": item.params,
        "defines": item.defines,
        "macros": r1_res.get("macros", {}),
        "cells": [Path(c).name for c in item.cells],
        "param_conflicts": conflicts,
        "stage_depth_hint": depth if fixed_latency else None,
        "config_macros": cfg_macros,
        "figures": [Path(f).name for f in item.attachments],
        "named_registers": named_registers(item.prompt_text, item.top),
        "lessons": list(dict.fromkeys(lessons)),
    }
    (work_dir / "top.l1.json").write_text(json.dumps(top_l1, ensure_ascii=False, indent=2), encoding="utf-8")
    (work_dir / "tables.md").write_text(tables, encoding="utf-8")
    (work_dir / "notes.json").write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")
    return top_l1, tables, notes


def make_bench_spec_task(item: BenchItem, tables: str, header_text: str, notes: dict) -> Task:
    """정답 RTL(reference_rtl)을 절대 노출하지 않고 프롬프트와 헤더, 2~4절 표만 주는 BenchSpecWriter Task."""
    inputs: dict[str, str | Path] = {
        "template.md": TEMPLATE,
        "tables.md": tables,
        "prompt.txt": item.prompt_text,
        "header.sv": header_text,
        "notes.json": json.dumps(notes, ensure_ascii=False, indent=2),
    }
    for f in item.attachments:   # 명세가 참조하는 그림 (prompt.txt의 ./figures/<이름> 경로와 맞춘다)
        if Path(f).exists():
            inputs[f"figures/{Path(f).name}"] = Path(f)
    text = BENCH_SPEC_TASK.format(top=item.top, out=OUT)
    return Task("spec_writer", f"bench_spec/{item.id}", text, inputs=inputs, outputs=[OUT], write_scope=["out/"])


def generate_spec(item: BenchItem, work_dir: Path, roles: roles_mod.Roles, isolate: bool = True) -> tuple[Path | None, list[str], TaskResult]:
    """BenchItem에서 L1 추출 → BenchSpecWriter 실행 → spec_gate 검증을 거쳐 고정된 spec.md를 생성한다."""
    work_dir = Path(work_dir).resolve()
    header_res = ensure_header(item, work_dir, roles, isolate=isolate)
    top_l1, tables, notes = extract_header_l1(item, work_dir / "l1_extract")
    header_text = (work_dir / "l1_extract" / "header.sv").read_text(encoding="utf-8")

    task = make_bench_spec_task(item, tables, header_text, notes)
    task.isolate = isolate
    cfg = roles.agents.get("spec_writer") or roles.agents.get("verifier")
    if isolate:
        from pipeline.agents.task import RUNS
        import hashlib
        key = hashlib.sha256(str(work_dir).encode()).hexdigest()[:8]
        agent_work = RUNS / f"bench-spec-{item.id}-{key}"
        run_root = RUNS
    else:
        agent_work = work_dir / "spec_writer_work"
        run_root = None

    res = run_task(cfg, task, agent_work, check=bench_spec_check(top_l1, item.prompt_text, item.top),
                   retries=roles.limits.get("agent_retries", 2), run_root=run_root)
    if header_res is not None:
        res.attempts += header_res.attempts
    if not res.ok:
        return None, res.errors, res

    spec_out = work_dir / "spec.md"
    # 원문 전문을 부록으로 붙여 Architect·Author·Verifier·Repair가 SPEC에서 빠진 세부를 원문에서 찾을 수 있게 한다
    spec_out.write_text(with_prompt_appendix(Path(res.outputs[OUT]).read_text(encoding="utf-8"), item.prompt_text), encoding="utf-8")
    gate_errs = spec_writer.gate(spec_out, top_l1) + undefined_output_errors(spec_out.read_text(encoding="utf-8"), top_l1)
    return (spec_out if not gate_errs else None), gate_errs, res
