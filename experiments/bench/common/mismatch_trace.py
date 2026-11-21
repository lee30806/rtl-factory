"""외부 채점 실패의 사이클 단위 불일치 요약: 신호별 첫 불일치 사이클과 그 전후의 입력·기대·실제 값.

Repair와 외부 수정(SpecWriter)이 "몇 개 틀림, 첫 시각 245"만으로는 원인을 좁히지 못한다 (RealBench sd_fifo_tx_filler:
불일치 개수만 보고 ackd 갱신 시점을 잘못 추정함). 정답 RTL은 주지 않고, 테스트벤치가 비교하는 출력의 기대값만 보인다.

행(row)은 한 클럭 상승 에지의 표본이다: {"cyc": 번호, "time": 시각 문자열 | None, "ins": {입력: 값}, "exp": {출력: 값}, "act": {출력: 값}}.
값은 2진 문자열(0/1/x/z)이다. 기대값의 x/z 비트는 비교하지 않는다.
"""
from __future__ import annotations

WINDOW_BEFORE = 6
WINDOW_AFTER = 1
MAX_INPUTS = 12
MAX_OUTPUTS = 6
MAX_CHARS = 3000


def differs(exp: str, act: str) -> bool:
    """기대값이 정해진(0/1) 비트 가운데 실제값과 다른 것이 있으면 True. 폭이 다르면 낮은 비트부터 맞춘다."""
    exp, act = exp.lower(), act.lower()
    n = max(len(exp), len(act))
    exp, act = exp.rjust(n, "0"), act.rjust(n, "0")
    return any(e in "01" and e != a for e, a in zip(exp, act))


def show(v: str) -> str:
    """0/1만이면 16진, x/z가 있으면 2진 그대로."""
    v = v.lower()
    if len(set(v)) == 1 and v[0] in "xz":
        return v[0]
    if v and set(v) <= {"0", "1"}:
        return "1" if v == "1" else ("0" if v == "0" else f"{int(v, 2):x}")
    return v


def summarize(rows: list[dict], clock: str = "clk", source: str = "") -> str:
    """신호별 첫 불일치와 가장 이른 불일치 전후 창. 불일치가 없으면 빈 문자열."""
    if not rows:
        return ""
    outs = list(rows[0]["exp"])
    first: dict[str, int] = {}
    count: dict[str, int] = {}
    for i, r in enumerate(rows):
        for o in outs:
            if differs(r["exp"].get(o, ""), r["act"].get(o, "")):
                first.setdefault(o, i)
                count[o] = count.get(o, 0) + 1
    if not first:
        return ""
    bad = sorted(first, key=lambda o: first[o])[:MAX_OUTPUTS]
    lines = [f"신호별 첫 불일치 ({source + ', ' if source else ''}사이클은 {clock} 상승 에지 표본 번호):"]
    for o in bad:
        r = rows[first[o]]
        at = f" (시각 {r['time']})" if r.get("time") else ""
        lines.append(f"- {o}: 사이클 {r['cyc']}{at}, 기대 {show(r['exp'][o])} 실제 {show(r['act'][o])}, 불일치 표본 {count[o]}개")
    i0 = first[bad[0]]
    lo, hi = max(0, i0 - WINDOW_BEFORE), min(len(rows), i0 + WINDOW_AFTER + 1)
    ins = list(rows[0]["ins"])
    changing = [n for n in ins if len({rows[j]["ins"].get(n) for j in range(lo, hi)}) > 1]
    ins = (changing + [n for n in ins if n not in changing])[:MAX_INPUTS]
    lines.append(f"가장 이른 불일치 전후 (사이클 {rows[lo]['cyc']}~{rows[hi - 1]['cyc']}). 입력 | 출력 기대/실제 (* = 불일치):")
    lines.append("cyc | " + " ".join(ins) + " | " + " ".join(bad))
    for j in range(lo, hi):
        r = rows[j]
        o_txt = " ".join(f"{show(r['exp'][o])}/{show(r['act'][o])}{'*' if differs(r['exp'][o], r['act'][o]) else ''}" for o in bad)
        lines.append(f"{r['cyc']} | " + " ".join(show(r["ins"].get(n, "?")) for n in ins) + " | " + o_txt)
    text = "\n".join(lines)
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + "\n..."
