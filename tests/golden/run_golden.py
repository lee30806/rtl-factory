"""이식 시험(P1) 실행기. 손으로 옮긴 IR을 방출하고 원본 블록의 기존 테스트벤치로 판정한다.

블록 디렉터리마다 `golden.json`이 있다.
    {"ir": ["ir/a.ir.json", ...],            # 손으로 옮긴 IR
     "cells": ["../../../pipeline/emitter/stubs/sync_2ff.sv"],   # 표준 셀 (선택)
     "runs": [{"top": "a", "tb": "test_a",  # 원본 테스트벤치 모듈 (upstream/tb/)
               "env": {...}}],              # 원본 테스트벤치가 읽는 환경 변수
     "upstream_rtl": ["upstream/rtl/a.v"],   # 원시 블록 비중의 비교 기준
     "upstream_params": {...},               # 기준선 실행 때 원본 RTL에 줄 파라미터
     "lint": ["top"]}                        # 원본 테스트벤치가 없어 E1만 보는 모듈 (예: 계층 top)

    .venv/bin/python tests/golden/run_golden.py [블록 ...] [--baseline] [-o build/golden]

순서: Validator(V1~V10) → 방출 → E1 lint → E2 sim(원본 테스트벤치). 결과는 블록별 JSON과 표로 낸다.
--baseline은 같은 테스트벤치를 원본 RTL에 돌린다 (테스트벤치 자체가 이 환경에서 통과하는지 확인).
--harness는 L1에서 생성한 하니스와 손으로 쓴 참조 모델(`harness/`)로 T1~T5를 돌린다 (golden.json의 "harness").
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from pipeline.emitter.sv import emit  # noqa: E402
from pipeline.ir.model import load_library  # noqa: E402
from pipeline.validator.checks import validate  # noqa: E402
from pipeline.verify.run import lint, sim  # noqa: E402

TIMESCALE = ("1ns", "1ps")


def _code_lines(text: str) -> int:
    """주석과 빈 줄을 뺀 줄 수."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return sum(1 for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("//"))


def raw_ratio(lib, texts: dict[str, str]) -> dict:
    """원시 블록 비중: 방출 RTL 전체 줄 수 대비 comb/seq body 줄 수."""
    body = sum(_code_lines(b["body"]) for m in lib.values() for b in m.blocks if b["kind"] in ("comb", "seq"))
    total = sum(_code_lines(t) for t in texts.values())
    return {"body_lines": body, "rtl_lines": total, "ratio": round(body / total, 3) if total else None}


