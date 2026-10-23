"""Validator V6~V11: 래치와 루프, 리셋, 도메인, FSM, 계층, 동결.

V1~V5를 통과한 IR만 받는다고 가정한다. 동결(V11)은 모듈 하나가 아니라 설계 전체(라이브러리) 단위다.
"""
from __future__ import annotations

import hashlib
import json
import re

import pyslang

from pipeline.ir.model import Module, frozen_l1
from pipeline.validator.checks import Issue, _block_usage, _endpoint_ports, call_args, task_outputs


# ---------------------------------------------------------------- 공통: 조합 경로
def _ids(node) -> set[str]:
    out: set[str] = set()

    def visit(n):
        if str(getattr(n, "kind", "")).endswith(("IdentifierName", "IdentifierSelectName")):
            out.add(n.identifier.valueText)
        return pyslang.VisitAction.Advance

    node.visit(visit)
    return out


def assign_deps(body: str, tasks: dict[str, list[str]] | None = None) -> dict[str, set[str]]:
    """comb 조각의 대입 대상별 의존 신호: 오른쪽 식의 신호와 블록 안 조건식(if, case)의 신호.

    조건식은 블록 전체에서 모아 모든 대입에 붙인다(보수적). 블록 단위 reads→writes보다 정밀해서,
    `s_ready = 1'b1`처럼 입력에 의존하지 않는 대입에는 경로를 만들지 않는다.
    task 호출(tasks: 이름 → 인자 방향)은 출력 인자마다 입력 인자의 신호에 의존한다.
    """
    t = pyslang.SyntaxTree.fromText(f"module __d; always_comb begin\n{body}\nend endmodule")
    conds: set[str] = set()
    assigns: list[tuple[str, set[str]]] = []

    def visit(n):
        k = str(getattr(n, "kind", ""))
        if k.endswith("ConditionalStatement"):
            conds.update(_ids(n.predicate))
        elif k.endswith("CaseStatement"):
            conds.update(_ids(n.expr))
        elif k.endswith(("SyntaxKind.AssignmentExpression", "NonblockingAssignmentExpression")):
            left = n.left
            lhs = left.identifier.valueText if hasattr(left, "identifier") else next(iter(_ids(left)), None)
            if lhs:
                assigns.append((lhs, _ids(n.right)))
        elif k.endswith("InvocationExpression") and tasks and hasattr(n.left, "identifier") and n.left.identifier.valueText in tasks:
            dirs = tasks[n.left.identifier.valueText]
            outs = task_outputs(n, dirs)
            out_at = {o.sourceRange.start.offset for o in outs}
            ins = set()
            for e in call_args(n):
                if e.sourceRange.start.offset not in out_at:
                    ins |= _ids(e)
            for o in outs:
                lhs = o.identifier.valueText if hasattr(o, "identifier") else next(iter(_ids(o)), None)
                if lhs:
                    assigns.append((lhs, ins))
        return pyslang.VisitAction.Advance

    t.root.visit(visit)
    deps: dict[str, set[str]] = {}
    for lhs, rhs in assigns:
        deps.setdefault(lhs, set()).update(rhs | conds)
    return deps


def _comb_edges(m: Module, library: dict[str, Module]) -> dict[str, set[str]]:
    """조합 의존 그래프: 읽는 신호 → 쓰는 신호. 인스턴스는 자식의 입력→출력 조합 경로로 관통한다."""
    edges: dict[str, set[str]] = {}
    for b in m.blocks:
        if b["kind"] == "comb":
            for w, deps in assign_deps(b["body"], m.tasks()).items():
                for r in deps & set(b["reads"]):
                    edges.setdefault(r, set()).add(w)
    for inst in m.instances:
        child = library.get(inst["module"])
        if not child:
            continue
        paths = comb_paths(child, library)
        nets = _inst_nets(m, inst["name"], library)
        for i, o in paths:
            if i in nets and o in nets:
                edges.setdefault(nets[i], set()).add(nets[o])
    return edges


