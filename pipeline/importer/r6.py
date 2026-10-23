"""P11 R6: SPEC 충실도 검사. SPEC만으로 다시 만든 설계(재생성)와 원본 RTL을 비교한다.

- 동작: 같은 시드 자극(`diffsim`, steady/reset, 시드 여럿)에서 출력 트랜잭션 순서를 비교한다. 지연이 달라도 SPEC을 지키는
  설계는 같은 순서를 내므로 사이클 단위로 보지 않는다. 차이는 `minimize_diff`로 줄인다.
  리셋 도메인이 둘 이상이면 reset 모드는 모든 도메인에 리셋을 함께 걸고 마지막 리셋 뒤 출력만 비교한다 (`joint_reset`,
  2026-10-22 사용자 결정: 한쪽만 리셋과 두 클럭 사이 리셋 전달 사이클은 SPEC이 정하지 않는다).
- 타이밍: 같은 실행에서 스트림마다 같은 순번 트랜잭션이 나온 사이클의 차이(재생성 - 원본)를 표로 낸다.
- 구조: 재생성 모듈 트리(Architect가 정함)와 원본 트리를 리포트로만 낸다.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.importer import r4
from pipeline.tb.diffsim import compare, describe

SEEDS = ((("steady", 1), ("steady", 2), ("reset", 3), ("reset", 4)))


def _regen_files(regen_ir: dict[str, dict], work: Path, cells: list[Path]) -> list[Path]:
    from pipeline.scheduler.run import emit_lib
    files, _ = emit_lib(regen_ir, work / "regen_rtl", cells)
    return files


def _ref(r1: dict, top_ir: dict, work: Path) -> tuple[list[Path], str, list[str]]:
    files, args = r4.ref_sources(r1, work)
    renames = r1["modules"].get(top_ir["module"], {}).get("renames", {})
    params = r4.ref_params(top_ir, r1)
    wrap, top = r4.ref_wrapper(top_ir, renames, work, params)
    if wrap:
        return [*files, wrap], top, args
    return files, top, [*args, *[f"-G{k}={v}" for k, v in params.items()]]


def timing(a: dict, b: dict) -> list[dict]:
    """스트림마다 같은 순번 트랜잭션의 사이클 차이 (b - a)."""
    out = []
    for s, xa in a["outputs"].items():
        xb = b["outputs"].get(s, [])
        deltas = [tb[1] - ta[1] for ta, tb in zip(xa, xb)]
        if not deltas:
            out.append({"stream": s, "n_ref": len(xa), "n_regen": len(xb)})
            continue
        out.append({"stream": s, "n_ref": len(xa), "n_regen": len(xb), "first_ref": xa[0][1], "first_regen": xb[0][1],
                    "delta_min": min(deltas), "delta_max": max(deltas), "delta_mean": round(sum(deltas) / len(deltas), 2)})
    return out


def plan_opts(h: dict) -> dict:
    """리셋 도메인이 둘 이상이면 리셋을 함께 건다."""
    return {"joint_reset": True} if len(h["resets"]) > 1 else {}


def run(top_ir: dict, regen_ir: dict[str, dict], r1: dict, work: Path, cells: list[Path], cycles: int = 3000,
        minimize: bool = True, seeds=SEEDS, budget: int = 80) -> dict:
    """→ {"status": "equal"|"differ"|"error", "runs": [...], "timing": [...], "difference": {...}, "structure": {...}}"""
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    ref_files, ref_top, ref_args = _ref(r1, top_ir, work)
    reg_files = _regen_files(regen_ir, work, cells)
    iw, regen_top = r4.ir_wrapper(regen_ir[top_ir["module"]], work)   # inout이 있으면 패드 래퍼
    if iw:
        reg_files = [*reg_files, iw]
    from pipeline.tb.gen import harness_config, pad_view
    from pipeline.ir.model import Module
    h = harness_config(Module(pad_view(top_ir)))
    opts = plan_opts(h)
    runs, first_diff = [], None
    for k, (mode, seed) in enumerate(seeds):
        ra, ea = r4.run_one(ref_files, ref_top, top_ir, mode, seed, work / "ref", [*ref_args, "-Wno-fatal"], {}, cycles,
                            rebuild=k == 0, opts=opts)
        rb, eb = r4.run_one(reg_files, regen_top, top_ir, mode, seed, work / "regen", ["-Wno-fatal"], {}, cycles,
                            rebuild=k == 0, opts=opts)
        if ra is None or rb is None:
            return {"status": "error", "error": ea if ra is None else eb, "mode": mode, "seed": seed}
        d = compare(ra, rb)
        runs.append({"mode": mode, "seed": seed, "equal": d is None, "timing": timing(ra, rb)})
        if d and first_diff is None:
            first_diff = {"mode": mode, "seed": seed, "difference": d}
    res = {"status": "equal" if first_diff is None else "differ", "runs": runs, "timing": runs[0]["timing"],
           "structure": structure(r1, regen_ir, top_ir["module"])}
    if first_diff and minimize:
        res["minimized"] = minimize_r6(ref_files, ref_top, ref_args, reg_files, regen_top, top_ir, first_diff["mode"],
                                       first_diff["seed"], work / "min", h, budget)
    if first_diff:
        res["difference"] = first_diff
    return res


def minimize_r6(ref_files, ref_top, ref_args, reg_files, regen_top, top_ir, mode, seed, work: Path, h: dict,
                budget: int = 80) -> dict | None:
    from pipeline.scheduler.run import minimize_diff

    def sim_fn(c, plan, rebuild):
        if c == 0:
            rec, _ = r4.run_one(ref_files, ref_top, top_ir, mode, seed, work / "ref", [*ref_args, "-Wno-fatal"], {}, 0, rebuild, plan)
        else:
            rec, _ = r4.run_one(reg_files, regen_top, top_ir, mode, seed, work / "regen", ["-Wno-fatal"], {}, 0, rebuild, plan)
        return rec
    m = minimize_diff({0: ref_files, 1: reg_files}, top_ir, mode, seed, work, None, None, budget=budget, sim_fn=sim_fn,
                      plan_opts=plan_opts(h))
    if m is None:
        return None
    m["stimulus"] = describe(m["plan"], h)
    acc = (m.get("inputs_accepted") or {}).get("A") or {}
    m["inputs"] = {b: m["plan"]["queues"][b][:min(n + 1, 64)] for b, n in acc.items() if b in m["plan"]["queues"]}
    return m


def judge_input(res: dict) -> dict:
    """Arbiter에게 줄 r6.json (원본 = a/ref, 재생성 = b/regen)."""
    m = res.get("minimized") or {}
    d = m.get("difference") or (res.get("difference") or {}).get("difference")
    return {"stimulus": m.get("stimulus", []), "inputs": m.get("inputs", {}), "difference": d,
            "inputs_accepted": m.get("inputs_accepted"),
            "mode": (res.get("difference") or {}).get("mode"), "timing": res.get("timing")}


def structure(r1: dict, regen_ir: dict[str, dict], top: str) -> dict:
    def tree(lib: dict[str, dict], name: str) -> dict:
        ir = lib.get(name, {})
        return {i["name"]: {"module": i["module"], "children": tree(lib, i["module"])} for i in ir.get("instances", [])}
    return {"original_modules": r1["order"], "regen_modules": sorted(regen_ir), "regen_tree": tree(regen_ir, top)}


def load_regen(run_dir: Path) -> dict[str, dict]:
    return {p.name.removesuffix(".ir.json"): json.loads(p.read_text(encoding="utf-8"))
            for p in sorted((Path(run_dir) / "ir").glob("*.ir.json"))}


def verifier_on_ref(tbs: list[str], top_ir: dict, r1: dict, work: Path) -> list[dict]:
    """재생성 실행에서 Verifier가 SPEC만 보고 쓴 테스트를 원본 RTL에 돌린다. 실패한 테스트마다 하니스가 남긴 자극
    (`pl.py refsim` 형식)을 `work/stim/`에 두고 `stim`에 그 경로를 적는다 (SpecWriter가 원본에 다시 넣어 봄)."""
    from pipeline.ir.model import Module
    from pipeline.tb.gen import harness_source
    from pipeline.tb.lib import _safe
    from pipeline.verify.run import sim

    work = Path(work)
    hdir = work / "harness"
    hdir.mkdir(parents=True, exist_ok=True)
    (hdir / f"{top_ir['module']}_harness.py").write_text(harness_source(Module(top_ir)), encoding="utf-8")
    files, top, args = _ref(r1, top_ir, work)
    out = []
    for k, tb in enumerate(tbs):
        r = sim(files, top, Path(tb).stem, Path(tb).parent, work / f"sim_v{k}", build_args=[*args, "-Wno-fatal"], harness_dir=hdir)
        findings = [{"test": f.test, "rule": f.rule, "message": f.message[:300]} for f in r.findings][:10]
        for f in findings:
            res = next((work / f"sim_v{k}" / "tb_out").glob(f"*/{_safe(f['test'])}.result.json"), None)
            stim = json.loads(res.read_text(encoding="utf-8")).get("stimulus") if res else None
            if stim:
                (work / "stim").mkdir(exist_ok=True)
                f["stim"] = str(work / "stim" / f"v{k}_{_safe(f['test'])}.json")
                Path(f["stim"]).write_text(json.dumps(stim), encoding="utf-8")
        out.append({"verifier": k, "ok": r.ok, "findings": findings})
    return out
