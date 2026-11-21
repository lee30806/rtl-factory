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

from pipeline.agents.task import ROOT, SPEC_GUIDE, Task
from pipeline.ir.model import Module, frozen_l1
from pipeline.validator.checks import errors, validate

GUIDE = ROOT / "pipeline" / "agents" / "prompts" / "ir_guide.md"
EXAMPLE_TOP = ROOT / "tests" / "fixtures" / "pkt_rx_top.ir.json"
EXAMPLE_MOD = ROOT / "tests" / "fixtures" / "pkt_parser.ir.json"

CELLS_NOTE = """
## 제공 셀 (cells/)
- `cells/` 디렉토리에 외부 하위 모듈의 실제 Verilog/SystemVerilog 파일이 제공된다.
- 하위 모듈 중 제공 셀과 이름이 같은 모듈은 해당 파일의 포트 이름, 방향, 비트 폭, 클럭/리셋 신호명을 **정확히 그대로** L1(`out/ir/<모듈>.ir.json`)에 선언하고 Block Top에서 인스턴스/연결해야 한다.
- 제공 셀의 포트 목록에 없는 가상의 신호(예: 실제 셀은 wclk/rclk인데 단일 clk를 연결하거나, 없는 리셋/플래그 신호 추가)를 선언하거나 연결하면 검사에서 즉시 거부된다.
"""

