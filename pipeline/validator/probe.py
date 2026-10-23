"""V3, V4용 탐침 모듈.

IR 모듈 하나의 선언과 SV 문자열(타입, 리셋값, 조건식, 조각)을 한 SV 모듈에 모아
pyslang으로 컴파일한다. 줄마다 IR 노드를 기록해 두고, 진단이 난 줄을 노드로 되돌린다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pyslang

from pipeline.ir.model import Module

CLK = "__probe_clk"


@dataclass
class Probe:
    lines: list[str] = field(default_factory=list)
    nodes: list[tuple[str, str]] = field(default_factory=list)  # 줄별 (단계, 노드)

    def add(self, text: str, stage: str, node: str) -> None:
        for line in text.split("\n"):
            self.lines.append(line)
            self.nodes.append((stage, node))

    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass
class Diag:
    stage: str
    node: str
    message: str


def _enum_decl(t: dict) -> str:
    base = f'{t["base"]} ' if t.get("base") else ""
    return f'typedef enum {base}{{{", ".join(t["members"])}}} {t["name"]};'


def _struct_decl(t: dict) -> str:
    fields = " ".join(f'{f["type"]} {f["name"]};' for f in t["fields"])
    return f'typedef struct packed {{ {fields} }} {t["name"]};'


def build(m: Module, library: dict[str, Module] | None = None) -> Probe:
    p = Probe()
    p.add(f"module __probe_{m.name};", "-", "-")
    p.add(f"  logic {CLK};", "-", "-")
    for prm in m.params:
        p.add(f'  localparam {prm["type"]} {prm["name"]} = {prm["default"]};', "V3", f'params/{prm["name"]}')
    for t in m.types:
        p.add("  " + (_enum_decl(t) if t["kind"] == "enum" else _struct_decl(t)), "V3", f'types/{t["name"]}')
    for port in m.ports:
        if not port.derived:
            p.add(f"  {port.type} {port.name};", "V3", f"ports/{port.name}")
    for name in sorted(m.all_reset_ports()):   # comb 조각이 리셋 포트를 읽을 수 있음
        p.add(f"  logic {name};", "V3", f"domains/{name}")
    for s in m.signals:
        p.add(f"  {s.type} {s.name};", "V3", f"signals/{s.name}")
    for s in m.state_regs():
        p.add(f"  {s.type} {s.name};", "V3", f"blocks/{s.name}")
    for b in m.blocks:
        if b["kind"] == "memory":
            p.add(f'  {b["word_type"]} {b["name"]} [{b["depth"]}];', "V3", f'blocks/{b["id"]}')
    from pipeline.emitter.sv import function_head, function_tail
    for f in m.functions:   # 함수 선언 줄이 노드를 정한다 (v3_v4가 선언 시작 줄로 함수를 찾음)
        node = f'functions/{f["name"]}'
        p.add("  " + function_head(f), "V4", node)
        for v in f.get("vars", []):
            p.add(f'    {v["type"]} {v["name"]};', "V4", node)
        p.add(f["body"], "V4", node)
        p.add("  " + function_tail(f), "V4", node)
    # 리셋값은 대상의 타입으로 상수 대입이 되는지 본다.
    for port in m.ports:
        if port.reset is not None:
            p.add(f"  localparam {port.type} __rst_{port.name} = {port.reset};", "V3", f"ports/{port.name}/reset")
    for s in m.signals:
        if s.reset is not None:
            p.add(f"  localparam {s.type} __rst_{s.name} = {s.reset};", "V3", f"signals/{s.name}/reset")
    for b in m.blocks:
        if b["kind"] == "fsm":
            p.add(f'  localparam {b["state_type"]} __rst_{b["id"]} = {b["reset_state"]};', "V3", f'blocks/{b["id"]}/reset_state')
            for tr in b["transitions"]:
                p.add(f'  wire __cond_{tr["id"]} = ({tr["cond"]});', "V3", f'blocks/{b["id"]}/transitions/{tr["id"]}')
        elif b["kind"] == "comb":
            p.add(f'  always_comb begin\n{b["body"]}\n  end', "V4", f'blocks/{b["id"]}')
        elif b["kind"] == "seq":
            p.add(f'  always_ff @(posedge {CLK}) begin\n{b["body"]}\n  end', "V4", f'blocks/{b["id"]}')
    for a in m.assertions:   # 클럭과 disable iff는 Emitter가 씌우므로 식만 본다
        p.add(f'  {a["kind"]} property (@(posedge {CLK}) ({a["expr"]}));', "V4", f'assertions/{a["id"]}')
    # 인스턴스 파라미터 값은 자식 모듈의 파라미터 타입으로 검사한다.
    for inst in m.instances:
        child = (library or {}).get(inst["module"])
        if not child:
            continue
        ptypes = {q["name"]: q["type"] for q in child.params}
        for k, v in inst.get("params", {}).items():
            if k in ptypes:
                p.add(f'  localparam {ptypes[k]} __ip_{inst["name"]}_{k} = {v};', "V3", f'instances/{inst["name"]}/params/{k}')
    p.add("endmodule", "-", "-")
    return p


# 경고 중에서도 폭이 맞지 않는 대입은 오류로 다룬다.
_WIDTH_CODES = ("WidthTrunc", "WidthExpand", "ConstantConversion", "SignConversion")


def compile_probe(p: Probe) -> tuple[pyslang.SyntaxTree, list[Diag]]:
    tree = pyslang.SyntaxTree.fromText(p.text())
    comp = pyslang.Compilation()
    comp.addSyntaxTree(tree)
    sm = tree.sourceManager
    out: list[Diag] = []
    for d in comp.getAllDiagnostics():
        code = str(d.code)
        if not (d.isError() or any(c in code for c in _WIDTH_CODES)):
            continue
        line = sm.getLineNumber(d.location)
        stage, node = p.nodes[line - 1] if 0 < line <= len(p.nodes) else ("V3", "-")
        msg = pyslang.DiagnosticEngine.reportAll(sm, [d]).strip().splitlines()[0]
        out.append(Diag(stage if stage != "-" else "V3", node, msg.split(": ", 2)[-1]))
    return tree, out
