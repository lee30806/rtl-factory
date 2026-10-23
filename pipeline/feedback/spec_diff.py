"""SPEC 개정의 영향: 바뀐 요구사항 ID, 바뀐 예시 시나리오, Block Top L1이 바뀌었는지.

L1(SPEC 2~4번 섹션 표)이 바뀌면 인터페이스가 바뀐 것이므로 처음부터 다시 실행한다.
9번 섹션(구현 제약)이 바뀌어도 처음부터 다시 실행한다. 구조 제약은 테스트로 드러나지 않아 실패한 모듈만 고치는 경로로는 반영되지 않는다.
L1이 같으면 Verifier가 바뀐 요구사항에 맞춰 테스트를 고치고, 선택된 RTL을 그 테스트로 다시 검증해 실패한 모듈만 고친다.
"""
from __future__ import annotations

from pathlib import Path

from pipeline.ir.model import frozen_l1
from pipeline.spec.l1 import block_top_l1
from pipeline.spec.parse import load


def diff(old: str | Path, new: str | Path) -> dict:
    a, b = load(old), load(new)
    ra = {r.id: (r.level, r.text) for r in a.requirements}
    rb = {r.id: (r.level, r.text) for r in b.requirements}
    sa = {s["id"]: s for s in a.scenarios}
    sb = {s["id"]: s for s in b.scenarios}
    la, _ = block_top_l1(a)
    lb, _ = block_top_l1(b)
    cons = [_constraints(x) for x in (a, b)]
    changed = sorted(i for i in ra.keys() & rb.keys() if ra[i] != rb[i])
    scen_changed = sorted(i for i in sa.keys() & sb.keys()
                          if (sa[i].get("title"), sa[i].get("table")) != (sb[i].get("title"), sb[i].get("table")))
    return {"added": sorted(rb.keys() - ra.keys()), "removed": sorted(ra.keys() - rb.keys()), "changed": changed,
            "scenarios_added": sorted(sb.keys() - sa.keys()), "scenarios_removed": sorted(sa.keys() - sb.keys()),
            "scenarios_changed": scen_changed, "l1_changed": frozen_l1(la) != frozen_l1(lb),
            "constraints_changed": cons[0] != cons[1], "module": b.module}


def _constraints(spec) -> list[str]:
    sec = spec.sections.get(9)
    return [ln.strip() for _, ln in sec.lines if ln.strip()] if sec else []


def full_rerun(d: dict) -> bool:
    return bool(d["l1_changed"] or d.get("constraints_changed"))


def affected_reqs(d: dict) -> list[str]:
    return sorted(set(d["added"]) | set(d["removed"]) | set(d["changed"]))
