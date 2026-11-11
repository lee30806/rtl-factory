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
    """외부 TB 보정 칸: 회차 수(Repair 회차 포함), 마지막 계기와 상태, 외부 TB로 고른 golden 후보, 최종 외부 TB 결과."""
    parts = []
    if r.ext_fix:
        last = r.ext_fix[-1]
        trig = {"external_fail": "외부 실패", "internal_mismatch": "내부 불일치", "external_repair": "미정의 Repair"}.get(
            last.get("trigger"), last.get("trigger"))
        parts.append(f"{len(r.ext_fix)}회 ({trig}) `{last.get('pipeline_status')}`")
    if r.golden and r.golden.get("by") == "external_tb":
        parts.append(f"외부 TB로 c{r.golden.get('candidate')} 선택")
    if not parts:
        return "-"
    return " ".join(parts) + (" PASS" if r.bench_pass_chosen_final else " FAIL")


def _lec_cell(r: BenchResult) -> str:
    """첫 회차 선택 후보의 등가 채점 방식: 증명(equiv, comb_sat)인지 유한 깊이 BMC인지."""
    idx = set((r.chosen or {}).values())
    ev = r.candidates.get(f"c{next(iter(idx))}", {}).get("bench_eval", {}) if len(idx) == 1 else {}
    if not ev.get("ok"):
        return "-"
    return f"BMC {ev.get('depth')}" if ev.get("bounded") else "증명"


def _roundtrip_lines(results: list[BenchResult], s: dict[str, Any]) -> list[str]:
    n, c = s["total"], s["counts"]
    pct = lambda k: f"{c[k]}/{n} ({c[k] / n * 100:.1f}%)"  # noqa: E731
    bounded = sum(1 for r in results if r.bench_pass_chosen and _lec_cell(r).startswith("BMC"))
    lines = [
        "# Benchmark Summary (RTL-BenchLS Round-Trip)",
        "",
        f"- **Total Items**: {n}",
        f"- **SPEC 생성 (R5 게이트)**: {pct('spec_gate_pass')}",
        f"- **P11 완주 (`pass`)**: {pct('pipeline_pass')}",
        f"- **1회 왕복 등가 (`pass@round1`, 선택 후보)**: {pct('bench_pass_chosen')} — 그중 유한 깊이 BMC로만 확인 {bounded}개",
        f"- **1회 왕복 등가 (후보 중 하나라도)**: {pct('bench_pass_any')}",
        f"- **R6 SPEC 보정 뒤 (`pass@r6`, 선택 후보)**: {pct('bench_pass_chosen_final')}",
        f"- **R6 SPEC 보정 뒤 (어느 회차 후보든)**: {pct('bench_pass_any_final')}",
        "",
        "등가 채점은 Yosys(`equiv_*` 귀납 증명, 실패 시 miter + `sat`)다. 공식 채점(JasperGold SEC, Conformal LEC)과 다르다.",
        "",
        "| ID | Type | P11 | R6 회차 | round1 선택 | round1 c0 | round1 c1 | 등가 방식 | 보정 뒤 선택 | Agent Runs | Min |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        ok = lambda k: "PASS" if r.candidates.get(k, {}).get("bench_eval", {}).get("ok") else ("-" if k not in r.candidates else "FAIL")  # noqa: E731
        lines.append(f"| `{r.id}` | {r.category} | `{r.pipeline_status}` | {len(r.round_trip)} | {'PASS' if r.bench_pass_chosen else 'FAIL'} "
                     f"| {ok('c0')} | {ok('c1')} | {_lec_cell(r)} | {'PASS' if r.bench_pass_chosen_final else 'FAIL'} "
                     f"| {r.agent_runs} | {r.minutes} |")
    return lines


def write_summary(results: list[BenchResult], out_dir: Path) -> dict[str, Any]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize(results)
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if results and all(r.benchmark == "rtl_benchls" for r in results):
        (out_dir / "summary.md").write_text("\n".join(_roundtrip_lines(results, summary)) + "\n", encoding="utf-8")
        return summary

    lines = [
        "# Benchmark Summary",
        "",
        f"- **Total Items**: {summary['total']}",
        f"- **SPEC Gate Pass**: {summary.get('counts', {}).get('spec_gate_pass', 0)}/{summary['total']} ({summary.get('rates', {}).get('spec_gate_pass', 0) * 100:.1f}%)",
        f"- **Pipeline Pass**: {summary.get('counts', {}).get('pipeline_pass', 0)}/{summary['total']} ({summary.get('rates', {}).get('pipeline_pass', 0) * 100:.1f}%)",
        f"- **Bench Pass (`pass@1` chosen)**: {summary.get('counts', {}).get('bench_pass_chosen', 0)}/{summary['total']} ({summary.get('rates', {}).get('bench_pass_chosen', 0) * 100:.1f}%)",
        f"- **Bench Pass (any candidate)**: {summary.get('counts', {}).get('bench_pass_any', 0)}/{summary['total']} ({summary.get('rates', {}).get('bench_pass_any', 0) * 100:.1f}%)",
        f"- **최종 (golden, 외부 TB 보정 뒤)**: {summary.get('counts', {}).get('bench_pass_chosen_final', 0)}/{summary['total']} ({summary.get('rates', {}).get('bench_pass_chosen_final', 0) * 100:.1f}%)"
        f" — 보정한 문제 {summary.get('counts', {}).get('ext_fix_items', 0)}개. 외부 TB를 통과한 후보를 golden으로 고르고 외부 TB를 근거로 Repair·SPEC 보정을 한 결과라 pass@1이 아님",
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
    if with_lessons := [r for r in results if r.lessons]:
        lines += ["", "## LESSON·재검토 알람 (사람이 확인할 헤더·리셋·파라미터 판정과 SPEC 작성자의 가정)", ""]
        lines += [f"- `{r.id}`: {x}" for r in with_lessons for x in r.lessons]
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary
