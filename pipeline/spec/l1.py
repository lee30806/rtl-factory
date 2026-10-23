"""SPEC 2~4번 섹션의 표에서 Block Top의 L1(IR)을 만든다. 결정론적 툴이다.

    2. 클럭, 리셋, 전력 → domains, power_domains
    3. 인터페이스       → ports (굵은 글씨 행 `**이름** (role)`이 번들의 시작. 그 아래 도메인 칸이 빈 행이 번들 신호)
    4. 파라미터         → params

클럭과 리셋 포트는 domains에서 유도되므로 ports에 넣지 않는다. 도메인이 "비동기"인 포트는 domain null.

2번 섹션 표의 칸:
- 리셋 포트와 리셋 극성: 쉼표로 여럿을 쓰면 첫째가 주 리셋, 나머지는 추가 동기 리셋(`reset.extra`). 리셋 포트가 "없음"이면 리셋 없는 도메인.
- 리셋 동기: "비동기"가 있으면 비동기 리셋. "블록 안에서 동기화"가 있으면 `reset.synchronize`.
- 도메인 간 관계: "<도메인>과 비동기"는 `async_to`, "<도메인>에서 N분주"는 `sync_to`.
  클럭 포트가 앞 도메인과 같으면 그 도메인과 같은 클럭(`sync_to`, 분주비는 앞 도메인과 같음)으로 본다.
- 클럭 에지는 상승 에지로 고정한다 (하강 에지 도메인은 쓰지 않음).
- 조합 블록(클럭 없음): 도메인과 클럭 포트 칸이 모두 "없음"인 행 하나만 둔다. 이 행은 도메인이 아니라 domains가 빈다.
  3번 섹션 포트의 도메인 칸은 "–".
만든 IR의 각 노드가 어느 SPEC 줄에서 왔는지 `provenance`로 함께 돌려준다.

    python3 -m pipeline.spec.l1 <spec.md> -o <block_top.ir.json>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from pipeline.spec.parse import Spec, Table, load

ASYNC_WORDS = ("비동기", "async", "none", "-", "–", "")
NONE_WORDS = ("없음", "none", "-", "–", "")


class SpecTableError(Exception):
    pass


def _table(spec: Spec, sec: int, must: tuple[str, ...]) -> Table:
    s = spec.sections.get(sec)
    if s is None:
        raise SpecTableError(f"{sec}번 섹션이 없음")
    for t in s.tables:
        if all(c in t.header for c in must):
            return t
    raise SpecTableError(f"{sec}번 섹션에 {must} 열을 가진 표가 없음")


def _freq_mhz(text: str) -> float | None:
    m = re.search(r"([\d.]+)\s*(G|M|k)?Hz", text, re.I)
    if not m:
        return None
    scale = {"g": 1000.0, "m": 1.0, "k": 0.001, None: 1e-6}[(m.group(2) or "").lower() or None]
    return float(m.group(1)) * scale


def domains(spec: Spec) -> tuple[list[dict], list[dict], dict]:
    t = _table(spec, 2, ("도메인", "클럭 포트", "리셋 포트", "리셋 극성"))
    rows = [(r, line) for r, line in zip(t.dicts(), t.row_lines)
            if not (r["도메인"] in NONE_WORDS and r["클럭 포트"] in NONE_WORDS)]   # 조합 블록 표시 행
    names = [r["도메인"] for r, _ in rows]
    doms, pds, prov = [], {}, {}
    for r, line in rows:
        rports = [x.strip() for x in r["리셋 포트"].split(",")]
        actives = ["low" if "low" in x.lower() else "high" for x in r["리셋 극성"].split(",")]
        actives += actives[-1:] * (len(rports) - len(actives))   # 극성을 하나만 쓰면 모두 같은 극성
        sync_col = r.get("리셋 동기", "")
        edge_col = r.get("클럭 에지", "").lower()   # 선택 열. 없으면 상승 에지
        d = {"name": r["도메인"], "clock": r["클럭 포트"],
             "edge": "neg" if any(w in edge_col for w in ("하강", "neg", "fall")) else "pos",
             "reset": {"port": rports[0], "active": actives[0],
                       "async": "비동기" in sync_col or "async" in sync_col.lower()}}
        if rports[0] in NONE_WORDS:   # 리셋 없는 도메인
            del d["reset"]
        elif len(rports) > 1:
            d["reset"]["extra"] = [{"port": p, "active": a} for p, a in zip(rports[1:], actives[1:])]
        if "블록 안에서 동기화" in sync_col and "reset" in d:
            d["reset"]["synchronize"] = True
        f = _freq_mhz(r.get("주파수", ""))
        if f:
            d["freq_mhz"] = f
        rel = r.get("도메인 간 관계", "")
        # 한글 조사가 바로 붙으므로("b와") \b 대신 ASCII 식별자 경계로 찾는다.
        others = [n for n in names if n != d["name"] and re.search(rf"(?<![A-Za-z0-9_]){re.escape(n)}(?![A-Za-z0-9_])", rel)]
        if others and ("비동기" in rel or "async" in rel.lower()):
            d["async_to"] = others
        m = re.search(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)\s*에서\s*(\d+)\s*분주", rel)
        if m:
            d["sync_to"] = {"domain": m.group(1), "div": int(m.group(2))}
        else:
            same = next((x for x in doms if x["clock"] == d["clock"]), None)
            if same is not None:   # 클럭 포트가 같은 앞 도메인: 같은 클럭
                d["sync_to"] = dict(same["sync_to"]) if same.get("sync_to") else {"domain": same["name"], "div": 1}
        pd = r.get("전력 도메인", "")
        if pd not in NONE_WORDS:
            d["power_domain"] = pd
            ent = {"name": pd}
            for k in ("isolation", "retention"):
                v = r.get(k, "")
                if v and v not in ("없음", "none", "-", "–"):
                    ent[k] = v
            pds[pd] = ent
        doms.append(d)
        prov[f'domains/{d["name"]}'] = line
    return doms, list(pds.values()), prov


def ports(spec: Spec, doms: list[dict]) -> tuple[list[dict], dict]:
    t = _table(spec, 3, ("포트 또는 번들", "방향", "타입", "도메인"))
    derived = {d["clock"] for d in doms} | {x["port"] for d in doms if d.get("reset") for x in [d["reset"], *d["reset"].get("extra", [])]}
    out, prov, bundle = [], {}, None
    for r, line in zip(t.dicts(), t.row_lines):
        name = r["포트 또는 번들"]
        m = re.match(r"^(\w+)\s*\((\w+)\)$", name)
        if m and not r["방향"]:
            bundle = {"bundle": m.group(1), "protocol": r.get("프로토콜 태그", ""), "role": m.group(2),
                      "domain": r["도메인"], "signals": []}
            out.append(bundle)
            prov[f"ports/{bundle['bundle']}"] = line
            continue
        if bundle is not None and not r["도메인"]:
            bundle["signals"].append({"name": name, "dir": r["방향"], "type": r["타입"]})
            prov[f"ports/{bundle['bundle']}/{name}"] = line
            continue
        bundle = None
        if name in derived:
            continue
        dom = r["도메인"]
        out.append({"name": name, "dir": r["방향"], "type": r["타입"], "domain": None if dom in ASYNC_WORDS else dom})
        prov[f"ports/{name}"] = line
    return out, prov


def params(spec: Spec) -> tuple[list[dict], dict]:
    if 4 not in spec.sections or not spec.sections[4].tables:
        return [], {}
    t = _table(spec, 4, ("이름", "타입", "기본값"))
    out, prov = [], {}
    for r, line in zip(t.dicts(), t.row_lines):
        p = {"name": r["이름"], "type": r["타입"], "default": r["기본값"]}
        if r.get("허용 범위"):
            p["range"] = r["허용 범위"]
        out.append(p)
        prov[f'params/{p["name"]}'] = line
    return out, prov


def block_top_l1(spec: Spec) -> tuple[dict, dict]:
    """(IR, provenance). provenance는 IR 노드 → SPEC 줄 번호."""
    doms, pds, p1 = domains(spec)
    prts, p2 = ports(spec, doms)
    prms, p3 = params(spec)
    ir = {"module": spec.module, "role": "block_top", "params": prms, "domains": doms}
    if pds:
        ir["power_domains"] = pds
    ir["ports"] = prts
    return ir, {**p1, **p2, **p3}


def main(argv=None) -> int:
    from pipeline.ir.model import Module
    from pipeline.validator.checks import validate

    ap = argparse.ArgumentParser(description="SPEC 표 → Block Top L1")
    ap.add_argument("spec")
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args(argv)
    ir, prov = block_top_l1(load(args.spec))
    issues = validate(Module(ir), {}, upto=3)
    Path(args.out).write_text(json.dumps(ir, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    Path(args.out).with_suffix(".provenance.json").write_text(json.dumps(prov, indent=1), encoding="utf-8")
    for i in issues:
        print(i)
    print(f"{args.out}: V1~V3 {'통과' if not issues else f'{len(issues)}건'}")
    return 0 if not issues else 1


if __name__ == "__main__":
    sys.exit(main())