def _inst_nets(m: Module, inst: str, library: dict[str, Module]) -> dict[str, str]:
    """인스턴스 포트 → 부모 쪽 이름 (self 끝점만, 인스턴스끼리는 `<inst>_<port>`)."""
    from pipeline.emitter.sv import _instance_nets   # 방출기와 같은 넷 이름 (인스턴스 출력이 부모 포트로도 나가는 경우 포함)

    try:
        nets, _ = _instance_nets(m, library)
    except Exception:  # noqa: BLE001  끝점을 해석할 수 없으면 V2가 보고한다
        return {}
    return {port: net for (i, port), (net, _) in nets.items() if i == inst}


_PATH_CACHE: dict[tuple[str, int], set[tuple[str, str]]] = {}


def comb_paths(m: Module, library: dict[str, Module]) -> set[tuple[str, str]]:
    """모듈의 (입력 포트, 출력 포트) 조합 경로 요약."""
    key = (m.name, id(m))
    if key in _PATH_CACHE:
        return _PATH_CACHE[key]
    ins = [p.name for p in m.ports if p.dir == "input" and not p.derived]
    outs = {p.name for p in m.ports if p.dir == "output"}
    if m.role == "external":
        declared = m.raw.get("comb_paths")
        res = {(c["from"], c["to"]) for c in declared} if declared is not None else {(i, o) for i in ins for o in outs}
    else:
        edges = _comb_edges(m, library)
        res = set()
        for i in ins:
            seen, stack = set(), [i]
            while stack:
                n = stack.pop()
                for nxt in edges.get(n, ()):
                    if nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
            res |= {(i, o) for o in seen & outs}
    _PATH_CACHE[key] = res
    return res


_FEED_GENERIC = re.compile(r"feedthrough|조합 통과 경로|Registered Output|출력 레지스터|레지스터 출력", re.I)
_FEED_PAIR = re.compile(r"(\w+?)_?\*\s*포트에서\s*(\w+?)_?\*\s*포트로")


def feedthrough_rule(spec_text: str) -> list[tuple[str, str]] | None:
    """SPEC 9절(구현 제약)의 입력→출력 조합 경로 금지. None이면 제약 없음, [("", "")]이면 모든 입력→출력
    (출력 레지스터, feedthrough 차단 같은 일반 문장), 아니면 (입력 접두사, 출력 접두사) 목록
    (예: "s_axis_* 포트에서 m_axis_* 포트로의 순수 조합 통과 경로"만 있을 때)."""
    m = re.search(r"^##\s*9\..*?(?=^##\s*\d+\.|\Z)", spec_text, re.S | re.M)
    sec = m.group(0) if m else ""
    general = "\n".join(ln for ln in sec.splitlines() if not _FEED_PAIR.search(ln))   # 접두사를 정한 문장은 빼고 본다
    if _FEED_GENERIC.search(general):   # 입출력 전체 (P8 첫 실행의 fail에는 m_axis_tready → s_axis_tready도 있었음)
        return [("", "")]
    pairs = [(a.rstrip("_") + "_", b.rstrip("_") + "_") for a, b in _FEED_PAIR.findall(sec)]
    return pairs or None


