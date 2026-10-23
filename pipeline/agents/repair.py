"""Repair 에이전트: 실패 리포트를 보고 자기 후보의 IR을 고치는 patch 목록을 낸다.

볼 수 있는 것: SPEC, 자기 후보의 IR, 실패 리포트(분류 결과, 불일치 리포트), 이전 patch 이력.
하위 모듈이 여럿이면 Block Top의 인스턴스와 연결, 형제 모듈의 L1도 본다 (`block.json`).
Repair끼리 원인을 서로 넘겨 lead가 이 모듈을 고칠 곳으로 지정했으면(`lead.json`) 형제 모듈을 지목할 수 없다.
쓸 수 있는 것: `out/patches.json`. IR을 직접 고치지 않는다. 테스트와 모델은 볼 수 없고 고칠 수도 없다.
종료 게이트: patch 툴이 거부하지 않고(쓰기 범위, 동결 범위, waiver), 적용 결과가 Validator를 통과한다.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.agents.task import ROOT, Task
from pipeline.ir.model import Module
from pipeline.patch.apply import apply
from pipeline.validator.checks import errors, validate

GUIDE = ROOT / "pipeline" / "agents" / "prompts" / "ir_guide.md"
OUT = "out/patches.json"

TASK = """\
# 과제: {module} 모듈의 실패 고치기

너는 RTL 생성 파이프라인의 Repair 에이전트다. 검증에서 실패한 IR을 고치는 patch 목록을 쓴다.

## 입력
- `ir.json`: 지금 IR
- `failure.json`: 실패 리포트. `classified`는 실패 유형과 대상 노드, `report`는 첫 불일치(기댓값과 실제값),
  그 전후의 FSM 상태, 불일치 출력의 팬인 노드와 신호 값, 관련 SPEC 구절이다
- `history.json`: 이전에 적용한 patch (같은 patch를 반복하지 않는다)
- `spec.md`: SPEC. 무엇이 맞는 동작인지는 SPEC이 정한다
- `guide.md`: IR 작성 안내. IR의 `lang`이 `py`면 body와 cond는 Python 조각이다 (안내의 "Python 조각" 절).
  실패 메시지에 "Python 조각 N번째 줄"이 있으면 그 조각의 N번째 줄이 방출된 RTL의 오류 위치다

## 할 일
1. `failure.json`의 실패를 SPEC과 비교해 원인 노드를 찾는다. 팬인 노드 가운데에서 찾는다.
2. 고치는 patch 목록을 `{out}`에 JSON 배열로 쓴다. patch 하나는 아래 형식이다.
   - `{{"op": "set", "target": "blocks/<id>", "field": "body", "value": "..."}}`
   - `{{"op": "set", "target": "blocks/<id>/transitions/<id>", "field": "cond", "value": "..."}}`
   - `{{"op": "add", "target": "signals", "value": {{...}}}}`, `{{"op": "remove", "target": "signals/<name>"}}`
   - `{{"op": "set", "target": "functions/<name>", "field": "body", "value": "..."}}`, `{{"op": "set", "target": "assertions/<id>", "field": "expr", "value": "..."}}`
3. `python3 tools/pl.py patch ir.json {out}`가 `"ok": true`가 될 때까지 고친다. 이 도구는 patch를 적용해 보고 검사만 한다.

## 규칙
- 고칠 수 있는 것은 types, signals, blocks, functions, assertions, connections이다. L1(ports, params, domains)과 인스턴스는 고칠 수 없다.
- 실패가 IR의 SVA(`assertions/<id>`)이면 RTL이 그 성질을 어긴 것이다. 먼저 SPEC과 비교해 성질이 맞는지 보고, 맞으면 동작 블록을 고치고
  성질이 SPEC과 다르면 그 속성을 고친다. 테스트를 통과시키려고 속성을 지우거나 약하게 만들지 않는다.
  L1을 바꿔야 한다고 판단되면 patch 대신 `{out}`에 `[{{"op": "request_interface_change", "reason": "..."}}]`를 쓴다.
