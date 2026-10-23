"""P8a 작업 9: 실제 블록에서 SV 후보와 Python 후보를 비교한다.

    .venv/bin/python experiments/p8a/pilot.py [블록 ...]

블록마다 스케줄러를 한 번 돌리되 Author 후보 c0는 SV 조각, c1은 Python 조각으로 쓴다 (같은 Verifier 테스트를 공유).
실행 상태는 build/pilot/pilot-<블록>/에 남고, 멈춘 뒤 같은 명령으로 다시 실행하면 이어서 실행한다.
후보마다 상태, Repair 수, lint, 번역기 검사 결과를 모으고, golden 블록은 후보별 IR을 원본 테스트벤치로도 판정한다.
결과는 build/pilot/pilot-<블록>/pilot.json.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "golden"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(GOLDEN))

import run_golden  # noqa: E402

from pipeline.agents import roles  # noqa: E402
from pipeline.scheduler.run import Run  # noqa: E402

LANGS = ("sv", "py")
SPECS = {b: GOLDEN / b / "block_spec.md" for b in ("axis_adapter", "axis_cobs_decode", "axis_async_fifo")}
SPECS["axis_crc_append"] = Path(__file__).parent / "axis_crc_append.spec.md"   # F0001 승인 개정판 (실행 674c)


def candidate_irs(out: Path, k: int) -> dict[str, dict]:
    return {p.name.removesuffix(f"__c{k}.json"): json.loads(p.read_text())["ir"]
            for p in (out / "modules").glob(f"*__c{k}.json") if json.loads(p.read_text()).get("ir")}


def pilot(block: str) -> dict:
    r = roles.load()
    r.author_langs = LANGS
    cells = []
    if (GOLDEN / block / "golden.json").exists():
        cells = [(GOLDEN / block / c).resolve() for c in
                 json.loads((GOLDEN / block / "golden.json").read_text()).get("cells", [])]
    t0 = time.time()
    # 에이전트 작업 디렉터리는 RUNS/<실행 디렉터리 이름>이므로 이전 E2E 실행과 겹치지 않게 이름을 구분한다
    run = Run(ROOT / "build" / "pilot" / f"pilot-{block}", SPECS[block], r, cells=cells or None)
    rep = run.execute()
    out = run.dir
    rec = {"block": block, "langs": list(LANGS), "status": rep["status"], "agent_runs": rep["agent_runs"],
           "chosen": rep["chosen"], "minutes": round((time.time() - t0) / 60, 1),
           "review_codes": sorted({x["code"] for x in rep["review"]}), "candidates": {}}
    for key, c in rep["candidates"].items():
        mod, ck = key.rsplit("/c", 1)
        # 번역기 검사 결과는 검증 단계마다 verify/<태그>/pyequiv_<모듈>/에 남는다 (state에는 요약만 있음)
        pe = {p.parts[-3]: json.loads(p.read_text()).get("ok")
              for p in sorted((out / "verify").glob(f"*_c{ck}_*/pyequiv_{mod}/pyequiv_result.json"))}
        rec["candidates"][key] = {**c, "lang": LANGS[int(ck) % len(LANGS)], "py_equiv": pe}
    if rep["status"] in ("pass", "failed") and (GOLDEN / block / "golden.json").exists():
        rec["upstream_tb"] = {}
        for k in range(r.counts["author"]):
            irs = candidate_irs(out, k)
            if not irs:
                continue
            # IR이 없는 모듈은 run_golden이 golden IR로 채우므로 그 후보의 결과가 아니다
            if missing := sorted(p.name.removesuffix(f"__c{k}.json") for p in (out / "modules").glob(f"*__c{k}.json")
                                 if not json.loads(p.read_text()).get("ir")):
                rec["upstream_tb"][f"c{k}"] = {"ok": None, "skipped": f"IR 없는 모듈: {missing}"}
                continue
            try:
                g = run_golden.run_block(block, out / "upstream_tb" / f"c{k}", ir_override=irs)
                rec["upstream_tb"][f"c{k}"] = {"ok": g["ok"], "tbs": [
                    {"tb": x["tb"], "passed": x["tests"] - len(x["failed"]), "tests": x["tests"]} for x in g.get("e2", [])]}
            except Exception as e:  # noqa: BLE001  후보 IR이 원본 TB 구성과 맞지 않으면 그대로 기록
                rec["upstream_tb"][f"c{k}"] = {"ok": False, "error": str(e)[:500]}
    (out / "pilot.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1))
    return rec


def main(argv=None) -> int:
    blocks = (argv if argv is not None else sys.argv[1:]) or list(SPECS)
    for b in blocks:
        rec = pilot(b)
        print(json.dumps(rec, ensure_ascii=False)[:3000], flush=True)
        if rec["status"] == "paused_quota":
            print("사용량 한도로 멈춤. 한도가 풀린 뒤 같은 명령으로 이어서 실행", flush=True)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