def feedthrough_paths(m: Module, library: dict[str, Module], rule: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """규칙에 걸리는 (입력 포트, 출력 포트) 조합 경로 (P8: pi.lead가 RTL을 읽어 찾은 위반을 E1에서 찾음)."""
    return sorted((i, o) for i, o in comb_paths(m, library) if any(i.startswith(a) and o.startswith(b) for a, b in rule))


# ---------------------------------------------------------------- V6
def _top_level_items(body: str):
    t = pyslang.SyntaxTree.fromText(f"module __v6; always_comb begin\n{body}\nend endmodule")
    found = []

    def visit(n):
        if str(getattr(n, "kind", "")).endswith("AlwaysCombBlock"):
            found.extend(n.statement.items)
            return pyslang.VisitAction.Skip
        return pyslang.VisitAction.Advance

    t.root.visit(visit)
    return found


def _first_full_writes(body: str, tasks: dict[str, list[str]]) -> dict[str, bool]:
    """신호 → 처음 쓰는 최상위 문장이 무조건 전체 대입인지. task 호출의 출력 인자(이름 하나)도 전체 대입이다
    (task 본문이 출력 인자를 먼저 무조건 대입하는지는 task 쪽에서 본다)."""
    first: dict[str, bool] = {}
    for item in _top_level_items(body):
        writes, _, _, _ = _block_usage(item, tasks)
        plain: set[str] = set()
        if str(item.kind).endswith("ExpressionStatement"):
            e = item.expr
            if str(e.kind).endswith("AssignmentExpression") and str(e.left.kind).endswith("IdentifierName"):
                plain = {e.left.identifier.valueText}
            elif str(e.kind).endswith("InvocationExpression") and hasattr(e.left, "identifier") and e.left.identifier.valueText in tasks:
                plain = {o.identifier.valueText for o in task_outputs(e, tasks[e.left.identifier.valueText])
                         if str(o.kind).endswith("IdentifierName")}
        for w in writes:
            first.setdefault(w, w in plain)
    return first


def v6_latch_loop(m: Module, library: dict[str, Module]) -> list[Issue]:
    out: list[Issue] = []
    tasks = m.tasks()
    for b in m.blocks:
        if b["kind"] != "comb":
            continue
        first = _first_full_writes(b["body"], tasks)
        for w in b["writes"]:
            if not first.get(w, False):
                out.append(Issue("V6", f'blocks/{b["id"]}', f"래치 위험: {w}에 본문 최상위의 무조건 대입이 먼저 있어야 함"))
    for f in m.functions:   # task의 출력 인자도 같은 규칙 (comb 블록에서 부르므로)
        if f["kind"] == "task":
            first = _first_full_writes(f["body"], tasks)
            for a in f["args"]:
                if a["dir"] == "output" and not first.get(a["name"], False):
                    out.append(Issue("V6", f'functions/{f["name"]}', f'래치 위험: 출력 인자 {a["name"]}에 본문 최상위의 무조건 대입이 먼저 있어야 함'))
    # 조합 루프
    edges = _comb_edges(m, library)
    color: dict[str, int] = {}
    stack: list[str] = []

    def dfs(n: str) -> list[str] | None:
        color[n] = 1
        stack.append(n)
        for nxt in sorted(edges.get(n, ())):
            if color.get(nxt) == 1:
                return stack[stack.index(nxt):] + [nxt]
            if color.get(nxt) is None:
                cyc = dfs(nxt)
                if cyc:
                    return cyc
        stack.pop()
        color[n] = 2
        return None

    for n in sorted(edges):
        if color.get(n) is None:
            cyc = dfs(n)
            if cyc:
                out.append(Issue("V6", f"signals/{cyc[0]}", f"조합 루프: {' → '.join(cyc)}"))
                break
    return out


# ---------------------------------------------------------------- V7
def v7_reset(m: Module) -> list[Issue]:
    out: list[Issue] = []
    storage = m.storage()
    for b in m.blocks:
        if b["kind"] == "seq":
            has = {w: storage[w].reset is not None for w in b["writes"] if w in storage}
            if len(set(has.values())) > 1:
                out.append(Issue("V7", f'blocks/{b["id"]}', f"리셋 유무가 섞임: 있음 {sorted(k for k, v in has.items() if v)}, "
                                                            f"없음 {sorted(k for k, v in has.items() if not v)}"))
        if b["kind"] in ("seq", "fsm") and b.get("domain") in m.domains and not m.reset_of(b["domain"]):
            if b["kind"] == "fsm":
                out.append(Issue("V7", f'blocks/{b["id"]}', f'리셋 없는 도메인 {b["domain"]}에는 fsm을 둘 수 없음 (리셋 상태가 없음). seq로 씀'))
            elif any(storage[w].reset is not None for w in b["writes"] if w in storage):
                out.append(Issue("V7", f'blocks/{b["id"]}', f'리셋 없는 도메인 {b["domain"]}의 레지스터에 리셋값이 있음'))
    for b in m.blocks:   # 동기화 셀이 받는 쪽 리셋이 없으면 셀 리셋을 1로 묶음 (리셋 직후 값이 정해지지 않음)
        if b["kind"] in ("cdc_sync", "cdc_fifo") and b.get("dst_domain") in m.domains and not m.reset_of(b["dst_domain"]):
            out.append(Issue("V7", f'blocks/{b["id"]}', f'동기화 셀의 받는 쪽 도메인 {b["dst_domain"]}에 리셋이 없음', severity="warning"))
    return out


# ---------------------------------------------------------------- V8
ASYNC = "<async>"



def signal_domains(m: Module, library: dict[str, Module]) -> tuple[dict[str, set[str]], list[Issue]]:
    """신호별 도메인 집합. wire는 팬인에서 유도한다."""
    out: list[Issue] = []
    dom: dict[str, set[str]] = {}
    for p in m.ports:
        if p.dir == "input" and not p.derived:
            # 도메인이 없는 조합 모듈의 입력은 비동기가 아니라 부모가 정하는 도메인이다 (None)
            dom[p.name] = {p.domain or (ASYNC if m.domains else None)}
    for name, s in m.storage().items():
        if s.kind == "reg":
            dom[name] = {s.domain}
    for b in m.blocks:
        if b["kind"] == "cdc_sync":
            dom[b["dst"]] = {b["dst_domain"]}
        elif b["kind"] == "cdc_fifo":
            dom[b["src"]["ready"]] = {b["src_domain"]}
            dom[b["dst"]["valid"]] = dom[b["dst"]["data"]] = {b["dst_domain"]}
        elif b["kind"] == "tristate":   # 패드 입력은 tristate 도메인의 신호로 본다 (원본의 관행)
            dom[b["i"]] = {b["domain"]}
    comb_insts = []   # 도메인이 없는 조합 모듈 인스턴스: (입력 넷, 출력 넷). 출력은 입력 넷의 도메인을 물려받음
    for inst in m.instances:
        child = library.get(inst["module"])
        if not child:
            continue
        nets = _inst_nets(m, inst["name"], library)
        if not child.domains:
            dirs = {pt: getattr(child.port(pt), "dir", None) for pt in nets}
            ins = [n for pt, n in nets.items() if dirs[pt] == "input"]
            outs = [n for pt, n in nets.items() if dirs[pt] == "output"]
            comb_insts.append({"reads": ins, "writes": outs})
            continue
        for port, net in nets.items():
            cp = child.port(port)
            if cp and cp.dir == "output":
                dom[net] = {inst["domain_map"].get(cp.domain, cp.domain) if cp.domain else ASYNC}
    combs = [b for b in m.blocks if b["kind"] == "comb"] + comb_insts
    for _ in range(len(combs) + 1):  # 고정점까지 전파
        changed = False
        for b in combs:
            src: set[str] = set()
            for r in b["reads"]:
                src |= dom.get(r, set())
            for w in b["writes"]:
                if not src <= dom.get(w, set()):
                    dom[w] = dom.get(w, set()) | src
                    changed = True
        if not changed:
            break
    return dom, out


def _one_group(m: Module, ds: set) -> bool:
    """도메인 집합이 한 클럭 그룹 안에 있는지 (비동기 신호 제외)."""
    ds = [d for d in ds if d is not None]
    return ASYNC not in ds and all(m.same_clock(ds[0], d) for d in ds[1:])


def v8_domain(m: Module, library: dict[str, Module]) -> list[Issue]:
    """도메인 교차. 같은 클럭 그룹(기준 도메인과 그 분주 도메인) 안에서는 동기화 셀 없이 읽는다.
    리셋 포트는 comb만 읽고, 도메인을 만들지 않는다 (리셋이 걸린 동안의 출력을 정하는 데 씀)."""
    dom, out = signal_domains(m, library)
    storage = m.storage()
    for name, ds in sorted(dom.items()):
        s = storage.get(name)
        if s and s.kind != "reg" and len(ds - {None}) > 1 and not _one_group(m, ds):
            out.append(Issue("V8", f"signals/{name}", f"wire에 클럭 그룹이 다른 도메인이 섞임: {sorted(ds)}"))
    mems = m.memories()
    resets = m.all_reset_ports()
    for b in m.blocks:
        node = f'blocks/{b["id"]}'
        if b["kind"] in ("cdc_sync", "cdc_fifo") and b["src_domain"] and m.same_clock(b["src_domain"], b["dst_domain"]):
            out.append(Issue("V8", node, f'같은 클럭 그룹({b["src_domain"]}, {b["dst_domain"]}) 사이에는 동기화 셀이 필요 없음',
                             severity="warning"))
        if b["kind"] == "tristate":
            for k in ("o", "oe"):
                ds = dom.get(b.get(k), set())
                if k in b and (ASYNC in ds or not all(m.same_clock(b["domain"], d) for d in ds - {None})):
                    out.append(Issue("V8", node, f'{k}({b[k]}, {sorted(ds)})는 {b["domain"]} 도메인 신호여야 함'))
        if b["kind"] == "cdc_fifo":
            for side, k in (("src", "valid"), ("src", "data"), ("dst", "ready")):
                want = b[f"{side}_domain"]
                ds = dom.get(b[side][k], set())
                if ASYNC in ds or not all(m.same_clock(want, d) for d in ds - {None}):
                    out.append(Issue("V8", node, f'{side}.{k}({b[side][k]}, {sorted(ds)})는 {want} 도메인 신호여야 함'))
        reads = list(b.get("reads", []))
        for r in [r for r in reads if r in resets]:
            if b["kind"] != "comb":
                out.append(Issue("V8", node, f"리셋 포트는 comb 블록에서만 읽음 (seq와 fsm은 Emitter가 리셋 분기를 씌움): {r}"))
        reads = [r for r in reads if r not in resets]
        for r in [r for r in reads if r in mems]:
            if b["kind"] != "seq" or b["domain"] not in m.memory_domains(r, "read"):
                out.append(Issue("V8", node, f'메모리는 읽기 포트 도메인({sorted(m.memory_domains(r, "read"))})의 seq 블록에서만 읽음: {r}'))
        reads = [r for r in reads if r not in mems]
        if b["kind"] in ("seq", "fsm"):
            for r in reads:
                ds = dom.get(r, set())
                if ASYNC in ds:
                    out.append(Issue("V8", node, f"비동기 신호는 cdc_sync의 src로만 읽을 수 있음: {r}"))
                elif ds and not all(m.same_clock(d, b["domain"]) for d in ds - {None}):
                    out.append(Issue("V8", node, f'다른 클럭 그룹의 신호를 읽음: {r} ({sorted(ds)}), 블록 도메인 {b["domain"]}'))
                else:
                    out += _rdc(m, node, r, ds, b["domain"], storage)
        elif b["kind"] == "comb":
            for r in reads:
                if ASYNC in dom.get(r, set()):
                    out.append(Issue("V8", node, f"비동기 신호는 cdc_sync의 src로만 읽을 수 있음: {r}"))
    for a in m.assertions:   # SVA는 도메인 클럭으로 표본을 뜨므로 같은 클럭 그룹의 신호만 읽는다 (리셋 포트는 예외)
        node = f'assertions/{a["id"]}'
        for r in a["reads"]:
            ds = dom.get(r, set())
            if r in resets or r in mems:
                continue
            if ASYNC in ds or (ds and not all(m.same_clock(d, a["domain"]) for d in ds - {None})):
                out.append(Issue("V8", node, f'다른 클럭 그룹이나 비동기 신호를 읽음: {r} ({sorted(map(str, ds))}), 속성 도메인 {a["domain"]}'))
    return out


def _rdc(m: Module, node: str, r: str, ds: set, here: str, storage: dict) -> list[Issue]:
    """리셋 도메인 교차 (경고): 같은 클럭 그룹이라도 비동기 리셋이 다른 도메인의 레지스터를 읽으면, 그 리셋이
    걸리는 순간 값이 이 도메인의 클럭과 무관하게 바뀐다. 실제 블록에서 본 뒤 오류로 할지 정한다."""
    out = []
    s = storage.get(r)
    for d in sorted(ds - {None, here}):
        rd, rh = m.domains[d].get("reset"), m.domains[here].get("reset") or {}
        if s is not None and s.kind == "reg" and rd and rd["async"] and rd["port"] != rh.get("port"):
            out.append(Issue("V8", node, f'리셋 도메인 교차: {r}는 비동기 리셋 {rd["port"]}로 바뀌는 {d}의 레지스터 '
                                         f'({here}의 리셋은 {rh.get("port", "없음")})', severity="warning"))
    return out


# ---------------------------------------------------------------- V9
def v9_fsm(m: Module) -> list[Issue]:
    out: list[Issue] = []
    for b in m.blocks:
        if b["kind"] != "fsm":
            continue
        node = f'blocks/{b["id"]}'
        members = next(t["members"] for t in m.types if t["name"] == b["state_type"])
        nxt: dict[str, set[str]] = {}
        for t in b["transitions"]:
            nxt.setdefault(t["from"], set()).add(t["to"])
        seen, stack = {b["reset_state"]}, [b["reset_state"]]
        while stack:
            for s in nxt.get(stack.pop(), ()):
                if s not in seen:
                    seen.add(s)
                    stack.append(s)
        for s in members:
            if s not in seen:
                out.append(Issue("V9", node, f"리셋 상태에서 도달할 수 없는 상태: {s}"))
            if s in seen and s not in nxt:
                out.append(Issue("V9", node, f"나가는 전이가 없는 상태: {s}", severity="warning"))
    return out


# ---------------------------------------------------------------- V10
def v10_hierarchy(m: Module, library: dict[str, Module]) -> list[Issue]:
    out: list[Issue] = []
    dom, _ = signal_domains(m, library)
    driven: dict[tuple[str, str], int] = {}
    for c in m.connections:
        node = f'connections/{c["id"]}'
        ends = [_endpoint_ports(m, c[k], library) for k in ("from", "to")]
        owners = [c[k].split(".", 1)[0] for k in ("from", "to")]
        names = [c[k].split(".", 1)[1] for k in ("from", "to")]
        if not all(ends):
            continue
        # 번들 호환: 프로토콜이 같고, 부모-자식은 같은 역할, 인스턴스끼리는 반대 역할
        bundles = []
        for o, n in zip(owners, names):
            mod = m if o == "self" else library[next(i["module"] for i in m.instances if i["name"] == o)]
            bundles.append(mod.bundles.get(n))
        if all(bundles):
            ba, bb = bundles
            if ba["protocol"] != bb["protocol"]:
                out.append(Issue("V10", node, f'번들 프로토콜이 다름: {ba["protocol"]} vs {bb["protocol"]}'))
            same_role = "self" in owners
            if (ba["role"] == bb["role"]) != same_role:
                want = "같아야" if same_role else "반대여야"
                out.append(Issue("V10", node, f'번들 역할이 {want} 함: {ba["role"]} vs {bb["role"]}'))
        elif any(bundles):
            out.append(Issue("V10", node, "번들과 단독 포트를 연결함"))
        # 인스턴스 입력은 정확히 한 번 연결
        for o, e in zip(owners, ends):
            if o != "self":
                for d, n in e:
                    if d == "input":
                        driven[(o, n)] = driven.get((o, n), 0) + 1
        # 도메인 교차: 양쪽 클럭이 다르면 수신 측이 비동기 포트여야 함
        for (da, na), (db, nb) in zip(*ends):
            def port_dom(owner, d, n):
                if owner == "self":
                    return next(iter(dom.get(n, {None})), None)
                inst = next(i for i in m.instances if i["name"] == owner)
                child = library[inst["module"]]
                cp = child.port(n)
                if not child.domains:   # 조합 모듈: 연결한 넷의 도메인을 물려받으므로 여기서 보지 않음
                    return None
                return inst["domain_map"].get(cp.domain) if cp.domain else ASYNC
            pa, pb = port_dom(owners[0], da, na), port_dom(owners[1], db, nb)
            if ASYNC not in (pa, pb) and None not in (pa, pb) and not m.same_clock(pa, pb):
                out.append(Issue("V10", node, f"도메인이 다른 연결({na}: {pa}, {nb}: {pb})은 동기화 셀을 거치거나 수신 측이 비동기 포트여야 함"))
    for inst in m.instances:
        child = library.get(inst["module"])
        if not child:
            continue
        for p in child.ports:
            if p.dir == "input" and not p.derived:
                n = driven.get((inst["name"], p.name), 0)
                if n != 1:
                    out.append(Issue("V10", f'instances/{inst["name"]}', f"입력 {p.name}의 연결 수가 {n} (정확히 1이어야 함)"))
    return out


# ---------------------------------------------------------------- V11
def frozen_scope(library: dict[str, Module]) -> dict[str, str]:
    """동결 범위의 해시: 모든 모듈의 L1과 인스턴스 목록, top/block_top의 연결, 동기화 셀, 신호, 타입.

    top/block_top의 신호는 연결과 동기화 셀에만 쓰이므로, 신호의 타입이나 도메인이 바뀌면 동결된 연결이
    바뀐 것과 같다. 그래서 신호와 타입도 함께 동결한다.
    """
    out: dict[str, str] = {}
    for name, m in sorted(library.items()):
        parts = {
            "l1": frozen_l1(m.raw),
            "instances": m.raw.get("instances", []),
        }
        if m.role in ("top", "block_top"):
            parts["connections"] = m.raw.get("connections", [])
            parts["signals"] = m.raw.get("signals", [])
            parts["types"] = m.raw.get("types", [])
            # 해석에 실패한 IR에서도 같은 값이 나오도록 raw에서 직접 읽는다.
            parts["cdc_sync"] = [b for b in m.raw.get("blocks", []) if isinstance(b, dict) and b.get("kind") == "cdc_sync"]
            fifos = [b for b in m.raw.get("blocks", []) if isinstance(b, dict) and b.get("kind") == "cdc_fifo"]
            if fifos:   # 없으면 키를 만들지 않음 (이전 실행의 동결 기준선과 해시가 같도록)
                parts["cdc_fifo"] = fifos
            tris = [b for b in m.raw.get("blocks", []) if isinstance(b, dict) and b.get("kind") == "tristate"]
            if tris:
                parts["tristate"] = tris
        for k, v in parts.items():
            out[f"{name}/{k}"] = hashlib.sha256(json.dumps(v, sort_keys=True).encode()).hexdigest()
    return out


def v11_frozen(library: dict[str, Module], baseline: dict[str, str]) -> list[Issue]:
    now = frozen_scope(library)
    out = [Issue("V11", k, "동결 범위가 바뀜") for k in sorted(set(now) | set(baseline)) if now.get(k) != baseline.get(k)]
    return out
