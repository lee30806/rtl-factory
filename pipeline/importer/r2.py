"""P11 R2: R1이 알 수 없는 것을 사용자에게 묻고, 답을 L1에 반영한다. 에이전트가 추정하지 않는다.

요청 항목 (`r2_request.json`의 `items`, 항목마다 `answer`가 비어 있으면 미답):
- `freq.<도메인>`: Block Top 도메인의 클럭 주파수(MHz). 분주 관계(sync_to)인 도메인은 비워 두면 계산한다.
- `rel.<a>.<b>`: 클럭 포트가 다른 Block Top 도메인 쌍의 관계. `async`, `<a>/N`(b = a의 N분주), `<b>/N`(a = b의 N분주).
  클럭 포트가 같은 도메인은 묻지 않고 앞 도메인의 1분주(sync_to div 1)로 둔다.
- `proto.<모듈>.<번들>`: 번들 후보의 프로토콜 태그. `valid_ready` 또는 `none`(번들을 풀어 개별 포트로).
- `review.<n>`: R1 검토 항목. `ok`(이대로 진행) 또는 `stop`(실행을 멈춤). 도메인을 못 정한 포트(`port_domain`)는
  도메인 이름이나 `async`로 답한다.
하위 모듈의 주파수와 도메인 관계는 Block Top의 답을 domain_map으로 따라 내려 채운다.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from pipeline.ir.model import Module
from pipeline.validator.checks import errors, validate


def make_request(r1: dict, irs: dict[str, dict]) -> dict:
    top = irs[r1["top"]]
    items = []
    doms = top["domains"]
    base = _clock_bases(doms)
    for d in doms:
        if d["name"] in base:
            continue
        items.append({"id": f"freq.{d['name']}", "kind": "freq", "domain": d["name"], "clock": d["clock"],
                      "question": f"{d['name']} 도메인(클럭 {d['clock']})의 주파수는 몇 MHz입니까? 다른 도메인의 N분주면 비워 두세요",
                      "answer": None})
    roots = [d for d in doms if d["name"] not in base]
    for i, a in enumerate(roots):
        for b in roots[i + 1:]:
            items.append({"id": f"rel.{a['name']}.{b['name']}", "kind": "relation", "a": a["name"], "b": b["name"],
                          "question": f"{a['name']}({a['clock']})와 {b['name']}({b['clock']})의 관계는? "
                                      f"async / {a['name']}/N ({b['name']}가 {a['name']}의 N분주) / {b['name']}/N",
                          "options": ["async", f"{a['name']}/N", f"{b['name']}/N"], "answer": None})
    for name in r1["order"]:
        for p in irs[name]["ports"]:
            if "bundle" in p:
                items.append({"id": f"proto.{name}.{p['bundle']}", "kind": "protocol", "module": name, "bundle": p["bundle"],
                              "signals": [s["name"] for s in p["signals"]], "role": p["role"],
                              "question": f"{name}의 {p['bundle']}({', '.join(s['name'] for s in p['signals'])})를 "
                                          f"valid_ready 번들({p['role']})로 볼까요? valid_ready / none",
                              "options": ["valid_ready", "none"], "answer": None})
    for n, r in enumerate(r1.get("reviews", []), 1):
        item = {"id": f"review.{n}", "kind": "review", "review_kind": r["kind"], "module": r.get("module"),
                "file": r.get("file"), "line": r.get("line"), "question": r["message"], "options": ["ok", "stop"], "answer": None}
        m = re.match(r"포트 (\S+)", r["message"]) if r["kind"] == "port_domain" else None
        if m and r.get("module") in irs:
            item["port"] = m.group(1)
            item["options"] = [d["name"] for d in irs[r["module"]]["domains"]] + ["async", "stop"]
        items.append(item)
    return {"top": r1["top"], "items": items}


def _clock_bases(doms: list[dict]) -> dict[str, str]:
    """클럭 포트가 같은 도메인 → 앞 도메인 (sync_to div 1)."""
    first: dict[str, str] = {}
    out = {}
    for d in doms:
        if d["clock"] in first:
            out[d["name"]] = first[d["clock"]]
        else:
            first[d["clock"]] = d["name"]
    return out


def unanswered(req: dict) -> list[dict]:
    return [i for i in req["items"] if i["answer"] in (None, "") and not (i["kind"] == "freq" and i.get("derived"))]


def set_answers(req: dict, answers: dict[str, str]) -> list[str]:
    """답을 넣는다 → 오류 목록 (없는 항목, 고를 수 없는 값)."""
    errs = []
    byid = {i["id"]: i for i in req["items"]}
    for k, v in answers.items():
        it = byid.get(k)
        if it is None:
            errs.append(f"없는 항목: {k}")
            continue
        v = str(v).strip()
        if it["kind"] == "freq":
            if v:
                try:
                    if float(v) <= 0:
                        raise ValueError
                except ValueError:
                    errs.append(f"{k}: 주파수는 양수(MHz)")
                    continue
        elif it["kind"] == "relation":
            ok = v == "async" or re.fullmatch(rf"({re.escape(it['a'])}|{re.escape(it['b'])})/[1-9]\d*", v)
            if not ok:
                errs.append(f"{k}: async, {it['a']}/N, {it['b']}/N 중 하나 (N은 1 이상 정수)")
                continue
        elif v not in it.get("options", [v]):
            errs.append(f"{k}: {it['options']} 중 하나")
            continue
        it["answer"] = v
    return errs


def apply(req: dict, r1: dict, irs: dict[str, dict]) -> tuple[dict[str, dict], list[str]]:
    """답을 반영한 IR 묶음과 오류. 모든 항목에 답이 있어야 한다 (`stop` 답이 있으면 오류)."""
    errs = []
    stops = [i["id"] for i in req["items"] if i["answer"] == "stop"]
    if stops:
        return irs, [f"사용자가 멈춤: {', '.join(stops)}"]
    missing = [i["id"] for i in req["items"] if i["answer"] in (None, "") and i["kind"] != "freq"]
    out = copy.deepcopy(irs)
    top = out[req["top"]]
    doms = {d["name"]: d for d in top["domains"]}
    base = _clock_bases(top["domains"])
    for d, b in base.items():
        doms[d]["sync_to"] = {"domain": b, "div": 1}
    freq = {i["domain"]: float(i["answer"]) for i in req["items"] if i["kind"] == "freq" and i["answer"] not in (None, "")}
    for i in req["items"]:
        if i["kind"] != "relation" or i["answer"] in (None, ""):
            continue
        a, b, v = i["a"], i["b"], i["answer"]
        if v == "async":
            doms[a].setdefault("async_to", []).append(b)
            doms[b].setdefault("async_to", []).append(a)
        else:
            src, n = v.split("/")
            dst = b if src == a else a
            if "sync_to" in doms[dst]:
                errs.append(f"{dst}가 이미 {doms[dst]['sync_to']['domain']}의 분주 도메인 (사슬 금지)")
                continue
            doms[dst]["sync_to"] = {"domain": src, "div": int(n)}
    # 같은 클럭 포트 도메인은 기준 도메인의 비동기 관계를 따른다
    for d, b in base.items():
        if doms[b].get("async_to"):
            doms[d]["async_to"] = list(doms[b]["async_to"])
            for o in doms[b]["async_to"]:
                doms[o].setdefault("async_to", []).append(d)
    for name, d in doms.items():
        s = d.get("sync_to")
        root = s["domain"] if s else name
        if root in freq:
            want = freq[root] / (s["div"] if s else 1)
            if name in freq and abs(freq[name] - want) > 1e-6 * want:
                errs.append(f"{name}의 주파수 {freq[name]}MHz가 {root}의 {s['div']}분주({want:g}MHz)와 다름")
            d["freq_mhz"] = round(want, 6)
        elif name in freq:
            d["freq_mhz"] = freq[name]
        else:
            missing.append(f"freq.{name}")
    for d in doms.values():
        if "async_to" in d:
            d["async_to"] = sorted(set(d["async_to"]))
    # 번들과 포트 도메인
    for i in req["items"]:
        if i["kind"] == "protocol" and i["answer"] == "none":
            _unbundle(out[i["module"]], i["bundle"])
        if i["kind"] == "review" and i.get("port") and i["answer"] not in (None, "", "ok"):
            _set_port_domain(out[i["module"]], i["port"], None if i["answer"] == "async" else i["answer"])
    # 하위 모듈로 내려 보냄
    _propagate(out, req["top"], r1["order"])
    for name in r1["order"]:
        bundle_connections(out[name], out)
    if missing:
        errs.append(f"답이 없는 항목: {', '.join(sorted(set(missing)))}")
    if not errs:
        lib = {n: Module(r) for n, r in out.items()}
        for n in r1["order"]:
            errs += [f"{n}: {x}" for x in errors(validate(lib[n], lib, upto=3))]
    return out, errs


def _unbundle(ir: dict, bundle: str) -> None:
    ports = []
    for p in ir["ports"]:
        if p.get("bundle") == bundle:
            ports += [{**s, "domain": p["domain"]} for s in p["signals"]]
        else:
            ports.append(p)
    ir["ports"] = ports


def _set_port_domain(ir: dict, port: str, dom: str | None) -> None:
    for p in ir["ports"]:
        if p.get("name") == port:
            p["domain"] = dom
        for s in p.get("signals", []):
            if s["name"] == port:
                p["domain"] = dom


def _propagate(irs: dict[str, dict], top: str, order: list[str]) -> None:
    """부모의 주파수와 도메인 관계를 domain_map으로 하위 모듈에 채운다 (위에서 아래로)."""
    done = {top}
    for name in reversed(order):
        if name not in done:
            continue
        parent = irs[name]
        pd = {d["name"]: d for d in parent["domains"]}
        for inst in parent.get("instances", []):
            child = irs.get(inst["module"])
            if child is None or inst["module"] in done:
                continue
            cd = {d["name"]: d for d in child["domains"]}
            dm = inst.get("domain_map", {})
            for c, p in dm.items():
                if c in cd and p in pd and "freq_mhz" in pd[p]:
                    cd[c]["freq_mhz"] = pd[p]["freq_mhz"]
            inv: dict[str, list[str]] = {}
            for c, p in dm.items():
                inv.setdefault(p, []).append(c)
            for c, p in dm.items():
                if c not in cd or p not in pd:
                    continue
                for c2, p2 in dm.items():
                    if c2 == c or c2 not in cd:
                        continue
                    if p2 in pd[p].get("async_to", []):
                        cd[c].setdefault("async_to", [])
                        if c2 not in cd[c]["async_to"]:
                            cd[c]["async_to"].append(c2)
                s = pd[p].get("sync_to")
                if s and s["domain"] in inv:
                    base = inv[s["domain"]][0]
                    if base != c:
                        cd[c]["sync_to"] = {"domain": base, "div": s["div"]}
                elif inv[p][0] != c and inv[p][0] in cd:
                    cd[c]["sync_to"] = {"domain": inv[p][0], "div": 1}
            for d in cd.values():
                if "async_to" in d:
                    d["async_to"] = sorted(d["async_to"])
            done.add(inst["module"])


def bundle_connections(ir: dict, irs: dict[str, dict]) -> None:
    """신호마다 이은 연결 중, 두 번들의 모든 신호가 같은 뒷부분(`_tdata` 등)끼리 이어진 것을 번들 연결 한 줄로 합친다."""
    conns = ir.get("connections")
    if not conns:
        return
    mods = {i["name"]: irs.get(i["module"], {}) for i in ir.get("instances", [])}
    mods["self"] = ir

    def owner(where: str, sig: str):
        for p in mods.get(where, {}).get("ports", []):
            if "bundle" in p and any(s["name"] == sig for s in p["signals"]):
                return p
        return None

    groups: dict[tuple, list[int]] = {}
    for k, c in enumerate(conns):
        ends = [tuple(c["from"].split(".")), tuple(c["to"].split("."))]
        info = []
        for w, sig in ends:
            b = owner(w, sig)
            if b is None:
                break
            info.append((w, b["bundle"], sig[len(b["bundle"]):]))
        if len(info) != 2 or info[0][2] != info[1][2]:
            continue
        key = tuple(sorted([info[0][:2], info[1][:2]]))
        groups.setdefault(key, []).append(k)
    drop, new = set(), []
    for ((wa, ba), (wb, bb)), ks in groups.items():
        pa = next(p for p in mods[wa]["ports"] if p.get("bundle") == ba)
        pb = next(p for p in mods[wb]["ports"] if p.get("bundle") == bb)
        if pa["protocol"] != pb["protocol"] or len(pa["signals"]) != len(pb["signals"]) or len(ks) != len(pa["signals"]):
            continue
        is_src = lambda w, p: (p["role"] == "sink") if w == "self" else (p["role"] == "source")
        a, b = ((wa, ba), (wb, bb)) if is_src(wa, pa) else ((wb, bb), (wa, ba))
        new.append({"id": re.sub(r"\W", "_", f"c_{a[0]}_{a[1]}_{b[0]}_{b[1]}"), "from": f"{a[0]}.{a[1]}", "to": f"{b[0]}.{b[1]}"})
        drop |= set(ks)
    if new:
        ir["connections"] = new + [c for k, c in enumerate(conns) if k not in drop]


def load_json(p: Path) -> dict:
    return json.loads(Path(p).read_text(encoding="utf-8"))