TASK = """\
# 과제: {top} Block Top의 모듈 트리와 인터페이스 정하기

너는 RTL 생성 파이프라인의 Architect 에이전트다. SPEC을 하위 모듈로 나누고 각 모듈의 인터페이스(L1)를 정한다.
동작(L2, L3)은 쓰지 않는다. 그건 Author가 모듈마다 쓴다.

## 입력
- `spec_guide.md`: SPEC 읽기 안내. 절마다 목적과 구속력이 적혀 있다. `spec.md`보다 먼저 읽는다
- `spec.md`: 고정된 SPEC. 6절 지연과 9절 필수 구조를 하위 모듈 계약에 나누되 합을 바꾸지 않는다. 10절 [SPEC 재검토 알람]은 의도된 비표준 구조다
- `block_top.json`: SPEC 2~4번 섹션 표에서 만든 Block Top의 L1 (바꾸지 않는다)
- `guide.md`: IR 작성 안내 (role, 번들, 도메인, 연결 규칙)
- `examples/`: Block Top(`pkt_rx_top`)과 하위 모듈(`pkt_parser`) IR 예시

## 할 일
1. 모듈 트리를 정한다. Block Top(role `block_top`)은 동작 블록을 가질 수 없고(동기화 셀 `cdc_sync`, `cdc_fifo`만 가능), 인스턴스와 연결만 가진다.
   그래서 동작은 모두 하위 모듈(role `module`)에 둔다. 모듈은 Author가 하나씩 따로 쓰므로 너무 크지도 잘게 쪼개지도 않게 나눈다.
2. 하위 모듈마다 L1(module, role, params, domains, ports)만 있는 IR을 `out/ir/<모듈>.ir.json`에 쓴다.
3. Block Top IR을 `out/ir/{top}.ir.json`에 쓴다. `block_top.json`의 L1을 그대로 두고 `instances`(파라미터 바인딩, domain_map)와
   `connections`를 더한다. 번들은 프로토콜이 같으면 번들 이름으로 한 번에 잇는다 (예: `self.s_axis` → `u_core.s_axis`).
   하위 모듈(제공 셀 포함)을 인스턴스하면 그 출력은 결과를 쓰는 모듈의 입력이나 Block Top 출력으로 반드시 잇는다. 결과가 버려지는
   인스턴스는 검사에서 거부된다.
   SPEC(9절 `#### 원문 명시 인스턴스`, 부록 원문)이 하위 모듈의 인스턴스 이름을 말하면
   그 하위 모듈을 원문이 말한 위치에 그 이름 그대로 인스턴스한다. 원본 IP의 테스트벤치가 계층 경로로 내부 이름을 읽는 경우가 있다.
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
   - SPEC 6절이 고정 지연(N사이클)을 정하고 하위 모듈끼리 잇는 연결이 있으면 `"stage_budget": {{"<인스턴스>": <레지스터 단 수>, ...}}`를
     같은 파일에 쓴다. Block Top 입력에서 출력까지 데이터가 지나는 인스턴스마다 그 안의 레지스터 단 수를 적고, 합이 N이어야 한다.
     모듈 경계로 단을 옮기거나 경계 신호에 SPEC에 없는 레지스터를 더하지 않는다 (검사에서 합이 다르면 거부된다).
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


def cell_port_names(cell: Path) -> dict[str, list[str]]:
    """셀 파일 안의 모듈별 포트 이름 목록 (선언 순서). ANSI와 non-ANSI를 모두 읽는다."""
    text = Path(cell).read_text(encoding="utf-8", errors="replace")
    text = re.sub(r"//.*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"/\*[\s\S]*?\*/", "", text)
    out: dict[str, list[str]] = {}
    mod_re = re.compile(r"\bmodule\s+([A-Za-z_]\w*)([\s\S]*?)\bendmodule\b")
    decl_re = re.compile(
        r"\b(?P<dir>input|output|inout)\b"
        r"(?:\s+(?:wire|reg|logic|signed|unsigned|\w+_t))*"
        r"(?:\s*\[[^\]]+\])*"
        r"(?P<rest>[\s\S]*?)(?=(?:\b(?:input|output|inout)\b|;|\)\s*;|\Z))"
    )
    for m in mod_re.finditer(text):
        mname = m.group(1)
        body = m.group(2)
        ports: list[str] = []
        for dmatch in decl_re.finditer(body):
            rest = dmatch.group("rest")
            rest = re.sub(r"=[^,]+", "", rest)
            rest = re.sub(r"\[[^\]]+\]", "", rest)
            for tok in rest.split(","):
                tok = tok.strip()
                m_ident = re.search(r"\b([A-Za-z_]\w*)\b", tok)
                if m_ident:
                    name = m_ident.group(1)
                    if name not in ("input", "output", "inout", "wire", "reg", "logic", "signed", "unsigned"):
                        if name not in ports:
                            ports.append(name)
        out[mname] = ports
    return out


def cell_port_errors(children: dict[str, dict], cells: list[Path] | None) -> list[str]:
    """제공 셀과 같은 이름의 하위 모듈이 셀에 없는 포트/도메인 신호를 쓰면 알린다."""
    if not cells:
        return []
    cell_ports: dict[str, list[str]] = {}
    for c in cells:
        if Path(c).is_file():
            cell_ports.update(cell_port_names(Path(c)))
    errs = []
    for n, c in children.items():
        if n not in cell_ports:
            continue
        valid = set(cell_ports[n])
        used = set()
        for d in c.get("domains", []):
            for k in ("clock", "reset"):
                if d.get(k):
                    used.add(d[k])
        for b in c.get("ports", []):
            for sig in b.get("signals", [b]):
                if sig.get("name"):
                    used.add(sig["name"])
        bad = sorted(used - valid)
        if bad:
            errs.append(f"[제공 셀] {n}: 실제 셀 파일에 없는 포트 {bad}를 씀. 실제 포트: {cell_ports[n]}")
    return errs


def make_revise_task(name: str, block_top_l1: dict, spec: str | Path, tree: dict, conflicts: list[dict],
                     max_modules: int | None = None, cells: list[Path] | None = None) -> Task:
    """lead의 근본 원인 수정 요청: 지금 트리(tree: {"top", "children", "contracts"})와 충돌 경위를 받아 모듈 트리를 다시 정한다."""
    t = make_task(name, block_top_l1, spec, max_modules, cells=cells)
    t.inputs["current/contracts.json"] = json.dumps({"contracts": tree.get("contracts", [])}, ensure_ascii=False, indent=1)
    t.inputs[f"current/{tree['top']['module']}.ir.json"] = json.dumps(tree["top"], ensure_ascii=False, indent=1)
    for n, ir in tree["children"].items():
        t.inputs[f"current/{n}.ir.json"] = json.dumps(ir, ensure_ascii=False, indent=1)
    t.inputs["conflict.json"] = json.dumps(conflicts, ensure_ascii=False, indent=1)
    t.text += REVISE
    return t


def make_task(name: str, block_top_l1: dict, spec: str | Path, max_modules: int | None = None,
              cells: list[Path] | None = None) -> Task:
    """max_modules: 하위 모듈 수 상한 (P11 R6 재생성에서 원본 모듈 수로 정함). None이면 제한 없음. cells: 제공 셀 파일 목록."""
    inputs = {"spec.md": Path(spec), "spec_guide.md": SPEC_GUIDE, "block_top.json": json.dumps(block_top_l1, ensure_ascii=False, indent=1),
              "guide.md": GUIDE, "examples/pkt_rx_top.ir.json": EXAMPLE_TOP, "examples/pkt_parser.ir.json": EXAMPLE_MOD}
    if cells:
        for c in cells:
            p = Path(c)
            if p.is_file():
                inputs[f"cells/{p.name}"] = p.resolve()
    top = block_top_l1["module"]
    cells_note = CELLS_NOTE if cells else ""
    text = TASK.format(top=top, limit=LIMIT.format(n=max_modules) if max_modules else "") + cells_note
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


def dangling_instances(top_ir: dict, children: dict[str, dict]) -> list[str]:
    """출력이 하나도 이어지지 않은 인스턴스. 결과를 아무도 받지 않으므로 배선이 빠진 것이다
    (제공 셀을 Block Top에 두고 결과를 제어 모듈로 돌려보내지 않은 경우). 출력 일부만 쓰는 것은 허용한다."""
    used = set()
    for c in top_ir.get("connections", []):
        for k in ("from", "to"):
            inst, _, port = str(c.get(k, "")).partition(".")
            if port:
                used.add((inst, port))
    out = []
    for i in top_ir.get("instances", []):
        child = children.get(i["module"]) or {}
        outs, groups = [], []
        for b in child.get("ports", []):
            sigs = [x for x in b.get("signals", [b]) if x.get("dir") == "output"]
            outs += [x["name"] for x in sigs]
            if sigs and "bundle" in b:
                groups.append(b["bundle"])
        if outs and not any((i["name"], n) in used for n in [*outs, *groups]):
            out.append(f"[배선] {i['name']}({i['module']})의 출력 {outs[:6]}이 하나도 이어지지 않음: 결과를 쓰는 모듈 입력이나 "
                       f"Block Top 출력으로 잇는다")
    return out


def spec_latency(spec: str | Path) -> int | None:
    """SPEC 6절 (필수) 문장이 정한 고정 지연 사이클 수. 값이 하나로 정해질 때만 돌려주고, 여러 값이거나 "구현에 따름"이면 None."""
    from pipeline.spec.parse import parse
    try:
        reqs = parse(Path(spec).read_text(encoding="utf-8")).requirements
    except OSError:
        return None
    vals = set()
    for r in reqs:
        if r.section != 6 or r.level != "필수" or "구현에 따름" in r.text or not re.search(r"지연|latency|뒤|후", r.text, re.I):
            continue
        vals |= {int(v) for v in re.findall(r"(\d+)\s*(?:사이클|클럭|cycles?)", r.text, re.I)}
    vals.discard(0)
    return vals.pop() if len(vals) == 1 else None


def stage_budget_errors(top_ir: dict, work: Path, latency: int | None) -> list[str]:
    """하위 모듈이 직렬로 이어지는데 SPEC이 고정 지연을 정하면, Architect가 인스턴스별 단 수를 적고 그 합이 SPEC 지연과 같아야 한다.
    모듈 경계에서 레지스터 단이 옮겨지거나 늘어 사이클 등가가 깨지는 것을 막는다 (단일 파이프라인 연산기를 둘로 나눈 경우)."""
    if not latency or not sibling_links(top_ir):
        return []
    p = Path(work) / CONTRACTS
    try:
        raw = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except json.JSONDecodeError:
        return []                                   # contract_errors가 따로 알린다
    budget = raw.get("stage_budget") if isinstance(raw, dict) else None
    if not isinstance(budget, dict) or not budget:
        return [f"[단 배분] SPEC 6절 지연이 {latency}사이클인데 {CONTRACTS}에 stage_budget(인스턴스별 레지스터 단 수)이 없음"]
    insts = {i["name"] for i in top_ir.get("instances", [])}
    errs = [f"[단 배분] stage_budget의 {k}는 인스턴스가 아님" for k in budget if k not in insts]
    if not all(isinstance(v, int) and v >= 0 for v in budget.values()):
        return errs + ["[단 배분] stage_budget 값은 0 이상의 정수"]
    if sum(budget.values()) != latency:
        errs.append(f"[단 배분] stage_budget 합 {sum(budget.values())} ≠ SPEC 6절 지연 {latency}사이클: 모듈 경계로 단 수를 바꾸지 않는다")
    return errs


def check(block_top_l1: dict, max_modules: int | None = None, latency: int | None = None,
          cells: list[Path] | None = None):
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
        errs += dangling_instances(top_ir, children)
        errs += stage_budget_errors(top_ir, work, latency)
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

        # 제공 셀 포트 정합성 검사
        errs += cell_port_errors(children, cells)
        return errs
    return _check
