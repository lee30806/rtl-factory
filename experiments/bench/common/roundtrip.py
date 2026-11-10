"""B5 왕복 실행: 원본 RTL → P11(R1 → R2 무인 응답 → R3·R4 Porter → R5 SpecWriter → R6 SPEC만으로 재생성)
→ 회차마다 재생성 RTL을 원본과 Yosys 등가로 채점한다.

R6의 원본 비교(트랜잭션 시뮬레이션)는 파이프라인 안의 판단이고, 등가 채점은 그와 별개인 벤치 판정이다.
첫 회차 결과가 1회 왕복(`bench_pass_chosen`), 마지막 회차 결과가 R6 SPEC 보정 뒤(`bench_pass_chosen_final`)다.
원본 RTL은 Porter와 SpecWriter만 보고, 재생성 실행(Run)은 SPEC만 본다.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

from experiments.bench.common.runner import _strip_code_fences, evaluate_rtl, extract_candidate_rtl, run_and_score
from experiments.bench.common.schema import BenchItem, BenchResult
from pipeline.agents import roles as roles_mod
from pipeline.importer import r6
from pipeline.agents.task import RUNS, QuotaExceeded, Task, run_task
from pipeline.importer.sources import Inputs
from pipeline.scheduler.port import PortRun
from pipeline.scheduler.run import STUB_CELLS


# 단독 왕복: 논문 Task 1의 두 프롬프트(rtlbenchls/runner.py T1_*)를 파일 입출력으로 옮긴 것. 같은 모델이 두 번 부른다.
# 논문은 SPEC 길이를 원본 토큰 수 이하로 자르지만(max_tokens), CLI 에이전트에는 그 상한을 걸 수 없어 두지 않는다.
SPEC_TASK = """\
# Task: Write a plain-English specification

You are an expert hardware design engineer. Describe the behavior, inputs, outputs, and sub-modules of the Verilog module `{top}`
in the files under `rtl/`. Write a plain-English specification to `out/spec.md`.
Do NOT include any Verilog or code in your response. Only create or modify files under `out/`.
"""
REGEN_TASK = """\
# Task: Generate the Verilog module `{top}`

