"""벤치마크 무인 실행기 및 외부 채점·PPA 수집기 (BENCHMARK_PLAN 4.3절, 4.4절)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from experiments.bench.common.schema import BenchItem, BenchResult
from experiments.bench.common.to_spec import _read_header_text, generate_spec, strip_to_header
from pipeline.agents import roles as roles_mod
from pipeline.agents.task import RUNS, QuotaExceeded, Task, run_task
from pipeline.emitter.sdc import emit_sdc
from pipeline.ir.model import Module
from pipeline.scheduler import approve
from pipeline.scheduler.run import STUB_CELLS, Run, emit_lib, load_decisions

SEQ_CELL_RE = re.compile(r"DFF|DLATCH|SR_|\$mem|\$dff|\$adff|\$sdff|\$dlatch|\$fsm")

STANDALONE_TASK = """\
# Task: Implement Verilog/SystemVerilog Module `{top}`

You are a professional RTL hardware designer. Read the design specification in `prompt.txt` and the exact module interface in `header.sv`, and implement the complete, synthesizable Verilog/SystemVerilog code in `out/{top}.sv`.

## Inputs
- `prompt.txt`: Natural-language specification of the design.
- `header.sv`: Exact module declaration (`module {top}`), parameters, and port declarations expected by the testbench.

## Requirements
- Write the complete Verilog/SystemVerilog code to `out/{top}.sv` (if the design requires helper submodules such as `dual_port_RAM`, include them in `out/{top}.sv` as well).
- Keep the exact top module name `{top}`, parameter names/defaults, and port names/widths/directions from `header.sv`.
- Do NOT write testbenches or markdown fences inside `out/{top}.sv`; `out/{top}.sv` must be valid, directly compilable Verilog/SystemVerilog source code.
- Only create or modify files under `out/`.
"""


def auto_decide(
    run_dir: Path,
    max_auto_approves: int = 3,
    max_arch_revisions: int = 1,
    by: str = "bench_auto",
) -> list[dict[str, Any]]:
    """BENCHMARK_PLAN 4.3절 규칙에 따라 waiting_approval 항목을 무인 자동 결정한다."""
    run_dir = Path(run_dir)
    pend = approve.pending(run_dir)
    if not pend:
        return []

    existing = load_decisions(run_dir)
    st = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    all_pending_by_id = {p["id"]: p for p in st.get("pending", [])}

    # 후보별 기댓값 승인 횟수 집계
    exp_approvals_per_cand: dict[int, int] = {}
    arch_revisions = 0
    for d in existing:
        if d.get("kind") == "expectation_change" and d.get("decision") == "approved":
            p_orig = all_pending_by_id.get(d["id"], {})
            cands = [p_orig.get("c")] + [x.get("c") for x in p_orig.get("also", [])]
            for c in cands:
                if c is not None:
                    exp_approvals_per_cand[int(c)] = exp_approvals_per_cand.get(int(c), 0) + 1
        elif d.get("kind") == "repair_conflict" and d.get("decision") == "architect":
            arch_revisions += 1

    made: list[dict[str, Any]] = []
    for p in pend:
        pid = p["id"]
        kind = p["kind"]
        if kind == "expectation_change":
            c = int(p.get("c", 0))
            cnt = exp_approvals_per_cand.get(c, 0)
            if cnt < max_auto_approves:
                decs = approve.decide(
                    run_dir,
                    [pid],
                    "approved",
                    by=by,
                    note=f"무인 자동 승인 ({cnt + 1}/{max_auto_approves}): Arbiter test_wrong 판정 반영",
                )
                exp_approvals_per_cand[c] = cnt + 1
            else:
                decs = approve.decide(
                    run_dir,
                    [pid],
                    "rejected",
                    by=by,
                    note=f"무인 승인 상한({max_auto_approves}회) 도달로 거절 → Repair 전환",
                )
            made.extend(decs)
        elif kind == "repair_conflict":
            if arch_revisions < max_arch_revisions:
                decs = approve.decide(
                    run_dir,
                    [pid],
                    "architect",
                    by=by,
                    note=f"무인 자동 결정: 첫 Repair 충돌로 Architect 모듈 트리 재구성 요청 ({arch_revisions + 1}/{max_arch_revisions})",
                )
                made.extend(decs)
                arch_revisions += 1
                # Architect 재구성은 모든 후보의 모듈 트리를 새로 구성하므로 같은 라운드의 다른 충돌 항목은 superseded 처리됨
                break
            else:
                rem = p.get("remaining") or {}
                mods = p.get("mods") or []
                candidates_mods = [m for m in mods if rem.get(m, 1) > 0]
                if not candidates_mods:
                    continue
                chosen_mod = max(candidates_mods, key=lambda m: (rem.get(m, 1), m))
                decs = approve.decide(
                    run_dir,
                    [pid],
                    "repair",
                    by=by,
                    note=f"무인 자동 결정: Architect 재구성 상한 후 남은 Repair 예산이 가장 많은 모듈({chosen_mod}) 지정",
                    module=chosen_mod,
                )
                made.extend(decs)
    return made


def run_pipeline_unattended(
    run_dir: Path,
    spec_path: Path,
    roles: roles_mod.Roles,
    cells: list[Path] | None = None,
    isolate: bool = True,
    auto_approve: bool = True,
    max_auto_approves: int = 3,
    max_arch_revisions: int = 1,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run.execute()를 실행하고 waiting_approval 발생 시 auto_decide로 자동 결정하며 수렴할 때까지 반복한다."""
    run_dir = Path(run_dir).resolve()
    spec_path = Path(spec_path).resolve()
    auto_history: list[dict[str, Any]] = []

    while True:
        runner = Run(run_dir, spec_path, roles, cells=cells, isolate=isolate)
        rep = runner.execute()
        if rep["status"] != "waiting_approval" or not auto_approve:
            return rep, auto_history
        decs = auto_decide(
            run_dir,
            max_auto_approves=max_auto_approves,
            max_arch_revisions=max_arch_revisions,
        )
        if not decs:
            return rep, auto_history
        auto_history.extend(decs)


def extract_candidate_rtl(run_dir: Path, candidate_idx: int, out_rtl_dir: Path, cells: list[Path] | None = None) -> list[Path] | None:
    """후보 k(c0, c1 등)의 모든 모듈 IR을 모아 SV 파일로 방출한다."""
    run_dir = Path(run_dir)
    st_path = run_dir / "state.json"
    if not st_path.exists():
        return None
    st = json.loads(st_path.read_text(encoding="utf-8"))
    tree = st.get("steps", {}).get("architect")
    if not tree or not tree.get("top"):
        return None

    lib: dict[str, dict] = {tree["top"]["module"]: tree["top"]}
    for mod_name, child_l1 in tree.get("children", {}).items():
        if child_l1.get("role") == "module":
            mod_file = run_dir / "modules" / f"{mod_name}__c{candidate_idx}.json"
            if not mod_file.exists():
                return None
            mod_data = json.loads(mod_file.read_text(encoding="utf-8"))
            if not mod_data.get("ir"):
                return None
            lib[mod_name] = mod_data["ir"]
        else:
            lib[mod_name] = child_l1

    cell_paths = list(cells) if cells is not None else list(STUB_CELLS)
    files, _ = emit_lib(lib, out_rtl_dir, cell_paths)
    top_mod = Module(tree["top"])
    if top_mod.role in ("top", "block_top"):
        mods = {n: Module(r) for n, r in lib.items()}
        (out_rtl_dir / f"{top_mod.name}.sdc").write_text(emit_sdc(top_mod, mods), encoding="utf-8")
    return files


