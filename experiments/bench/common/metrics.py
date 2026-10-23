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
        },
        "rates": {
            "spec_gate_pass": round(spec_ok / total, 4),
            "pipeline_pass": round(pipe_ok / total, 4),
            "bench_pass_chosen": round(chosen_ok / total, 4),
            "bench_pass_any": round(any_ok / total, 4),
        },
        "avg_agent_runs": round(sum(r.agent_runs for r in results) / total, 2),
        "avg_minutes": round(sum(r.minutes for r in results) / total, 2),
        "auto_approvals_total": sum(len(r.auto_approvals) for r in results),
        "by_lang": by_lang,
        "items": [r.to_dict() for r in results],
    }


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
        "",
        "## Per-Item Results",
        "",
        "| ID | Top | SPEC Gate | Pipeline | Chosen `pass@1` | c0 (SV) | c1 (Py) | Synth Cells | Comb Loops | Agent Runs |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        c0_ok = r.candidates.get("c0", {}).get("bench_eval", {}).get("ok")
        c1_ok = r.candidates.get("c1", {}).get("bench_eval", {}).get("ok")
        ppa = r.ppa_chosen or r.candidates.get("c0", {}).get("ppa") or {}
        cells = ppa.get("cells", "-")
        loops = ppa.get("comb_loops", "-")
        lines.append(
            f"| `{r.id}` | `{r.top}` | {'PASS' if r.spec_gate_ok else 'FAIL'} | `{r.pipeline_status}` "
            f"| {'PASS' if r.bench_pass_chosen else 'FAIL'} | {'PASS' if c0_ok else 'FAIL'} "
            f"| {'PASS' if c1_ok else ('-' if 'c1' not in r.candidates else 'FAIL')} | {cells} | {loops} | {r.agent_runs} |"
        )
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary
