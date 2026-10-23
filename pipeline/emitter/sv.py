"""IR에서 SystemVerilog를 결정론적으로 방출한다 (최소 Emitter, v0).

- 클럭과 리셋 포트는 domains에서 만든다. `seq` 블록의 `always_ff`와 리셋 분기는 Emitter가 씌운다.
- `fsm`은 상태 레지스터 `<state_name>_q`와 next-state 로직으로 방출한다.
- `cdc_sync`는 프로젝트 표준 동기화 셀 인스턴스로 방출한다. 셀 이름과 포트는 사내 자료로 확정할 때까지
  임시 표준 `sync_2ff #(WIDTH) (clk, rst_n, d, q)`를 쓴다 (`stubs/sync_2ff.sv`). `gray`는 같은 셀에 폭만 준다.
  셀의 리셋은 active-low이므로, 도착 도메인의 리셋이 active-high면 반전해 연결한다.
  `pulse`는 `sync_pulse`, `cdc_fifo` 블록은 `cdc_fifo` 셀이다 (`stubs/`). 모두 임시 표준 셀이고 사내 셀을 받으면 바꾼다.
  셀의 리셋은 설계의 리셋 방식을 따른다: 동기 리셋 설계면 `ASYNC_RST(1'b0)`를 준다 (기본은 비동기).
- 도메인의 추가 동기 리셋(`reset.extra`)은 `if (주 리셋) ... else if (추가 리셋) ... else 본문`으로 씌운다.
- `reset.synchronize` 도메인은 `rst_sync` 셀로 리셋을 동기화하고, 그 도메인의 셀과 인스턴스에는 동기화한 리셋을 연결한다.
- 인스턴스의 클럭과 리셋은 `domain_map`으로만 연결한다.
- inout 포트는 `inout wire`로 선언하고, `tristate` 블록은 `assign pad = oe ? o : 'z; assign i = pad;`로 낸다
  (open_drain은 `oe ? '0 : 'z`, oe가 pad와 같은 폭이면 비트마다).
- `functions`는 신호 선언 뒤에 `function automatic`/`task automatic`으로 낸다 (지역 변수 `vars`는 본문 앞에 선언).
- `assertions`는 모듈 끝에 `` `ifndef SYNTHESIS `` 안에서 `<id>: assert property (@(<도메인 에지> <클럭>) disable iff (<리셋>) (<expr>))`로
  낸다. 리셋은 `<도메인>_sva_rst` wire로 옮겨 쓴다 (비동기 리셋 포트를 속성에 바로 쓰면 Verilator SYNCASYNCNET 경고).
  실패하면 시뮬레이션을 멈추지 않고 `[SVA] FAIL module=<모듈> id=<id> kind=<kind>` 줄을 낸다 (Verify가 로그에서 읽어 그 테스트의 실패로 냄).
  `cover`는 방출만 한다 (Verilator 커버리지 빌드에서 user 항목으로 셈).
- 노드마다 `// @ir <노드>` 주석을 달고, 소스맵(노드 → 줄 범위)을 함께 낸다.
  `lang: py` 모듈의 조각은 문장마다 `py_line`(조각 안의 Python 줄)을 단 세부 항목을 더 낸다.
  IR 값이 그대로 놓인 줄(조각 본문, 전이 조건, 신호 타입, 리셋값, 파라미터와 포트 선언, 연결로 정해진 핀)은
  `fragment: true`인 세부 항목으로 따로 적는다. Classifier는 이 항목으로 lint 에러가 조각 안인지 골격인지 가른다.

`external` 모듈은 방출하지 않고 등록된 RTL 파일(`rtl_files`)을 파일 목록에 그대로 넣는다.
`top`과 `block_top`에는 SDC 골격(`sdc.py`)을 함께 낸다.
`memory` 블록은 `impl: inferred`만 방출한다 (배열 선언). `macro`는 매크로 래퍼 규약을 받은 뒤 확정한다.

    python3 -m pipeline.emitter.sv <ir...> -o <dir>   # <모듈>.sv, .srcmap.json, .sdc, filelist.f
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.ir.model import Module, load_library

SYNC_CELL = {"module": "sync_2ff", "clk": "clk", "rst": "rst_n", "d": "d", "q": "q"}
PULSE_CELL = "sync_pulse"   # (src_clk, src_rst_n, dst_clk, dst_rst_n, d, q)
FIFO_CELL = "cdc_fifo"      # #(WIDTH, DEPTH) (src_clk, src_rst_n, src_valid, src_ready, src_data, dst_clk, dst_rst_n, dst_valid, dst_ready, dst_data)
RST_CELL = "rst_sync"       # (clk, rst_n, rst_n_sync)


class EmitError(Exception):
    pass


@dataclass
class Emitted:
    module: str
    text: str
    sourcemap: list[dict] = field(default_factory=list)  # {"node", "start", "end"[, "fragment", "py_line"]} (1부터 센 줄 번호)


class _Writer:
    def __init__(self):
        self.lines: list[str] = []
        self.map: list[dict] = []

    def line(self, s: str = "", frag: str | None = None, py: list[tuple[int, int, int]] | None = None) -> None:
        """frag를 주면 이 줄(들)을 LLM이 쓴 노드 `frag`의 내용으로 소스맵에 적는다.
        py는 Python 조각의 문장별 줄 대응 (s 안의 줄 번호)."""
        start = len(self.lines) + 1
        self.lines.extend(s.split("\n"))
        if frag:
            self.map.append({"node": frag, "start": start, "end": len(self.lines), "fragment": True})
            for a, b, pl in py or []:
                self.map.append({"node": frag, "start": start + a - 1, "end": start + b - 1, "fragment": True, "py_line": pl})

    def node(self, node: str):
        w = self

        class _Ctx:
            def __enter__(self):
                w.line(f"  // @ir {node}")
                self.start = len(w.lines) + 1

            def __exit__(self, *exc):
                w.map.append({"node": node, "start": self.start, "end": len(w.lines)})

        return _Ctx()


def _decl(type_: str, name: str) -> str:
    """`logic [7:0]` 같은 타입 문자열과 이름으로 선언문을 만든다."""
    return f"{type_} {name};"


def _reset_cond(dom: dict) -> str:
    r = dom["reset"]
    return f'!{r["port"]}' if r["active"] == "low" else r["port"]


def _extra_cond(dom: dict) -> str | None:
    """추가 동기 리셋 조건 (없으면 None)."""
    xs = dom["reset"].get("extra", [])
    return " || ".join(f'!{x["port"]}' if x["active"] == "low" else x["port"] for x in xs) or None


def _rst_n(m: Module, dom: dict) -> str:
    """이 모듈 안에서 도메인 리셋을 active-low로 쓴 식. 블록 안에서 동기화하는 도메인은 동기화한 넷. 리셋 없는 도메인은 1."""
    if not dom.get("reset"):
        return "1'b1"
    if dom["reset"].get("synchronize"):
        return m.synced_reset(dom["name"])
    r = dom["reset"]
    return r["port"] if r["active"] == "low" else f'!{r["port"]}'


def _cell_params(m: Module, prm: list[str] | None = None) -> str:
    """표준 셀의 파라미터 목록. 동기 리셋 설계면 셀도 동기 리셋으로 (ASYNC_RST=0)."""
    prm = [*(prm or []), *([".ASYNC_RST(1'b0)"] if m.reset_async() is False else [])]
    return f' #({", ".join(prm)})' if prm else ""


def _sensitivity(dom: dict) -> str:
    edge = "posedge" if dom["edge"] == "pos" else "negedge"
    s = f'{edge} {dom["clock"]}'
    r = dom.get("reset")
    if r and r["async"]:
        s += f' or {"negedge" if r["active"] == "low" else "posedge"} {r["port"]}'
    return s


def _indent(body: str, n: int) -> str:
    pad = " " * n
    return "\n".join(pad + ln.strip() for ln in body.strip().split("\n"))


def _seq(w: _Writer, m: Module, dom: dict, resets: list[tuple[str, str, str]], body: str, body_node: str | None) -> None:
    """resets: (이름, 리셋값, 리셋값이 온 노드)."""
    w.line(f"  always_ff @({_sensitivity(dom)}) begin")
    if resets and dom.get("reset"):   # 리셋 없는 도메인의 리셋값은 V7이 막음
        width = max(len(n) for n, _, _ in resets)
        for kw, cond in (("if", _reset_cond(dom)), ("end else if", _extra_cond(dom))):
            if cond is None:
                continue
            w.line(f"    {kw} ({cond}) begin")
            for name, val, src in resets:
                w.line(f"      {name.ljust(width)} <= {val};", frag=src)
        w.line("    end else begin")
        w.line(_indent(body, 6), frag=body_node, py=m.py_lines.get(body_node))
        w.line("    end")
    else:
        w.line(_indent(body, 4), frag=body_node, py=m.py_lines.get(body_node))
    w.line("  end")


def _fsm(w: _Writer, m: Module, b: dict) -> None:
    dom = m.domains[b["domain"]]
    q, d = f'{b["state_name"]}_q', f'{b["state_name"]}_d'
    _seq(w, m, dom, [(q, b["reset_state"], f'blocks/{b["id"]}/reset_state')], f"{q} <= {d};", None)
    w.line("  always_comb begin")
    w.line(f"    {d} = {q};")
    w.line(f"    case ({q})")
    by_from: dict[str, list[dict]] = {}
    for t in b["transitions"]:
        by_from.setdefault(t["from"], []).append(t)
    for st, trs in by_from.items():
        w.line(f"      {st}: begin")
        for i, t in enumerate(trs):  # 배열 순서가 우선순위
            kw = "if" if i == 0 else "else if"
            w.line(f'        {kw} ({t["cond"]}) {d} = {t["to"]};  // @ir {t["id"]}',
                   frag=f'blocks/{b["id"]}/transitions/{t["id"]}')
        w.line("      end")
    w.line("      default: ;")
    w.line("    endcase")
    w.line("  end")


def _parent_type(t: str, child: Module, inst: dict) -> str:
    """자식 포트 타입을 부모 쪽 이름으로: 자식 파라미터를 인스턴스가 준 값(없으면 자식 기본값)으로 바꾼다.
    부모 모듈에는 자식 파라미터 이름이 없으므로 그대로 쓰면 wire 선언이 깨진다."""
    vals = {p["name"]: str(inst.get("params", {}).get(p["name"], p["default"])) for p in child.params}
    if not vals:
        return t
    return re.sub(r"\b(" + "|".join(map(re.escape, vals)) + r")\b", lambda x: f"({vals[x.group(1)]})", t)


def _instance_nets(m: Module, library: dict[str, Module]) -> tuple[dict[tuple[str, str], tuple[str, str]], list[tuple[str, str, str]]]:
    """(인스턴스, 자식 포트) → (부모 쪽 넷 이름, 연결 id). 인스턴스끼리 잇는 넷은 새 wire로 선언한다."""
    from pipeline.validator.checks import _endpoint_ports  # 같은 끝점 해석을 쓴다

    nets: dict[tuple[str, str], tuple[str, str]] = {}
    wires: list[tuple[str, str, str]] = []
    resolved = []
    for c in m.connections:
        ends = [_endpoint_ports(m, c[k], library) for k in ("from", "to")]
        if not all(ends):
            raise EmitError(f'connections/{c["id"]}: 끝점을 해석할 수 없음 (Validator V2를 먼저 통과해야 함)')
        resolved.append((c, ends, [c[k].split(".", 1)[0] for k in ("from", "to")]))
    # 인스턴스 출력이 부모 포트에도 이어지면 그 포트 이름을 넷 이름으로 쓴다. 같은 출력을 읽는 다른 인스턴스도
    # 그 이름을 써야 한다 (wire를 따로 만들면 아무도 구동하지 않음)
    alias: dict[tuple[str, str], str] = {}
    for c, ends, owners in resolved:
        for (da, na), (db, nb) in zip(*ends):
            if owners[0] == "self" and db == "output":
                alias[(owners[1], nb)] = na
            elif owners[1] == "self" and da == "output":
                alias[(owners[0], na)] = nb
    declared = set()
    for c, ends, owners in resolved:
        for (da, na), (db, nb) in zip(*ends):
            if owners[0] == "self" or owners[1] == "self":
                self_name = na if owners[0] == "self" else nb
                inst, port = (owners[1], nb) if owners[0] == "self" else (owners[0], na)
                nets[(inst, port)] = (self_name, c["id"])
            else:
                # 출력 쪽 인스턴스 포트 이름으로 wire를 만든다 (부모 포트로도 나가면 그 포트 이름).
                src_i, src_p = (owners[0], na) if da == "output" else (owners[1], nb)
                net = alias.get((src_i, src_p), f"{src_i}_{src_p}")
                if (src_i, src_p) not in alias and net not in declared:
                    declared.add(net)
                    inst = next(i for i in m.instances if i["name"] == src_i)
                    child = library[inst["module"]]
                    wires.append((_parent_type(child.port(src_p).type, child, inst), net, c["id"]))
                nets[(owners[0], na)] = (net, c["id"])
                nets[(owners[1], nb)] = (net, c["id"])
    return nets, wires


def function_head(f: dict) -> str:
    """`function automatic <ret> name(input <type> a, ...);` 또는 `task automatic name(...);`."""
    args = ", ".join(f'{a["dir"]} {a["type"]} {a["name"]}' for a in f["args"])
    kw = f'function automatic {f["ret"]}' if f["kind"] == "function" else "task automatic"
    return f'{kw} {f["name"]}({args});'


def function_tail(f: dict) -> str:
    return "endfunction" if f["kind"] == "function" else "endtask"


SVA_FAIL = "[SVA] FAIL"   # 실패 보고 줄의 머리. pipeline/verify/run.py가 로그에서 찾는다


def assertion_lines(m: Module, a: dict) -> list[str]:
    """속성 한 줄(과 실패 보고). 클럭 에지와 disable iff는 도메인에서."""
    from pipeline.validator.checks import sva_reset
    dom = m.domains[a["domain"]]
    edge = "posedge" if dom["edge"] == "pos" else "negedge"
    dis = f" disable iff ({sva_reset(a['domain'])})" if dom.get("reset") and a.get("reset_disable", True) else ""
    head = f'{a["id"]}: {a["kind"]} property (@({edge} {dom["clock"]}){dis} ({a["expr"]}))'
    if a["kind"] == "cover":
        return [f"{head};"]
    return [f"{head}", f'  else begin $display("{SVA_FAIL} module={m.name} id={a["id"]} kind={a["kind"]} at %m"); $fflush(); end']


def storage_or_port_type(m: Module, name: str) -> str:
    st = m.storage().get(name)
    if st is not None:
        return st.type
    p = m.port(name)
    return p.type if p else "logic"


def emit(m: Module, library: dict[str, Module] | None = None) -> Emitted:
    if m.role == "external":
        raise EmitError(f"{m.name}: external 모듈은 방출하지 않음. rtl_files를 그대로 참조함")
    library = {**(library or {}), m.name: m}
    w = _Writer()
    w.line(f"// Generated from IR module '{m.name}'. Do not edit; change the IR and re-emit.")
    params = [p for p in m.params if not p.get("local")]
    if params:
        w.line(f"module {m.name} #(")
        for i, p in enumerate(params):
            w.line(f'  parameter {p["type"]} {p["name"]} = {p["default"]}' + ("," if i < len(params) - 1 else ""),
                   frag=f'params/{p["name"]}')
        w.line(") (")
    else:
        w.line(f"module {m.name} (")
    for i, p in enumerate(m.ports):
        src = f"domains/{p.domain}" if p.derived else f"ports/{p.bundle or p.name}"
        typ = ("wire" + p.type[len("logic"):]) if p.dir == "inout" and p.type.startswith("logic") else p.type
        w.line(f"  {p.dir.ljust(6)} {typ} {p.name}" + ("," if i < len(m.ports) - 1 else ""), frag=src)
    w.line(");")

    for p in m.params:
        if p.get("local"):
            w.line(f'  localparam {p["type"]} {p["name"]} = {p["default"]};', frag=f'params/{p["name"]}')
    for t in m.types:
        with w.node(f'types/{t["name"]}'):
            if t["kind"] == "enum":
                base = f'{t["base"]} ' if t.get("base") else ""
                w.line(f'  typedef enum {base}{{{", ".join(t["members"])}}} {t["name"]};', frag=f'types/{t["name"]}')
            else:
                fields = " ".join(f'{f["type"]} {f["name"]};' for f in t["fields"])
                w.line(f'  typedef struct packed {{ {fields} }} {t["name"]};', frag=f'types/{t["name"]}')
    for s in m.signals:
        w.line(f"  {_decl(s.type, s.name)}", frag=f"signals/{s.name}")
    for b in m.blocks:
        if b["kind"] == "fsm":
            w.line(f'  {b["state_type"]} {b["state_name"]}_q, {b["state_name"]}_d;')
        elif b["kind"] == "memory":
            if b["impl"] != "inferred":
                raise EmitError(f'blocks/{b["id"]}: memory impl {b["impl"]}은 매크로 래퍼 규약을 받은 뒤 방출함')
            with w.node(f'blocks/{b["id"]}'):
                w.line(f'  {b["word_type"]} {b["name"]} [{b["depth"]}];', frag=f'blocks/{b["id"]}')

    nets, wires = _instance_nets(m, library) if m.instances else ({}, [])
    for type_, name, cid in wires:
        w.line(f"  {_decl(type_, name)}", frag=f"connections/{cid}")
    w.line()
    for f in m.functions:
        node = f'functions/{f["name"]}'
        with w.node(node):
            w.line(f"  {function_head(f)}", frag=node)
            for v in f.get("vars", []):
                w.line(f'    {_decl(v["type"], v["name"])}', frag=node)
            w.line(_indent(f["body"], 4), frag=f"{node}/body", py=m.py_lines.get(f"{node}/body"))
            w.line(f"  {function_tail(f)}")
        w.line()

    for d in m.domains.values():
        if (d.get("reset") or {}).get("synchronize"):
            with w.node(f'domains/{d["name"]}/reset'):
                r = d["reset"]
                src = r["port"] if r["active"] == "low" else f'!{r["port"]}'
                w.line(f"  logic {m.synced_reset(d['name'])};")
                w.line(f'  {RST_CELL} u_rst_sync_{d["name"]} (.clk({d["clock"]}), .rst_n({src}), .rst_n_sync({m.synced_reset(d["name"])}));')
            w.line()

    storage = m.storage()
    for b in m.blocks:
        if b["kind"] == "memory":
            continue  # 선언부에서 방출함
        node = f'blocks/{b["id"]}'
        with w.node(node):
            if b["kind"] == "comb":
                w.line("  always_comb begin")
                w.line(_indent(b["body"], 4), frag=f"{node}/body", py=m.py_lines.get(f"{node}/body"))
                w.line("  end")
            elif b["kind"] == "seq":
                resets = [(n, storage[n].reset, f'{"ports" if storage[n].origin == "port" else "signals"}/{n}')
                          for n in b["writes"] if storage[n].reset is not None]
                _seq(w, m, m.domains[b["domain"]], resets, b["body"], f"{node}/body")
            elif b["kind"] == "fsm":
                _fsm(w, m, b)
            elif b["kind"] == "cdc_sync" and b["style"] == "pulse":
                s, d = m.domains[b["src_domain"]], m.domains[b["dst_domain"]]
                w.line(f'  {PULSE_CELL}{_cell_params(m)} u_{b["id"]} (.src_clk({s["clock"]}), .src_rst_n({_rst_n(m, s)}), '
                       f'.dst_clk({d["clock"]}), .dst_rst_n({_rst_n(m, d)}), .d({b["src"]}), .q({b["dst"]}));')
            elif b["kind"] == "cdc_sync":
                dom = m.domains[b["dst_domain"]]
                c = SYNC_CELL
                prm = _cell_params(m, [f'.WIDTH($bits({b["dst"]}))'] if b["style"] == "gray" else [])
                w.line(f'  {c["module"]}{prm} u_{b["id"]} (.{c["clk"]}({dom["clock"]}), .{c["rst"]}({_rst_n(m, dom)}), '
                       f'.{c["d"]}({b["src"]}), .{c["q"]}({b["dst"]}));')
            elif b["kind"] == "cdc_fifo":
                s, d, a, z = m.domains[b["src_domain"]], m.domains[b["dst_domain"]], b["src"], b["dst"]
                prm = _cell_params(m, [f'.WIDTH($bits({z["data"]}))', f'.DEPTH({b["depth"]})'])
                w.line(f'  {FIFO_CELL}{prm} u_{b["id"]} (')
                w.line(f'    .src_clk({s["clock"]}), .src_rst_n({_rst_n(m, s)}), .src_valid({a["valid"]}), '
                       f'.src_ready({a["ready"]}), .src_data({a["data"]}),')
                w.line(f'    .dst_clk({d["clock"]}), .dst_rst_n({_rst_n(m, d)}), .dst_valid({z["valid"]}), '
                       f'.dst_ready({z["ready"]}), .dst_data({z["data"]}));')
            elif b["kind"] == "tristate":
                pad, oe = b["pad"], b["oe"]
                drive = b["o"] if b["style"] == "push_pull" else "'0"
                if "".join(storage_or_port_type(m, oe).split()) in ("logic", "bit"):
                    w.line(f"  assign {pad} = {oe} ? {drive} : 'z;")
                else:   # 비트마다 enable
                    w.line(f"  for (genvar k_{b['id']} = 0; k_{b['id']} < $bits({pad}); k_{b['id']}++) begin : g_{b['id']}")
                    bit = f"{b['o']}[k_{b['id']}]" if b["style"] == "push_pull" else "1'b0"
                    w.line(f"    assign {pad}[k_{b['id']}] = {oe}[k_{b['id']}] ? {bit} : 1'bz;")
                    w.line("  end")
                w.line(f"  assign {b['i']} = {pad};")
            else:
                raise EmitError(f"{node}: v0 Emitter는 {b['kind']} 블록을 방출하지 않음")
        w.line()

    for inst in m.instances:
        child = library[inst["module"]]
        conns: list[tuple[str, str | None]] = []  # (핀 연결, 그 줄을 정한 노드)
        pins: set[str] = set()   # 자식 도메인끼리 함께 쓰는 클럭·리셋 포트는 한 번만 (같은 넷인지는 V2가 봄)
        for cd, dom in child.domains.items():
            pd = m.domains[inst["domain_map"][cd]]
            cr, pr = dom.get("reset"), pd.get("reset") or {}
            pins_ = [(dom["clock"], pd["clock"])]
            if cr:   # 리셋 있는 자식 도메인을 리셋 없는 부모 도메인에 잇는 것은 V2가 막음
                prst = pr.get("port")
                if pr.get("synchronize"):   # 동기화한 리셋 (active-low)을 자식 극성으로
                    prst = m.synced_reset(pd["name"]) if cr["active"] == "low" else f'!{m.synced_reset(pd["name"])}'
                # 추가 리셋의 개수와 극성은 V2가 봄
                pins_ += [(cr["port"], prst), *((cx["port"], px["port"]) for cx, px in zip(cr.get("extra", []), pr.get("extra", [])))]
            for cport, net in pins_:
                if cport not in pins:
                    pins.add(cport)
                    conns.append((f".{cport}({net})", None))
        for p in child.ports:
            if not p.derived:
                net, cid = nets.get((inst["name"], p.name), ("", None))
                # 연결이 없는 핀은 연결을 추가해서 고친다.
                conns.append((f'.{p.name}({net})', f"connections/{cid}" if cid else "connections"))
        prm = ", ".join(f".{k}({v})" for k, v in inst.get("params", {}).items())
        with w.node(f'instances/{inst["name"]}'):
            head = f'  {child.name} #({prm}) {inst["name"]} (' if prm else f'  {child.name} {inst["name"]} ('
            w.line(head)
            for i, (c, src) in enumerate(conns):
                w.line("    " + c + ("," if i < len(conns) - 1 else ""), frag=src)
            w.line("  );")
        w.line()
    if m.assertions:
        from pipeline.validator.checks import sva_reset
        w.line("`ifndef SYNTHESIS")
        for d in sorted({a["domain"] for a in m.assertions if m.domains[a["domain"]].get("reset")}):
            r = m.domains[d]["reset"]
            act = " || ".join([_reset_cond(m.domains[d]), *([_extra_cond(m.domains[d])] if r.get("extra") else [])])
            w.line(f"  wire {sva_reset(d)} = {act};")
        for a in m.assertions:
            node = f'assertions/{a["id"]}'
            with w.node(node):
                w.line("\n".join("  " + x for x in assertion_lines(m, a)), frag=node)
        w.line("`endif")
        w.line()
    w.line("endmodule")
    return Emitted(m.name, "\n".join(w.lines) + "\n", w.map)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="IR → SystemVerilog 방출")
    ap.add_argument("ir", nargs="+", help="IR JSON 파일들 (인스턴스가 참조하는 모듈도 함께)")
    ap.add_argument("-o", "--out", required=True, help="출력 디렉터리")
    ap.add_argument("--cells", nargs="*", default=[], help="표준 셀 RTL (동기화 셀 등). 파일 목록 맨 앞에 넣음")
    args = ap.parse_args(argv)
    from pipeline.emitter.sdc import emit_sdc

    lib = load_library(args.ir)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cells = [str(Path(c).resolve()) for c in args.cells]
    externals: list[str] = []
    files: list[str] = []
    for m in lib.values():
        if m.role == "external":
            externals += [str((m.path.parent / f).resolve()) for f in m.raw.get("rtl_files", [])]
            continue
        e = emit(m, lib)
        (out / f"{m.name}.sv").write_text(e.text, encoding="utf-8")
        (out / f"{m.name}.srcmap.json").write_text(json.dumps(e.sourcemap, indent=1), encoding="utf-8")
        files.append(str((out / f"{m.name}.sv").resolve()))
        if m.role in ("top", "block_top"):
            (out / f"{m.name}.sdc").write_text(emit_sdc(m, lib), encoding="utf-8")
        print(out / f"{m.name}.sv")
    # 표준 셀, 외부 RTL, 방출 파일 순. 모두 절대 경로라 어디서 -f로 읽어도 된다. 중복은 한 번만.
    (out / "filelist.f").write_text("\n".join(dict.fromkeys(cells + externals + files)) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
