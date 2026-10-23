"""L1에서 cocotb 하니스를 생성한다.

- `<module>_harness.py`: L1(도메인, 포트, 번들)만으로 만든 하니스 설정. Verifier가 테스트에서 import한다.
- `<module>_trace.json`: 트레이스할 내부 신호(L2)와 도메인. Verify 툴만 쓰고 Verifier에게는 주지 않는다.

    python3 -m pipeline.tb.gen <ir.json> -o <하니스 dir> --trace-out <Verify 전용 dir>

트레이스 파일은 Verifier에게 주는 디렉터리와 다른 곳에 써야 한다.

번들 프로토콜은 지금 `valid_ready`만 안다 (표준 프로토콜 목록은 P0 자료로 확정).
번들 안의 신호 이름은 `..._valid`, `..._ready`로 끝나는 것이 각각 valid, ready이고 나머지가 data다.
"""
from __future__ import annotations

import argparse
import json
import pprint
import sys
from pathlib import Path

from pipeline.ir.model import Module, load

DEFAULT_PERIOD_NS = 10.0


class GenError(Exception):
    pass


def _width1(type_: str) -> bool:
    return type_.strip() in ("logic", "bit", "reg", "wire")


def signal_widths(raw: dict) -> dict[str, int]:
    """L1 포트 신호의 비트 폭. 파라미터 기본값으로 계산하고, 계산할 수 없으면 8로 둔다."""
    import re
    params = {p["name"]: int(p["default"]) for p in raw.get("params", []) if str(p["default"]).isdigit()}
    out = {}
    raw = pad_view(raw)   # inout 포트는 패드 모델 포트로
    for p in raw["ports"]:
        for s in p.get("signals", [p]):
            m = re.search(r"\[(.+?):(.+?)\]", s["type"])
            if not m:
                out[s["name"]] = 1
                continue
            try:
                out[s["name"]] = (eval(m.group(1), {"__builtins__": {}}, params)
                                  - eval(m.group(2), {"__builtins__": {}}, params) + 1)
            except Exception:
                out[s["name"]] = 8
    return out


def _bundle(p: dict) -> dict:
    if p["protocol"] != "valid_ready":
        raise GenError(f'ports/{p["bundle"]}: 하니스가 모르는 프로토콜 {p["protocol"]}')
    if p["role"] not in ("source", "sink"):
        raise GenError(f'ports/{p["bundle"]}: valid_ready의 role은 source 또는 sink ({p["role"]})')
    names = [s["name"] for s in p["signals"]]
    valid = [n for n in names if n.endswith("valid")]
    ready = [n for n in names if n.endswith("ready")]
    if len(valid) != 1 or len(ready) != 1:
        raise GenError(f'ports/{p["bundle"]}: valid와 ready 신호를 하나씩 찾지 못함 ({names})')
    return {"name": p["bundle"], "protocol": p["protocol"], "domain": p["domain"], "dut_drives": p["role"] == "source",
            "valid": valid[0], "ready": ready[0], "data": [n for n in names if n not in (valid[0], ready[0])]}


def harness_config(m: Module) -> dict:
    """L1만 읽는다. inout 포트가 있으면 패드 모델 뷰(pad_view)로 바꿔 읽는다 (DUT 자리에는 pad_top_sv 래퍼를 둠).
    도메인이 없는 조합 모듈은 가상 클럭 뷰(comb_view)로 읽는다 (DUT 자리에는 pad_top 래퍼를 둠).
    pads는 패드 체커(동시 구동 충돌)가 볼 inout 포트 이름이다."""
    if not m.domains:
        m = Module(comb_view(m.raw))
    pads = [p["name"] for p in inout_ports(m.raw)]
    if pads:
        m = Module(pad_view(m.raw))
    clocks = {d["name"]: (d["clock"], round(1000.0 / d["freq_mhz"], 3) if d.get("freq_mhz") else DEFAULT_PERIOD_NS)
              for d in m.domains.values()}
    # 리셋 없는 도메인은 resets에 넣지 않는다
    resets = {d["name"]: (d["reset"]["port"], 0 if d["reset"]["active"] == "low" else 1) for d in m.domains.values() if d.get("reset")}
    # 분주 도메인: 하니스가 기준 클럭 주기(ps) × 분주비로 맞춰 상승 에지를 정렬한다
    clock_sync = {d["name"]: (d["sync_to"]["domain"], d["sync_to"]["div"]) for d in m.domains.values() if d.get("sync_to")}
    extra_resets = {d["name"]: [(x["port"], 0 if x["active"] == "low" else 1) for x in d["reset"]["extra"]]
                    for d in m.domains.values() if (d.get("reset") or {}).get("extra")}
    bundles, outputs, inputs = [], [], []
    for p in m.raw["ports"]:
        if "bundle" in p:
            bundles.append(_bundle(p))
        elif p["dir"] == "output":
            outputs.append({"name": p["name"], "domain": p["domain"], "width": 1 if _width1(p["type"]) else 0})
        else:
            inputs.append(p["name"])
    port_domains = {p.name: p.domain for p in m.ports if not p.derived}
    cfg = {"module": m.name, "clocks": clocks, "resets": resets, "bundles": bundles, "outputs": outputs,
           "inputs": inputs, "port_domains": port_domains}
    if clock_sync:   # 단일 클럭 블록의 하니스는 예전과 같게 둔다
        cfg["clock_sync"] = clock_sync
    neg = [d["name"] for d in m.domains.values() if d["edge"] == "neg"]
    if neg:   # 하강 에지 도메인: 입력은 상승 에지에서 바꾸고 DUT는 하강 에지에서 받는다
        cfg["neg_edge"] = neg
    if pads:
        cfg["pads"] = pads
    if extra_resets:
        cfg["extra_resets"] = extra_resets
    return cfg


