"""Author 에이전트: 동결된 L1과 SPEC 해당 부분을 받아 자기 모듈의 L2, L3를 쓴다.

볼 수 있는 것: SPEC, 동결된 L1, (자식 모듈이 있으면) 자식의 L1, IR 안내서와 일반 예시. 다른 후보의 산출물은 보지 못한다.
쓸 수 있는 것: `out/<module>.ir.json` 하나.
종료 게이트: Validator V1~V10 통과와 동결 L1 일치, 정해진 조각 언어(`lang`). 실패하면 오류를 붙여 재시도한다.
조각 언어는 스케줄러가 후보마다 정한다 (`sv` 또는 `py`, config/roles.json의 author.langs).
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.agents.task import ROOT, SPEC_GUIDE, Task
from pipeline.ir.model import Module, frozen_l1
from pipeline.validator.checks import errors, validate

GUIDE = ROOT / "pipeline" / "agents" / "prompts" / "ir_guide.md"
EXAMPLES = [ROOT / "tests" / "fixtures" / "pkt_parser.ir.json", ROOT / "tests" / "fixtures" / "pkt_rx_top.ir.json"]
PY_EXAMPLES = [ROOT / "tests" / "fixtures" / "py" / "pkt_parser.ir.json", ROOT / "tests" / "fixtures" / "py" / "py_ops.ir.json"]
LANG_NOTE = {
    "sv": "- 조각 언어: SystemVerilog. `lang`은 쓰지 않거나 `\"sv\"`로 둔다\n",
    "py": ("- 조각 언어: Python. IR 최상위에 `\"lang\": \"py\"`를 쓰고, body와 cond는 `guide.md`의 \"Python 조각\" 절을 따른다. "
           "`reads`와 `writes`는 툴이 계산하므로 쓰지 않아도 된다. 예시는 `examples/py/`\n"),
}

TASK = """\
# 과제: {module} 모듈의 IR 작성

너는 RTL 생성 파이프라인의 Author 에이전트다. 동결된 L1을 받아 이 모듈의 동작(L2, L3)을 IR로 쓴다.

## 핵심 원칙 (SPEC-First & 웹 검색 금지)
1. **SPEC은 유일한 진실 원천(Single Source of Truth)이다.**
   - 무엇이 맞는 동작인지는 오직 `spec.md`가 결정한다.
   - SPEC에 없는 동작을 임의로 지어내지 않는다. 미정의 동작은 부록(원문 명세)을 따르거나 가장 단순한 기본 동작을 선택한다.
2. **웹 검색 및 외부 역공학 절대 금지 (NO WebSearch / NO WebFetch)**:
   - `search_web`, `WebSearch`, `WebFetch` 등 웹 검색이나 외부 네트워크 접근 도구를 절대로 호출하지 마라.
   - 인터넷, GitHub, 외부 오픈소스에서 원본 코드를 검색하여 역공학으로 끼워 맞추는 행위는 엄격히 금지된다.
   - 모든 설계와 구현은 오직 작업 디렉터리에 제공된 로컬 파일(`spec.md`, `l1.json`, `guide.md` 등)에 기반하여 연역적으로만 수행하라.
3. **미사용 입력 포트 처리 (L1 동결 보존)**:
   - SPEC L1에 정의되었으나 기능상 불필요한 미사용 입력 포트가 있더라도 포트를 누락시키지 않는다.
   - Verilator `UNUSEDSIGNAL` 린트 경고를 방지하기 위해 내부 블록에 `unused_sink = ^unused_port` 또는 더미 연산을 포함하여 L1 인터페이스를 완벽히 유지하라.

## 입력
- `l1.json`: 동결된 L1 (module, role, params, domains, ports){children}
- `spec_guide.md`: SPEC 읽기 안내. 절마다 목적과 구속력(계약·제약·근거·참조)이 적혀 있다. `spec.md`보다 먼저 읽는다
- `spec.md`: SPEC. 5~7절과 9절이 계약, 1절과 부록이 문맥, 10절이 근거다. 10절의 [LESSON](추정값)과 [SPEC 재검토 알람](의도된 비표준
  구조)을 먼저 확인해 어디가 추정이고 어디가 일부러 표준과 다른지 알고 나서 5~9절을 구현한다. 둘 다 바꾸지 않는다
- `guide.md`: IR 작성 안내. 먼저 읽는다
- `examples/`: 다른 모듈의 IR 예시 (형식 참고용)

## 할 일
1. `spec_guide.md`와 `guide.md`를 읽는다.
2. L1을 그대로 포함하고 L2(types, signals, 출력 포트의 kind/reset)와 L3(blocks, 필요하면 functions와 assertions)를 채운 IR 전체를 `{out}`에 JSON으로 쓴다.
3. `python3 tools/pl.py validate {out} --l1 l1.json{lib_arg}`가 `"ok": true`가 될 때까지 고친다.

## 규칙
{lang_note}- `{out}` 외의 파일은 만들거나 고치지 않는다.
- SPEC에 없는 동작을 지어내지 않는다. SPEC이 정하지 않은 세부는 부록(원문 명세)이 정하면 그것을, 없으면 단순한 쪽을 고른다.
- SPEC이나 부록(원문)이 이름 붙인 내부 레지스터·메모리 배열·인스턴스는 그 이름 그대로 쓴다.
  원본 IP의 테스트벤치가 계층 경로로 내부 신호를 읽는 경우가 있어, 이름을 바꾸면 맞는 설계도 채점에서 실패한다.