def run_block(name: str, out: Path, baseline: bool = False, ir_override: dict[str, dict] | None = None) -> dict:
    """ir_override: 모듈 이름 → IR. 손으로 옮긴 IR 대신 쓴다 (에이전트가 쓴 IR 평가용)."""
    d = HERE / name
    cfg = json.loads((d / "golden.json").read_text())
    bout = out / name / ("baseline" if baseline else "ir")
    bout.mkdir(parents=True, exist_ok=True)
    cells = [(d / c).resolve() for c in cfg.get("cells", [])]
    res: dict = {"block": name, "mode": "baseline" if baseline else "ir"}
    if baseline:
        files = [(d / f).resolve() for f in cfg["upstream_rtl"]]
    else:
        lib = load_library([d / f for f in cfg["ir"]])
        if ir_override:
            from pipeline.ir.model import Module

            lib.update({n: Module(r) for n, r in ir_override.items()})
        issues = {n: [str(i) for i in validate(m, lib)] for n, m in lib.items()}
        res["validator"] = issues
        if any(v for v in issues.values() if any("경고" not in x for x in v)):
            res["ok"] = False
            return res
        texts = {}
        for m in lib.values():
            if m.role == "external":
                continue
            e = emit(m, lib)
            (bout / f"{m.name}.sv").write_text(e.text)
            (bout / f"{m.name}.srcmap.json").write_text(json.dumps(e.sourcemap, indent=1))
            texts[m.name] = e.text
        files = cells + [bout / f"{n}.sv" for n in texts]
        res["raw_ratio"] = raw_ratio(lib, texts)
        res["upstream_lines"] = sum(_code_lines((d / f).read_text()) for f in cfg["upstream_rtl"])
        res["e1"] = {}
        for top in sorted({r["top"] for r in cfg["runs"]} | set(cfg.get("lint", []))):
            r = lint(files, top)
            res["e1"][top] = [f"{f.rule} {Path(f.file).name if f.file else ''}:{f.line} {f.message}" for f in r.findings]
    res["e2"] = []
    for run in cfg["runs"]:
        params = cfg.get("upstream_params", {}).get(run["top"], {}) if baseline else {}
        env = {**run.get("env", {}), **{f"PARAM_{k}": str(v) for k, v in params.items()}}
        build_args = [f"-G{k}={v}" for k, v in params.items()] + (["-Wno-fatal"] if baseline else [])
        r = sim(files, run["top"], run["tb"], d / "upstream" / "tb", bout / f'sim_{run["top"]}_{run["tb"]}',
                build_args=build_args, env=env, timescale=TIMESCALE)
        xml = sorted((bout / f'sim_{run["top"]}_{run["tb"]}').glob("results.*.xml"))
        total = len(re.findall(r"<testcase ", xml[0].read_text())) if xml else 0
        res["e2"].append({"top": run["top"], "tb": run["tb"], "ok": r.ok, "tests": total,
                          "failed": [f"{f.test}: {f.rule}: {f.message[:200]}" for f in r.findings]})
    res["ok"] = all(x["ok"] for x in res["e2"])
    return res


