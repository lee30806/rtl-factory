"""벤치마크 입력 변환기 (BENCHMARK_PLAN 4.2절): 모듈 헤더 SV + 자연어 프롬프트 → L1 추출 → 2~4절 표 → 10개 섹션 spec.md."""
from __future__ import annotations

import json
import re
from pathlib import Path

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
- `notes.json`: 고정 파라미터, 매크로 상수, 재사용 하위 셀 정보

## 할 일
`{out}`에 10개 섹션 SPEC을 쓴다.
- 제목은 반드시 `# {top} SPEC`으로 쓴다.
- **2절, 3절, 4절은 `tables.md`의 내용을 글자 하나 바꾸지 않고 그대로 복사해 넣는다.**
- 1절 (`## 1. 목적과 범위`): 모듈의 목적을 한 문단으로 설명하고, 범위 밖 항목을 명시한다.
- 5절 (`## 5. 기능 요구사항`): `- REQ-01 (필수) ...` 형식으로 모든 출력 포트와 입력 포트가 최소 한 번 이상 등장하도록
  동작을 구체적인 수치·조건·타이밍으로 기술한다.
  - **중요 (`spec_gate` 규칙):** 3절 인터페이스 표에 있는 모든 포트 이름(번들인 경우 번들 이름 또는 하위 신호 이름, 클럭·리셋 제외)이
    5~7절 요구사항 문장에 최소 한 번 이상 그대로 적혀야 하며, 모든 출력 포트의 구동 조건과 값이 정의되어야 한다.
  - **한 문장 규칙:** 각 요구사항 ID 줄(`- ID (필수) ...`)에는 마침표로 끝나는 **문장 하나만** 쓴다 (두 문장 이상 쓰면 `REQ_MULTI` 경고가 발생함).
  - **프롬프트 충실성 규칙:** `prompt.txt`에 적히지 않은 리셋 해제 직후 첫 사이클 특수 동작(예: 리셋 중의 값을 첫 입력 비트로 간주할지 여부 등)을 임의로 지어내어 필수 요구사항으로 강제하지 않는다. 리셋 해제 후에는 명시적으로 인가된 입력 시퀀스에 대해서만 출력을 규정한다.
  - **명시된 구조는 표준과 달라도 지키고 재검토 알람을 남긴다:** `prompt.txt`가 레지스터 단 수, 동기화 단 수, 포인터나 플래그를
    만드는 레지스터 구성, 카운터 값에 따른 출력 토글 시점처럼 출력 타이밍을 정하는 구조를 명시하면, 표준 관행과 달라도 그 단 수와
    지연, 위상을 5절, 6절, 9절 필수 요구사항에 그대로 쓴다. 이런 설계는 사이클 단위 기대값으로 검증되므로 "더 나은" 구조로 바꾸면
    맞는 설계가 실패한다. 그리고 10절에 `- Q-01 [SPEC 재검토 알람] ...` 형식으로 표준과 다른 점과 그 영향을 적어 사람이 다시 보게 한다.
  - **비동기 FIFO / CDC 포인터 및 상태 플래그 규칙:** 비동기 FIFO(`asyn_fifo`)나 복수 클럭 도메인 간의 CDC(Clock Domain Crossing) 설계에서, 2단 동기화기(2-stage synchronizer, 예: `rptr_syn`, `wptr_syn`)를 통과한 신호는 이미 해당 수신 클럭 도메인에 안전하게 동기화된 신호이다.
    따라서 수신 도메인의 로컬 포인터(`wptr`, `rptr`)와 2단 동기화된 상대 포인터를 비교하여 가득 참(`wfull`) 및 비어 있음(`rempty`) 플래그를 생성할 때는 추가 플립플롭 없이 **순수 조합논리 비교(`output wire wfull; assign wfull = ...;`)**로 구동하는 것이 표준이다. 동기화된 신호 간의 조합논리 플래그 생성을 "비동기 직접 비교 글리치 유발"로 간주하여 임의로 금지하거나 플래그에 불필요한 레지스터(`wfull_reg`, `rempty_reg`)를 추가하지 않는다.