- lint waiver나 검사를 끄는 주석을 넣지 않는다.
- `{out}` 외의 파일은 만들거나 고치지 않는다.
{block}"""

BLOCK = """
## 형제 모듈
이 모듈은 Block Top `{top}` 안에서 형제 모듈 {siblings}와 함께 검증된다. 불일치는 Block Top의 출력에서 관측된 것이다.
`block.json`에 Block Top의 인스턴스와 연결, 형제 모듈의 L1, Architect가 정한 모듈 사이 계약(`contracts`: 실어 나르는 값의 뜻,
레지스터 단 수와 ready 관계)이 있다. 계약은 형제 모듈도 지키므로 이 모듈을 고칠 때 계약을 바꾸지 않는다. 실패가 계약을 어긴 쪽에서
났으면 그 모듈을 지목한다. 실패 리포트의 팬인은 Block Top의 출력을 구동하는 인스턴스까지다.
원인이 이 모듈이 아니라 형제 모듈에 있다고 판단되면 patch 대신 `{out}`에
`[{{"op": "suspect_module", "module": "<형제 모듈 이름>", "reason": "..."}}]`를 쓴다.
"""

LEAD = """
## lead 지정
형제 모듈의 Repair들이 이 실패의 원인을 서로 넘겨서 Design-lead가 결정했다: 원인은 이 모듈(`{module}`)에서 고친다.
`lead.json`에 그 결정(메모, 서로 넘긴 경위)이 있다. `suspect_module`은 쓸 수 없다. 형제 모듈의 동작은 바뀌지 않는다고 보고,
이 모듈의 동작을 바꿔 Block Top 출력이 SPEC을 만족하게 한다. 이 모듈의 L1로는 불가능하면 `request_interface_change`를 쓴다.
"""


def make_task(name: str, ir: dict, spec: str | Path, failure: dict, history: list[dict], block: dict | None = None,
              lead: dict | None = None) -> Task:
    """block: 하위 모듈이 여럿일 때 {"top": 이름, "instances", "connections", "siblings": {이름: L1}, "contracts": [...]}.
    lead: lead가 이 모듈을 고칠 곳으로 지정한 결정 {"id", "note", "chain", "claims"}."""
    inputs = {"ir.json": json.dumps(ir, ensure_ascii=False, indent=1), "spec.md": Path(spec), "guide.md": GUIDE,
              "failure.json": json.dumps(failure, ensure_ascii=False, indent=1),
              "history.json": json.dumps(history, ensure_ascii=False, indent=1)}
    extra = ""
    if block:
        inputs["block.json"] = json.dumps(block, ensure_ascii=False, indent=1)
        extra = BLOCK.format(top=block["top"], siblings=", ".join(f"`{n}`" for n in block["siblings"]), out=OUT)
    if lead:
        inputs["lead.json"] = json.dumps(lead, ensure_ascii=False, indent=1)
        extra += LEAD.format(module=ir["module"])
    return Task("repair", name, TASK.format(module=ir["module"], out=OUT, block=extra), inputs=inputs, outputs=[OUT])


def interface_change(patches: object) -> str | None:
    """Repair가 L1 변경을 요청했으면 그 이유. 스케줄러가 Architect로 올린다."""
    if isinstance(patches, list) and patches and all(isinstance(p, dict) for p in patches):
        req = [p for p in patches if p.get("op") == "request_interface_change"]
        if req:
            return str(req[0].get("reason", ""))
    return None


def suspect(patches: object) -> tuple[str, str] | None:
    """Repair가 원인을 형제 모듈로 지목했으면 (모듈, 이유). 스케줄러가 그 모듈의 Repair로 넘긴다."""
    if isinstance(patches, list) and patches and all(isinstance(p, dict) for p in patches):
        req = [p for p in patches if p.get("op") == "suspect_module"]
        if req:
            return str(req[0].get("module", "")), str(req[0].get("reason", ""))
    return None


def check(ir: dict, library: dict[str, Module] | None = None, siblings: set[str] | None = None, pinned: bool = False):
    """pinned: lead가 이 모듈을 고칠 곳으로 지정함. 형제 모듈 지목을 받지 않는다."""
    def _check(work: Path) -> list[str]:
        try:
            patches = json.loads((work / OUT).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return [f"patch 목록을 읽을 수 없음: {e}"]
        if interface_change(patches) is not None:
            return []
        if (sus := suspect(patches)) is not None:
            if pinned:
                return [f"lead가 이 모듈을 고칠 곳으로 지정함: suspect_module({sus[0]})을 쓸 수 없음 (patch 또는 request_interface_change)"]
            return [] if siblings and sus[0] in siblings else [f"형제 모듈이 아님: {sus[0]} (형제: {sorted(siblings or [])})"]
        res = apply(ir, patches)
        if not res.ok:
            return [f"patch 거부: {r['reason']}" for r in res.rejected]
        return [str(i) for i in errors(validate(Module(res.ir), library or {}))]
    return _check
