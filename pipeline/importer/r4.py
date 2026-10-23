"""P11 R4: 이식 등가 판정. 원본 RTL과 IR에서 방출한 RTL에 같은 자극을 넣고 모든 출력 포트를 사이클 단위로 비교한다.

- 자극은 `pipeline.tb.diffsim`의 시드 기반 계획이다 (DUT 응답과 무관해서 두 RTL을 따로 돌려도 입력이 같다).
- 모듈마다 아래에서 위로 비교한다. 원본은 그 모듈을 top으로, IR의 파라미터 기본값(고정한 설정)을 `-G`로 준다.
- 원본 포트 이름을 바꾼 모듈(SV 예약어)은 바꾼 이름으로 감싼 래퍼를 top으로 쓴다.
- DUT가 내는 번들의 data는 valid=1인 사이클에만 비교한다 (`strict`면 모든 사이클).
- 리셋 직후 차이: 차이가 모두 리셋 구간과 풀린 뒤 `reset_window` 사이클 안에만 있으면 `reset_only`로 통과시키고 검토 항목으로 남긴다
  (원본의 리셋 없는 플롭을 IR 규칙으로 옮기면 생기는 차이. 리셋 방식이 섞인 원본은 R1이 거부함. P1 우회 표현 2, 7, 8).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from pipeline.importer.sources import KW_CLOSE, KW_OPEN
from pipeline.ir.model import Module

ROOT = Path(__file__).resolve().parents[2]
SEEDS = (1, 2, 3)
MODES = ("steady", "reset")
RESET_WINDOW = 4


def ref_sources(r1: dict, build: Path) -> tuple[list[Path], list[str]]:
    """원본 컴파일 단위(목록 순서, R1이 감싼 Verilog 파일은 감싼 복사본)와 Verilator 인자(include, define)."""
    inp = r1["inputs"]
    out = build / "ref_src"
    out.mkdir(parents=True, exist_ok=True)
    files = []
    incs = list(dict.fromkeys([*inp["incdirs"], *[str(Path(h).parent) for h in r1.get("headers", [])]]))
    sim = r1.get("sim_units", {})
    for k, u in enumerate(r1["units"]):
        u = Path(u)
        src = Path(sim.get(str(u), u))   # 시뮬레이션 사본이 있으면 그것 (defparam 옮김)
        if str(u) in r1.get("wrapped", []):   # R1이 1364-2005 키워드로 감싸 읽은 파일
            dst = out / f"{k:02d}_{u.stem}.sv"
            dst.write_text(KW_OPEN + src.read_text(encoding="utf-8", errors="replace") + KW_CLOSE, encoding="utf-8")
            files.append(dst)
        else:
            files.append(src)
        if str(u.parent) not in incs:
            incs.append(str(u.parent))
    args = [f"-I{d}" for d in incs] + [f"-D{k}={v}" for k, v in inp["defines"].items()]
    return files, args


def ref_params(ir: dict, r1: dict) -> dict[str, str]:
    """원본에 줄 파라미터 (원본 이름 → 값): IR의 파라미터(local 제외)와, R1이 고른 구조에서 쓰이지 않는다고 지운 파라미터의
    고정 값. 지운 것을 빼고 빌드하면 원본이 기본값으로 다른 구조를 골라, 이식 IR과 함께 틀린 채로 R4가 같다고 본다
    (2026-10-22 axis_async_fifo: PAUSE_ENABLE 고정 1, 기본 0. 원본 테스트벤치의 pause, overflow가 이식 IR에서 실패)."""
    meta = r1["modules"].get(ir["module"], {})
    inv = {v: k for k, v in meta.get("renames", {}).items()}
    out = {inv.get(p["name"], p["name"]): p["default"] for p in ir["params"] if not p.get("local")}
    removed = meta.get("removed_params")
    if removed is None and ir["module"] == r1.get("top"):   # 이 기록이 생기기 전의 R1: top은 고정 설정에서
        removed = r1["inputs"]["params"]
    for k, v in (removed or {}).items():
        out.setdefault(k, str(v))
    return out


def ref_wrapper(ir: dict, renames: dict[str, str], build: Path, params: dict[str, str] | None = None) -> tuple[Path | None, str]:
    """원본을 감싼 래퍼 (파일, top 이름). 포트 이름을 바꿨거나(SV 예약어), inout 포트가 있거나, 조합 모듈(가상 클럭)이면 만든다.
    없으면 (None, 원본 이름). 래퍼가 top이면 -G가 원본에 닿지 않으므로 파라미터(고정한 설정)는 래퍼 안의 인스턴스에 준다.
    params: 원본에 줄 파라미터 (`ref_params`). 없으면 IR의 파라미터."""
    from pipeline.tb.gen import comb_view, inout_ports, pad_wrapper_sv

    name = ir["module"]
    hview = comb_view(ir)
    if not renames and not inout_ports(ir) and hview is ir:
        return None, name
    inv = {v: k for k, v in renames.items()}
    if params is None:
        params = {inv.get(p["name"], p["name"]): p["default"] for p in ir["params"] if not p.get("local")}
    f = build / f"{name}_ref_top.sv"
    f.write_text(pad_wrapper_sv(hview, name, f"{name}_ref_top", params, inv, inner_raw=ir), encoding="utf-8")
    return f, f"{name}_ref_top"


def ir_wrapper(ir: dict, build: Path, inner: str | None = None) -> tuple[Path | None, str]:
    """방출 RTL의 래퍼 (inout 포트가 있거나 조합 모듈일 때). inner: 감쌀 모듈 (기본은 ir의 모듈)."""
    from pipeline.tb.gen import comb_view, inout_ports, pad_wrapper_sv

    name = ir["module"]
    hview = comb_view(ir)
    if not inout_ports(ir) and hview is ir and inner is None:
        return None, name
    f = build / f"{name}_pad_top.sv"
    f.write_text(pad_wrapper_sv(hview, inner or name, f"{name}_pad_top", inner_raw=ir), encoding="utf-8")
    return f, f"{name}_pad_top"


def _diff_cfg(top_ir: dict, mode: str, seed: int, fixed: dict, cycles: int) -> dict:
    from pipeline.tb.gen import comb_view, harness_config, pad_view, signal_widths
    top_ir = pad_view(comb_view(top_ir))
    return {"harness": harness_config(Module(top_ir)), "widths": signal_widths(top_ir), "mode": mode, "seed": seed,
            "cycles": cycles, "drain": 200, "model": None, "harness_dir": None, "cycle_trace": True, "fixed": fixed}


def run_one(files: list[Path], toplevel: str, top_ir: dict, mode: str, seed: int, build: Path, build_args: list[str],
            fixed: dict, cycles: int, rebuild: bool, plan: dict | None = None,
            opts: dict | None = None) -> tuple[dict | None, str]:
    """opts는 자극 계획 설정에 더한다 (예: R6의 `joint_reset`)."""
    from pipeline.verify.run import sim
    build.mkdir(parents=True, exist_ok=True)
    cfg = _diff_cfg(top_ir, mode, seed, fixed, cycles) | (opts or {}) | ({"plan": plan} if plan else {})
    (build / "diff_cfg.json").write_text(json.dumps(cfg), encoding="utf-8")
    out = build / "diff_out.json"
    out.unlink(missing_ok=True)
    res = sim(files, toplevel, "diffsim", ROOT / "pipeline" / "tb", build, build_args=build_args,
              env={"DIFF_CFG": str((build / "diff_cfg.json").resolve()), "DIFF_OUT": str(out.resolve())},
              rebuild=rebuild, timeout_s=300)
    if out.exists():
        return json.loads(out.read_text()), ""
    return None, "\n".join(f.message for f in res.findings)[-3000:] or res.log[-3000:]


def compare_cycles(a: dict, b: dict, h: dict, reset: dict[str, list[int]], strict: bool = False,
                   window: int = RESET_WINDOW) -> dict:
    """사이클 기록 비교 → {"status": "equal"|"reset_only"|"differ", "first": 첫 차이, "count": 차이 수}.
    reset은 도메인별 사이클마다 리셋 여부 (자극 계획). 리셋 사이클과 풀린 뒤 window 사이클 안의 차이는 따로 센다."""
    qual = {}
    if not strict:
        for bd in h["bundles"]:
            if bd["dut_drives"]:
                for n in bd["data"]:
                    qual[n] = bd["valid"]
    first, count, outside = None, 0, 0
    for d, rows_a in a["cycles"].items():
        rows_b = b["cycles"].get(d, [])
        win = reset_windows(reset.get(d, []), window)
        for c, (ra, rb) in enumerate(zip(rows_a, rows_b)):
            for n, va in ra.items():
                vb = rb.get(n)
                if n in qual and ra.get(qual[n]) != 1 and rb.get(qual[n]) != 1:
                    continue
                if va != vb:
                    count += 1
                    inw = c in win
                    outside += 0 if inw else 1
                    if first is None or (first["in_window"] and not inw):
                        first = {"domain": d, "cycle": c, "port": n, "ref": va, "ir": vb, "in_window": inw}
        if len(rows_a) != len(rows_b):
            count += 1
            outside += 1
            first = first or {"domain": d, "cycle": min(len(rows_a), len(rows_b)), "port": None, "ref": len(rows_a),
                              "ir": len(rows_b), "in_window": False}
    status = "equal" if count == 0 else ("reset_only" if outside == 0 else "differ")
    return {"status": status, "first": first, "count": count}


def reset_windows(reset: list[int], window: int) -> set[int]:
    """리셋이 걸린 사이클과 풀린 뒤 window 사이클."""
    out, last = set(), None
    for c, r in enumerate(reset):
        if r:
            last = c
        if last is not None and c - last <= window:
            out.add(c)
    return out


def run_module(name: str, lib: dict[str, dict], r1: dict, work: Path, cells: list[Path], seeds=SEEDS, modes=MODES,
               fixed: dict | None = None, cycles: int = 1500, strict: bool = False, window: int = RESET_WINDOW) -> dict:
    """모듈 하나의 R4. lib는 이식 IR 묶음 (그 모듈과 하위 모듈 포함)."""
    from pipeline.scheduler.run import emit_lib

    work = Path(work) / name
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    top_ir = lib[name]
    sub = _sub_lib(name, lib)
    ir_files, _ = emit_lib(sub, work / "rtl", cells)
    from pipeline.tb.gen import comb_view, harness_config, merge_view, merge_wrapper_sv, pad_view
    tri = r1["modules"].get(name, {}).get("tri_split") or {}
    view = merge_view(top_ir, tri)    # inout을 나눈 하위 모듈은 원본처럼 inout으로 되돌려 비교
    inner = None
    if tri:
        mw = work / f"{name}_merged.sv"
        mw.write_text(merge_wrapper_sv(top_ir, tri, f"{name}_merged"), encoding="utf-8")
        ir_files, inner = [*ir_files, mw], f"{name}_merged"
    iw, ir_top = ir_wrapper(view, work, inner)
    if iw:
        ir_files = [*ir_files, iw]
    ref_files, ref_args = ref_sources(r1, work)
    renames = r1["modules"].get(name, {}).get("renames", {})
    rparams = ref_params(top_ir, r1)
    wrap, ref_top = ref_wrapper(view, renames, work, rparams)
    if wrap:
        ref_files = [*ref_files, wrap]
    gparams = [] if wrap else [f"-G{k}={v}" for k, v in rparams.items()]
    top_ir = view
    h = harness_config(Module(pad_view(comb_view(top_ir))))
    results = []
    status = "equal"
    for mode in modes:
        for k, seed in enumerate(seeds):
            ra, err_a = run_one(ref_files, ref_top, top_ir, mode, seed, work / "ref", [*ref_args, *gparams, "-Wno-fatal"],
                                fixed or {}, cycles, rebuild=(mode == modes[0] and k == 0))
            rb, err_b = run_one(ir_files, ir_top, top_ir, mode, seed, work / "ir", ["-Wno-fatal"], fixed or {}, cycles,
                                rebuild=(mode == modes[0] and k == 0))
            if ra is None or rb is None:
                return {"module": name, "status": "error", "error": err_a if ra is None else err_b, "mode": mode, "seed": seed}
            from pipeline.tb.diffsim import make_plan
            plan = make_plan(_diff_cfg(top_ir, mode, seed, fixed or {}, cycles))
            cmp = compare_cycles(ra, rb, h, {d: dp["reset"] for d, dp in plan["domains"].items()}, strict, window)
            from pipeline.tb.diffsim import compare as txn_compare
            txn = txn_compare(ra, rb)
            results.append({"mode": mode, "seed": seed, **cmp, "txn": txn,
                            "outputs": {s: len(v) for s, v in ra["outputs"].items()}})
            if cmp["status"] == "differ" or (txn and cmp["status"] != "reset_only"):
                return {"module": name, "status": "differ", "mode": mode, "seed": seed, "first": cmp["first"], "txn": txn,
                        "runs": results}
            if cmp["status"] == "reset_only":
                status = "reset_only"
    out = {"module": name, "status": status, "runs": results}
    firsts = [r["first"] for r in results if r["first"]]
    if firsts:
        out["first"] = firsts[0]
    return out


def _sub_lib(name: str, lib: dict[str, dict]) -> dict[str, dict]:
    out, todo = {}, [name]
    while todo:
        n = todo.pop()
        if n in out or n not in lib:
            continue
        out[n] = lib[n]
        todo += [i["module"] for i in lib[n].get("instances", [])]
    return out


def run_all(order: list[str], lib: dict[str, dict], r1: dict, work: Path, cells: list[Path], **kw) -> dict:
    """하위 모듈부터. 한 모듈이 다르면 그 위 모듈은 건너뛴다 (원인이 아래에 있으므로)."""
    out = {}
    bad = set()
    for name in order:
        deps = set(_sub_lib(name, lib)) - {name}
        if deps & bad:
            out[name] = {"module": name, "status": "skipped", "reason": f"하위 모듈 불일치: {sorted(deps & bad)}"}
            bad.add(name)
            continue
        res = run_module(name, lib, r1, work, cells, **kw)
        out[name] = res
        if res["status"] in ("differ", "error"):
            bad.add(name)
    return out