def trace_names(m: Module, library: dict[str, Module] | None = None) -> dict:
    """트레이스 설정. names: 신호 → 도메인 (포트, 내부 신호, FSM 상태. 도메인이 없는 wire는 모든 도메인 표본에서 읽음).
    library가 있으면 인스턴스 포트도 `<인스턴스>.<포트>`로 넣는다 (Block Top에서 원인 모듈을 좁히려고).
    outputs: DUT 출력 포트. Verify가 시드별 결과를 비교할 때(T1) 쓴다.
    qualifiers: DUT가 내는 번들의 data 출력 → 그 번들의 valid. valid가 0인 사이클의 data 값은 비교하지 않는다.
    domains: 클럭 도메인. 둘 이상이면 T1은 사이클 비교를 하지 않는다 (시드에 따라 도메인 사이 위상이 바뀜).
    inout 포트가 있거나 조합 모듈이면 DUT 자리에 래퍼가 오므로 래퍼의 포트만 넣는다 (내부 신호는 래퍼 안이라 보이지 않음)."""
    if inout_ports(m.raw) or not m.domains:
        v = Module(pad_view(comb_view(m.raw)))
        return {"names": {p.name: p.domain for p in v.ports if not p.derived}, "outputs": [p.name for p in v.ports if p.dir == "output"],
                "qualifiers": {}, "domains": sorted(v.domains)}
    names = {p.name: p.domain for p in m.ports if not p.derived}
    for s in m.signals + m.state_regs():
        names[s.name] = s.domain if s.kind == "reg" else None
    for inst in m.instances if library else []:
        child = library.get(inst["module"])
        for p in child.ports if child else []:
            if not p.derived:
                names[f'{inst["name"]}.{p.name}'] = inst.get("domain_map", {}).get(p.domain)
    qual = {}
    for p in m.raw["ports"]:
        if "bundle" in p and p.get("protocol") == "valid_ready" and p.get("role") == "source":
            b = _bundle(p)
            qual.update({d: b["valid"] for d in b["data"]})
    return {"names": names, "outputs": [p.name for p in m.ports if p.dir == "output"], "qualifiers": qual,
            "domains": sorted(m.domains)}


