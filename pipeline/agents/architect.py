"""Architect 에이전트: SPEC과 Block Top L1을 받아 모듈 트리를 정하고 인터페이스를 동결한다.

볼 수 있는 것: SPEC, SPEC 표에서 만든 Block Top의 L1, IR 안내서.
쓸 수 있는 것: `out/ir/<모듈>.ir.json`(Block Top은 L1 + instances + connections, 하위 모듈은 L1만), `out/assumptions.json`.
재구성(make_revise_task): 형제 모듈의 Repair들이 실패 원인을 서로 넘겨 Design-lead가 근본 원인 수정을 요청하면, 지금 모듈 트리와
충돌 경위(`conflict.json`)를 받아 모듈 경계와 하위 모듈 L1을 다시 정한다. Block Top의 L1은 그대로라서 Verifier 테스트는 그대로 쓴다.
종료 게이트: Block Top의 L1이 바뀌지 않음, Block Top이 V1~V10 통과(하위 모듈 L1을 라이브러리로), 하위 모듈 L1이 V1~V3 통과,
             가정 목록이 있음, 하위 모듈끼리 잇는 포트가 여러 필드를 묶은 워드가 아님(폭이 파라미터 둘 이상의 합),
             하위 모듈끼리 잇는 연결마다 계약(`out/contracts.json`: 실어 나르는 값의 뜻, 레지스터 단 수와 ready 관계)이 있음,
             하위 모듈 수 상한(max_modules, P11 R6 재생성)을 넘지 않음.
계약은 스케줄러가 그 연결의 양쪽 모듈 Author와 Repair에게 준다 (형제 모듈을 따로 쓰는 Author들이 같은 뜻과 시점을 쓰게).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from pipeline.agents.task import ROOT, Task
from pipeline.ir.model import Module, frozen_l1
from pipeline.validator.checks import errors, validate

GUIDE = ROOT / "pipeline" / "agents" / "prompts" / "ir_guide.md"
EXAMPLE_TOP = ROOT / "tests" / "fixtures" / "pkt_rx_top.ir.json"
EXAMPLE_MOD = ROOT / "tests" / "fixtures" / "pkt_parser.ir.json"

TASK = """\
# 과제: {top} Block Top의 모듈 트리와 인터페이스 정하기

너는 RTL 생성 파이프라인의 Architect 에이전트다. SPEC을 하위 모듈로 나누고 각 모듈의 인터페이스(L1)를 정한다.
동작(L2, L3)은 쓰지 않는다. 그건 Author가 모듈마다 쓴다.

## 입력
- `spec.md`: 고정된 SPEC
- `block_top.json`: SPEC 2~4번 섹션 표에서 만든 Block Top의 L1 (바꾸지 않는다)
- `guide.md`: IR 작성 안내 (role, 번들, 도메인, 연결 규칙)
- `examples/`: Block Top(`pkt_rx_top`)과 하위 모듈(`pkt_parser`) IR 예시

## 할 일
1. 모듈 트리를 정한다. Block Top(role `block_top`)은 동작 블록을 가질 수 없고(동기화 셀 `cdc_sync`, `cdc_fifo`만 가능), 인스턴스와 연결만 가진다.
   그래서 동작은 모두 하위 모듈(role `module`)에 둔다. 모듈은 Author가 하나씩 따로 쓰므로 너무 크지도 잘게 쪼개지도 않게 나눈다.
2. 하위 모듈마다 L1(module, role, params, domains, ports)만 있는 IR을 `out/ir/<모듈>.ir.json`에 쓴다.
3. Block Top IR을 `out/ir/{top}.ir.json`에 쓴다. `block_top.json`의 L1을 그대로 두고 `instances`(파라미터 바인딩, domain_map)와
   `connections`를 더한다. 번들은 프로토콜이 같으면 번들 이름으로 한 번에 잇는다 (예: `self.s_axis` → `u_core.s_axis`).
