"""sim 불일치 리포트와 FSM 전이 커버리지. 하니스가 남긴 결과와 트레이스, IR로 만든다.

불일치 리포트는 Repair에 넘길 네 가지를 묶는다.
- 첫 불일치 트랜잭션 (스코어보드, 프로토콜 체커, 속성 중 처음 것)
- 그 시점 전후의 FSM 상태 트레이스
- 불일치 출력의 팬인에 속한 신호 값. 블록의 reads/writes로 고른다
- 해당 SPEC 구절 (요구사항 ID가 들어간 줄)

    python3 -m pipeline.tb.report <ir.json> <x.result.json> <x.trace.json> [--spec spec.md]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from pipeline.ir.model import Module, load

WINDOW_BEFORE, WINDOW_AFTER = 8, 2


def _writers(m: Module, library: dict[str, Module] | None) -> dict[str, list[tuple[str, list[str]]]]:
    """신호 → [(쓰는 노드, 그 노드가 읽는 신호)]. 인스턴스 출력의 팬인은 그 인스턴스의 입력 포트(`<인스턴스>.<포트>`)다."""
    from pipeline.validator.checks import _endpoint_ports

    out: dict[str, list[tuple[str, list[str]]]] = {}
    for b in m.blocks:
        node = f'blocks/{b["id"]}'
        if b["kind"] in ("comb", "seq"):
            for w in b["writes"]:
                out.setdefault(w, []).append((node, list(b["reads"])))
        elif b["kind"] == "fsm":
            q = f'{b["state_name"]}_q'
            out.setdefault(q, []).append((node, list(b["reads"]) + [q]))
        elif b["kind"] == "cdc_sync":
            out.setdefault(b["dst"], []).append((node, [b["src"]]))
    lib = {**(library or {}), m.name: m}
    inst_mod = {i["name"]: lib.get(i["module"]) for i in m.instances}

    def inputs(inst: str) -> list[str]:
        # 인스턴스 출력의 팬인은 그 인스턴스의 입력 포트다 (형제 인스턴스가 구동하면 그쪽으로 이어짐)
        child = inst_mod.get(inst)
        return [f"{inst}.{p.name}" for p in child.ports if p.dir == "input" and not p.derived] if child else []

    for c in m.connections:
        ends = [_endpoint_ports(m, c[k], lib) for k in ("from", "to")]
        owners = [c[k].split(".", 1)[0] for k in ("from", "to")]
        if not all(ends):
            continue
        for (da, na), (db, nb) in zip(*ends):  # 번들은 신호 단위로 펼쳐진다
            for (d, n, own), (_, other, oown) in (((da, na, owners[0]), (db, nb, owners[1])),
                                                  ((db, nb, owners[1]), (da, na, owners[0]))):
                # 인스턴스의 출력 포트 → 반대편 신호(자기 포트나 내부 신호, 다른 인스턴스는 넷)
                if own != "self" and d == "output":
                    out.setdefault(other if oown == "self" else f"{oown}.{other}", []).append((f"instances/{own}", inputs(own)))
    return out


def fanin(m: Module, signals: list[str], library: dict[str, Module] | None = None) -> tuple[list[str], list[str]]:
    """출력 신호들의 팬인 → (노드 목록, 신호 목록). 입력 포트에서 멈추고, 인스턴스는 입력 포트를 거쳐 형제 인스턴스로 이어진다."""
    writers = _writers(m, library)
    seen_sig, nodes, todo = set(), [], list(signals)
    while todo:
        s = todo.pop(0)
        if s in seen_sig:
            continue
        seen_sig.add(s)
        for node, reads in writers.get(s, []):
            if node not in nodes:
                nodes.append(node)
            todo += [r for r in reads if r not in seen_sig]
    return nodes, sorted(seen_sig)


def _stream_signals(m: Module, stream: str) -> list[str]:
    b = next((p for p in m.raw["ports"] if p.get("bundle") == stream), None)
    return [s["name"] for s in b["signals"] if s["dir"] == "output"] if b else [stream]


def _state_names(m: Module) -> dict[str, list[str]]:
    """FSM 상태 레지스터 → enum 멤버 (값 = 순서)."""
    enums = {t["name"]: t["members"] for t in m.types if t["kind"] == "enum"}
    return {f'{b["state_name"]}_q': enums.get(b["state_type"], []) for b in m.blocks if b["kind"] == "fsm"}


def _state(names: list[str], v):
    return names[v] if isinstance(v, int) and 0 <= v < len(names) else v


def _first_failure(result: dict) -> dict | None:
    cands = []
    if result.get("scoreboard"):
        sb = result["scoreboard"]
        missing = sb["kind"] == "missing" and sb.get("expected_since") is not None
        anchor = sb["expected_since"] if missing else sb["cycle"]
        dom = sb.get("expected_domain") if missing else sb.get("domain")
        cands.append({"check": "T3", **sb, "anchor": anchor, "anchor_domain": dom, "signals_from": sb["stream"]})
    for v in result.get("protocol", []):
        cands.append({"check": "T2", **v, "anchor": v["cycle"], "anchor_domain": v.get("domain"), "signals_from": v["bundle"]})
    for p in result.get("properties", []):
        cands.append({"check": "T4", **p, "anchor": p["cycle"], "anchor_domain": p.get("domain"), "signals_from": None})
    # 사이클은 도메인마다 따로 세므로, 도메인이 섞이면 "가장 이른 것"은 근사다. 같은 도메인 안에서는 정확하다.
    # 사이클을 모르는 실패(테스트가 사이클 없이 넣은 기댓값의 missing)는 맨 뒤로 본다
    return min(cands, key=lambda c: (c["anchor"] is None, c["anchor"] or 0)) if cands else None


def mismatch_report(m: Module, result: dict, trace: list[dict], spec_text: str | None = None,
                    library: dict[str, Module] | None = None) -> dict | None:
    first = _first_failure(result)
    if first is None:
        return None
    dom = first.get("anchor_domain")
    if first["anchor"] is None:   # 사이클을 모름: 트레이스 끝 구간을 보여 준다 (끝까지 오지 않은 출력)
        first["anchor"] = max((t["cycle"] for t in trace if dom is None or t["domain"] == dom), default=0)
        first["anchor_note"] = "실패 사이클을 모름. 트레이스 끝 구간"
    lo, hi = first["anchor"] - WINDOW_BEFORE, first["anchor"] + WINDOW_AFTER
    window = [t for t in trace if lo <= t["cycle"] <= hi and (dom is None or t["domain"] == dom)]
    outs = _stream_signals(m, first["signals_from"]) if first["signals_from"] else \
        [p.name for p in m.ports if p.dir == "output"]
    nodes, sigs = fanin(m, outs, library)
    states = _state_names(m)
    fsm_trace, fsm_nodes = {}, []
    for b in m.blocks:
        if b["kind"] == "fsm" and f'blocks/{b["id"]}' in nodes:
            q = f'{b["state_name"]}_q'
            fsm_trace[q] = [{"cycle": t["cycle"], "state": _state(states[q], t["values"][q])}
                            for t in window if q in t["values"]]
            fsm_nodes += [f'blocks/{b["id"]}/transitions/{tr["id"]}' for tr in b["transitions"]]
    values = [{"cycle": t["cycle"], **{s: t["values"][s] for s in sigs if s in t["values"]}} for t in window]
    req = first.get("req")
    reqs = [req] if isinstance(req, str) else list(req or [])  # 기댓값 하나에 요구사항이 여럿일 수 있음
    clauses = [ln.strip() for ln in (spec_text or "").splitlines() if any(r in ln for r in reqs)]
    return {"test": result.get("test"), "first_failure": {k: v for k, v in first.items() if k != "signals_from"},
            "fsm_trace": fsm_trace, "fanin": {"outputs": outs, "nodes": nodes, "fsm_transitions": fsm_nodes,
                                              "signals": sigs, "values": values},
            "spec": {"req": req, "clauses": clauses}}


def fsm_coverage(m: Module, traces: list[list[dict]]) -> dict[str, dict]:
    """T5 일부. 관측된 (이전 상태, 다음 상태) 쌍으로 IR 전이를 덮었는지 본다.

    같은 from/to를 가진 전이가 여럿이면 모두 덮은 것으로 센다. from == to 인 전이는 상태 변화로 관측할 수 없다.
    """
    states = _state_names(m)
    out = {}
    for b in m.blocks:
        if b["kind"] != "fsm":
            continue
        q, names = f'{b["state_name"]}_q', states[f'{b["state_name"]}_q']
        seen = set()
        for tr in traces:
            vals = [t["values"].get(q) for t in tr if q in t["values"]]
            for a, c in zip(vals, vals[1:]):
                if a != c:
                    seen.add((_state(names, a), _state(names, c)))
        trs = b["transitions"]
        out[f'blocks/{b["id"]}'] = {
            "covered": [t["id"] for t in trs if (t["from"], t["to"]) in seen],
            "uncovered": [t["id"] for t in trs if t["from"] != t["to"] and (t["from"], t["to"]) not in seen],
            "unobservable": [t["id"] for t in trs if t["from"] == t["to"]],
        }
    return out


_REQ = re.compile(r"^\s*[-*]\s*([A-Z][A-Z0-9]*-\d+)\s*:", re.M)


def req_coverage(results: list[dict], spec_text: str) -> dict:
    """T5. SPEC에 있는 요구사항 ID 중 테스트에서 실행된 것 (기댓값이 맞았거나, 속성이 평가됐거나, 체커가 검사함)."""
    spec = _REQ.findall(spec_text)
    hit = set().union(*(set(r.get("reqs", [])) for r in results)) if results else set()
    return {"total": len(spec), "covered": sorted(set(spec) & hit), "missed": [r for r in spec if r not in hit],
            "ratio": round(len(set(spec) & hit) / len(spec), 3) if spec else None}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="sim 불일치 리포트")
    ap.add_argument("ir")
    ap.add_argument("result")
    ap.add_argument("trace")
    ap.add_argument("--spec")
    args = ap.parse_args(argv)
    rep = mismatch_report(load(args.ir), json.loads(Path(args.result).read_text()), json.loads(Path(args.trace).read_text()),
                          Path(args.spec).read_text(encoding="utf-8") if args.spec else None)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