def harness_source(m: Module) -> str:
    cfg = pprint.pformat(harness_config(m), width=110, sort_dicts=False)
    return (f'"""Generated from IR L1 of \'{m.name}\'. Do not edit; change the IR and regenerate."""\n'
            "from pipeline.tb.lib import Harness\n\n"
            f"CFG = {cfg}\n\n\n"
            "def make(dut, model=None, *, name):\n"
            "    \"\"\"name은 테스트 이름. 결과 파일 이름이 된다.\"\"\"\n"
            "    return Harness(dut, CFG, model, name=name)\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="L1 → cocotb 하니스")
    ap.add_argument("ir")
    ap.add_argument("-o", "--out", required=True, help="하니스 디렉터리 (Verifier에게 줌)")
    ap.add_argument("--trace-out", required=True, help="트레이스 설정 디렉터리 (Verify만 씀)")
    args = ap.parse_args(argv)
    m = load(args.ir)
    out, tout = Path(args.out), Path(args.trace_out)
    if out.resolve() == tout.resolve():
        ap.error("--trace-out은 -o와 다른 디렉터리여야 함 (L2 이름이 Verifier에게 보이지 않도록)")
    out.mkdir(parents=True, exist_ok=True)
    tout.mkdir(parents=True, exist_ok=True)
    (out / f"{m.name}_harness.py").write_text(harness_source(m), encoding="utf-8")
    (tout / f"{m.name}_trace.json").write_text(json.dumps(trace_names(m), indent=1), encoding="utf-8")
    print(out / f"{m.name}_harness.py")
    return 0


# ---------------------------------------------------------------- inout 패드 모델 (P11)
PAD_SUFFIX = ("_pad_o", "_pad_oe", "_pad_val")


def inout_ports(raw: dict) -> list[dict]:
    return [p for p in raw.get("ports", []) if "bundle" not in p and p.get("dir") == "inout"]


def _param_header(raw: dict) -> str:
    """래퍼의 파라미터 머리 (포트 타입이 IR 파라미터를 씀). 모두 IR 기본값(고정한 설정)."""
    ps = raw.get("params", [])
    if not ps:
        return ""
    return "#(" + ", ".join(f'parameter {p["type"]} {_esc(p["name"])} = {p["default"]}' for p in ps) + ") "


def _esc(name: str) -> str:
    """SV 예약어인 원본 이름은 이스케이프 식별자로 (Verilog 원본의 포트 `logic` 등)."""
    from pipeline.validator.checks import SV_KEYWORDS
    return f"\\{name} " if name in SV_KEYWORDS else name


def pad_view(raw: dict) -> dict:
    """inout 포트 P를 바깥 장치의 구동값 P_pad_o(입력), 구동 enable P_pad_oe(입력), 패드 값 P_pad_val(출력)로 바꾼 L1.
    하니스와 차분 시뮬레이션은 이 L1로 패드 래퍼(pad_wrapper_sv)를 구동한다."""
    pads = inout_ports(raw)
    if not pads:
        return raw
    names = {s["name"] for p in raw["ports"] for s in (p["signals"] if "bundle" in p else [p])}
    ports = []
    for p in raw["ports"]:
        if p in pads:
            new = [f"{p['name']}{s}" for s in PAD_SUFFIX]
            if names & set(new):
                raise GenError(f"ports/{p['name']}: 패드 모델 포트 이름이 겹침 ({sorted(names & set(new))})")
            ports += [{"name": new[0], "dir": "input", "type": p["type"], "domain": p["domain"]},
                      {"name": new[1], "dir": "input", "type": "logic", "domain": p["domain"]},
                      {"name": new[2], "dir": "output", "type": p["type"], "domain": p["domain"], "kind": "wire"}]
        else:
            ports.append(p)
    return {**raw, "ports": ports}


def pad_wrapper_sv(raw: dict, inner: str, wrapper: str, params: dict[str, str] | None = None,
                   pin: dict[str, str] | None = None, inner_raw: dict | None = None) -> str:
    """패드 래퍼 (와 이름 바꾼 포트 연결). 바깥 장치는 약한 구동(weak)이라 DUT가 구동하면 DUT 값이 이긴다. 패드마다 풀업.
    pin: IR 포트 이름 → inner 모듈의 포트 이름 (원본 RTL의 이름이 다를 때). params: inner에 줄 파라미터.
    inner_raw: inner 모듈의 L1 (raw가 하니스용 뷰라 포트가 다를 때, 예: 조합 모듈의 가상 클럭)."""
    pin = pin or {}
    view = Module(pad_view(raw))
    pads = {p["name"]: p for p in inout_ports(raw)}
    decl = []
    for p in view.ports:
        rng = p.type[len("logic"):].strip() if p.type.startswith("logic") else p.type
        decl.append(f"  {p.dir} logic {rng} {p.name}".replace("  ", " ").rstrip())
    lines = [f"module {wrapper} {_param_header(raw)}(", ",\n".join(decl), ");"]
    for n, p in pads.items():
        rng = p["type"][len("logic"):].strip()
        lines += [f"  wire {rng} {n};".replace("  ", " "),
                  f"  assign (weak0, weak1) {n} = {n}_pad_oe ? {n}_pad_o : 'z;",
                  f"  for (genvar k = 0; k < $bits({n}); k++) begin : g_pu_{n}",
                  f"    pullup ({n}[k]);" if rng else f"    pullup ({n});",
                  "  end",
                  f"  assign {n}_pad_val = {n};"]
    m = Module(inner_raw or raw)
    conns = ", ".join(f".{_esc(pin.get(p.name, p.name))}({p.name})" for p in m.ports)
    pa = ", ".join(f".{_esc(k)}({v})" for k, v in (params or {}).items())
    lines.append(f"  {inner} {'#(' + pa + ') ' if pa else ''}u_dut ({conns});")
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


def merge_view(raw: dict, tri_split: dict[str, dict]) -> dict:
    """inout을 o/oe/i로 나눈 하위 모듈의 L1을 원본처럼 inout 포트 하나로 되돌린다 (원본과 모듈 단위로 비교할 때)."""
    if not tri_split:
        return raw
    split = {v[k]: pad for pad, v in tri_split.items() for k in ("o", "oe", "i") if v.get(k)}
    ports, done = [], set()
    for p in raw["ports"]:
        pad = split.get(p.get("name"))
        if pad is None:
            ports.append(p)
        elif pad not in done:
            done.add(pad)
            i = next(q for q in raw["ports"] if q.get("name") == tri_split[pad]["i"])
            ports.append({"name": pad, "dir": "inout", "type": i["type"], "domain": i["domain"]})
    return {**raw, "ports": ports}


def merge_wrapper_sv(raw: dict, tri_split: dict[str, dict], wrapper: str) -> str:
    """나눈 포트를 가진 모듈을 감싸 inout 포트로 되돌린 모듈 (원본의 3상태 assign과 같은 구동)."""
    view = Module(merge_view(raw, tri_split))
    decl = []
    for p in view.ports:
        rng = p.type[len("logic"):].strip() if p.type.startswith("logic") else p.type
        kw = "inout wire" if p.dir == "inout" else f"{p.dir} logic"
        decl.append(f"  {kw} {rng} {p.name}".replace("  ", " ").rstrip())
    lines = [f"module {wrapper} {_param_header(raw)}(", ",\n".join(decl), ");"]
    pins = {}
    for pad, v in tri_split.items():
        t = next(q for q in raw["ports"] if q.get("name") == v["i"])["type"]
        oe_t = next(q for q in raw["ports"] if q.get("name") == v["oe"])["type"]
        lines.append(f"  {oe_t} {pad}_m_oe;")
        drive = "'0"
        if v.get("o"):
            lines.append(f"  {t} {pad}_m_o;")
            pins[v["o"]] = f"{pad}_m_o"
            drive = f"{pad}_m_o"
        pins[v["oe"]] = f"{pad}_m_oe"
        pins[v["i"]] = pad
        lines.append(f"  assign {pad} = {pad}_m_oe ? {drive} : 'z;")
    m = Module(raw)
    conns = ", ".join(f".{p.name}({pins.get(p.name, p.name)})" for p in m.ports)
    lines.append(f"  {raw['module']} u_split ({conns});")
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


COMB_CLK = "tb_comb_clk"


def comb_view(raw: dict) -> dict:
    """도메인이 없는 조합 모듈을 하니스로 돌리기 위한 L1: 가상 클럭 하나(리셋 없음)의 도메인에 모든 포트를 둔다.
    래퍼(pad_wrapper_sv의 inner_raw)는 이 클럭을 DUT에 잇지 않는다. 도메인이 있으면 그대로 돌려준다."""
    if raw.get("domains"):
        return raw
    if any(p.get("name") == COMB_CLK for p in raw.get("ports", [])):
        raise GenError(f"포트 이름 {COMB_CLK}가 하니스의 가상 클럭과 겹침")
    ports = [{**p, "domain": "comb"} for p in raw.get("ports", [])]
    return {**raw, "domains": [{"name": "comb", "clock": COMB_CLK, "edge": "pos"}], "ports": ports}


def pad_top(raw: dict, out_dir: Path) -> tuple[list[Path], str]:
    """시뮬레이션 top: inout 포트가 있거나 조합 모듈(하니스의 가상 클럭 포트가 필요)이면 래퍼 파일과 그 이름,
    없으면 ([], 모듈 이름)."""
    view = comb_view(raw)
    if not inout_ports(raw) and view is raw:
        return [], raw["module"]
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f'{raw["module"]}_pad_top'
    f = out_dir / f"{name}.sv"
    f.write_text(pad_wrapper_sv(view, raw["module"], name, inner_raw=raw), encoding="utf-8")
    return [f], name


if __name__ == "__main__":
    sys.exit(main())
