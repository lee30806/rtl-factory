"""P4 완료 기준: 이식 시험 블록을 SPEC부터 끝까지 사람 개입 없이 실행하고, 결과 RTL을 원본 테스트벤치로 판정한다.

    .venv/bin/python tests/golden/e2e.py [블록 ...]

블록마다 스케줄러를 실제 coding agent(config/roles.json, 격리)로 돌린다. 실행 상태는 build/e2e/<블록>/에 남고,
멈춘 뒤(사용량 한도 등) 같은 명령을 다시 실행하면 이어서 실행한다.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import run_golden  # noqa: E402

from pipeline.agents import roles  # noqa: E402
from pipeline.scheduler.run import Run  # noqa: E402

BLOCKS = ["axis_adapter", "axis_cobs_decode", "axis_async_fifo"]


def e2e(block: str) -> dict:
    out = ROOT / "build" / "e2e" / block
    cells = [(HERE / block / c).resolve() for c in json.loads((HERE / block / "golden.json").read_text()).get("cells", [])]
    t0 = time.time()
    rep = Run(out, HERE / block / "block_spec.md", roles.load(), cells=cells or None).execute()
    rec = {"block": block, "status": rep["status"], "agent_runs": rep["agent_runs"], "chosen": rep["chosen"],
           "candidates": rep["candidates"], "minutes": round((time.time() - t0) / 60, 1),
           "review_codes": sorted({r["code"] for r in rep["review"]})}
    if rep["status"] == "pass":
        irs = {p.name.removesuffix(".ir.json"): json.loads(p.read_text()) for p in (out / "ir").glob("*.ir.json")}
        g = run_golden.run_block(block, out / "upstream_tb", ir_override=irs)
        rec["upstream_tb"] = [{"tb": x["tb"], "passed": x["tests"] - len(x["failed"]), "tests": x["tests"],
                               "failed": x["failed"][:5]} for x in g.get("e2", [])]
        rec["upstream_ok"] = g["ok"]
        golden = json.loads((HERE / block / json.loads((HERE / block / "golden.json").read_text())["ir"][0]).read_text())
        rec["identical_to_golden"] = any(ir.get("blocks") == golden.get("blocks") for ir in irs.values())
    (out / "e2e.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1))
    return rec


def main(argv=None) -> int:
    blocks = (argv if argv is not None else sys.argv[1:]) or BLOCKS
    for b in blocks:
        rec = e2e(b)
        print(json.dumps(rec, ensure_ascii=False)[:2500], flush=True)
        if rec["status"] == "paused_quota":
            print("사용량 한도로 멈춤. 한도가 풀린 뒤 같은 명령으로 이어서 실행", flush=True)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