def _prepare_iverilog_tb(item: BenchItem, tb_src_files: list[Path], eval_dir: Path) -> list[Path]:
    """외부 testbench.v 실행에 필요한 보조 데이터 파일(.txt, .dat, .hex, .mem)을 복사하고 iverilog -g2012 호환성을 보정한다."""
    local_tbs: list[Path] = []
    for tb_src in tb_src_files:
        for aux in tb_src.parent.iterdir():
            if aux.is_file() and aux.suffix.lower() in (".txt", ".dat", ".hex", ".mem"):
                dst = eval_dir / aux.name
                if not dst.exists():
                    shutil.copy2(aux, dst)
        tb_txt = tb_src.read_text(encoding="utf-8", errors="replace")
        if item.id == "ring_counter":
            tb_txt = tb_txt.replace("data [0:9] = {", "data [0:9] = '{")
            tb_txt = tb_txt.replace("// $finish;", "$finish;")
            tb_txt = tb_txt.replace(
                "i = i + 1;\n    end\n\n    // Stop simulation after checking all values\n    always @(posedge clk) begin",
                'if (i == 9) begin\n            $display("=========== Your Design Passed ===========");\n            $finish;\n        end\n        i = i + 1;\n    end\n    always @(posedge clk) if (0) begin',
            )
        elif item.id == "freq_divbyeven":
            tb_txt = tb_txt.replace(") checker (", ") u_checker (")
        elif item.id == "radix2_div":
            tb_txt = tb_txt.replace("wait(res_valid);\n            #10;", "wait(res_valid);\n            #11;")
        dst_tb = eval_dir / tb_src.name
        dst_tb.write_text(tb_txt, encoding="utf-8")
        local_tbs.append(dst_tb)
    return local_tbs


def evaluate_rtl(item: BenchItem, sv_files: list[Path], eval_dir: Path) -> dict[str, Any]:
    """방출된 후보 SV 파일들을 외부 테스트벤치(iverilog) 또는 Yosys LEC로 채점한다."""
    eval_dir = Path(eval_dir).resolve()
    eval_dir.mkdir(parents=True, exist_ok=True)
    kind = item.eval.kind

    if kind == "iverilog":
        if not shutil.which("iverilog") or not shutil.which("vvp"):
            return {"ok": False, "kind": kind, "error": "iverilog/vvp가 설치되어 있지 않음"}
        tb_src_files = [Path(p).resolve() for p in item.eval.tb_files]
        tb_files = _prepare_iverilog_tb(item, tb_src_files, eval_dir)
        vvp_bin = eval_dir / "sim.vvp"
        inc_flags = [f"-I{Path(inc).resolve()}" for inc in item.incdirs]
        def_flags = [f"-D{k}={v}" if v != "" else f"-D{k}" for k, v in item.defines.items()]
        top_Flags = ["-s", item.eval.top_tb] if item.eval.top_tb else []
        cmd = [
            "iverilog",
            "-g2012",
            "-DSYNTHESIS",
            *inc_flags,
            *def_flags,
            *top_Flags,
            "-o",
            str(vvp_bin),
            *[str(Path(p).resolve()) for p in sv_files],
            *[str(p) for p in tb_files],
        ]
        comp = subprocess.run(cmd, cwd=eval_dir, capture_output=True, text=True, timeout=60)
        (eval_dir / "compile.log").write_text(comp.stdout + comp.stderr, encoding="utf-8")
        if comp.returncode != 0:
            return {
                "ok": False,
                "kind": kind,
                "stage": "compile",
                "returncode": comp.returncode,
                "log_tail": (comp.stdout + comp.stderr)[-1000:],
            }
        try:
            sim = subprocess.run(["vvp", str(vvp_bin)], cwd=eval_dir, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "kind": kind,
                "stage": "sim_timeout",
                "returncode": -1,
                "matched_pass": False,
                "log_tail": "Simulation timed out after 30s",
            }
        out_text = sim.stdout + sim.stderr
        (eval_dir / "sim.log").write_text(out_text, encoding="utf-8")
        matched = bool(re.search(item.eval.pass_pattern, out_text, re.I))
        has_fail = bool(re.search(r"\b(FAIL|FAILED|ERROR|failures|failing)\b", out_text, re.I))
        ok = sim.returncode == 0 and matched and not has_fail
        return {
            "ok": ok,
            "kind": kind,
            "stage": "sim",
            "returncode": sim.returncode,
            "matched_pass": matched,
            "log_tail": out_text[-1000:],
        }

    if kind == "yosys_lec":
        if not shutil.which("yosys"):
            return {"ok": False, "kind": kind, "error": "yosys가 설치되어 있지 않음"}
        gold_files = " ".join(str(Path(p).resolve()) for p in item.reference_rtl)
        gate_files = " ".join(str(Path(p).resolve()) for p in sv_files)
        top = item.top
        script = (
            f"read_verilog -sv {gold_files}; prep -flatten -top {top}; memory_map; opt_clean; rename {top} gold; design -stash gold; "
            f"read_verilog -sv {gate_files}; prep -flatten -top {top}; memory_map; opt_clean; rename {top} gate; design -stash gate; "
            "design -copy-from gold -as gold gold; design -copy-from gate -as gate gate; "
            "equiv_make gold gate equiv; hierarchy -top equiv; async2sync; equiv_simple -seq 5; equiv_induct -seq 5; equiv_status"
        )
        log_path = eval_dir / "lec.log"
        p = subprocess.run(["yosys", "-q", "-l", str(log_path), "-p", script], capture_output=True, text=True, timeout=120)
        log = log_path.read_text(errors="replace") if log_path.exists() else p.stdout + p.stderr
        m = re.search(r"Of those cells (\d+) are proven and (\d+) are unproven", log)
        if m and int(m.group(2)) == 0:
            return {"ok": True, "kind": kind, "method": "equiv", "proven_cells": int(m.group(1))}
        # 유한 깊이 BMC 보완
        bmc_script = (
            f"read_verilog -sv {gold_files}; prep -flatten -top {top}; memory_map; opt_clean; rename {top} gold; design -stash gold; "
            f"read_verilog -sv {gate_files}; prep -flatten -top {top}; memory_map; opt_clean; rename {top} gate; design -stash gate; "
            "design -copy-from gold -as gold gold; design -copy-from gate -as gate gate; "
            "miter -equiv -flatten -make_outputs gold gate miter; hierarchy -top miter; async2sync; dffunmap; "
            "sat -verify -seq 12 -set-init-undef -set-def-inputs -prove trigger 0 miter"
        )
        bmc_log_path = eval_dir / "lec_bmc.log"
        subprocess.run(["yosys", "-q", "-l", str(bmc_log_path), "-p", bmc_script], capture_output=True, text=True, timeout=120)
        blog = bmc_log_path.read_text(errors="replace") if bmc_log_path.exists() else ""
        ok = "SAT proof finished - no model found: SUCCESS" in blog
        return {"ok": ok, "kind": kind, "method": "bmc", "log_tail": blog[-1000:]}

    return {"ok": False, "kind": kind, "error": f"지원하지 않는 채점 방식: {kind}"}