4. SPEC에서 정하지 않아 네가 정한 것(모듈 경계, 하위 모듈 사이 프로토콜, 지연 배분 등)을 `out/assumptions.json`에
   `{{"assumptions": [{{"text": "...", "affects": [...]}}, ...]}}`로 쓴다. `affects`는 그 가정이 건드리는 것이다:
   `l1`(Block Top의 포트, 파라미터, 프로토콜의 해석), `timing`(지연, 처리율, 사이클 시점), `reset`(리셋 동작),
   `spec_gap`(SPEC이 정하지 않거나 미정의로 둔 동작을 정함). 모듈 안의 배분만 정한 것(모듈 경계, 하위 모듈 사이 포트)은 `[]`.
   `affects`가 비지 않은 가정은 사람이 검토한다.
5. 하위 모듈끼리 잇는 연결(양쪽이 모두 `self`가 아닌 것)마다 계약을 `out/contracts.json`에
   `{{"contracts": [{{"from": "<인스턴스>.<포트나 번들>", "to": "<인스턴스>.<포트나 번들>", "carries": "...", "timing": "..."}}, ...]}}`로 쓴다.
   두 모듈은 Author가 따로 쓰고, 각 Author는 이 계약과 자기 L1만 보고 경계를 맞춘다. 그러니 둘이 다르게 읽을 수 없게 쓴다.
   - `carries`: 실어 나르는 값의 뜻. 필드마다 무엇인지(원시 입력 그대로인지, 처리한 결과인지), 끝 표시나 오류 플래그가 언제 1인지.
   - `timing`: 보내는 모듈이 Block Top 입력을 받은 사이클에서 몇 사이클 뒤에 이 연결로 내는지(레지스터 단 수),
     받는 모듈이 받은 뒤 몇 사이클 뒤에 내는지, ready가 valid나 다른 입력에 조합으로 걸리는지, 몇 개까지 보관하는지.
   SPEC의 지연(6절)을 두 모듈에 어떻게 나눴는지가 여기서 드러나야 한다. 하위 모듈이 하나면 `{{"contracts": []}}`.
6. `python3 tools/pl.py validate out/ir/{top}.ir.json --lib out/ir`가 `"ok": true`가 될 때까지 고친다.
   하위 모듈은 아직 동작이 없으므로 `validate`로 검사하지 않는다.

## 규칙
- `out/` 밖의 파일은 만들거나 고치지 않는다.
- 클럭과 리셋은 `domain_map`으로만 연결한다.
- 하위 모듈 사이에서 여러 필드(data, last, id 등)를 한 워드로 묶어 넘기지 않는다. 묶는 모듈과 푸는 모듈이 비트 순서를 따로 정하면
  어긋난다. 필드마다 포트를 둔다. 메모리에 워드로 저장해야 하면 묶기와 풀기를 모두 그 메모리를 가진 모듈 안에 둔다.
- 도메인을 넘는 상태 플래그는 0이 안전한 뜻으로 정한다 (`cdc_sync`의 `dst`는 리셋 직후 0이고 늦게 바뀐다). 예: "상대가 준비됨"(1이면 진행).
  포트 이름과 가정 목록에 그 뜻을 적는다.
- 클럭 그룹(기준 도메인과 그 분주 도메인, `sync_to`)이 다른 도메인 사이는 동기화 셀로만 넘긴다. 1비트 수준 신호는 `cdc_sync` `2ff`,
  펄스는 `pulse`, 여러 비트 값은 `cdc_fifo`(valid/ready)를 쓴다. 같은 클럭 그룹 안에서는 셀 없이 잇는다.
- 리셋은 설계 전체에서 한 방식이다. SPEC의 도메인 표가 정한 대로 모든 도메인을 비동기 리셋이나 동기 리셋 하나로 둔다.
- 하위 모듈의 도메인마다 클럭과 리셋 포트를 따로 둔다. Block Top이 리셋을 블록 안에서 동기화하면(`reset.synchronize`)
  도메인마다 동기화한 리셋이 따로 나오므로, 두 도메인이 리셋 포트 하나를 함께 쓰면 연결할 수 없다.