def run_harness(name: str, out: Path, mutate=None, seeds: list[int] | None = None, coverage: bool = True,
                tb: Path | None = None) -> dict:
    """생성 하니스 + 참조 모델로 T1~T5. 내부 신호 목록은 Verifier가 보지 않는 디렉터리에 둔다.
    mutate(raw)는 대상 모듈의 IR을 고친다 (버그 주입 시험용). 결과의 "out"에 결과 디렉터리가 있다.
    tb를 주면 손으로 쓴 테스트벤치 대신 그 파일(에이전트가 쓴 cocotb 테스트 모듈)로 돌린다."""
    import copy

    from pipeline.ir.model import Module
    from pipeline.tb.gen import harness_source, trace_names
    from pipeline.tb.report import fsm_coverage, req_coverage

    d = HERE / name
    cfg = json.loads((d / "golden.json").read_text())
    hc = cfg["harness"]
    bout = out / name / "harness"
    (bout / "verifier").mkdir(parents=True, exist_ok=True)
    (bout / "verify_only").mkdir(parents=True, exist_ok=True)
    lib = load_library([d / f for f in cfg["ir"]])
    if mutate:
        raw = copy.deepcopy(lib[hc["top"]].raw)
        mutate(raw)
        lib[hc["top"]] = Module(raw)
    files = [(d / c).resolve() for c in cfg.get("cells", [])]
    for m in lib.values():
        if m.role != "external":
            (bout / f"{m.name}.sv").write_text(emit(m, lib).text)
            files.append(bout / f"{m.name}.sv")
    top = lib[hc["top"]]
    (bout / "verifier" / f"{top.name}_harness.py").write_text(harness_source(top))
    trace = bout / "verify_only" / f"{top.name}_trace.json"
    trace.write_text(json.dumps(trace_names(top)))
    seeds = seeds or hc.get("seeds", [1, 2])
    tb_dir, tb_mod = (Path(tb).parent, Path(tb).stem) if tb else (d / "harness", hc["tb"])
    r = sim(files, top.name, tb_mod, tb_dir, bout / "sim", seeds=seeds, harness_dir=bout / "verifier",
            trace=trace, timescale=TIMESCALE, coverage=coverage)
    tb_out = bout / "sim" / "tb_out" / str(seeds[0])
    results = [json.loads(p.read_text()) for p in sorted(tb_out.glob("*.result.json"))]
    traces = [json.loads(p.read_text()) for p in sorted(tb_out.glob("*.trace.json"))]
    spec = (d / hc["spec"]).read_text(encoding="utf-8")
    t1 = [f for f in r.findings if f.rule == "T1_INIT"]
    return {
        "block": name, "ok": r.ok, "tests": len(results), "seeds": seeds, "out": str(tb_out),
        "findings": [{"test": f.test, "rule": f.rule} for f in r.findings],
        "T1": "pass" if not t1 else t1[0].message,
        "T2": sum(len(x["protocol"]) for x in results),
        "T3_matched": sum(x["matched"] for x in results),
        "T3_failures": [x["scoreboard"] for x in results if x["scoreboard"]],
        "T4": sum(len(x["properties"]) for x in results),
        "T5": {"requirements": req_coverage(results, spec), "fsm": fsm_coverage(top, traces),
               "verilator": {k: {"ratio": v["ratio"], "points": v["points"]} for k, v in (r.coverage or {}).items()}},
        "failed": [f"{f.test}: {f.rule}: {f.message[:200]}" for f in r.findings],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="이식 시험 실행기")
    ap.add_argument("blocks", nargs="*")
    ap.add_argument("--baseline", action="store_true", help="원본 RTL에 원본 테스트벤치를 돌린다")
    ap.add_argument("--harness", action="store_true", help="생성 하니스와 참조 모델로 T1~T5")
    ap.add_argument("-o", "--out", default=str(ROOT / "build" / "golden"))
    args = ap.parse_args(argv)
    if args.harness:
        names = args.blocks or sorted(p.parent.name for p in HERE.glob("*/golden.json")
                                      if "harness" in json.loads(p.read_text()))
        res = [run_harness(b, Path(args.out)) for b in names]
        (Path(args.out) / "harness.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))
        for x in res:
            t5 = x["T5"]
            cov = ", ".join(f'{k} {v["ratio"]}' for k, v in sorted(t5["verilator"].items()))
            fsm = "; ".join(f'{k} 미커버 {v["uncovered"]}' for k, v in t5["fsm"].items())
            print(f'{x["block"]:18} {"PASS" if x["ok"] else "FAIL"} | 테스트 {x["tests"]} × 시드 {len(x["seeds"])} | '
                  f'T1 {x["T1"]} | T2 위반 {x["T2"]} | T3 일치 {x["T3_matched"]} | T4 위반 {x["T4"]} | '
                  f'요구사항 {len(t5["requirements"]["covered"])}/{t5["requirements"]["total"]} | {cov} | {fsm}')
            for f in x["failed"][:5]:
                print("    " + f)
        return 0 if all(x["ok"] for x in res) else 1
    blocks = args.blocks or sorted(p.parent.name for p in HERE.glob("*/golden.json"))
    out = Path(args.out)
    results = [run_block(b, out, args.baseline) for b in blocks]
    (out / ("baseline.json" if args.baseline else "results.json")).write_text(json.dumps(results, indent=1, ensure_ascii=False))
    for r in results:
        e2 = ", ".join(f'{x["top"]}/{x["tb"]}: {x["tests"] - len(x["failed"])}/{x["tests"]}' for x in r.get("e2", []))
        extra = ""
        if "raw_ratio" in r:
            extra = f' | raw {r["raw_ratio"]["ratio"]} | E1 {sum(len(v) for v in r["e1"].values())}건'
        print(f'{r["block"]:18} {"PASS" if r["ok"] else "FAIL"} | {e2}{extra}')
        for n, v in r.get("validator", {}).items():
            for i in v:
                print(f"    {n}: {i}")
        for x in r.get("e2", []):
            for f in x["failed"][:5]:
                print(f"    {x['tb']}: {f}")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
