"""벤치마크 통합 실행 CLI 진입점 (BENCHMARK_PLAN 3절, B0, B1).

    python3 -m experiments.bench.run_bench --bench rtllm [--dataset <dir>] [--mode harness|standalone|both] [--workers 4] [--resume]
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.bench.adapters.rtllm import load_rtllm_dir, materialize_builtin_pilot  # noqa: E402
from experiments.bench.common.metrics import write_summary  # noqa: E402
from experiments.bench.common.runner import run_bench_item, run_standalone_item  # noqa: E402
from experiments.bench.common.schema import BenchItem, BenchResult  # noqa: E402
from pipeline.agents import roles as roles_mod  # noqa: E402


def _run_suite(
    items: list[BenchItem],
    out_dir: Path,
    roles: roles_mod.Roles,
    mode: str,
    isolate: bool,
    auto_approve: bool,
    resume: bool,
    workers: int,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    results_by_id: dict[str, BenchResult] = {}

    def _job(it: BenchItem) -> BenchResult:
        if mode == "standalone":
            return run_standalone_item(it, out_dir / it.id, roles, isolate=isolate, resume=resume)
        return run_bench_item(
            it,
            out_dir / it.id,
            roles,
            isolate=isolate,
            auto_approve=auto_approve,
            resume=resume,
        )

    if workers <= 1:
        for idx, it in enumerate(items, 1):
            res = _job(it)
            results_by_id[it.id] = res
            print(
                f"[{mode} {idx}/{len(items)}] {it.id}: status={res.pipeline_status} "
                f"chosen_pass={res.bench_pass_chosen} any_pass={res.bench_pass_any} "
                f"runs={res.agent_runs} min={res.minutes}",
                flush=True,
            )
            write_summary([results_by_id[x.id] for x in items if x.id in results_by_id], out_dir)
            if res.pipeline_status == "paused_quota":
                print("사용량 한도(paused_quota)로 일시 중지됨. 한도 해제 후 --resume으로 재개 가능.", file=sys.stderr)
                break
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            fut_to_item = {ex.submit(_job, it): it for it in items}
            done_cnt = 0
            for fut in concurrent.futures.as_completed(fut_to_item):
                it = fut_to_item[fut]
                done_cnt += 1
                res = fut.result()
                results_by_id[it.id] = res
                print(
                    f"[{mode} {done_cnt}/{len(items)}] {it.id}: status={res.pipeline_status} "
                    f"chosen_pass={res.bench_pass_chosen} any_pass={res.bench_pass_any} "
                    f"runs={res.agent_runs} min={res.minutes}",
                    flush=True,
                )
                ordered = [results_by_id[x.id] for x in items if x.id in results_by_id]
                write_summary(ordered, out_dir)

    ordered_results = [results_by_id[x.id] for x in items if x.id in results_by_id]
    summary = write_summary(ordered_results, out_dir)
    print(f"\n[{mode} Summary saved to {out_dir / 'summary.json'} and {out_dir / 'summary.md'}]", flush=True)
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="외부 RTL 벤치마크 실행기")
    ap.add_argument("--bench", choices=["rtllm"], default="rtllm", help="대상 벤치마크")
    ap.add_argument("--dataset", type=Path, help="벤치마크 데이터셋 디렉터리 (생략 시 내장 파일럿 예제 사용)")
    ap.add_argument("--subset", choices=["sequential", "combinational", "all"], default="sequential")
    ap.add_argument("--mode", choices=["harness", "standalone", "both"], default="harness", help="실행 모드")
    ap.add_argument("--filter", default="", help="문제 ID 부분 문자열 또는 정규식 필터")
    ap.add_argument("--limit", type=int, default=0, help="최대 실행 문제 수 (0이면 제한 없음)")
    ap.add_argument("--workers", type=int, default=1, help="병렬 문제 실행 워커 수")
    ap.add_argument("--resume", action="store_true", help="이미 완료된 문제는 건너뛰고 이어서 실행")
    ap.add_argument("--roles", type=Path, default=ROOT / "config" / "roles.json", help="역할 설정 JSON")
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "bench" / "rtllm", help="결과 저장 디렉터리")
    ap.add_argument("--auto-approve", action="store_true", default=True, help="waiting_approval 무인 자동 결정 활성화")
    ap.add_argument("--no-auto-approve", dest="auto_approve", action="store_false")
    ap.add_argument("--no-isolate", action="store_true", help="테스트용 샌드박스 격리 해제")
    args = ap.parse_args(argv)

    roles = roles_mod.load(args.roles)
    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.dataset and args.dataset.exists():
        items = load_rtllm_dir(args.dataset, subset=args.subset)
    else:
        items = [materialize_builtin_pilot(out_dir / "_pilot_src")]

    if args.filter:
        items = [it for it in items if args.filter in it.id or args.filter in it.top]
    if args.limit > 0:
        items = items[: args.limit]

    if args.mode == "both":
        s_sum = _run_suite(
            items,
            out_dir / "standalone",
            roles,
            "standalone",
            isolate=not args.no_isolate,
            auto_approve=args.auto_approve,
            resume=args.resume,
            workers=args.workers,
        )
        h_sum = _run_suite(
            items,
            out_dir / "harness",
            roles,
            "harness",
            isolate=not args.no_isolate,
            auto_approve=args.auto_approve,
            resume=args.resume,
            workers=args.workers,
        )
        return 0 if (s_sum["total"] > 0 or h_sum["total"] > 0) else 1

    summary = _run_suite(
        items,
        out_dir,
        roles,
        args.mode,
        isolate=not args.no_isolate,
        auto_approve=args.auto_approve,
        resume=args.resume,
        workers=args.workers,
    )
    return 0 if summary["total"] > 0 and summary["counts"]["bench_pass_any"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
