"""WaveJSON을 사이클 단위로 펼치고 정답과 비교한다.

비교 규칙
- 신호는 이름으로 짝짓는다 (대소문자, 공백 무시).
- 각 사이클의 상태와 데이터 라벨이 모두 같아야 일치로 본다.
- '.', '|'는 직전 사이클 유지. h/H→1, l/L→0, P→p, N→n, '2'~'9'→'=' (색 차이는 무시).
- period가 정수 k이면 문자 하나가 k 사이클. phase는 무시한다.
- 정답에 있는데 예측에 없는 신호는 모든 사이클을 불일치로 센다.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_NORMALIZE = {"h": "1", "H": "1", "l": "0", "L": "0", "P": "p", "N": "n"}
_DATA_STATES = set("=23456789")


def parse_wavejson(text: str) -> dict:
    """JSON 또는 따옴표 없는 키를 쓴 WaveDrom 관용 표기를 읽는다."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fixed = re.sub(r'([{,]\s*)([A-Za-z_]\w*)\s*:', r'\1"\2":', text)
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        # 작은따옴표 문자열. 8'hA5 같은 라벨이 있으면 여기서도 실패할 수 있다.
        return json.loads(fixed.replace("'", '"'))


def flatten(wj: dict) -> dict[str, dict]:
    """그룹(배열)과 빈 spacer({})를 걷어내고 이름 → 신호 사전을 만든다."""
    out: dict[str, dict] = {}

    def walk(items):
        for it in items:
            if isinstance(it, list):
                walk(it[1:] if it and isinstance(it[0], str) else it)
            elif isinstance(it, dict) and "wave" in it and "name" in it:
                out[norm_name(it["name"])] = it

    walk(wj.get("signal", []))
    return out


def norm_name(name: str) -> str:
    return re.sub(r"\s+", "", str(name)).lower()


def expand(sig: dict) -> list[tuple[str, str | None]]:
    """신호 하나를 [(상태, 데이터 라벨)] 사이클 목록으로 펼친다."""
    data = sig.get("data", [])
    labels = data.split() if isinstance(data, str) else [str(d) for d in data]
    period = sig.get("period", 1)
    period = int(period) if isinstance(period, (int, float)) and period >= 1 else 1

    cycles: list[tuple[str, str | None]] = []
    li = 0
    prev: tuple[str, str | None] = ("x", None)
    for ch in sig.get("wave", ""):
        if ch in ".|":
            cur = prev
        elif ch in _DATA_STATES:
            cur = ("=", labels[li].strip() if li < len(labels) else None)
            li += 1
        else:
            cur = (_NORMALIZE.get(ch, ch), None)
        cycles.extend([cur] * period)
        prev = cur
    return cycles


def compare(gt: dict, pred: dict) -> dict:
    g, p = flatten(gt), flatten(pred)
    per_signal = {}
    total = hits = 0
    for name, gsig in g.items():
        gc = expand(gsig)
        pc = expand(p[name]) if name in p else []
        length = max(len(gc), len(pc))
        h = sum(1 for a, b in zip(gc, pc) if a == b)
        per_signal[name] = {"cycles": length, "match": h, "present": name in p}
        total += length
        hits += h
    missing = [n for n in g if n not in p]
    extra = [n for n in p if n not in g]
    return {
        "cycle_accuracy": hits / total if total else 1.0,
        "signal_recall": (len(g) - len(missing)) / len(g) if g else 1.0,
        "signal_precision": (len(p) - len(extra)) / len(p) if p else (1.0 if not g else 0.0),
        "missing": missing,
        "extra": extra,
        "exact": hits == total and not extra,
        "per_signal": per_signal,
    }


# ---------------------------------------------------------------- 그리기 (원본과 나란히 대조하는 용도)
CW, RH, X0, Y0, AMP = 48, 36, 90, 20, 18  # 사이클 폭, 행 높이, 파형 시작 x, 첫 행 y, 진폭


def render(wj: dict, path: Path) -> None:
    """WaveJSON을 PNG 타이밍 다이어그램으로 그린다. 변환 결과를 원본 그림과 나란히 놓고 사람이 대조한다."""
    try:
        import pymupdf
    except ImportError:
        import fitz as pymupdf

    sigs = list(flatten(wj).values())
    ncyc = max(len(expand(s)) for s in sigs)
    doc = pymupdf.open()
    page = doc.new_page(width=X0 + CW * ncyc + 20, height=Y0 + RH * len(sigs) + 10)
    black, grey = (0, 0, 0), (0.75, 0.75, 0.75)
    for c in range(ncyc + 1):  # 사이클 경계 보조선
        page.draw_line((X0 + c * CW, Y0 - 8), (X0 + c * CW, Y0 + RH * len(sigs)), color=grey, width=0.5, dashes="[2] 0")
    for r, sig in enumerate(sigs):
        top = Y0 + r * RH + 6
        hi, lo, mid = top, top + AMP, top + AMP / 2
        page.insert_text((8, mid + 4), sig["name"], fontsize=11, color=black)
        prev = None
        for i, (st, label) in enumerate(expand(sig)):
            x1, x2 = X0 + i * CW, X0 + (i + 1) * CW
            line = lambda a, b: page.draw_line(a, b, color=black, width=1.2)  # noqa: E731
            if st in ("p", "n"):
                first, second = (hi, lo) if st == "p" else (lo, hi)
                line((x1, lo), (x1, hi)) if st == "p" else line((x1, hi), (x1, lo))
                line((x1, first), ((x1 + x2) / 2, first))
                line(((x1 + x2) / 2, first), ((x1 + x2) / 2, second))
                line(((x1 + x2) / 2, second), (x2, second))
            elif st in ("0", "1"):
                y = lo if st == "0" else hi
                if prev and prev[0] in ("0", "1") and prev[0] != st:
                    line((x1, hi), (x1, lo))
                line((x1, y), (x2, y))
            elif st in ("=", "x"):
                new_seg = prev != (st, label)
                start = x1 + 6 if new_seg else x1
                if new_seg:
                    line((x1, mid), (start, hi))
                    line((x1, mid), (start, lo))
                line((start, hi), (x2, hi))
                line((start, lo), (x2, lo))
                if st == "x":
                    for k in range(int(start), int(x2), 8):
                        page.draw_line((k, lo), (min(k + 8, x2), hi), color=grey, width=0.8)
                elif new_seg and label:
                    page.insert_text((start + 4, mid + 4), label, fontsize=10, color=black)
            elif st == "z":
                line((x1, mid), (x2, mid))
            prev = (st, label)
    page.get_pixmap(dpi=144).save(str(path))
    doc.close()


