"""벤치마크 결과 집계 및 마크다운·JSON 리포트 생성기 (BENCHMARK_PLAN 6절)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from experiments.bench.common.schema import BenchResult


def summarize(results: list[BenchResult]) -> dict[str, Any]:
    total = len(results)
    if total == 0:
        return {
            "total": 0,
            "spec_gate_pass": 0.0,
            "pipeline_pass": 0.0,
            "bench_pass_chosen": 0.0,
            "bench_pass_any": 0.0,
            "by_lang": {},
            "items": [],
        }

    spec_ok = sum(1 for r in results if r.spec_gate_ok)
    pipe_ok = sum(1 for r in results if r.pipeline_status == "pass")
    chosen_ok = sum(1 for r in results if r.bench_pass_chosen)
    any_ok = sum(1 for r in results if r.bench_pass_any)
    # 외부 TB 보정 뒤 (보정을 하지 않은 문제는 첫 결과와 같음). 외부 TB를 근거로 SPEC을 고쳤으므로 pass@1과 따로 센다
    chosen_final = sum(1 for r in results if r.bench_pass_chosen or r.bench_pass_chosen_final)
    any_final = sum(1 for r in results if r.bench_pass_any or r.bench_pass_any_final)
    ext_fixed = sum(1 for r in results if r.ext_fix)

    lang_stats: dict[str, dict[str, Any]] = {}
    for r in results:
        for c_info in r.candidates.values():
            lang = c_info.get("lang", "sv")
            st = lang_stats.setdefault(lang, {"total": 0, "pipeline_pass": 0, "bench_pass": 0, "repairs_sum": 0})
            st["total"] += 1
            if c_info.get("pipeline_ok"):
                st["pipeline_pass"] += 1
            if c_info.get("bench_eval", {}).get("ok"):
                st["bench_pass"] += 1
            st["repairs_sum"] += int(c_info.get("repairs", 0))

    by_lang = {}
    for lang, st in lang_stats.items():
        n = max(st["total"], 1)
        by_lang[lang] = {
            "total": st["total"],
            "pipeline_pass_rate": round(st["pipeline_pass"] / n, 4),
            "bench_pass_rate": round(st["bench_pass"] / n, 4),
            "avg_repairs": round(st["repairs_sum"] / n, 2),
        }

    return {
        "total": total,
        "counts": {
            "spec_gate_pass": spec_ok,
            "pipeline_pass": pipe_ok,
            "bench_pass_chosen": chosen_ok,
            "bench_pass_any": any_ok,
            "bench_pass_chosen_final": chosen_final,
            "bench_pass_any_final": any_final,
            "ext_fix_items": ext_fixed,
        },
        "rates": {
            "spec_gate_pass": round(spec_ok / total, 4),
            "pipeline_pass": round(pipe_ok / total, 4),
            "bench_pass_chosen": round(chosen_ok / total, 4),
            "bench_pass_any": round(any_ok / total, 4),
            "bench_pass_chosen_final": round(chosen_final / total, 4),
            "bench_pass_any_final": round(any_final / total, 4),
        },
        "avg_agent_runs": round(sum(r.agent_runs for r in results) / total, 2),
        "avg_minutes": round(sum(r.minutes for r in results) / total, 2),
        "auto_approvals_total": sum(len(r.auto_approvals) for r in results),
        "by_lang": by_lang,
        "items": [r.to_dict() for r in results],
    }


def _ext_cell(r: BenchResult) -> str:
    """외부 TB 보정 칸: 회차 수, 계기, 마지막 회차의 상태와 선택 후보 외부 TB 결과."""
    if not r.ext_fix:
        return "-"
    last = r.ext_fix[-1]
    trig = {"external_fail": "외부 실패", "internal_mismatch": "내부 불일치"}.get(last.get("trigger"), last.get("trigger"))
    return f"{len(r.ext_fix)}회 ({trig}) `{last.get('pipeline_status')}` {'PASS' if r.bench_pass_chosen_final else 'FAIL'}"


def write_summary(results: list[BenchResult], out_dir: Path) -> dict[str, Any]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize(results)
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Benchmark Summary",
        "",
        f"- **Total Items**: {summary['total']}",
        f"- **SPEC Gate Pass**: {summary.get('counts', {}).get('spec_gate_pass', 0)}/{summary['total']} ({summary.get('rates', {}).get('spec_gate_pass', 0) * 100:.1f}%)",
        f"- **Pipeline Pass**: {summary.get('counts', {}).get('pipeline_pass', 0)}/{summary['total']} ({summary.get('rates', {}).get('pipeline_pass', 0) * 100:.1f}%)",
        f"- **Bench Pass (`pass@1` chosen)**: {summary.get('counts', {}).get('bench_pass_chosen', 0)}/{summary['total']} ({summary.get('rates', {}).get('bench_pass_chosen', 0) * 100:.1f}%)",
        f"- **Bench Pass (any candidate)**: {summary.get('counts', {}).get('bench_pass_any', 0)}/{summary['total']} ({summary.get('rates', {}).get('bench_pass_any', 0) * 100:.1f}%)",
        f"- **외부 TB 보정 뒤 (chosen)**: {summary.get('counts', {}).get('bench_pass_chosen_final', 0)}/{summary['total']} ({summary.get('rates', {}).get('bench_pass_chosen_final', 0) * 100:.1f}%)"
        f" — 보정한 문제 {summary.get('counts', {}).get('ext_fix_items', 0)}개. 외부 TB를 근거로 SPEC을 고친 결과라 pass@1이 아님",
        f"- **외부 TB 보정 뒤 (any candidate)**: {summary.get('counts', {}).get('bench_pass_any_final', 0)}/{summary['total']} ({summary.get('rates', {}).get('bench_pass_any_final', 0) * 100:.1f}%)",
        f"- **시뮬레이션 모델 (합성, PPA 없음)**: {sum(r.sim_model for r in results)}/{summary['total']}",
        "",
        "## Per-Item Results",
        "",
        "| ID | Top | SPEC Gate | Pipeline | Chosen `pass@1` | c0 (SV) | c1 (Py) | 외부 TB 보정 | Synth Cells | Comb Loops | Agent Runs |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        c0_ok = r.candidates.get("c0", {}).get("bench_eval", {}).get("ok")
        c1_ok = r.candidates.get("c1", {}).get("bench_eval", {}).get("ok")
        ppa = r.ppa_chosen or r.candidates.get("c0", {}).get("ppa") or {}
        cells = ppa.get("cells", "-")
        loops = ppa.get("comb_loops", "-")
        lines.append(
            f"| `{r.id}`{' (시뮬레이션 모델)' if r.sim_model else ''} | `{r.top}` | {'PASS' if r.spec_gate_ok else 'FAIL'} | `{r.pipeline_status}` "
            f"| {'PASS' if r.bench_pass_chosen else 'FAIL'} | {'PASS' if c0_ok else 'FAIL'} "
            f"| {'PASS' if c1_ok else ('-' if 'c1' not in r.candidates else 'FAIL')} | {_ext_cell(r)} | {cells} | {loops} | {r.agent_runs} |"
        )
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary
