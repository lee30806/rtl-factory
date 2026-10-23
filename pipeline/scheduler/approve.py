"""사람 결정: 스케줄러가 멈춰 기다리는 항목을 보고 결정한다. 항목은 두 종류다.
- 기댓값 수정안 (expectation_change): 승인자가 승인하거나 거절한다.
- Repair 충돌 (repair_conflict): 형제 모듈의 Repair들이 원인을 서로 넘김. Design-lead가 고칠 모듈을 지정하거나 Architect 재구성을 요청한다.

    python3 -m pipeline.scheduler.approve <run_dir> list
    python3 -m pipeline.scheduler.approve <run_dir> show <id>
    python3 -m pipeline.scheduler.approve <run_dir> approve <id>... --by <이름> [--note "..."]
    python3 -m pipeline.scheduler.approve <run_dir> reject <id>... --by <이름> [--note "..."]
    python3 -m pipeline.scheduler.approve <run_dir> resolve <id> (--repair <모듈> | --architect) --by <이름> [--note "..."]

결정은 `<run_dir>/decisions.json`에 쌓이고(실행 ID, 요구사항 ID, SPEC 버전과 함께), 스케줄러를 같은 명령으로 다시 실행하면 반영된다.
- 승인: 그 Verifier가 수정안대로 테스트를 고치고, 그 Verifier를 기다리던 후보를 모두 다시 검증한다.
- 거절: 테스트가 맞다고 보고, 그 후보의 그 Verifier 실패를 Arbiter 없이 Repair로 보낸다.
- 고칠 모듈 지정: 그 후보 묶음을 다시 검증하고, Repair는 지정한 모듈에서만 한다 (형제 모듈 지목 금지).
- Architect 재구성: Architect가 충돌 경위를 보고 모듈 트리를 다시 정하고, 모든 후보를 Author부터 다시 돌린다.
결정은 되돌리지 않는다. 같은 항목을 두 번 결정할 수 없다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

from pipeline.scheduler.run import load_decisions


def _state(run_dir: Path) -> dict:
    return json.loads((run_dir / "state.json").read_text(encoding="utf-8"))


def pending(run_dir: Path) -> list[dict]:
    """결정을 기다리는 항목 (이미 결정된 것은 뺀다)."""
    decided = {d["id"] for d in load_decisions(run_dir)}
    return [p for p in _state(run_dir).get("pending", []) if p["id"] not in decided and not p.get("applied")]


DECISIONS = {"expectation_change": ("approved", "rejected"), "repair_conflict": ("repair", "architect")}


def decide(run_dir: Path, ids: list[str], decision: str, by: str, note: str = "", module: str | None = None) -> list[dict]:
    """module: repair_conflict를 repair로 결정할 때 고칠 모듈 (그 항목의 후보 묶음 모듈 가운데 하나)."""
    if not by.strip():
        raise ValueError("결정한 사람(--by)이 필요함")
    st = _state(run_dir)
    items = {p["id"]: p for p in st.get("pending", [])}
    open_ids = {p["id"] for p in pending(run_dir)}
    for i in ids:
        if i not in items:
            raise ValueError(f"없는 항목: {i}")
        if i not in open_ids:
            raise ValueError(f"이미 결정된 항목: {i}")
        kind = items[i]["kind"]
        if decision not in DECISIONS[kind]:
            raise ValueError(f"{i}({kind})의 결정은 {' 또는 '.join(DECISIONS[kind])}: {decision}")
        if kind == "repair_conflict" and decision == "repair" and module not in items[i]["mods"]:
            raise ValueError(f"{i}: 고칠 모듈은 {items[i]['mods']} 가운데 하나여야 함 (지금 {module})")
        if kind == "repair_conflict" and decision == "repair" and items[i].get("remaining", {}).get(module) == 0:
            raise ValueError(f"{i}: {module}은 Repair 상한을 다 써서 지정해도 Repair 없이 실패함. 다른 모듈이나 architect를 고름")
    spec = Path(st["spec"])
    spec_sha = hashlib.sha256(spec.read_bytes()).hexdigest()[:16] if spec.exists() else None
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    new = [{"id": i, "decision": decision, "by": by, "note": note, "at": now, "run_id": run_dir.name,
            "reqs": items[i].get("reqs"), "spec_sha256": spec_sha, "kind": items[i]["kind"]}
           | ({"module": module} if decision == "repair" else {}) for i in ids]
    path = run_dir / "decisions.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(load_decisions(run_dir) + new, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return new


def _reqs(p: dict) -> str:
    """요구사항 ID (Repair 충돌은 Repair마다 목록이라 펼친다)."""
    out = []
    for r in p.get("reqs") or []:
        out += r if isinstance(r, list) else [r]
    return ",".join(dict.fromkeys(out))


def _summary(p: dict) -> str:
    ff = p.get("first_failure") or {}
    if p["kind"] == "repair_conflict":
        left = "  남은 Repair " + ", ".join(f"{m} {n}" for m, n in p["remaining"].items()) if p.get("remaining") else ""
        return (f"{p['id']}  Repair 충돌  {p['group']} 후보 {p['c']}  {_reqs(p)}  {ff.get('reason', '')}{left}\n"
                + "\n".join(f"    {x['module']}" + (f" → {x['suspects']}" if x.get('suspects') else "") + f": {x['reason'][:160]}" for x in p["claims"]))
    where = ff.get("property") or ff.get("stream") or ff.get("bundle") or ""
    cs = ",".join(str(c) for c in [p["c"]] + [x["c"] for x in p.get("also", [])])   # 같은 실패로 묶인 후보들
    return (f"{p['id']}  {p['group']} 후보 {cs}  Verifier {p['verifier']}  {_reqs(p)}  "
            f"{ff.get('check', '')} {where}\n    수정안: {(p.get('proposal') or '')[:200]}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="사람 승인")
    ap.add_argument("run_dir", type=Path)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sh = sub.add_parser("show")
    sh.add_argument("id")
    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("ids", nargs="+")
        p.add_argument("--by", required=True)
        p.add_argument("--note", default="")
    r = sub.add_parser("resolve")
    r.add_argument("id")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--repair", metavar="모듈")
    g.add_argument("--architect", action="store_true")
    r.add_argument("--by", required=True)
    r.add_argument("--note", default="")
    a = ap.parse_args(argv)
    if a.cmd == "list":
        items = pending(a.run_dir)
        print("\n".join(_summary(p) for p in items) if items else "결정을 기다리는 항목이 없음")
        return 0
    if a.cmd == "show":
        p = next((x for x in _state(a.run_dir).get("pending", []) if x["id"] == a.id), None)
        if p is None:
            print(f"없는 항목: {a.id}", file=sys.stderr)
            return 1
        print(json.dumps(p, ensure_ascii=False, indent=1))
        return 0
    try:
        if a.cmd == "resolve":
            new = decide(a.run_dir, [a.id], "architect" if a.architect else "repair", a.by, a.note, module=a.repair)
        else:
            new = decide(a.run_dir, a.ids, "approved" if a.cmd == "approve" else "rejected", a.by, a.note)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    print(json.dumps(new, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
