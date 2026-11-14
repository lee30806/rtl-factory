"""벤치마크 통합 실행 CLI 진입점 (BENCHMARK_PLAN 3절, B0, B1).

    python3 -m experiments.bench.run_bench --bench rtllm [--dataset <dir>] [--mode harness|standalone|both] [--workers 4] [--resume]
    python3 -m experiments.bench.run_bench --bench rtl_benchls --dataset <slice_XX.jsonl> [--filter ...] [--limit N]
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import sys
import threading
from pathlib import Path

# 벤치 설계는 작아서 시뮬레이션이 몇 분을 넘기면 멈춘 것이다. 기본 감시 시간(900초)은 멈춤 한 번에 15분을 쓰므로 줄인다.
# 환경 변수로 이미 정했으면 그 값을 따른다. pipeline을 import하기 전에 정해야 한다.
os.environ.setdefault("CHIPDEPT_SIM_TIMEOUT", "180")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.bench.adapters.chipverilog import load_chipverilog  # noqa: E402
from experiments.bench.adapters.cvdp import load_cvdp  # noqa: E402
from experiments.bench.adapters.realbench import load_realbench  # noqa: E402
from experiments.bench.adapters.rtl_benchls import load_benchls  # noqa: E402
from experiments.bench.adapters.rtllm import load_rtllm_dir, materialize_builtin_pilot  # noqa: E402
from experiments.bench.common.metrics import write_summary  # noqa: E402
from experiments.bench.common.roundtrip import rescore_item, run_roundtrip_item, run_roundtrip_standalone  # noqa: E402
from experiments.bench.common.runner import run_bench_item, run_spec_item, run_standalone_item  # noqa: E402
from experiments.bench.common.schema import BenchItem, BenchResult  # noqa: E402
from pipeline.agents import roles as roles_mod  # noqa: E402

MANIFEST = Path(__file__).resolve().parent / "datasets.json"   # 평가 데이터셋 4종의 출처·채점기·제외 문제


def manifest(bench: str) -> dict:
    try:
        return json.loads(MANIFEST.read_text(encoding="utf-8")).get(bench, {})
    except (OSError, json.JSONDecodeError):
        return {}


def _run_suite(
    items: list[BenchItem],
    out_dir: Path,
    roles: roles_mod.Roles,
    mode: str,
    isolate: bool,
    auto_approve: bool,
    resume: bool,
    workers: int,
    max_auto_approves: int = 3,
    spec_dir: Path | None = None,
    ext_fix_rounds: int = 1,
    skip_verifier: bool = False,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    results_by_id: dict[str, BenchResult] = {}
    if resume:
        for it in items:
            rf = out_dir / it.id / "bench_result.json"
            if rf.exists():
                saved = json.loads(rf.read_text(encoding="utf-8"))
                skip_statuses = ("paused_quota",) if mode == "spec" else ("paused_quota", "spec_only")
                if saved.get("pipeline_status") not in skip_statuses:
                    results_by_id[it.id] = BenchResult(**saved)
    quota_event = threading.Event()

    def _job(it: BenchItem) -> BenchResult | None:
        if quota_event.is_set():
            return None
        if it.benchmark == "rtl_benchls" and mode == "standalone":
            res = run_roundtrip_standalone(it, out_dir / it.id, roles, isolate=isolate, resume=resume)
        elif it.benchmark == "rtl_benchls":
            res = run_roundtrip_item(it, out_dir / it.id, roles, isolate=isolate, resume=resume,
                                     max_auto_approves=max_auto_approves, skip_verifier=skip_verifier)
        elif mode == "standalone":
            res = run_standalone_item(it, out_dir / it.id, roles, isolate=isolate, resume=resume)
        elif mode == "spec":
            res = run_spec_item(it, out_dir / it.id, roles, isolate=isolate, resume=resume)
        else:
            res = run_bench_item(
                it,
                out_dir / it.id,
                roles,
                isolate=isolate,
                auto_approve=auto_approve,
                max_auto_approves=max_auto_approves,
                resume=resume,
                spec_dir=spec_dir,
                ext_fix_rounds=ext_fix_rounds,
                skip_verifier=skip_verifier,
            )
        if res.pipeline_status == "paused_quota":
            quota_event.set()
        return res

    if workers <= 1:
        for idx, it in enumerate(items, 1):
            res = _job(it)
            if res is None:
                break
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
                res = fut.result()
                if res is None:
                    continue
                done_cnt += 1
                results_by_id[it.id] = res
                print(
                    f"[{mode} {done_cnt}/{len(items)}] {it.id}: status={res.pipeline_status} "
                    f"chosen_pass={res.bench_pass_chosen} any_pass={res.bench_pass_any} "
                    + (f"ext_fix={len(res.ext_fix)} final_chosen_pass={res.bench_pass_chosen_final} " if res.ext_fix else "")
                    + f"runs={res.agent_runs} min={res.minutes}",
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
    ap.add_argument("--bench", choices=["rtllm", "rtl_benchls", "chipverilog", "realbench", "cvdp"], default="rtllm", help="대상 벤치마크")
    ap.add_argument("--dataset", type=Path, help="벤치마크 데이터셋 디렉터리, rtl_benchls는 slice_XX.jsonl (rtllm은 생략 시 내장 파일럿 예제 사용. "
                                                 "chipverilog·realbench·cvdp는 생략 시 experiments/bench/datasets.json의 기본 경로)")
    ap.add_argument("--include-defects", action="store_true",
                    help="datasets.json의 exclude(정답 자체가 채점을 통과하지 못하는 문제)도 실행")
    ap.add_argument("--subset", choices=["sequential", "combinational", "all"], default="sequential")
    ap.add_argument("--mode", choices=["harness", "standalone", "both", "spec"], default="harness", help="실행 모드")
    ap.add_argument("--spec-dir", type=Path, help="미리 작성된 SPEC 디렉터리 (지정 시 <spec-dir>/<id>/spec을 복사해 재사용)")
    ap.add_argument("--filter", default="", help="문제 ID 부분 문자열 또는 정규식 필터")
    ap.add_argument("--limit", type=int, default=0, help="최대 실행 문제 수 (0이면 제한 없음)")
    ap.add_argument("--workers", type=int, default=1, help="병렬 문제 실행 워커 수")
    ap.add_argument("--resume", action="store_true", help="이미 완료된 문제는 건너뛰고 이어서 실행")
    ap.add_argument("--roles", type=Path, default=ROOT / "config" / "roles.json", help="역할 설정 JSON")
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "bench" / "rtllm", help="결과 저장 디렉터리")
    ap.add_argument("--auto-approve", action="store_true", default=True, help="waiting_approval 무인 자동 결정 활성화")
    ap.add_argument("--no-auto-approve", dest="auto_approve", action="store_false")
    ap.add_argument("--max-auto-approves", type=int, default=3, help="문제당 기댓값 수정 무인 승인 상한")
    ap.add_argument("--ext-fix-rounds", type=int, default=1,
                    help="외부 TB를 통과해 선택된 후보가 없을 때 외부 TB 결과로 SPEC을 고쳐 다시 돌리는 최대 횟수 (0이면 하지 않음)")
    ap.add_argument("--skip-verifier", action="store_true",
                    help="외부 TB가 있는 경우 내부 Verifier 에이전트를 생략하고 E2 검증에서 외부 TB를 직접 실행")
    ap.add_argument("--no-isolate", action="store_true", help="테스트용 샌드박스 격리 해제")
    ap.add_argument("--rescore", action="store_true", help="rtl_benchls: 저장된 후보 RTL을 지금의 등가 채점으로 다시 채점 (에이전트 호출 없음)")
    args = ap.parse_args(argv)

    roles = roles_mod.load(args.roles)
    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.bench == "rtl_benchls":
        if args.mode not in ("harness", "standalone"):
            ap.error("rtl_benchls는 --mode harness(P11 왕복) 또는 standalone(같은 모델로 RTL → 자연어 → RTL)만 지원")
        items = load_benchls(args.dataset, out_dir / "_gold")
        if args.rescore:
            items = [it for it in items if args.filter in it.id or re.fullmatch(args.filter or ".*", it.id)][: args.limit or None]
            res = [r for it in items if (r := rescore_item(it, out_dir / it.id))]
            write_summary(res, out_dir)
            print(f"다시 채점 {len(res)}개: {out_dir / 'summary.md'}")
            return 0
    elif args.bench == "chipverilog":
        items = load_chipverilog(args.dataset or ROOT / "build" / "cvdp_datasets" / "cvdp_chipverilog.jsonl", out_dir / "_src")
    elif args.bench == "realbench":
        items = load_realbench(args.dataset, out_dir / "_src")
    elif args.bench == "cvdp":
        items = load_cvdp(args.dataset, out_dir / "_src")
    elif args.dataset and args.dataset.exists():
        items = load_rtllm_dir(args.dataset, subset=args.subset)
    else:
        items = [materialize_builtin_pilot(out_dir / "_pilot_src")]

    if not args.include_defects and (excl := manifest(args.bench).get("exclude")):
        dropped = [it.id for it in items if it.id in excl]
        items = [it for it in items if it.id not in excl]
        if dropped:
            print(f"[{args.bench}] 데이터셋 결함으로 제외 {len(dropped)}개 (--include-defects로 포함): {dropped}", flush=True)
    if args.filter:
        items = [it for it in items if args.filter in it.id or args.filter in it.top
                 or re.fullmatch(args.filter, it.id) or re.fullmatch(args.filter, it.top)]
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
            max_auto_approves=args.max_auto_approves,
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
            max_auto_approves=args.max_auto_approves,
            spec_dir=args.spec_dir,
            ext_fix_rounds=args.ext_fix_rounds,
            skip_verifier=args.skip_verifier,
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
        max_auto_approves=args.max_auto_approves,
        spec_dir=args.spec_dir,
        ext_fix_rounds=args.ext_fix_rounds,
        skip_verifier=args.skip_verifier,
    )
    if args.mode == "spec":
        return 0 if summary["total"] > 0 and summary["counts"]["spec_gate_pass"] > 0 else 1
    return 0 if summary["total"] > 0 and summary["counts"]["bench_pass_any"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
