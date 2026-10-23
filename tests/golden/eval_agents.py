"""P4 에이전트 단독 평가 (golden 블록). 실제 coding agent를 부르므로 CI에서는 돌리지 않는다.

    .venv/bin/python tests/golden/eval_agents.py author [블록 ...] [-o build/agents]

author: golden IR의 동결 L1과 block_spec.md만 주고 L2, L3를 쓰게 한 뒤, 방출 RTL을 원본 테스트벤치로 판정한다.
repair: golden IR에 버그를 넣고(bugs.py) 생성 하니스로 실패시킨 뒤, 실패 리포트로 Repair가 낸 patch를 적용해
        하니스와 원본 테스트벤치로 다시 판정한다. 최대 2번 반복한다.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import run_golden  # noqa: E402

from bugs import BUGS  # noqa: E402

from pipeline.agents import arbiter, architect, author, repair, roles, verifier  # noqa: E402
from pipeline.agents.task import RUNS, run_task  # noqa: E402
from pipeline.ir.model import frozen_l1  # noqa: E402

AGENT_BLOCKS = ["axis_adapter", "axis_cobs_decode", "axis_async_fifo"]
# 에이전트 작업 디렉터리는 저장소 밖에 둔다 (격리). 평가 요약만 저장소의 build/agents에 쓴다.
WORK = RUNS / f"eval-{time.strftime('%Y%m%d-%H%M%S')}-{__import__('os').getpid()}"


def eval_author(block: str, out: Path, cand: int = 0) -> dict:
    r = roles.load()
    cfg = json.loads((HERE / block / "golden.json").read_text())
    golden = json.loads((HERE / block / cfg["ir"][0]).read_text())
    l1 = frozen_l1(golden)
    task = author.make_task(f"author/{block}/c{cand}", l1, HERE / block / "block_spec.md")
    work = WORK / block / f"author_c{cand}"
    res = run_task(r.agents["author"], task, work, check=author.check(l1), retries=r.limits.get("agent_retries", 1),
                   run_root=RUNS)
    rec = {"block": block, "candidate": cand, "agent": res.agent, "ok": res.ok, "attempts": res.attempts,
           "duration_s": res.duration_s, "errors": res.errors[:10], "violations": res.violations}
    if res.ok:
        ir = json.loads(Path(res.outputs[author.out_rel(l1["module"])]).read_text())
        rec["identical_to_golden"] = ir == golden
        g = run_golden.run_block(block, out / block / f"tb_c{cand}", ir_override={l1["module"]: ir})
        rec["tb"] = [{"tb": x["tb"], "passed": x["tests"] - len(x["failed"]), "tests": x["tests"], "failed": x["failed"][:5]}
                     for x in g.get("e2", [])]
        rec["tb_ok"] = g["ok"]
        rec["e1"] = {k: len(v) for k, v in g.get("e1", {}).items()}
        rec["validator"] = g.get("validator")
    (out / block).mkdir(parents=True, exist_ok=True)
    (out / block / f"author_c{cand}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1))
    return rec


def _failure(block: str, raw: dict, h: dict) -> dict:
    """하니스 결과 → Repair에 줄 실패 리포트 (분류 + 불일치 리포트)."""
    from pipeline.classifier.classify import classify_findings
    from pipeline.ir.model import Module, load_library
    from pipeline.tb.report import mismatch_report
    from pipeline.verify.run import Finding

    out = Path(h["out"])
    test = next(f["test"] for f in h["findings"] if f["test"])
    cfg = json.loads((HERE / block / "golden.json").read_text())
    lib = load_library([HERE / block / f for f in cfg["ir"]])
    rep = mismatch_report(Module(raw), json.loads((out / f"{test}.result.json").read_text()),
                          json.loads((out / f"{test}.trace.json").read_text()),
                          (HERE / block / "block_spec.md").read_text(), lib)
    cls = classify_findings([Finding("cocotb", f["rule"], None, None, "", test=f["test"]) for f in h["findings"]], {}, {})
    return {"classified": [{"type": c.type, "test": c.source.get("test")} for c in cls], "report": rep}


def eval_repair(bug: str, out: Path, rounds: int = 2) -> dict:
    import copy

    from pipeline.patch.apply import apply

    r = roles.load()
    block, mutate, cause = BUGS[bug]
    cfg = json.loads((HERE / block / "golden.json").read_text())
    raw = copy.deepcopy(json.loads((HERE / block / cfg["ir"][0]).read_text()))
    mutate(raw)
    rec = {"bug": bug, "block": block, "cause": cause, "rounds": []}
    history: list = []
    for n in range(rounds):
        cur = copy.deepcopy(raw)
        h = run_golden.run_harness(block, out / bug / f"r{n}", mutate=lambda x, c=cur: (x.clear(), x.update(c)),
                                   seeds=[1], coverage=False)
        if h["ok"]:
            break
        failure = _failure(block, raw, h)
        task = repair.make_task(f"repair/{bug}/r{n}", raw, HERE / block / "block_spec.md", failure, history)
        res = run_task(r.agents["repair"], task, WORK / bug / f"repair_r{n}", check=repair.check(raw),
                       retries=r.limits.get("agent_retries", 1), run_root=RUNS)
        step = {"round": n, "ok": res.ok, "attempts": res.attempts, "duration_s": res.duration_s,
                "violations": res.violations, "errors": res.errors[:5]}
        rec["rounds"].append(step)
        if not res.ok:
            break
        patches = json.loads(Path(res.outputs[repair.OUT]).read_text())
        step["targets"] = sorted({p.get("target") for p in patches if isinstance(p, dict)})
        step["hit_cause"] = any(t and (cause.startswith(t) or t.startswith(cause)) for t in step["targets"])
        applied = apply(raw, patches)
        raw = applied.ir
        history += patches
    h = run_golden.run_harness(block, out / bug / "final", mutate=lambda x, c=copy.deepcopy(raw): (x.clear(), x.update(c)),
                               seeds=[1], coverage=False)
    g = run_golden.run_block(block, out / bug / "final_tb", ir_override={raw["module"]: raw})
    rec.update(harness_ok=h["ok"], tb_ok=g["ok"],
               tb=[{"tb": x["tb"], "passed": x["tests"] - len(x["failed"]), "tests": x["tests"]} for x in g.get("e2", [])],
               violations=sum(len(s["violations"]) for s in rec["rounds"]))
    (out / f"repair_{bug}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1))
    return rec


def eval_verifier(block: str, out: Path, cand: int = 0) -> dict:
    import copy

    r = roles.load()
    cfg = json.loads((HERE / block / "golden.json").read_text())
    golden = json.loads((HERE / block / cfg["ir"][0]).read_text())
    l1 = frozen_l1(golden)
    task = verifier.make_task(f"verifier/{block}/c{cand}", l1, HERE / block / "block_spec.md")
    leaked = [k for k in ("signals", "blocks", "types") if k in json.loads(task.inputs["l1.json"])]
    res = run_task(r.agents["verifier"], task, WORK / block / f"verifier_c{cand}", check=verifier.check(l1),
                   retries=r.limits.get("agent_retries", 1), run_root=RUNS)
    rec = {"block": block, "agent": res.agent, "ok": res.ok, "attempts": res.attempts, "duration_s": res.duration_s,
           "errors": res.errors[:5], "violations": res.violations, "isolation_leak": leaked}
    if res.ok:
        tb = Path(res.outputs[verifier.out_rel(l1["module"])])
        g = run_golden.run_harness(block, out / block / f"vgolden_c{cand}", seeds=[1, 2], coverage=True, tb=tb)
        rec["golden"] = {"ok": g["ok"], "tests": g["tests"], "T3_matched": g["T3_matched"], "failed": g["failed"][:5],
                         "requirements": g["T5"]["requirements"], "line": g["T5"]["verilator"].get("line")}
        rec["bugs"] = {}
        for bug, (b, mutate, _) in BUGS.items():
            if b == block:
                h = run_golden.run_harness(block, out / block / f"vbug_{bug}", mutate=mutate, seeds=[1], coverage=False, tb=tb)
                rec["bugs"][bug] = "검출" if not h["ok"] else "놓침"
    (out / block).mkdir(parents=True, exist_ok=True)
    (out / block / f"verifier_c{cand}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1))
    return rec


def eval_architect(block: str, out: Path) -> dict:
    from pipeline.spec.l1 import block_top_l1
    from pipeline.spec.parse import load

    r = roles.load()
    spec = HERE / block / "block_spec.md"
    top_l1, _ = block_top_l1(load(spec))
    task = architect.make_task(f"architect/{block}", top_l1, spec)
    work = WORK / block / "architect"
    res = run_task(r.agents["architect"], task, work, check=architect.check(top_l1), retries=r.limits.get("agent_retries", 1),
                   run_root=RUNS)
    rec = {"block": block, "ok": res.ok, "attempts": res.attempts, "duration_s": res.duration_s, "errors": res.errors[:5],
           "violations": res.violations}
    if res.ok:
        top_ir, children, assumptions = architect.read_tree(work, top_l1["module"])
        def ports(ir):
            return sorted(p.get("bundle") or p["name"] for p in ir["ports"])
        rec.update(children=sorted(children), instances=len(top_ir.get("instances", [])),
                   assumptions=assumptions, golden_modules=1,
                   child_matches_top=[n for n, c in children.items() if ports(c) == ports(top_ir)])
    (out / block).mkdir(parents=True, exist_ok=True)
    (out / block / "architect.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1))
    return rec


ARBITER_CASES = {
    # 기댓값을 일부러 틀림: 출력 바이트를 높은 레인부터 낸다고 기대 (SPEC ADP-01은 낮은 레인부터)
    "adapter_wrong_test": ("axis_adapter", None, "test_wrong",
                           lambda src: src.replace("for i in range(n):", "for i in reversed(range(n)):")),
    # RTL 버그: FSM 전이 조건 (bugs.py)
    "cobs_rtl_bug": ("axis_cobs_decode", "cobs_transition", "rtl_wrong", None),
}


def eval_arbiter(case: str, out: Path) -> dict:
    import copy

    from pipeline.ir.model import Module, load_library
    from pipeline.tb.report import mismatch_report

    r = roles.load()
    block, bug, expect, edit_tb = ARBITER_CASES[case]
    cfg = json.loads((HERE / block / "golden.json").read_text())
    tb_src = (HERE / block / "harness" / f'{cfg["harness"]["tb"]}.py').read_text()
    tb = out / case / "tb" / f'{cfg["harness"]["tb"]}.py'
    tb.parent.mkdir(parents=True, exist_ok=True)
    tb.write_text(edit_tb(tb_src) if edit_tb else tb_src)
    mutate = BUGS[bug][1] if bug else None
    h = run_golden.run_harness(block, out / case / "sim", mutate=mutate, seeds=[1], coverage=False, tb=tb)
    rec = {"case": case, "expect": expect, "sim_failed": not h["ok"]}
    if h["ok"]:
        rec["error"] = "불일치가 나지 않음"
    else:
        raw = copy.deepcopy(json.loads((HERE / block / cfg["ir"][0]).read_text()))
        if mutate:
            mutate(raw)
        test = next(f["test"] for f in h["findings"] if f["test"])
        o = Path(h["out"])
        lib = load_library([HERE / block / f for f in cfg["ir"]])
        rep = mismatch_report(Module(raw), json.loads((o / f"{test}.result.json").read_text()),
                              json.loads((o / f"{test}.trace.json").read_text()), (HERE / block / "block_spec.md").read_text(), lib)
        spec = HERE / block / "block_spec.md"
        res = run_task(r.agents["arbiter"], arbiter.make_task(f"arbiter/{case}", spec, rep, tb), WORK / case / "arbiter",
                       check=arbiter.check(spec), retries=r.limits.get("agent_retries", 1), run_root=RUNS)
        rec.update(ok=res.ok, duration_s=res.duration_s, errors=res.errors[:5], violations=res.violations)
        if res.ok:
            v = json.loads(Path(res.outputs[arbiter.OUT]).read_text())
            rec.update(verdict=v["verdict"], reqs=v.get("reqs"), reason=v.get("reason"), proposal=v.get("proposal"),
                       correct=v["verdict"] == expect)
    (out / f"arbiter_{case}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1))
    return rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("agent", choices=["author", "repair", "verifier", "architect", "arbiter"])
    ap.add_argument("blocks", nargs="*")
    ap.add_argument("-o", "--out", default=str(ROOT / "build" / "agents"))
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    default = {"repair": sorted(BUGS), "arbiter": sorted(ARBITER_CASES)}.get(a.agent, AGENT_BLOCKS)
    for b in a.blocks or default:
        t0 = time.time()
        rec = {"author": eval_author, "repair": eval_repair, "verifier": eval_verifier, "architect": eval_architect,
               "arbiter": eval_arbiter}[a.agent](b, out)
        print(json.dumps(rec, ensure_ascii=False)[:1500], f"({time.time() - t0:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
