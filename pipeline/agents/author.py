"""Author 에이전트: 동결된 L1과 SPEC 해당 부분을 받아 자기 모듈의 L2, L3를 쓴다.

볼 수 있는 것: SPEC, 동결된 L1, (자식 모듈이 있으면) 자식의 L1, IR 안내서와 일반 예시. 다른 후보의 산출물은 보지 못한다.
쓸 수 있는 것: `out/<module>.ir.json` 하나.
종료 게이트: Validator V1~V10 통과와 동결 L1 일치, 정해진 조각 언어(`lang`). 실패하면 오류를 붙여 재시도한다.
조각 언어는 스케줄러가 후보마다 정한다 (`sv` 또는 `py`, config/roles.json의 author.langs).
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.agents.task import ROOT, Task
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

## 입력
- `l1.json`: 동결된 L1 (module, role, params, domains, ports){children}
- `spec.md`: SPEC. 이 모듈의 동작은 5~7번 섹션의 요구사항을 따른다. 9번 섹션의 구현 제약도 지킨다
- `guide.md`: IR 작성 안내. 먼저 읽는다
- `examples/`: 다른 모듈의 IR 예시 (형식 참고용)

## 할 일
1. `guide.md`를 읽는다.
2. L1을 그대로 포함하고 L2(types, signals, 출력 포트의 kind/reset)와 L3(blocks, 필요하면 functions와 assertions)를 채운 IR 전체를 `{out}`에 JSON으로 쓴다.
3. `python3 tools/pl.py validate {out} --l1 l1.json{lib_arg}`가 `"ok": true`가 될 때까지 고친다.

## 규칙
{lang_note}- `{out}` 외의 파일은 만들거나 고치지 않는다.
- SPEC에 없는 동작을 지어내지 않는다. SPEC이 정하지 않은 세부는 단순한 쪽을 고른다.
- 비표준 동기화 지연이나 추가 파이프라인/포인터 레지스터 단 수라도 SPEC(5, 6, 9절)이 요구한 사항이면 표준 관행과 다르더라도 **반드시 SPEC이 요구한 레지스터 단 수와 지연 시점을 그대로 지킨다**.
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
                    "guide.md": GUIDE}
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