- Block Top에 inout 포트가 있으면 Block Top에 `tristate` 블록을 두고(`guide.md`의 blocks 표), 하위 모듈은 inout 대신
  `<이름>_o`, `<이름>_oe`(출력), `<이름>_i`(입력) 포트를 둔다. Block Top은 이 셋을 wire 신호로 받아 `tristate`의 o, oe, i에 잇는다.
- 비표준 동기화 지연이나 추가 레지스터 단 수라도 SPEC이 명시적으로 요구한 사항이면 임의로 생략하거나 바꾸지 말고 **일단 SPEC을 그대로 지키며**, `out/assumptions.json`에 `{{"text": "[SPEC 재검토 알람] ...", "affects": ["timing"]}}` 항목을 추가해 SPEC 재검토 알람을 발생시킨다.
{limit}"""

LIMIT = """- 하위 모듈(role `module`)은 {n}개 이하로 둔다. 이 블록은 크지 않아 나누면 모듈 사이 계약만 늘어난다.
  하나면 Block Top 아래 하위 모듈 하나에 동작을 모두 둔다.
"""


REVISE = """
## 재구성 요청 (Design-lead)
지금 모듈 트리로 Author와 Repair를 돌렸더니, 같은 실패를 두고 형제 모듈의 Repair들이 원인을 서로 넘겼다.
요구사항 하나를 만족시키는 데 필요한 결정(예: 출력 시점과 그때의 ready)이 모듈 경계에 나뉘어 있어 어느 한 모듈만 고쳐서는
풀 수 없다는 신호다. Design-lead가 근본 원인을 모듈 트리에서 고치라고 요청했다.
- `current/`: 지금 모듈 트리 (Block Top IR과 하위 모듈 L1)
- `conflict.json`: 충돌마다 실패한 요구사항과 첫 불일치, Repair들이 서로 넘긴 경위(모듈과 이유), lead의 메모
위 할 일을 다시 하되, 충돌한 요구사항을 만족시키는 데 필요한 상태와 결정이 한 모듈 안에 있도록 모듈 경계나 하위 모듈 사이 인터페이스를
바꾼다 (모듈을 합치거나, 경계를 옮기거나, 필요한 신호를 인터페이스에 더함). 충돌과 관계없는 부분은 그대로 둔다.
무엇을 왜 바꿨는지 `out/assumptions.json`의 가정 목록 맨 앞에 적는다. Block Top의 L1은 이번에도 바꾸지 않는다.
경계가 남는 연결의 계약(`out/contracts.json`)도 다시 쓴다. 충돌이 계약의 뜻이나 시점을 두 모듈이 다르게 읽어서 생겼으면
그 부분을 더 분명하게 쓴다 (`current/contracts.json`이 지금 계약).
"""


def make_revise_task(name: str, block_top_l1: dict, spec: str | Path, tree: dict, conflicts: list[dict],
                     max_modules: int | None = None) -> Task:
    """lead의 근본 원인 수정 요청: 지금 트리(tree: {"top", "children", "contracts"})와 충돌 경위를 받아 모듈 트리를 다시 정한다."""
    t = make_task(name, block_top_l1, spec, max_modules)
    t.inputs["current/contracts.json"] = json.dumps({"contracts": tree.get("contracts", [])}, ensure_ascii=False, indent=1)
    t.inputs[f"current/{tree['top']['module']}.ir.json"] = json.dumps(tree["top"], ensure_ascii=False, indent=1)
    for n, ir in tree["children"].items():
        t.inputs[f"current/{n}.ir.json"] = json.dumps(ir, ensure_ascii=False, indent=1)
    t.inputs["conflict.json"] = json.dumps(conflicts, ensure_ascii=False, indent=1)
    t.text += REVISE
    return t


def make_task(name: str, block_top_l1: dict, spec: str | Path, max_modules: int | None = None) -> Task:
    """max_modules: 하위 모듈 수 상한 (P11 R6 재생성에서 원본 모듈 수로 정함). None이면 제한 없음."""
    inputs = {"spec.md": Path(spec), "block_top.json": json.dumps(block_top_l1, ensure_ascii=False, indent=1),
              "guide.md": GUIDE, "examples/pkt_rx_top.ir.json": EXAMPLE_TOP, "examples/pkt_parser.ir.json": EXAMPLE_MOD}
    top = block_top_l1["module"]
    text = TASK.format(top=top, limit=LIMIT.format(n=max_modules) if max_modules else "")
    return Task("architect", name, text, inputs=inputs,
                outputs=[f"out/ir/{top}.ir.json", "out/assumptions.json"], write_scope=["out/"])


def read_tree(work: Path, top: str) -> tuple[dict, dict[str, dict], list[str]]:
    """(Block Top IR, 하위 모듈 L1, 가정 목록)."""
    irs = {}
    for p in sorted((work / "out" / "ir").glob("*.ir.json")):
        raw = json.loads(p.read_text(encoding="utf-8"))
        irs[raw.get("module", p.stem.removesuffix(".ir"))] = raw
    raw = json.loads((work / "out" / "assumptions.json").read_text(encoding="utf-8")).get("assumptions", [])
    # 문자열만 쓴 가정은 무엇을 건드리는지 모르므로 affects=None (사람이 검토)
    assumptions = [a if isinstance(a, dict) else {"text": str(a), "affects": None} for a in raw]
    top_ir = irs.pop(top)
    return top_ir, irs, assumptions


AFFECTS = {"l1", "timing", "reset", "spec_gap"}
CONTRACTS = "out/contracts.json"


def read_contracts(work: Path) -> list[dict]:
    p = Path(work) / CONTRACTS
    raw = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return [c for c in raw.get("contracts", []) if isinstance(c, dict)] if isinstance(raw, dict) else []


def sibling_links(top_ir: dict) -> list[tuple[str, str]]:
    """하위 모듈끼리 잇는 연결 (양쪽 끝점이 모두 self가 아님)."""
    return [(c["from"], c["to"]) for c in top_ir.get("connections", [])
            if not str(c.get("from", "self")).startswith("self") and not str(c.get("to", "self")).startswith("self")]


def contracts_for(contracts: list[dict], top_ir: dict, module: str) -> list[dict]:
    """이 모듈의 인스턴스가 한쪽 끝인 계약."""
    insts = {i["name"] for i in top_ir.get("instances", []) if i["module"] == module}
    return [c for c in contracts if str(c.get("from", "")).split(".")[0] in insts or str(c.get("to", "")).split(".")[0] in insts]


def contract_errors(top_ir: dict, contracts: list[dict]) -> list[str]:
    have = {(c.get("from"), c.get("to")) for c in contracts}
    errs = [f"[계약] {a} → {b}: out/contracts.json에 계약이 없음 (carries, timing)" for a, b in sibling_links(top_ir) if (a, b) not in have]
    for c in contracts:
        for k in ("carries", "timing"):
            if not str(c.get(k, "")).strip():
                errs.append(f"[계약] {c.get('from')} → {c.get('to')}: {k}가 비어 있음")
    return errs


def needs_review(a: dict) -> bool:
    """사람이 검토할 가정: L1, 타이밍, 리셋을 건드리거나 SPEC의 빈 곳을 정한 것, 또는 표시가 없는 것 (P8 회고)."""
    return a.get("affects") is None or bool(set(a["affects"]) & AFFECTS)


def packed_ports(top_ir: dict, children: dict[str, dict]) -> list[str]:
    """하위 모듈끼리 잇는 포트 중 폭이 자식 파라미터 둘 이상의 합인 것 (여러 필드를 한 워드로 묶은 것으로 봄).
    P8a async_fifo: 묶는 모듈과 푸는 모듈이 비트 순서를 따로 정해 한 비트씩 어긋났다."""
    insts = {i["name"]: i["module"] for i in top_ir.get("instances", [])}
    binds = {i["name"]: i.get("params", {}) for i in top_ir.get("instances", [])}
    top_params = {p["name"] for p in top_ir.get("params", [])}
    out = []
    for c in top_ir.get("connections", []):
        ends = [c.get(k, "").split(".", 1) for k in ("from", "to")]
        if any(len(e) != 2 or e[0] == "self" or e[0] not in insts for e in ends):
            continue
        for owner, port in ends:
            child = children.get(insts[owner]) or {}
            params = {p["name"] for p in child.get("params", [])}
            for b in child.get("ports", []):
                for sig in b.get("signals", [b]):
                    if port not in (b.get("bundle"), sig.get("name")):
                        continue
                    m = re.search(r"\[(.+):", sig.get("type", ""))
                    if not m:
                        continue
                    # 자식 파라미터 하나(WORD_WIDTH)에 합을 바인딩해 숨긴 경우도 보도록 인스턴스 바인딩으로 바꿔 본다
                    width = re.sub(r"[A-Za-z_]\w*", lambda x: f"({binds[owner][x.group(0)]})"
                                   if x.group(0) in params and x.group(0) in binds[owner] else x.group(0), m.group(1))
                    used = set(re.findall(r"[A-Za-z_]\w*", width)) & (params | top_params) if "+" in width else set()
                    if len(used) >= 2:
                        out.append(f'[묶음 워드] {owner}.{sig["name"]} ({sig["type"]}): 하위 모듈 사이에서 여러 필드를 한 워드로 '
                                   "묶어 넘기지 않는다. 필드마다 포트를 두거나, 묶기와 풀기를 같은 모듈(메모리를 가진 모듈) 안에 둔다")
    return sorted(set(out))


def check(block_top_l1: dict, max_modules: int | None = None):
    top = block_top_l1["module"]

    def _check(work: Path) -> list[str]:
        try:
            top_ir, children, assumptions = read_tree(work, top)
        except (OSError, json.JSONDecodeError, KeyError) as e:
            return [f"결과를 읽을 수 없음: {e}"]
        errs = []
        if top_ir.get("role") != "block_top":
            errs.append(f"{top}의 role이 block_top이 아님")
        mine, frozen = frozen_l1(top_ir), frozen_l1(block_top_l1)
        errs += [f"[동결] {top}.{k}: Block Top L1과 다름" for k in frozen if mine.get(k) != frozen.get(k)]
        lib = {n: Module(c) for n, c in children.items()}
        for n, c in children.items():
            if c.get("role") not in ("module", "external"):
                errs.append(f"{n}: 하위 모듈의 role은 module 또는 external")
            errs += [f"{n}: {i}" for i in errors(validate(Module(c), lib, upto=3))]
        errs += [f"{top}: {i}" for i in errors(validate(Module(top_ir), lib))]
        used = {i["module"] for i in top_ir.get("instances", [])}
        errs += [f"쓰이지 않는 하위 모듈: {n}" for n in children if n not in used]
        errs += packed_ports(top_ir, children)
        try:
            errs += contract_errors(top_ir, read_contracts(work))
        except json.JSONDecodeError as e:
            errs.append(f"{CONTRACTS}를 읽을 수 없음: {e}")
        n_mod = sum(1 for c in children.values() if c.get("role") == "module")
        if max_modules and n_mod > max_modules:
            errs.append(f"하위 모듈(role module)이 {n_mod}개: {max_modules}개 이하로 둔다")
        if not assumptions:
            errs.append("가정 목록이 비어 있음 (SPEC이 정하지 않아 정한 것을 적음)")
        for a in assumptions:
            if not str(a.get("text", "")).strip():
                errs.append("가정에 text가 없음")
            if a.get("affects") is not None and (not isinstance(a["affects"], list) or set(a["affects"]) - AFFECTS):
                errs.append(f'가정의 affects는 {sorted(AFFECTS)} 중에서 고른 목록: {a.get("affects")!r}')
        return errs
    return _check