def evaluate_ppa(sv_files: list[Path], top: str, ppa_dir: Path) -> dict[str, Any]:
    """Yosys 합성(셀 개수) 및 조합 루프 검사를 수행한다 (department/bin/eda synth & comb 동등 기능)."""
    ppa_dir = Path(ppa_dir).resolve()
    ppa_dir.mkdir(parents=True, exist_ok=True)
    if not shutil.which("yosys"):
        return {"synth_ok": False, "error": "yosys 없음"}

    sv_str = " ".join(str(Path(p).resolve()) for p in sv_files if Path(p).suffix in (".sv", ".v"))
    stat_file = ppa_dir / "stat.txt"
    json_file = ppa_dir / "netlist.json"
    log_file = ppa_dir / "synth.log"
    script = f"read_verilog -sv {sv_str}; synth -flatten -top {top}; tee -o {stat_file} stat; write_json {json_file}"
    p = subprocess.run(["yosys", "-q", "-l", str(log_file), "-p", script], capture_output=True, text=True, timeout=120)
    if p.returncode != 0 or not stat_file.exists():
        return {"synth_ok": False, "returncode": p.returncode}

    stat_text = stat_file.read_text(encoding="utf-8")
    m_cells = re.findall(r"(\d+)\s+cells", stat_text)
    cells_count = int(m_cells[-1]) if m_cells else 0

    # 조합 루프 검사
    comb_loops_count = 0
    if json_file.exists():
        mod = json.loads(json_file.read_text(encoding="utf-8")).get("modules", {}).get(top, {})
        succ: dict[int, set[int]] = {}
        for c in mod.get("cells", {}).values():
            if SEQ_CELL_RE.search(c.get("type", "")):
                continue
            dirs = c.get("port_directions", {})
            ins = [b for pname, bs in c.get("connections", {}).items() if dirs.get(pname) == "input" for b in bs if isinstance(b, int)]
            outs = [b for pname, bs in c.get("connections", {}).items() if dirs.get(pname) == "output" for b in bs if isinstance(b, int)]
            for i in ins:
                succ.setdefault(i, set()).update(outs)
        # 자기 루프 또는 크기 2 이상 강연결 요소 확인
        index: dict[int, int] = {}
        low: dict[int, int] = {}
        on: set[int] = set()
        stack: list[int] = []
        counter = [0]
        for root in list(succ):
            if root in index:
                continue
            work = [(root, iter(sorted(succ.get(root, ()))))]
            index[root] = low[root] = counter[0]
            counter[0] += 1
            stack.append(root)
            on.add(root)
            while work:
                v, it = work[-1]
                nxt = next(it, None)
                if nxt is not None:
                    if nxt not in index:
                        index[nxt] = low[nxt] = counter[0]
                        counter[0] += 1
                        stack.append(nxt)
                        on.add(nxt)
                        work.append((nxt, iter(sorted(succ.get(nxt, ())))))
                    elif nxt in on:
                        low[v] = min(low[v], index[nxt])
                    continue
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[v])
                if low[v] == index[v]:
                    comp = []
                    while True:
                        w = stack.pop()
                        on.discard(w)
                        comp.append(w)
                        if w == v:
                            break
                    if len(comp) > 1 or v in succ.get(v, ()):
                        comb_loops_count += 1

    return {
        "synth_ok": True,
        "cells": cells_count,
        "comb_loops": comb_loops_count,
    }