- 5절 요구사항 줄 아래의 `| 조건 | 출력 |` 표는 그 요구사항의 일부다. 모든 행을 구현한다.
- 비표준 동기화 지연이나 추가 파이프라인/포인터 레지스터 단 수라도 SPEC(5, 6, 9절)이 요구한 사항이면 표준 관행과 다르더라도 **반드시 SPEC이 요구한 레지스터 단 수와 지연 시점을 그대로 지킨다**.
- **미세 타이밍 및 레지스터 단 수 준수:**
  - SPEC(6절 TIM-xx)에 명시된 레이턴시와 타이밍을 엄격히 준수한다.
  - **에지 검출 및 펄스 출력 패턴:**
    - SPEC이 1클럭 사이클 펄스 출력을 요구할 때:
      - 동기 등록 출력: `r_prev <= in; out <= in && !r_prev;` (Non-blocking 대입으로 클럭 에지 Tn에서 `out`이 1로 세트되어 Tn+1까지 유지됨).
      - 불필요하게 `d1 <= in; d2 <= d1; out <= d1 && !d2;`처럼 입력을 2단 쉬프트 레지스터로 지연시키지 않는다. CDC 동기화 셀(`2ff`, `cdc_sync`)이 아닌 일반 단일 클럭 도메인 내부 신호에 다단 지연을 추가하면 테스트벤치의 1사이클 기대 시점을 놓치게 된다.
    - SPEC 6절(`TIM-xx`)이 "클럭 에지에 동기화되어 1사이클 동안 유지"라고 규정하면 `comb` 블록이 아닌 `seq` 블록의 레지스터로 구동한다. 반대로 "입력 변화 즉시 조합 함수로 출력"이라고 명시된 경우에만 `comb`로 구현한다.
  - **PISO 시프트 레지스터 인덱싱 준수:**
    - $W$비트 병렬 데이터를 MSB부터 LSB(`MSB to LSB`)로 직렬 출력할 때 카운터 k in [0, W-1]에 대해 직렬 출력 비트는 반드시 `tmp[(W-1) - k]` (8비트인 경우 `tmp[3'd7 - bit_cnt]`)로 슬라이스한다. `tmp[(W-2) - k]` (`tmp[6 - bit_cnt]`)와 같은 1비트 오프셋 오류를 범하지 않는다.
  - **N비트 카운터 자연 순환(Modulo Wrap-around) 준수:**
    - N비트 카운터는 SPEC이나 원문이 명시적으로 포화 래치(sticky/saturation latch)를 요구하지 않는 한 $2^N-1$ 이후 $0$으로 자연 순환(`cnt <= cnt + 1`)하도록 구현하며, 자의적으로 `overflowed` 레지스터를 도입해 출력을 영구 고정하지 않는다.
  - **미구현/비활성화 분기의 Default Fall-through 준수:**
    - 매크로 등으로 꺼진 연산 코드는 자의적 pass-through(`result <= a`)를 하지 않고 원문 아키텍처 기본 분기(`default: result = a & b;` 등)를 타도록 작성한다.
{contracts}"""

CONTRACTS = """- `contracts.json`: Architect가 정한, 이 모듈과 형제 모듈 사이 연결의 계약. `carries`(실어 나르는 값의 뜻)와
  `timing`(레지스터 단 수, ready 관계, 보관 개수)을 그대로 지킨다. 형제 모듈은 다른 Author가 같은 계약을 보고 쓴다.
  SPEC의 블록 전체 지연을 두 모듈에 어떻게 나눴는지는 계약이 정한다.
"""


def out_rel(module: str) -> str:
    return f"out/{module}.ir.json"


def make_task(name: str, l1: dict, spec: str | Path, children: dict[str, dict] | None = None, lang: str = "sv",
              contracts: list[dict] | None = None) -> Task:
    """contracts: 이 모듈이 한쪽 끝인 하위 모듈 사이 연결의 계약 (`architect.contracts_for`)."""
    module = l1["module"]
    inputs: dict = {"l1.json": json.dumps(l1, ensure_ascii=False, indent=1), "spec.md": Path(spec),
                    "spec_guide.md": SPEC_GUIDE, "guide.md": GUIDE}
    for e in EXAMPLES:
        inputs[f"examples/{e.name}"] = e
    for e in PY_EXAMPLES if lang == "py" else []:
        inputs[f"examples/py/{e.name}"] = e
    for cname, cl1 in (children or {}).items():
        inputs[f"lib/{cname}.ir.json"] = json.dumps(cl1, ensure_ascii=False, indent=1)
    text = TASK.format(module=module, out=out_rel(module),
                       children="\n- `lib/`: 인스턴스로 쓰는 자식 모듈의 L1" if children else "",
                       lib_arg=" --lib lib" if children else "", lang_note=LANG_NOTE[lang],
                       contracts=CONTRACTS if contracts else "")
    if contracts:
        inputs["contracts.json"] = json.dumps({"contracts": contracts}, ensure_ascii=False, indent=1)
    return Task("author", name, text, inputs=inputs, outputs=[out_rel(module)], write_scope=["out/"])


def check(l1: dict, children: dict[str, dict] | None = None, lang: str = "sv"):
    """종료 게이트: 저장소의 Validator(작업 디렉터리의 사본이 아님)와 원본 L1로 판정한다."""
    def _check(work: Path) -> list[str]:
        try:
            raw = json.loads((work / out_rel(l1["module"])).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return [f"IR을 읽을 수 없음: {e}"]
        lib = {n: Module(c) for n, c in (children or {}).items()}
        errs = [str(i) for i in errors(validate(Module(raw), lib))]
        mine, frozen = frozen_l1(raw), frozen_l1(l1)
        errs += [f"[동결] {k}: 동결된 L1과 다름" for k in frozen if mine.get(k) != frozen.get(k)]
        if raw.get("lang", "sv") != lang:
            errs.append(f'[lang] 이 후보의 조각 언어는 "{lang}"임 (IR의 lang: "{raw.get("lang", "sv")}")')
        return errs
    return _check
