"""P11 R5: Block Top L1 → SPEC 2~4절 표 (Markdown). `pipeline.spec.l1`의 역방향이라, 만든 표를 다시 L1로 읽으면 같은 L1이 된다.

비교에서 빼는 것: role(이식 IR의 top은 role module일 수 있고 SPEC의 L1은 block_top), 파라미터의 허용 범위(SpecWriter가 채움),
local 파라미터(SPEC 표에 없음).
"""
from __future__ import annotations

from pipeline.ir.model import frozen_l1

SEC2_HEAD = ["도메인", "클럭 포트", "주파수", "리셋 포트", "리셋 극성", "리셋 동기", "도메인 간 관계", "전력 도메인", "isolation", "retention"]
SEC3_HEAD = ["포트 또는 번들", "방향", "타입", "프로토콜 태그", "도메인"]
SEC4_HEAD = ["이름", "타입", "기본값", "허용 범위"]


def _row(cells: list[str]) -> str:
    return "| " + " | ".join(str(c).replace("|", "\\|") for c in cells) + " |"


def _table(head: list[str], rows: list[list[str]]) -> str:
    return "\n".join([_row(head), _row(["---"] * len(head)), *(_row(r) for r in rows)])


def _freq(f) -> str:
    if f is None:
        return "–"
    f = float(f)
    return f"{int(f)} MHz" if f.is_integer() else f"{f:g} MHz"


def section2(l1: dict) -> str:
    doms = l1["domains"]
    rows = []
    pds = {p["name"]: p for p in l1.get("power_domains", [])}
    for d in doms:
        r = d.get("reset")
        if r:
            ports = [r["port"], *(x["port"] for x in r.get("extra", []))]
            acts = [r["active"], *(x["active"] for x in r.get("extra", []))]
            sync = ("비동기 (블록 안에서 동기화)" if r.get("synchronize") else "비동기") if r["async"] else "동기"
        else:   # 리셋 없는 도메인
            ports, acts, sync = ["없음"], ["–"], "–"
        rel = []
        s = d.get("sync_to")
        same_clock = next((x for x in doms if x is not d and x["clock"] == d["clock"] and doms.index(x) < doms.index(d)), None)
        if s and not (same_clock and s["div"] == 1 and s["domain"] == same_clock["name"]):
            rel.append(f"{s['domain']}에서 {s['div']}분주")
        if d.get("async_to"):
            rel.append(f"{', '.join(d['async_to'])}와 비동기")
        if not rel:
            rel.append("같은 클럭" if same_clock else ("단일 도메인" if len(doms) == 1 else "–"))
        pd = d.get("power_domain")
        p = pds.get(pd, {})
        rows.append([d["name"], d["clock"], _freq(d.get("freq_mhz")), ", ".join(ports), ", ".join(acts), sync, ", ".join(rel),
                     pd or "없음", p.get("isolation", "없음"), p.get("retention", "없음")])
    if not doms:   # 조합 블록: 도메인이 아닌 표시 행 하나 (l1이 읽지 않음)
        rows.append(["없음", "없음", "–", "없음", "–", "–", "조합 블록", "없음", "없음", "없음"])
    if any(d["edge"] == "neg" for d in doms):   # 하강 에지 도메인이 있을 때만 클럭 에지 열을 넣는다
        head = SEC2_HEAD[:2] + ["클럭 에지"] + SEC2_HEAD[2:]
        rows = [r[:2] + ["하강" if d["edge"] == "neg" else "상승"] + r[2:] for r, d in zip(rows, doms)]
        return _table(head, rows)
    return _table(SEC2_HEAD, rows)


def section3(l1: dict) -> str:
    rows = []
    for d in l1["domains"]:
        rows.append([d["clock"], "input", "logic", "–", d["name"]])
        r = d.get("reset") or {}
        for p in ([r["port"], *(x["port"] for x in r.get("extra", []))] if r else []):
            if p not in [x[0] for x in rows]:
                rows.append([p, "input", "logic", "–", d["name"]])
    for p in l1["ports"]:
        if "bundle" in p:
            rows.append([f"**{p['bundle']}** ({p['role']})", "", "", p["protocol"], p["domain"]])
            for s in p["signals"]:
                rows.append([s["name"], s["dir"], s["type"], "", ""])
        else:
            rows.append([p["name"], p["dir"], p["type"], "–", p["domain"] if p["domain"] is not None else "비동기" if l1["domains"] else "–"])
    return _table(SEC3_HEAD, rows)


def section4(l1: dict) -> str:
    rows = [[p["name"], p["type"], p["default"], p.get("range", "")] for p in l1["params"] if not p.get("local")]
    return _table(SEC4_HEAD, rows)


def comparable(l1: dict) -> dict:
    """SPEC 왕복 비교용 L1: role, 파라미터 허용 범위, local 파라미터, 출력 kind/리셋값을 뺀다."""
    out = frozen_l1(l1)
    out.pop("role", None)
    out["params"] = [{k: v for k, v in p.items() if k != "range"} for p in out["params"]]
    for d in out.get("domains", []):
        if "freq_mhz" in d:
            d["freq_mhz"] = float(d["freq_mhz"])
        if "async_to" in d:
            d["async_to"] = sorted(d["async_to"])
    return out


def roundtrip_diff(ir_l1: dict, spec_l1: dict) -> list[str]:
    a, b = comparable(ir_l1), comparable(spec_l1)
    out = []
    for k in sorted(set(a) | set(b)):
        if a.get(k) != b.get(k):
            out.append(f"{k}: IR {a.get(k)!r} / SPEC {b.get(k)!r}")
    return out


def tables_md(l1: dict) -> str:
    """SPEC 2~4절 (제목 포함). SpecWriter가 그대로 넣는다."""
    return (f"## 2. 클럭, 리셋, 전력\n\n{section2(l1)}\n\n## 3. 인터페이스\n\n{section3(l1)}\n\n"
            f"## 4. 파라미터\n\n{section4(l1)}\n")