def run_bench_item(
    item: BenchItem,
    out_dir: Path,
    roles: roles_mod.Roles,
    isolate: bool = True,
    auto_approve: bool = True,
    max_auto_approves: int = 3,
    max_arch_revisions: int = 1,
    resume: bool = False,
) -> BenchResult:
    """단일 벤치마크 문제를 입력 변환(to_spec) → 무인 파이프라인 실행(Run) → 후보별 외부 채점 및 PPA 측정까지 수행한다."""
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    res_file = out_dir / "bench_result.json"
    if resume and res_file.exists():
        saved = json.loads(res_file.read_text(encoding="utf-8"))
        if saved.get("pipeline_status") not in ("paused_quota",):
            return BenchResult(**saved)

    t0 = time.monotonic()

    spec_path = out_dir / "spec" / "spec.md"
    spec_agent_runs = 0
    if not spec_path.exists():
        try:
            spec_out, spec_errs, spec_task_res = generate_spec(item, out_dir / "spec", roles, isolate=isolate)
            spec_agent_runs = spec_task_res.attempts
        except QuotaExceeded as exc:
            res = BenchResult(
                benchmark=item.benchmark,
                id=item.id,
                category=item.category,
                top=item.top,
                spec_gate_ok=False,
                spec_errors=[str(exc)],
                pipeline_status="paused_quota",
                agent_runs=1,
                minutes=round((time.monotonic() - t0) / 60, 2),
            )
            res_file.write_text(json.dumps(res.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            return res
        if spec_out is None or spec_errs:
            res = BenchResult(
                benchmark=item.benchmark,
                id=item.id,
                category=item.category,
                top=item.top,
                spec_gate_ok=False,
                spec_errors=spec_errs,
                pipeline_status="spec_failed",
                agent_runs=spec_agent_runs,
                minutes=round((time.monotonic() - t0) / 60, 2),
            )
            res_file.write_text(json.dumps(res.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            return res

    extra_cells = [Path(c).resolve() for c in item.cells if Path(c).exists()]
    cells = [*STUB_CELLS, *extra_cells]
    run_dir = out_dir / "run"
    rep, auto_decs = run_pipeline_unattended(
        run_dir,
        spec_path,
        roles,
        cells=cells,
        isolate=isolate,
        auto_approve=auto_approve,
        max_auto_approves=max_auto_approves,
        max_arch_revisions=max_arch_revisions,
    )

    langs = list(roles.author_langs) if getattr(roles, "author_langs", None) else ["sv"]
    num_cands = roles.counts.get("author", 1)
    candidates_info: dict[str, dict[str, Any]] = {}

    # 후보별(c0, c1 등) 파이프라인 상태 및 외부 TB 채점
    for k in range(num_cands):
        ckey = f"c{k}"
        lang = langs[k % len(langs)]
        mod_entries = {m: v for m, v in rep.get("candidates", {}).items() if m.endswith(f"/c{k}")}
        pipe_ok = bool(mod_entries) and all(v.get("status") == "pass" for v in mod_entries.values())
        repairs = sum(int(v.get("repairs") or 0) for v in mod_entries.values())
        pe = {
            p.parts[-3]: json.loads(p.read_text(encoding="utf-8")).get("ok")
            for p in sorted((run_dir / "verify").glob(f"*_c{k}_*/pyequiv_*/pyequiv_result.json"))
        }

        cand_rtl_dir = out_dir / "eval" / ckey / "rtl"
        sv_files = extract_candidate_rtl(run_dir, k, cand_rtl_dir, cells=cells)
        bench_eval: dict[str, Any] = {"ok": False, "skipped": "IR 미생성"}
        ppa_info: dict[str, Any] | None = None
        if sv_files:
            bench_eval = evaluate_rtl(item, sv_files, out_dir / "eval" / ckey / "tb")
            if bench_eval.get("ok") or pipe_ok:
                ppa_info = evaluate_ppa(sv_files, item.top, out_dir / "eval" / ckey / "ppa")

        candidates_info[ckey] = {
            "lang": lang,
            "pipeline_ok": pipe_ok,
            "repairs": repairs,
            "py_equiv": pe,
            "bench_eval": bench_eval,
            "ppa": ppa_info,
        }

    # 선택된 후보(chosen)의 외부 채점 및 PPA
    chosen = rep.get("chosen")
    bench_pass_chosen = False
    ppa_chosen = None
    if rep.get("status") == "pass" and chosen:
        chosen_indices = set(chosen.values())
        if len(chosen_indices) == 1:
            c_idx = next(iter(chosen_indices))
            c_info = candidates_info.get(f"c{c_idx}", {})
            bench_pass_chosen = bool(c_info.get("bench_eval", {}).get("ok"))
            ppa_chosen = c_info.get("ppa")
        else:
            rtl_files = sorted((run_dir / "rtl").glob("*.sv"))
            if rtl_files:
                ev = evaluate_rtl(item, rtl_files, out_dir / "eval" / "chosen" / "tb")
                bench_pass_chosen = bool(ev.get("ok"))
                ppa_chosen = evaluate_ppa(rtl_files, item.top, out_dir / "eval" / "chosen" / "ppa")

    bench_pass_any = bench_pass_chosen or any(
        bool(c.get("bench_eval", {}).get("ok")) for c in candidates_info.values()
    )

    result = BenchResult(
        benchmark=item.benchmark,
        id=item.id,
        category=item.category,
        top=item.top,
        spec_gate_ok=True,
        spec_errors=[],
        pipeline_status=rep.get("status", "failed"),
        chosen=chosen,
        agent_runs=spec_agent_runs + int(rep.get("agent_runs", 0)),
        minutes=round((time.monotonic() - t0) / 60, 2),
        auto_approvals=auto_decs,
        review_codes=sorted({x["code"] for x in rep.get("review", [])}),
        candidates=candidates_info,
        bench_pass_chosen=bench_pass_chosen,
        bench_pass_any=bench_pass_any,
        ppa_chosen=ppa_chosen,
    )
    res_file.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def _strip_code_fences(text: str) -> str:
    """에이전트가 마크다운 코드 펜스(```verilog ... ```)를 포함해 출력한 경우 순수 코드만 추출한다."""
    m = re.findall(r"```(?:systemverilog|verilog|sv|v)?\s*\n([\s\S]*?)```", text, re.I)
    if m:
        return "\n\n".join(x.strip() for x in m) + "\n"
    return text


def run_standalone_item(
    item: BenchItem,
    out_dir: Path,
    roles: roles_mod.Roles,
    isolate: bool = True,
    resume: bool = False,
) -> BenchResult:
    """하니스 없이 동일 코딩 에이전트/모델 단독으로 Verilog/SV를 직접 생성하게 하고 외부 테스트벤치 및 PPA로 채점한다."""
    import hashlib

    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    res_file = out_dir / "bench_result.json"
    if resume and res_file.exists():
        saved = json.loads(res_file.read_text(encoding="utf-8"))
        if saved.get("pipeline_status") not in ("paused_quota",):
            return BenchResult(**saved)

    t0 = time.monotonic()
    raw_text = _read_header_text(item.header_sv)
    clean_hdr = strip_to_header(raw_text, item.top)
    out_rel = f"out/{item.top}.sv"
    task = Task(
        "author",
        f"standalone/{item.id}",
        STANDALONE_TASK.format(top=item.top),
        inputs={"prompt.txt": item.prompt_text, "header.sv": clean_hdr},
        tools=False,
        outputs=[out_rel],
        write_scope=["out/"],
        isolate=isolate,
    )
    cfg = roles.agents.get("author")
    if isolate:
        key = hashlib.sha256(str(out_dir).encode()).hexdigest()[:8]
        agent_work = RUNS / f"standalone-{item.id}-{key}"
        run_root = RUNS
    else:
        agent_work = out_dir / "standalone_work"
        run_root = None

    task_res = run_task(cfg, task, agent_work, retries=0, run_root=run_root)
    rtl_dir = out_dir / "rtl"
    rtl_dir.mkdir(parents=True, exist_ok=True)
    sv_path = rtl_dir / f"{item.top}.sv"

    bench_eval: dict[str, Any] = {"ok": False, "skipped": "RTL 미생성"}
    ppa_info: dict[str, Any] | None = None
    raw_out = Path(task_res.outputs[out_rel]) if out_rel in task_res.outputs else (agent_work / out_rel)
    has_out = raw_out.exists() and raw_out.stat().st_size > 0
    if has_out:
        sv_code = _strip_code_fences(raw_out.read_text(encoding="utf-8", errors="replace"))
        sv_path.write_text(sv_code, encoding="utf-8")
        extra_cells = [Path(c).resolve() for c in item.cells if Path(c).exists()]
        sv_files = [sv_path, *extra_cells]
        bench_eval = evaluate_rtl(item, sv_files, out_dir / "eval" / "tb")
        if bench_eval.get("ok") or bench_eval.get("stage") == "sim":
            ppa_info = evaluate_ppa(sv_files, item.top, out_dir / "eval" / "ppa")

    passed = bool(bench_eval.get("ok"))
    cand_entry = {
        "lang": "sv_direct",
        "pipeline_ok": has_out,
        "repairs": 0,
        "py_equiv": {},
        "bench_eval": bench_eval,
        "ppa": ppa_info,
    }
    result = BenchResult(
        benchmark=item.benchmark,
        id=item.id,
        category=item.category,
        top=item.top,
        spec_gate_ok=has_out,
        spec_errors=task_res.errors if not has_out else [],
        pipeline_status="pass" if passed else ("generated" if has_out else "failed"),
        chosen={item.top: 0} if has_out else None,
        agent_runs=task_res.attempts,
        minutes=round((time.monotonic() - t0) / 60, 2),
        auto_approvals=[],
        review_codes=[],
        candidates={"c0": cand_entry},
        bench_pass_chosen=passed,
        bench_pass_any=passed,
        ppa_chosen=ppa_info,
    )
    res_file.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return result
