"""P11 R1: 원본을 시뮬레이션하기 위한 사본. Verilator가 받지 못하는 구문을 같은 뜻으로 바꾼다 (IR 추출에는 쓰지 않음).

- generate 배열 안 인스턴스를 가리키는 `defparam` (Verilator: "defparam with arrayed instance" 미지원):
  `defparam` 문장을 지우고, 대상 인스턴스의 파라미터 대입을 배열 원소마다 elaborate한 값을 고르는 조건식으로 바꾼다.
  예: `leaf #(.V(i)) u_l` + `defparam g[1].u_l.V = 7` → `leaf #(.V((i == 0) ? 0 : (i == 1) ? 7 : 2)) u_l`
바꾼 파일만 사본을 만들고, R4/R6은 원본 대신 사본을 컴파일한다.
"""
from __future__ import annotations

from pathlib import Path

import pyslang

from pipeline.importer.extract import const_text
from pipeline.importer.sources import KW_OPEN, Design

SK = pyslang.SymbolKind


def _walk(scope, out: list):
    for m in scope:
        if m.kind == SK.DefParam:
            out.append(m)
        elif m.kind == SK.Instance:
            _walk(m.body, out)
        elif m.kind == SK.GenerateBlock and not m.isUninstantiated:
            _walk(m, out)
        elif m.kind == SK.GenerateBlockArray:
            for e in m:
                if e.kind == SK.GenerateBlock and not e.isUninstantiated:
                    _walk(e, out)


def _instances(scope, path: list, out: list):
    """(인스턴스, [(genvar 이름, 값), ...]) — 고른 generate 갈래와 배열 원소 안까지."""
    for m in scope:
        if m.kind == SK.Instance:
            out.append((m, list(path)))
            _instances(m.body, [], out)
        elif m.kind == SK.GenerateBlock and not m.isUninstantiated:
            _instances(m, path, out)
        elif m.kind == SK.GenerateBlockArray:
            for e in m:
                if e.kind == SK.GenerateBlock and not e.isUninstantiated:
                    gv = next((x.name for x in e if x.kind == SK.Parameter), None)
                    _instances(e, [*path, (gv, e.arrayIndex)], out)


def make(design: Design, out_dir: Path) -> dict[str, str]:
    """사본이 필요한 파일 → 사본 경로. 필요 없으면 빈 dict."""
    dps: list = []
    _walk(design.top.body, dps)
    arrayed = [d for d in dps if "[" in d.target.hierarchicalPath.rsplit(".", 2)[0]]
    if not arrayed:
        return {}
    insts: list = []
    _instances(design.top.body, [], insts)
    edits: dict[str, list[tuple[int, int, str]]] = {}   # 파일 → (시작, 끝, 새 글자) 바이트 위치

    def add(node, text: str):
        r = design.sm.getFullyOriginalRange(node.sourceRange)
        f = str(design.sm.getFullPath(r.start.buffer))
        off = len(KW_OPEN.encode()) if f in design.wrapped else 0
        edits.setdefault(f, []).append((r.start.offset - off, r.end.offset - off, text))

    done_stmt, done_inst = set(), set()
    for d in arrayed:
        stmt = d.syntax.parent   # DefParam 문장
        key = (design.where(stmt.sourceRange.start))
        if key not in done_stmt:
            done_stmt.add(key)
            keep = [a for a in stmt.assignments if "[" not in design.text(a).split("=")[0].rsplit(".", 2)[0]] \
                if hasattr(stmt, "assignments") else []
            add(stmt, ("defparam " + ", ".join(design.text(a) for a in keep) + ";") if keep else
                "/* defparam: P11 시뮬레이션 사본에서 인스턴스 파라미터로 옮김 */")
        inst_path, pname = d.target.hierarchicalPath.rsplit(".", 1)
        target = next((i for i, _ in insts if i.hierarchicalPath == inst_path), None)
        if target is None:
            continue
        loc = design.where(target.syntax.sourceRange.start)
        if (loc, pname) in done_inst:
            continue
        done_inst.add((loc, pname))
        same = [(i, p) for i, p in insts if design.where(i.syntax.sourceRange.start) == loc]
        vals = []
        for i, path in same:
            v = next((const_text(p.value) for p in i.body.parameters if p.name == pname), None)
            cond = " && ".join(f"({g} == {idx})" for g, idx in path if g)
            vals.append((cond, v))
        expr = vals[-1][1]
        for cond, v in reversed(vals[:-1]):
            expr = f"{cond} ? {v} : {expr}" if cond else v
        hi = target.syntax.parent
        pv = hi.parameters
        if pv is not None:
            named = [a for a in pv.parameters if a.kind == pyslang.SyntaxKind.NamedParamAssignment and a.name.valueText == pname]
            if named and named[0].expr is not None:
                add(named[0].expr, f"({expr})")
            else:
                last = list(pv.parameters)[-1]
                r_text = design.text(last)
                add(last, f"{r_text}, .{pname}({expr})")
        else:
            add(hi.type, f"{design.text(hi.type)} #(.{pname}({expr}))")
    out = {}
    out_dir.mkdir(parents=True, exist_ok=True)
    for f, es in edits.items():
        data = Path(f).read_bytes()
        for a, b, t in sorted(es, reverse=True):
            data = data[:a] + t.encode() + data[b:]
        dst = out_dir / Path(f).name
        dst.write_bytes(data)
        out[str(Path(f).resolve())] = str(dst)
    return out