- 6절 (`## 6. 타이밍과 성능`): `- TIM-01 (필수) ...` 형식으로 클럭 에지 기준 출력 갱신 지연과 처리율을 한 줄에 한 문장씩 명시한다.
  - **지연을 지어내지 않으며 파이프라인 단 수를 보존한다:** `prompt.txt`에 없는 정확한 지연 사이클 수, 핸드셰이크 응답 사이클, 사이클 번호를 정하지 않는다. 단, `prompt.txt`가 구체적인 파이프라인/포인터 레지스터 구성을 명시해 지연 사이클 수가 결정되는 경우(예: 바이너리 주소 레지스터 갱신 후 그레이 레지스터 래치)에는 그 구성을 그대로 반영하며, 임의로 선행 계산(lookahead next-state)을 도입하여 파이프라인 지연을 1사이클 앞당기지 않는다.
    벤치마크 테스트벤치는 사이클 단위(cycle-accurate)로 기대값 벡터와 엄격하게 대조 검증하므로, 프롬프트가 구체적 레지스터 구성을 명시한 경우 "전달 지연은 구현에 따름(검증하지 않음)"과 같은 비검증 면책 문구를 임의로 남발하지 않는다.
    결과를 알리는 출력 플래그(예: `o_en`, `valid`, `done`, `rdy`)가 있고 `prompt.txt`가 지연 단 수를 강제하지 않으면 "o_en=1인 사이클의 result는 그 전에 i_en=1로 받은 입력들의
    결과를 받은 순서대로 낸다"처럼 플래그 기준으로 쓰고, 지연은 "구현에 따름(검증하지 않음)"으로 둔다. "여러 단의 레지스터"처럼
    수를 정하지 않은 표현을 특정 사이클 수로 바꾸지 않는다: 지어낸 값은 그와 다른 지연을 가진 맞는 설계를 실패시킨다.
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
    explicit_sync = bool(re.search(r"\bsync(hronous|hronously)?\s+reset|동기\s*리셋|리셋\s*동기\s*\|\s*동기", text_low))
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


def extract_header_l1(item: BenchItem, work_dir: Path) -> tuple[dict, str, dict]:
    """모듈 헤더 SV와 프롬프트에서 Block Top L1과 2~4절 마크다운 표(`tables.md`), `notes.json`을 결정론적으로 추출한다."""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    raw_text = _read_header_text(item.header_sv)
    lessons: list[str] = []
    clean_hdr = strip_to_header(raw_text, item.top, lessons)
    (work_dir / "header.sv").write_text(clean_hdr, encoding="utf-8")

    incdirs = [Path(p).resolve() for p in item.incdirs]
    cells = [Path(p).resolve() for p in item.cells if Path(p).exists()]
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
    notes = {
        "benchmark": item.benchmark,
        "id": item.id,
        "category": item.category,
        "params": item.params,
        "defines": item.defines,
        "macros": r1_res.get("macros", {}),
        "cells": [Path(c).name for c in item.cells],
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
    text = BENCH_SPEC_TASK.format(top=item.top, out=OUT)
    return Task("spec_writer", f"bench_spec/{item.id}", text, inputs=inputs, outputs=[OUT], write_scope=["out/"])


def generate_spec(item: BenchItem, work_dir: Path, roles: roles_mod.Roles, isolate: bool = True) -> tuple[Path | None, list[str], TaskResult]:
    """BenchItem에서 L1 추출 → BenchSpecWriter 실행 → spec_gate 검증을 거쳐 고정된 spec.md를 생성한다."""
    work_dir = Path(work_dir).resolve()
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

    res = run_task(cfg, task, agent_work, check=spec_writer.check(top_l1), retries=roles.limits.get("agent_retries", 2), run_root=run_root)
    if not res.ok:
        return None, res.errors, res

    spec_out = work_dir / "spec.md"
    spec_out.write_text(Path(res.outputs[OUT]).read_text(encoding="utf-8"), encoding="utf-8")
    gate_errs = spec_writer.gate(spec_out, top_l1)
    return (spec_out if not gate_errs else None), gate_errs, res
