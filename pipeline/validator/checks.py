"""Validator V1~V5와 전체 실행 진입점.

모든 오류는 IR 노드 경로(예: `blocks/k_cnt`, `signals/byte_cnt`)를 가진다.
V6~V11은 `structure.py`에 있다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pyslang
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from pipeline.ir.model import Module
from pipeline.validator import probe

SCHEMA = json.loads((Path(__file__).resolve().parents[2] / "schema" / "ir.schema.json").read_text(encoding="utf-8"))
_SCHEMA_V = Draft202012Validator(SCHEMA)

# IEEE 1800-2017 예약어 중 식별자로 쓰기 쉬운 것들
SV_KEYWORDS = set("""
alias always always_comb always_ff always_latch and assert assign assume automatic before begin bind bins binsof bit
break buf bufif0 bufif1 byte case casex casez cell chandle checker class clocking cmos config const constraint context
continue cover covergroup coverpoint cross deassign default defparam design disable dist do edge else end endcase
endchecker endclass endclocking endconfig endfunction endgenerate endgroup endinterface endmodule endpackage
endprimitive endprogram endproperty endspecify endsequence endtable endtask enum event eventually expect export extends
extern final first_match for force foreach forever fork forkjoin function generate genvar global highz0 highz1 if iff
ifnone ignore_bins illegal_bins implements implies import incdir include initial inout input inside instance int
integer interconnect interface intersect join join_any join_none large let liblist library local localparam logic
longint macromodule matches medium modport module nand negedge nettype new nexttime nmos nor noshowcancelled not
notif0 notif1 null or output package packed parameter pmos posedge primitive priority program property protected
pull0 pull1 pulldown pullup pulsestyle_ondetect pulsestyle_onevent pure rand randc randcase randsequence rcmos real
realtime ref reg reject_on release repeat restrict return rnmos rpmos rtran rtranif0 rtranif1 s_always s_eventually
s_nexttime s_until s_until_with scalared sequence shortint shortreal showcancelled signed small soft solve specify
specparam static string strong strong0 strong1 struct super supply0 supply1 sync_accept_on sync_reject_on table
tagged task this throughout time timeprecision timeunit tran tranif0 tranif1 tri tri0 tri1 triand trior trireg type
typedef union unique unique0 unsigned until until_with untyped use uwire var vectored virtual void wait wait_order
wand weak weak0 weak1 while wildcard wire with within wor xnor xor
""".split())


@dataclass(frozen=True)
class Issue:
    stage: str
    node: str
    message: str
    severity: str = "error"  # error | warning

    def __str__(self) -> str:
        tag = self.stage if self.severity == "error" else f"{self.stage} 경고"
        return f"[{tag}] {self.node}: {self.message}"


def _pick_branch_error(e):
    """oneOf 갈래 중 `kind`(또는 다른 const 식별 필드)가 맞는 갈래의 오류를 고른다."""
    branches: dict = {}
    for sub in e.context:
        branches.setdefault(sub.relative_schema_path[0], []).append(sub)
    matching = [errs for errs in branches.values()
                if not any(x.validator == "const" and len(x.relative_path) == 1 for x in errs)]
    if len(matching) == 1:
        return best_match(matching[0])
    return best_match(e.context)


# ---------------------------------------------------------------- V1
def v1_schema(m: Module) -> list[Issue]:
    out = []
    for e in sorted(_SCHEMA_V.iter_errors(m.raw), key=lambda e: list(map(str, e.absolute_path))):
        # oneOf 실패는 메시지에 노드 전체가 찍히므로, 가장 가까운 하위 오류로 바꿔 보고한다.
        if e.validator in ("oneOf", "anyOf") and e.context:
            e = _pick_branch_error(e)
        node = "/".join(map(str, e.absolute_path)) or "(root)"
        out.append(Issue("V1", node, e.message if len(e.message) < 160 else e.message[:157] + "..."))
    if out:
        return out  # 형식이 틀리면 이름 검사는 의미가 없다

    names: dict[str, str] = {}

    def claim(name: str, node: str) -> None:
        if name in SV_KEYWORDS:
            out.append(Issue("V1", node, f"SV 예약어는 이름으로 쓸 수 없음: {name}"))
        if name in names:
            out.append(Issue("V1", node, f"이름 중복: {name} (먼저 쓰인 곳: {names[name]})"))
        else:
            names[name] = node

    for p in m.params:
        claim(p["name"], f'params/{p["name"]}')
    for d in m.domains.values():
        claim(d["name"], f'domains/{d["name"]}')
    for port in m.ports:
        if not port.derived:
            claim(port.name, f"ports/{port.name}")
    for name in {p.name for p in m.ports if p.derived}:
        if name not in names:
            claim(name, f"domains/{name}")
    for d in m.domains.values():
        if (d.get("reset") or {}).get("synchronize"):
            claim(m.synced_reset(d["name"]), f'domains/{d["name"]}/reset')
    for b in m.bundles:
        claim(b, f"ports/{b}")
    for t in m.types:
        claim(t["name"], f'types/{t["name"]}')
        for mem in t.get("members", []):
            claim(mem, f'types/{t["name"]}/{mem}')
    for s in m.signals:
        claim(s.name, f"signals/{s.name}")
    for s in m.state_regs():
        claim(s.name, f"blocks/{s.name}")
    for i in m.instances:
        claim(i["name"], f'instances/{i["name"]}')
    for f in m.functions:
        claim(f["name"], f'functions/{f["name"]}')
    for d in sorted({a["domain"] for a in m.assertions if a["domain"] in m.domains and m.domains[a["domain"]].get("reset")}):
        claim(sva_reset(d), f"domains/{d}")   # Emitter가 SVA disable iff에 쓰는 리셋 wire
    for f in m.functions:   # 인자와 지역 변수: 함수 안에서 겹치지 않고, 모듈 이름을 가리지 않는다
        local: set[str] = set()
        for v in [*f["args"], *f.get("vars", [])]:
            node = f'functions/{f["name"]}/{v["name"]}'
            if v["name"] in SV_KEYWORDS:
                out.append(Issue("V1", node, f'SV 예약어는 이름으로 쓸 수 없음: {v["name"]}'))
            elif v["name"] in local:
                out.append(Issue("V1", node, f'인자·지역 변수 이름 중복: {v["name"]}'))
            elif v["name"] in names:
                out.append(Issue("V1", node, f'인자·지역 변수 이름이 모듈의 이름과 겹침: {v["name"]} ({names[v["name"]]})'))
            local.add(v["name"])
    ids: set[str] = set()
    for b in m.blocks:
        for nid in [b["id"]] + [t["id"] for t in b.get("transitions", [])]:
            if nid in ids:
                out.append(Issue("V1", f"blocks/{b['id']}", f"id 중복: {nid}"))
            ids.add(nid)
    for c in m.connections:
        if c["id"] in ids:
            out.append(Issue("V1", f'connections/{c["id"]}', f'id 중복: {c["id"]}'))
        ids.add(c["id"])
    for a in m.assertions:
        if a["id"] in ids:
            out.append(Issue("V1", f'assertions/{a["id"]}', f'id 중복: {a["id"]}'))
        ids.add(a["id"])
    return out


def sva_reset(dom: str) -> str:
    """SVA `disable iff`에 쓰는 리셋 wire (리셋이 걸리면 1). 비동기 리셋 포트를 속성에 바로 쓰면 Verilator가
    SYNCASYNCNET 경고를 내므로 wire로 옮겨 쓴다."""
    return f"{dom}_sva_rst"


# ---------------------------------------------------------------- V2
def _endpoint_ports(m: Module, ep: str, library: dict[str, Module]) -> list[tuple[str, str]] | None:
    """끝점을 (포트 방향, 신호 이름) 목록으로 펼친다. 해석할 수 없으면 None."""
    owner, name = ep.split(".", 1)
    if owner == "self":
        if name in m.bundles:
            return [(s["dir"], s["name"]) for s in m.bundles[name]["signals"]]
        port = m.port(name)
        if port:
            return [(port.dir, port.name)]
        if name in {s.name for s in m.signals}:
            return [("signal", name)]
        return None
    inst = next((i for i in m.instances if i["name"] == owner), None)
    child = library.get(inst["module"]) if inst else None
    if not child:
        return None
    if name in child.bundles:
        return [(s["dir"], s["name"]) for s in child.bundles[name]["signals"]]
    port = child.port(name)
    return [(port.dir, port.name)] if port and not port.derived else None


def v2_refs(m: Module, library: dict[str, Module]) -> list[Issue]:
    out: list[Issue] = []
    doms = set(m.domains)

    def dom(d, node):
        if d is not None and d not in doms:
            out.append(Issue("V2", node, f"정의되지 않은 도메인: {d}"))

    out += _domain_relations(m)
    for b in m.blocks:   # 표준 셀(stubs/)은 상승 에지로만 동작한다
        if b["kind"] in ("cdc_sync", "cdc_fifo"):
            negs = [b[k] for k in ("src_domain", "dst_domain") if m.domains.get(b.get(k) or "", {}).get("edge") == "neg"]
            if negs:
                out.append(Issue("V2", f'blocks/{b["id"]}', f"동기화 셀은 상승 에지 도메인 사이에만 둠 (하강 에지 도메인: {negs})"))
    tri = {}
    for b in m.blocks:
        if b["kind"] == "tristate":
            tri.setdefault(b["pad"], []).append(b["id"])
    for port in m.ports:
        if port.dir == "inout":
            if m.role not in ("top", "block_top"):
                out.append(Issue("V2", f"ports/{port.bundle or port.name}", f"inout 포트는 top과 block_top에만 둠 (하위 모듈은 o, oe, i로 나눔): {port.name}"))
            elif port.bundle:
                out.append(Issue("V2", f"ports/{port.bundle}", f"inout 포트는 번들에 넣지 않음: {port.name}"))
            elif len(tri.get(port.name, [])) != 1:
                out.append(Issue("V2", f"ports/{port.name}", f"inout 포트는 tristate 블록 하나가 맡아야 함: {port.name} ({tri.get(port.name, [])})"))
    for b in m.blocks:
        if b["kind"] == "tristate" and m.role not in ("top", "block_top"):
            out.append(Issue("V2", f'blocks/{b["id"]}', "tristate 블록은 top과 block_top에만 둠"))
    for port in m.ports:
        if not port.derived:
            dom(port.domain, f"ports/{port.bundle or port.name}")
    for s in m.signals:
        dom(s.domain, f"signals/{s.name}")
    readable, storage = m.readable(), m.storage()
    for b in m.blocks:
        node = f'blocks/{b["id"]}'
        if b["kind"] in ("seq", "fsm"):
            dom(b.get("domain"), node)
        if b["kind"] == "memory":
            for mp in b["ports"]:
                dom(mp["domain"], f'{node}/ports/{mp["name"]}')
        if b["kind"] == "cdc_sync":
            dom(b["dst_domain"], node)
            dom(b["src_domain"], node)
            for k, pool in (("src", readable), ("dst", storage)):
                if b[k] not in pool:
                    out.append(Issue("V2", node, f"{k}가 가리키는 신호가 없음: {b[k]}"))
            types = {k: (storage[b[k]].type if b[k] in storage else next((p.type for p in m.ports if p.name == b[k]), None))
                     for k in ("src", "dst")}
            norm = {k: "".join(v.split()) if v else None for k, v in types.items()}
            if b["style"] in ("2ff", "pulse") and any(v not in (None, "logic", "bit") for v in norm.values()):
                out.append(Issue("V2", node, f'{b["style"]}는 1비트만 동기화함. 여러 비트는 gray 코드로 만든 뒤 style "gray"를 '
                                             f"쓰거나 cdc_fifo를 씀: {types}"))
            if b["style"] == "pulse" and b["src_domain"] is None:
                out.append(Issue("V2", node, "pulse는 보내는 쪽 클럭으로 토글을 만들므로 src_domain이 있어야 함"))
            if b["style"] == "gray" and None not in norm.values() and norm["src"] != norm["dst"]:
                out.append(Issue("V2", node, f"gray 동기화의 src와 dst 타입이 다름: {types}"))
        if b["kind"] == "tristate":
            dom(b["domain"], node)
            pad = m.port(b["pad"])
            if pad is None or pad.dir != "inout":
                out.append(Issue("V2", node, f'pad는 inout 포트여야 함: {b["pad"]}'))
            for k in ("o", "oe"):
                if k in b and b[k] not in readable:
                    out.append(Issue("V2", node, f"{k}가 가리키는 신호가 없음: {b[k]}"))
            if b["i"] not in storage:
                out.append(Issue("V2", node, f'i가 가리키는 신호가 없음 (내부 wire 신호 또는 출력 포트): {b["i"]}'))

            def ttyp(n):
                t = storage[n].type if n in storage else next((p.type for p in m.ports if p.name == n), None)
                return "".join(t.split()) if t else None
            want = ttyp(b["pad"])
            for k in ("o", "i"):
                if k in b and None not in (want, ttyp(b[k])) and ttyp(b[k]) != want:
                    out.append(Issue("V2", node, f"{k}의 타입이 pad와 다름: {ttyp(b[k])} vs {want}"))
            if None not in (want, ttyp(b["oe"])) and ttyp(b["oe"]) not in ("logic", "bit", want):
                out.append(Issue("V2", node, f'oe는 1비트이거나 pad와 같은 폭이어야 함: {ttyp(b["oe"])}'))
        if b["kind"] == "cdc_fifo":
            dom(b["src_domain"], node)
            dom(b["dst_domain"], node)
            for side, k, pool in (("src", "valid", readable), ("src", "data", readable), ("dst", "ready", readable),
                                  ("src", "ready", storage), ("dst", "valid", storage), ("dst", "data", storage)):
                if b[side][k] not in pool:
                    out.append(Issue("V2", node, f'{side}.{k}가 가리키는 신호가 없음: {b[side][k]}'))

            def typ(n):
                t = storage[n].type if n in storage else next((p.type for p in m.ports if p.name == n), None)
                return "".join(t.split()) if t else None
            if None not in (typ(b["src"]["data"]), typ(b["dst"]["data"])) and typ(b["src"]["data"]) != typ(b["dst"]["data"]):
                out.append(Issue("V2", node, f'src.data와 dst.data의 타입이 다름: {typ(b["src"]["data"])} vs {typ(b["dst"]["data"])}'))
            for side, k in (("src", "valid"), ("src", "ready"), ("dst", "valid"), ("dst", "ready")):
                if typ(b[side][k]) not in (None, "logic", "bit"):
                    out.append(Issue("V2", node, f"{side}.{k}는 1비트여야 함: {b[side][k]}"))
        for r in b.get("reads", []):
            if r not in readable:
                out.append(Issue("V2", node, f"reads의 신호가 없음: {r}"))
        for w in b.get("writes", []):
            if w not in storage:
                out.append(Issue("V2", node, f"writes의 신호가 없음 (출력 포트 또는 내부 신호여야 함): {w}"))
        if b["kind"] == "fsm":
            if b["state_type"] not in m.type_names():
                out.append(Issue("V2", node, f'정의되지 않은 타입: {b["state_type"]}'))
            members = next((set(t["members"]) for t in m.types if t["name"] == b["state_type"] and t["kind"] == "enum"), set())
            for st in [b["reset_state"]] + [x for t in b["transitions"] for x in (t["from"], t["to"])]:
                if members and st not in members:
                    out.append(Issue("V2", node, f"상태 타입에 없는 상태: {st}"))
    for a in m.assertions:
        node = f'assertions/{a["id"]}'
        dom(a["domain"], node)
        for r in a["reads"]:
            if r not in readable:
                out.append(Issue("V2", node, f"reads의 신호가 없음: {r}"))
    for inst in m.instances:
        node = f'instances/{inst["name"]}'
        child = library.get(inst["module"])
        if not child:
            out.append(Issue("V2", node, f'라이브러리에 없는 모듈: {inst["module"]}'))
            continue
        for k in inst.get("params", {}):
            if k not in {p["name"] for p in child.params}:
                out.append(Issue("V2", node, f'{inst["module"]}에 없는 파라미터: {k}'))
            elif any(p["name"] == k and p.get("local") for p in child.params):
                out.append(Issue("V2", node, f'{inst["module"]}의 {k}는 localparam이라 바꿀 수 없음'))
        for cd, pd in inst["domain_map"].items():
            if cd not in child.domains:
                out.append(Issue("V2", node, f'{inst["module"]}에 없는 도메인: {cd}'))
            dom(pd, node)
        for cd in child.domains:
            if cd not in inst["domain_map"]:
                out.append(Issue("V2", node, f"domain_map에 자식 도메인이 빠짐: {cd}"))
        out += _domain_map_relations(m, child, inst, node)
    for c in m.connections:
        node = f'connections/{c["id"]}'
        ends = [_endpoint_ports(m, c[k], library) for k in ("from", "to")]
        for k, e in zip(("from", "to"), ends):
            if e is None:
                out.append(Issue("V2", node, f"끝점을 해석할 수 없음: {c[k]}"))
        if all(ends) and len(ends[0]) != len(ends[1]):
            out.append(Issue("V2", node, f"양쪽 신호 개수가 다름: {len(ends[0])} vs {len(ends[1])}"))
    return out


def _domain_relations(m: Module) -> list[Issue]:
    """도메인 사이 관계: 분주(sync_to), 같은 클럭 포트, 비동기(async_to), 리셋 동기화."""
    out: list[Issue] = []
    for d in m.domains.values():
        node = f'domains/{d["name"]}'
        s = d.get("sync_to")
        if s:
            ref = m.domains.get(s["domain"])
            if ref is None:
                out.append(Issue("V2", node, f'sync_to가 가리키는 도메인이 없음: {s["domain"]}'))
            elif s["domain"] == d["name"]:
                out.append(Issue("V2", node, "sync_to가 자기 자신을 가리킴"))
            elif ref.get("sync_to"):
                out.append(Issue("V2", node, f'분주의 사슬은 쓰지 않음: {s["domain"]}도 sync_to가 있음. 기준 도메인에서 바로 나눈 분주비를 씀'))
            elif d.get("freq_mhz") and ref.get("freq_mhz") and abs(d["freq_mhz"] * s["div"] - ref["freq_mhz"]) > 1e-6 * ref["freq_mhz"]:
                out.append(Issue("V2", node, f'freq_mhz({d["freq_mhz"]})가 {s["domain"]}({ref["freq_mhz"]}) / {s["div"]}와 다름'))
            if ref is not None and s["div"] > 1 and "neg" in (d["edge"], ref["edge"]):
                out.append(Issue("V2", node, "분주 관계는 상승 에지 도메인끼리만 씀 (분주 클럭은 상승 에지 정렬). "
                                             "하강 에지 도메인은 같은 클럭(div 1)으로만 묶음"))
        for o in d.get("async_to", []):
            if o in m.domains and m.same_clock(d["name"], o):
                out.append(Issue("V2", node, f"같은 클럭 그룹의 도메인을 async_to에 넣음: {o}"))
        r = d.get("reset")
        if r is None:
            continue   # 리셋 없는 도메인
        if r.get("synchronize"):
            if d["edge"] == "neg":
                out.append(Issue("V2", f"{node}/reset", "reset.synchronize는 상승 에지 도메인에만 씀 (표준 셀 rst_sync는 상승 에지)"))
            if m.role not in ("top", "block_top"):
                out.append(Issue("V2", f"{node}/reset", "reset.synchronize는 top과 block_top에만 둠 (하위 모듈은 동기화한 리셋을 받음)"))
            if not r["async"]:
                out.append(Issue("V2", f"{node}/reset", "reset.synchronize는 비동기 리셋(async: true)에만 씀"))
        if r["port"] in {x["port"] for x in r.get("extra", [])}:
            out.append(Issue("V2", f"{node}/reset", f'추가 리셋에 주 리셋 포트를 다시 씀: {r["port"]}'))
        if r["async"] != m.reset_async():
            out.append(Issue("V2", f"{node}/reset", "한 설계의 리셋은 모두 비동기이거나 모두 동기여야 함: "
                                                    f"{'비동기' if r['async'] else '동기'} 리셋 도메인과 "
                                                    f"{'비동기' if m.reset_async() else '동기'} 리셋 도메인이 섞임"))
    # 클럭 포트가 같은 도메인은 같은 클럭이어야 한다 (같은 그룹, 같은 분주비)
    by_clock: dict[str, list[str]] = {}
    for d in m.domains.values():
        by_clock.setdefault(d["clock"], []).append(d["name"])
    for clk, names in by_clock.items():
        roots = {m.clock_root(n) for n in names}
        if len(roots) > 1:
            out.append(Issue("V2", f"domains/{names[1]}", f"클럭 포트 {clk}를 함께 쓰는 도메인 {names}은 같은 클럭이어야 함. "
                                                          "기준 도메인에 sync_to (div 1)로 묶음"))
    return out


def _domain_map_relations(m: Module, child: Module, inst: dict, node: str) -> list[Issue]:
    """인스턴스의 domain_map이 자식의 클럭 관계와 리셋 구성을 지키는지."""
    out: list[Issue] = []
    dm = inst["domain_map"]
    nets: dict[str, str] = {}   # 자식 클럭·리셋 포트 → 부모 넷 (도메인끼리 포트를 함께 쓰면 같은 넷이어야 함)
    for cd, d in child.domains.items():
        pd = dm.get(cd)
        if pd not in m.domains:
            continue
        if d["edge"] != m.domains[pd]["edge"]:   # 자식은 자기 에지로 클럭 포트를 받으므로 부모 도메인과 에지가 같아야 함
            out.append(Issue("V2", node, f'자식 도메인 {cd}({d["edge"]} 에지)를 에지가 다른 도메인 {pd}({m.domains[pd]["edge"]} 에지)에 연결함'))
        pr, cr = m.domains[pd].get("reset"), d.get("reset")
        if cr and not pr:
            out.append(Issue("V2", node, f"리셋이 있는 자식 도메인 {cd}를 리셋 없는 도메인 {pd}에 연결함"))
            continue
        pr, cr = pr or {}, cr or {}
        if cr and cr["async"] != pr["async"]:
            out.append(Issue("V2", node, f"자식 도메인 {cd}({'비동기' if cr['async'] else '동기'} 리셋)를 "
                                         f"{'비동기' if pr['async'] else '동기'} 리셋 도메인 {pd}에 연결함 "
                                         "(한 설계의 리셋은 한 방식)"))
        pairs = [(d["clock"], m.domains[pd]["clock"])]
        if cr:
            pairs += [(cr["port"], m.synced_reset(pd) if pr.get("synchronize") else pr["port"]),
                      *zip((x["port"] for x in cr.get("extra", [])), (x["port"] for x in pr.get("extra", [])))]
        for cport, pnet in pairs:
            if nets.setdefault(cport, pnet) != pnet:
                out.append(Issue("V2", node, f"자식 포트 {cport}를 함께 쓰는 도메인들에 서로 다른 넷({nets[cport]}, {pnet})을 연결함. "
                                             "자식 도메인마다 포트를 따로 두거나 같은 클럭·리셋의 도메인에 연결함"))
        s = d.get("sync_to")
        if s and dm.get(s["domain"]) in m.domains:
            (ra, da), (rb, db) = m.clock_root(pd), m.clock_root(dm[s["domain"]])
            if ra != rb or da != db * s["div"]:
                out.append(Issue("V2", node, f'자식 도메인 {cd}는 {s["domain"]}에서 {s["div"]}분주인데 연결한 {pd}와 '
                                             f'{dm[s["domain"]]}의 관계가 다름'))
        ce = [x["active"] for x in cr.get("extra", [])]
        pe = [x["active"] for x in pr.get("extra", [])]
        if ce != pe[:len(ce)]:   # 자식은 부모 추가 리셋의 앞쪽 일부만 받을 수 있음 (순서대로 연결)
            out.append(Issue("V2", node, f"자식 도메인 {cd}의 추가 리셋({ce})이 연결한 {pd}의 추가 리셋({pe})과 맞지 않음 "
                                         "(부모 추가 리셋의 앞에서부터 같은 극성이어야 함)"))
    return out


# ---------------------------------------------------------------- V3, V4
_FORBIDDEN = {"DelayControl": "지연(#)", "ProceduralForceStatement": "force", "ProceduralReleaseStatement": "release",
              "ProceduralAssignStatement": "procedural assign", "ProceduralDeassignStatement": "deassign"}
# function/task 본문에서만 따로 보는 구문 (블록의 always_ff 자체가 타이밍 제어 문장이라 공통 목록에 넣지 않음)
_FN_FORBIDDEN = {"TimingControlStatement": "타이밍 제어(@, #)", "WaitStatement": "wait", "WaitForkStatement": "wait fork"}


# 합성 RTL 조각에 쓸 수 있는 시스템 함수 (상수 계산과 형 변환). 그 밖의 `$...`(PLI, $display, $random, $time 등)는 합성되지 않는다.
# SVA(`assertions`)는 Emitter가 `ifndef SYNTHESIS`로 감싸므로 보지 않는다 (2026-10-22 사용자 규칙: 합성 RTL에 task, PLI 금지).
SYNTH_SYSFUNCS = {"$clog2", "$bits", "$signed", "$unsigned", "$countones", "$onehot", "$onehot0",
                  "$size", "$high", "$low", "$left", "$right"}


def system_calls(node) -> list[str]:
    """구문 트리 안의 시스템 함수와 시스템 task 이름 (`$display` 등)."""
    found = []

    def visit(n):
        if str(getattr(n, "kind", "")).endswith("SystemName"):
            found.append(n.systemIdentifier.valueText)
        return pyslang.VisitAction.Advance

    node.visit(visit)
    return found


def _unsynth_calls(node) -> list[str]:
    return sorted({c for c in system_calls(node) if c not in SYNTH_SYSFUNCS})


def _kinds_in(node, kinds: dict[str, str]) -> list[str]:
    found = []

    def visit(n):
        k = str(getattr(n, "kind", "")).removeprefix("SyntaxKind.")
        if k in kinds:
            found.append(kinds[k])
        return pyslang.VisitAction.Advance

    node.visit(visit)
    return found


def invocations(node) -> list[tuple[str, object]]:
    """구문 트리 안의 호출 (이름, InvocationExpression). 시스템 함수($...)는 뺀다."""
    out = []

    def visit(n):
        if str(getattr(n, "kind", "")).endswith("InvocationExpression"):
            left = n.left
            if hasattr(left, "identifier"):
                out.append((left.identifier.valueText, n))
        return pyslang.VisitAction.Advance

    node.visit(visit)
    return out


def call_args(inv) -> list:
    """호출의 위치 인자 식 (구문 노드). 인자는 속성 식(SimplePropertyExpr, SimpleSequenceExpr)으로 감싸여 있어 벗긴다."""
    out = []
    for a in (inv.arguments.parameters if inv.arguments is not None else []):
        if not str(getattr(a, "kind", "")).endswith("OrderedArgument"):
            continue
        e = a.expr
        while str(e.kind).endswith(("SimplePropertyExpr", "SimpleSequenceExpr")):
            e = e.expr
        out.append(e)
    return out


def task_outputs(inv, dirs: list[str]) -> list:
    """task 호출의 출력 인자 식 (구문 노드)."""
    return [e for e, d in zip(call_args(inv), dirs) if d == "output"]


def _block_usage(node, tasks: dict[str, list[str]] | None = None) -> tuple[set[str], set[str], set[str], list[str]]:
    """(쓰는 이름, 읽는 이름, 대입 종류, 금지 구문) — 구문 트리에서 직접 뽑는다.
    tasks(이름 → 인자 방향)를 주면 task 호출의 출력 인자도 쓰기로 본다."""
    # 대입 왼쪽의 범위. 그 안의 이름은 쓰기다. 단 왼쪽 안의 인덱스(`[...]`)에 나오는 이름은 읽기다.
    # 왼쪽이 연결(`{a, b} <= ...`)이어도 각 원소가 쓰기가 된다.
    lhs: list[tuple[int, int]] = []
    selects: list[tuple[int, int]] = []
    writes, reads, kinds, forbidden = set(), set(), set(), []

    def within(off: int, ranges) -> bool:
        return any(a <= off < b for a, b in ranges)

    def visit(n):
        k = str(getattr(n, "kind", "")).removeprefix("SyntaxKind.")
        if k in ("AssignmentExpression", "NonblockingAssignmentExpression"):
            kinds.add("nonblocking" if k.startswith("Nonblocking") else "blocking")
            r = n.left.sourceRange
            lhs.append((r.start.offset, r.end.offset))
        elif k == "InvocationExpression" and tasks and hasattr(n.left, "identifier") and n.left.identifier.valueText in tasks:
            kinds.add("blocking")   # task의 출력 인자는 task가 끝날 때 blocking으로 복사된다
            for e in task_outputs(n, tasks[n.left.identifier.valueText]):
                lhs.append((e.sourceRange.start.offset, e.sourceRange.end.offset))
        elif k == "ElementSelect" and within(n.sourceRange.start.offset, lhs):
            selects.append((n.sourceRange.start.offset, n.sourceRange.end.offset))
        elif k in ("IdentifierName", "IdentifierSelectName"):
            name = n.identifier.valueText
            off = n.sourceRange.start.offset
            (writes if within(off, lhs) and not within(off, selects) else reads).add(name)
        elif k in _FORBIDDEN:
            forbidden.append(_FORBIDDEN[k])
        return pyslang.VisitAction.Advance

    node.visit(visit)
    return writes, reads, kinds, forbidden


def v3_v4(m: Module, library: dict[str, Module]) -> list[Issue]:
    pr = probe.build(m, library)
    tree, diags = probe.compile_probe(pr)
    out = [Issue(d.stage, d.node, d.message) for d in diags]
    blocks = {f'blocks/{b["id"]}': b for b in m.blocks}
    funcs = {f'functions/{f["name"]}': (i, f) for i, f in enumerate(m.functions)}
    order = {f["name"]: i for i, f in enumerate(m.functions)}
    tasks = m.tasks()
    for name in tasks:
        out.append(Issue("V4", f"functions/{name}", f"합성 RTL에 task를 쓰지 않음 (사내 규칙): {name}. "
                                                    "comb 블록에 풀어 쓰거나 값을 돌려주는 function으로 바꾼다"))
    sm = tree.sourceManager
    signal_names = m.readable()
    module_names = signal_names | set(m.storage())

    def visit(n):
        k = str(getattr(n, "kind", "")).removeprefix("SyntaxKind.")
        if k in ("FunctionDeclaration", "TaskDeclaration"):
            _, node = pr.nodes[sm.getLineNumber(n.sourceRange.start) - 1]
            if node in funcs:
                out.extend(_function_checks(m, node, *funcs[node], n, order, tasks, module_names))
            return pyslang.VisitAction.Skip
        if k not in ("AlwaysCombBlock", "AlwaysFFBlock"):
            return pyslang.VisitAction.Advance
        line = sm.getLineNumber(n.sourceRange.start)
        _, node = pr.nodes[line - 1]
        b = blocks.get(node)
        if b is None:
            return pyslang.VisitAction.Skip
        writes, reads, kinds, forbidden = _block_usage(n.statement, tasks)
        for f in forbidden:
            out.append(Issue("V4", node, f"금지 구문: {f}"))
        if bad := _unsynth_calls(n.statement):
            out.append(Issue("V4", node, f"합성되지 않는 시스템 함수/PLI: {bad} (상수 함수 {sorted(SYNTH_SYSFUNCS)}만)"))
        called = {name for name, _ in invocations(n.statement)}
        if b["kind"] != "comb" and called & set(tasks):
            out.append(Issue("V4", node, f"task는 comb 블록에서만 부름 (seq는 function을 씀): {sorted(called & set(tasks))}"))
        elif b["kind"] == "seq" and "blocking" in kinds:
            out.append(Issue("V4", node, "seq 블록에 blocking 대입(=)이 있음. non-blocking(<=)만 허용"))
        if b["kind"] == "comb" and "nonblocking" in kinds:
            out.append(Issue("V4", node, "comb 블록에 non-blocking 대입(<=)이 있음. blocking(=)만 허용"))
        declared_w, declared_r = set(b["writes"]), set(b["reads"])
        if writes != declared_w:
            extra, missing = sorted(writes - declared_w), sorted(declared_w - writes)
            out.append(Issue("V4", node, f"실제 쓰기와 writes가 다름 (선언 밖 쓰기: {extra}, 쓰지 않은 선언: {missing})"))
        undeclared = sorted((reads & signal_names) - declared_r)
        if undeclared:
            out.append(Issue("V4", node, f"reads에 없는 신호를 읽음: {undeclared}"))
        return pyslang.VisitAction.Skip

    tree.root.visit(visit)
    # FSM 조건식도 reads 안에서만 읽어야 한다.
    for b in m.blocks:
        if b["kind"] != "fsm":
            continue
        for tr in b["transitions"]:
            t = pyslang.SyntaxTree.fromText(f"module __c; wire w = ({tr['cond']}); endmodule")
            _, reads, _, _ = _block_usage(t.root)
            if bad := _unsynth_calls(t.root):
                out.append(Issue("V4", f'blocks/{b["id"]}/transitions/{tr["id"]}', f"합성되지 않는 시스템 함수/PLI: {bad}"))
            undeclared = sorted((reads & signal_names) - set(b["reads"]))
            if undeclared:
                out.append(Issue("V4", f'blocks/{b["id"]}/transitions/{tr["id"]}', f"reads에 없는 신호를 읽음: {undeclared}"))
    # SVA 식: 대입 없이 reads 안에서만 읽는다
    for a in m.assertions:
        node = f'assertions/{a["id"]}'
        t = pyslang.SyntaxTree.fromText(f"module __a; logic __c; assert property (@(posedge __c) ({a['expr']})); endmodule")
        writes, reads, kinds, forbidden = _block_usage(t.root, tasks)
        if writes or kinds or forbidden:
            out.append(Issue("V4", node, f"SVA 식에 대입이나 금지 구문이 있음 ({sorted(writes)} {forbidden})"))
        if {name for name, _ in invocations(t.root)} & set(tasks):
            out.append(Issue("V4", node, "SVA 식에서 task를 부를 수 없음 (function만)"))
        undeclared = sorted((reads & signal_names) - set(a["reads"]))
        if undeclared:
            out.append(Issue("V4", node, f"reads에 없는 신호를 읽음: {undeclared}"))
    return out


def _function_checks(m: Module, node: str, idx: int, f: dict, n, order: dict[str, int], tasks: dict, module_names: set[str]) -> list[Issue]:
    """function/task 본문: 금지 구문, blocking만, 모듈 신호를 직접 읽거나 쓰지 않음, 앞에 정의한 function만 부름 (재귀 없음)."""
    out = []
    writes, reads, kinds, forbidden = _block_usage(n, tasks)
    for x in forbidden + _kinds_in(n, _FN_FORBIDDEN):
        out.append(Issue("V4", node, f"금지 구문: {x}"))
    if bad := _unsynth_calls(n):
        out.append(Issue("V4", node, f"합성되지 않는 시스템 함수/PLI: {bad} (상수 함수 {sorted(SYNTH_SYSFUNCS)}만)"))
    if "nonblocking" in kinds:
        out.append(Issue("V4", node, f'{f["kind"]} 본문은 blocking(=)만 씀'))
    local = {v["name"] for v in [*f["args"], *f.get("vars", [])]}
    touched = sorted(((writes | reads) & module_names) - local)
    if touched:
        out.append(Issue("V4", node, f'{f["kind"]} 본문이 모듈 신호를 직접 읽거나 씀 (인자로 넘김): {touched}'))
    ins = {a["name"] for a in f["args"] if a["dir"] == "input"}
    if writes & ins:
        out.append(Issue("V4", node, f"입력 인자에 씀: {sorted(writes & ins)}"))
    for name, _ in invocations(n):
        if name == f["name"] or (name in order and order[name] > idx):
            out.append(Issue("V4", node, f"앞에 정의한 function만 부름 (재귀 없음): {name}"))
        elif name in tasks:
            if f["kind"] == "function":
                out.append(Issue("V4", node, f"function에서 task를 부를 수 없음: {name}"))
    return out


# ---------------------------------------------------------------- V5
def v5_drivers(m: Module, library: dict[str, Module]) -> list[Issue]:
    if m.role == "external":
        return []  # 동작은 등록된 RTL에 있다. 파이프라인은 외부 모듈 자체를 검증하지 않는다.
    out: list[Issue] = []
    writers: dict[str, list[tuple[str, dict | None]]] = {}

    def add(name, node, block=None):
        writers.setdefault(name, []).append((node, block))

    for b in m.blocks:
        node = f'blocks/{b["id"]}'
        if b["kind"] in ("comb", "seq"):
            for w in b["writes"]:
                add(w, node, b)
        elif b["kind"] == "fsm":
            add(f'{b["state_name"]}_q', node, b)
        elif b["kind"] == "cdc_sync":
            add(b["dst"], node, b)
        elif b["kind"] == "cdc_fifo":
            for side, k in (("src", "ready"), ("dst", "valid"), ("dst", "data")):
                add(b[side][k], node, b)
        elif b["kind"] == "tristate":
            add(b["i"], node, b)
    for c in m.connections:
        ends = [_endpoint_ports(m, c[k], library) for k in ("from", "to")]
        if not all(ends):
            continue  # V2가 보고함
        for (da, na), (db, nb) in zip(*ends, strict=False):
            # 같은 위치의 두 신호 중 값을 내는 쪽이 다른 쪽을 구동한다.
            a_self, b_self = c["from"].startswith("self."), c["to"].startswith("self.")
            src_a = (da == "input") if a_self else (da == "output")
            src_b = (db == "input") if b_self else (db == "output")
            if src_a == src_b and "signal" not in (da, db):
                out.append(Issue("V5", f'connections/{c["id"]}', f"{na}와 {nb}의 방향이 맞지 않음"))
                continue
            # 내부 신호 끝점은 반대쪽이 정한다: 인스턴스 입력으로 가면 신호가 값을 냄 (동기화 셀 출력 → 인스턴스 입력)
            if da == "signal" and db != "signal":
                src_a = not src_b
            elif db == "signal" and da != "signal":
                src_b = not src_a
            sink, sink_self = (nb, b_self) if (src_a or db == "signal" and not src_b) else (na, a_self)
            if sink_self:
                add(sink, f'connections/{c["id"]}')
    storage = m.storage()
    for p in m.ports:
        if p.dir == "input" and p.name in writers:
            out.append(Issue("V5", f"ports/{p.bundle or p.name}", f"입력 포트에 쓰기: {p.name}"))
    for name, s in storage.items():
        ws = writers.get(name, [])
        if s.origin == "memory":
            mnode = next(f'blocks/{b["id"]}' for b in m.blocks if b.get("name") == name and b["kind"] == "memory")
            wdoms = m.memory_domains(name, "write")
            for wnode, blk in ws:
                if blk is None or blk["kind"] != "seq" or blk.get("domain") not in wdoms:
                    out.append(Issue("V5", mnode, f"메모리는 쓰기 포트 도메인({sorted(wdoms)})의 seq 블록만 쓸 수 있음: {wnode}"))
            if len(ws) > sum(1 for p in m.memories()[name]["ports"] if p["kind"] in ("write", "rw")):
                out.append(Issue("V5", mnode, f"메모리를 쓰는 블록이 쓰기 포트보다 많음: {[w for w, _ in ws]}"))
            continue
        node = f"ports/{name}" if s.origin == "port" else (f"signals/{name}" if s.origin == "signal" else f"blocks/{name}")
        if len(ws) == 0:
            out.append(Issue("V5", node, f"구동하는 곳이 없음: {name}"))
        elif len(ws) > 1:
            out.append(Issue("V5", node, f"구동하는 곳이 여러 개: {name} ← {[w for w, _ in ws]}"))
        else:
            wnode, blk = ws[0]
            if s.kind == "reg":
                if blk is None or blk["kind"] not in ("seq", "fsm"):
                    out.append(Issue("V5", node, f"reg는 seq 블록 하나만 쓸 수 있음: {name} ← {wnode}"))
                elif s.domain != blk.get("domain"):
                    out.append(Issue("V5", node, f'reg의 도메인({s.domain})과 쓰는 블록의 도메인({blk.get("domain")})이 다름'))
            elif blk is not None and blk["kind"] == "seq":
                out.append(Issue("V5", node, f"wire를 seq 블록이 씀: {name} ← {wnode}"))
    return out


def validate(m: Module, library: dict[str, Module] | None = None, upto: int = 10) -> list[Issue]:
    """모듈 하나에 V1부터 upto 단계(최대 10)까지 실행한다.

    V1~V5는 앞 단계에서 오류가 나면 뒤 단계를 건너뛴다. V6~V10은 서로 독립이라 함께 실행한다.
    V11(동결)은 설계 전체 단위라 `structure.v11_frozen`으로 따로 부른다.
    """
    from pipeline.validator import structure as st

    library = {**(library or {}), m.name: m}
    if m.lower_errors:   # lang: py 조각을 SV로 옮기지 못함 (조각 오류이므로 V4). 옮긴 뒤에야 reads/writes가 채워지므로 V1보다 먼저
        return [Issue("V4", node, msg) for node, msg in m.lower_errors]
    out = v1_schema(m)
    if out or upto < 2:
        return out
    out = v2_refs(m, library)
    if out or upto < 3:
        return out
    out = v3_v4(m, library)
    if out or upto < 5:
        return out
    out = v5_drivers(m, library)
    if out or upto < 6:
        return out
    stages = [(6, lambda: st.v6_latch_loop(m, library)), (7, lambda: st.v7_reset(m)),
              (8, lambda: st.v8_domain(m, library)), (9, lambda: st.v9_fsm(m)),
              (10, lambda: st.v10_hierarchy(m, library))]
    st._PATH_CACHE.clear()
    for n, f in stages:
        if n <= upto:
            out += f()
    return out


def errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == "error"]