You are an expert Verilog RTL designer. Generate the Verilog module `{top}` based on the specification in `spec.md`
and write it to `out/{top}.sv`. Output only the Verilog source: no explanation, no markdown. Only create or modify files under `out/`.
"""


def run_roundtrip_standalone(item: BenchItem, out_dir: Path, roles: roles_mod.Roles, isolate: bool = True,
                             resume: bool = False) -> BenchResult:
    """하니스 없는 왕복: 원본 RTL → 자연어 SPEC → RTL (Author 모델 두 번) → Yosys 등가 채점."""
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    res_file = out_dir / "bench_result.json"
    if resume and res_file.exists():
        saved = json.loads(res_file.read_text(encoding="utf-8"))
        if saved.get("pipeline_status") != "paused_quota":
            return BenchResult(**saved)
    t0 = time.monotonic()
    cfg = roles.agents.get("author")
    key = hashlib.sha256(str(out_dir).encode()).hexdigest()[:8]
    work = RUNS / f"benchls-standalone-{item.id}-{key}"
    spec, sv, status, runs, errs = out_dir / "spec.md", out_dir / "rtl" / f"{item.top}.sv", "failed", 0, []
    try:
        if not spec.exists():
            t = Task("author", f"standalone/{item.id}/spec", SPEC_TASK.format(top=item.top),
                     inputs={f"rtl/{Path(f).name}": Path(f) for f in item.reference_rtl}, tools=False,
                     outputs=["out/spec.md"], write_scope=["out/"], isolate=isolate)
            r = run_task(cfg, t, work / "spec", retries=0, run_root=RUNS)
            runs += 1
            if "out/spec.md" in r.outputs:
                shutil.copy(r.outputs["out/spec.md"], spec)
            errs += r.errors
        if spec.exists():
            t = Task("author", f"standalone/{item.id}/regen", REGEN_TASK.format(top=item.top), inputs={"spec.md": spec},
                     tools=False, outputs=[f"out/{item.top}.sv"], write_scope=["out/"], isolate=isolate)
            r = run_task(cfg, t, work / "regen", retries=0, run_root=RUNS)
            runs += 1
            errs += r.errors
            if (o := r.outputs.get(f"out/{item.top}.sv")) and Path(o).stat().st_size:
                sv.parent.mkdir(parents=True, exist_ok=True)
                sv.write_text(_strip_code_fences(Path(o).read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
                status = "generated"
    except QuotaExceeded as e:
        status, errs = "paused_quota", [str(e)]
    ev: dict[str, Any] = {"ok": False, "skipped": "RTL 미생성"}
    if status == "generated":
        port = RUNS / f"benchls-{item.id}-{key}"   # 리셋은 R1으로 원본에서 찾는다 (에이전트 없음)
        if not (port / "r1" / "import.json").exists():
            PortRun(port).start(Inputs([Path(p) for p in item.reference_rtl], item.top))
        item.eval.lec_resets = lec_resets(port, item.top)
        ev = evaluate_rtl(item, [sv], out_dir / "eval" / "c0")
        status = "pass" if ev.get("ok") else status
    ok = bool(ev.get("ok"))
    res = BenchResult(benchmark=item.benchmark, id=item.id, category=item.category, top=item.top,
                      spec_gate_ok=spec.exists(), spec_errors=errs[:5], pipeline_status=status, chosen={item.top: 0} if ok else None,
                      agent_runs=runs, minutes=round((time.monotonic() - t0) / 60, 2),
                      candidates={"c0": {"lang": "sv_direct", "pipeline_ok": sv.exists(), "repairs": 0, "py_equiv": {}, "bench_eval": ev}},
                      bench_pass_chosen=ok, bench_pass_any=ok, bench_pass_chosen_final=ok, bench_pass_any_final=ok)
    res_file.write_text(json.dumps(res.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return res


def auto_answers(req: dict) -> dict[str, str]:
    """R2 무인 응답: 사람이 줄 정보가 원본에 없으므로 동작을 바꾸지 않는 쪽으로 고른다.
    주파수 100 MHz, 클럭 사이는 비동기, 묶음은 valid_ready로 보지 않음(원본 신호 그대로), 검토 항목은 그대로 진행,
    포트 도메인은 첫 후보 도메인."""
    out = {}
    for i in req["items"]:
        if i["answer"] not in (None, ""):
            continue
        k = i["kind"]
        if k == "freq":
            out[i["id"]] = "100"
        elif k == "relation":
            out[i["id"]] = "async"
        elif k == "protocol":
            out[i["id"]] = "none"
        elif k == "review":
            out[i["id"]] = i["options"][0] if i.get("port") else "ok"
    return out


def lec_resets(port_dir: Path, top: str) -> list[dict[str, str]]:
    p = Path(port_dir) / "r1" / "ir" / f"{top}.ir.json"
    if not p.exists():
        return []
    doms = json.loads(p.read_text(encoding="utf-8")).get("domains", [])
    seen = {}
    for d in doms:
        if r := d.get("reset"):
            seen[r["port"]] = {"port": r["port"], "active": r["active"]}
    return list(seen.values())


def _round_view(scored: dict[str, Any]) -> dict[str, Any]:
    rep = scored["rep"]
    return {"regen_status": rep.get("status"), "chosen": rep.get("chosen"), "agent_runs": rep.get("agent_runs", 0),
            "bench_pass_chosen": scored["bench_pass_chosen"], "bench_pass_any": scored["bench_pass_any"],
            "candidates": scored["candidates"], "review_codes": sorted({x.get("code") for x in rep.get("review", [])} - {None}),
            "auto_approvals": scored["auto_decs"]}


def run_roundtrip_item(item: BenchItem, out_dir: Path, roles: roles_mod.Roles, isolate: bool = True,
                       resume: bool = False, max_auto_approves: int = 3, skip_verifier: bool = False) -> BenchResult:
    """skip_verifier: 재생성 실행의 Verifier 에이전트를 생략하고 원본과의 Yosys 등가 채점을 E2 판정으로 쓴다.
    원본이 생성 루프의 판정에 들어가므로 SPEC만으로 재생성한 결과와는 따로 표기해야 한다 (정답 참조 조건)."""
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    res_file, rounds_file = out_dir / "bench_result.json", out_dir / "rounds.json"
    if resume and res_file.exists():
        saved = json.loads(res_file.read_text(encoding="utf-8"))
        if saved.get("pipeline_status") not in ("paused_quota",):
            return BenchResult(**saved)
    t0 = time.monotonic()
    # P11은 작업을 실행 디렉터리 아래에서 돌리므로 격리 규칙상 저장소 밖에 둔다. SPEC과 상태는 끝날 때 out_dir로 복사한다
    port_dir = RUNS / f"benchls-{item.id}-{hashlib.sha256(str(out_dir).encode()).hexdigest()[:8]}"
    rounds: dict[str, dict] = json.loads(rounds_file.read_text(encoding="utf-8")) if rounds_file.exists() else {}

    def regen_fn(spec: Path, regen_dir: Path):
        k = regen_dir.name.removeprefix("regen_")
        n_mod = max(1, len(json.loads((port_dir / "r1" / "import.json").read_text(encoding="utf-8"))["order"]) - 1)
        scored = run_and_score(item, regen_dir, out_dir / "eval" / f"round_{k}", spec, roles, STUB_CELLS, isolate=isolate,
                               max_auto_approves=max_auto_approves, max_modules=n_mod, skip_verifier=skip_verifier)
        rounds[k] = {"round": int(k), **_round_view(scored)}
        rounds_file.write_text(json.dumps(rounds, ensure_ascii=False, indent=1), encoding="utf-8")
        status = scored["rep"]["status"]
        if status == "paused_quota":
            # P11은 이 상태를 사람 결정 대기로 본다. 한도는 벤치가 --resume으로 이어 가도록 사용량 한도로 올린다
            raise QuotaExceeded(f"{item.id} 재생성 {k}회차: 사용량 한도")
        st = json.loads((regen_dir / "state.json").read_text(encoding="utf-8"))
        # Verifier를 생략하면 R6가 원본에 돌릴 재생성 테스트가 없다
        tbs = [] if skip_verifier else st["steps"].get("verifiers") or []
        return status, (r6.load_regen(regen_dir) if status == "pass" else {}), tbs, \
            scored["rep"].get("agent_runs", 0)

    pr = PortRun(port_dir, roles, isolate=isolate, regen_fn=regen_fn)
    if pr.state["stage"] == "r1":
        st = pr.start(Inputs([Path(p) for p in item.reference_rtl], item.top))
        if pr.state["status"] == "rejected":
            return _finish(item, out_dir, pr, rounds, t0, spec_errors=[r["message"] for r in st["review"]][:5])
    item.eval.lec_resets = lec_resets(port_dir, item.top)
    if pr.state["stage"] == "r2":
        req = json.loads((port_dir / "r2_request.json").read_text(encoding="utf-8"))
        out = pr.answer(auto_answers(req))
        if out.get("errors") or out.get("unanswered"):
            pr.state["status"] = "r2_failed"
            pr.save()
            return _finish(item, out_dir, pr, rounds, t0, spec_errors=[*out.get("errors", []), *out.get("unanswered", [])][:5])
    pr.resume()
    return _finish(item, out_dir, pr, rounds, t0)


def rescore_item(item: BenchItem, out_dir: Path) -> BenchResult | None:
    """회차별 후보 RTL을 지금의 방출기로 IR에서 다시 내고 지금의 등가 채점으로 다시 채점한다 (에이전트 호출 없음).
    채점 방식이나 방출기를 바꿨을 때 쓴다."""
    out_dir = Path(out_dir).resolve()
    res_file, rounds_file = out_dir / "bench_result.json", out_dir / "rounds.json"
    if not res_file.exists() or not (out_dir / "port" / "path.txt").exists():
        return None
    saved = json.loads(res_file.read_text(encoding="utf-8"))
    pr = PortRun(Path((out_dir / "port" / "path.txt").read_text(encoding="utf-8").strip()))
    rounds = json.loads(rounds_file.read_text(encoding="utf-8")) if rounds_file.exists() else {}
    item.eval.lec_resets = lec_resets(pr.dir, item.top)
    for k, rnd in rounds.items():
        ev_root = out_dir / "eval" / f"round_{k}"
        regen_dir = pr.dir / "r6" / f"regen_{k}"
        for c, info in rnd.get("candidates", {}).items():
            if regen_dir.exists():
                shutil.rmtree(ev_root / c / "rtl", ignore_errors=True)
                extract_candidate_rtl(regen_dir, int(c[1:]), ev_root / c / "rtl", cells=STUB_CELLS)
            rtl = sorted((ev_root / c / "rtl").glob("*.sv"))
            if rtl:
                info["bench_eval"] = evaluate_rtl(item, rtl, ev_root / c / "tb")
        idx = set((rnd.get("chosen") or {}).values())
        if rnd.get("regen_status") != "pass" or not idx:
            rnd["bench_pass_chosen"] = False
        elif len(idx) == 1:
            rnd["bench_pass_chosen"] = bool(rnd["candidates"].get(f"c{next(iter(idx))}", {}).get("bench_eval", {}).get("ok"))
        else:
            rtl = sorted((pr.dir / "r6" / f"regen_{k}" / "rtl").glob("*.sv"))
            rnd["bench_pass_chosen"] = bool(rtl) and bool(evaluate_rtl(item, rtl, ev_root / "chosen" / "tb").get("ok"))
        rnd["bench_pass_any"] = rnd["bench_pass_chosen"] or any(i.get("bench_eval", {}).get("ok") for i in rnd["candidates"].values())
    rounds_file.write_text(json.dumps(rounds, ensure_ascii=False, indent=1), encoding="utf-8")
    return _finish(item, out_dir, pr, rounds, 0.0, spec_errors=saved.get("spec_errors"), minutes=saved.get("minutes"))


def _finish(item: BenchItem, out_dir: Path, pr: PortRun, rounds: dict, t0: float, spec_errors: list[str] | None = None,
            minutes: float | None = None) -> BenchResult:
    st = pr.state
    keep = out_dir / "port"
    keep.mkdir(exist_ok=True)
    (keep / "path.txt").write_text(str(pr.dir) + "\n", encoding="utf-8")
    for f in [pr.dir / "state.json", pr.dir / "r2_request.json", *sorted(pr.dir.glob("spec_v*.md")), *sorted((pr.dir / "r6").glob("judge_*.json"))]:
        if f.exists():
            shutil.copy(f, keep / f.name)
    order = sorted(rounds.values(), key=lambda r: r["round"])
    r6_rounds = {x["round"]: x for x in st.get("r6", {}).get("rounds", [])}
    for r in order:
        x = r6_rounds.get(r["round"], {})
        r.update(spec_version=x.get("spec_version"), compare=x.get("compare"), verdict=(x.get("verdict") or {}).get("verdict"))
    first, last = (order[0], order[-1]) if order else ({}, {})
    res = BenchResult(
        benchmark=item.benchmark, id=item.id, category=item.category, top=item.top,
        spec_gate_ok=bool(st.get("spec_ok")), spec_errors=spec_errors or [],
        pipeline_status=st.get("status", "not_run"), chosen=first.get("chosen"), agent_runs=st.get("agent_runs", 0),
        minutes=minutes if minutes is not None else round((time.monotonic() - t0) / 60, 2),
        auto_approvals=[d for r in order for d in r.get("auto_approvals", [])],
        review_codes=sorted({x["code"] for x in st.get("review", [])}),
        candidates=first.get("candidates", {}),
        bench_pass_chosen=bool(first.get("bench_pass_chosen")), bench_pass_any=bool(first.get("bench_pass_any")),
        bench_pass_chosen_final=bool(last.get("bench_pass_chosen")),
        bench_pass_any_final=any(r.get("bench_pass_any") for r in order),
        round_trip=[{k: v for k, v in r.items() if k not in ("candidates", "auto_approvals")} for r in order],
    )
    (out_dir / "bench_result.json").write_text(json.dumps(res.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return res
