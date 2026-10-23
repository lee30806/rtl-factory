"""번역기 검사: `lang: py` 모듈의 설계 모델(`pysim`)과 방출한 RTL을 같은 무작위 자극으로 비교한다.

클럭 그룹(기준 도메인과 그 분주 도메인)이 여럿이면 그룹마다 서로소인 반주기를 주고, 조합을 세 번 바꿔 가며
도메인 사이 위상을 바꾼다 (`phases`). 사이클 수는 조합들이 나눠 쓴다.

이 검사가 보는 것은 Python 조각 → SV 번역(과 실행기)이 같은 의미인지다. SPEC 해석이 맞는지는 보지 않는다
(그것은 Verifier 테스트와 DV Pod의 독립 TB가 본다). 그래서 불일치는 Repair로 보내지 않고 생성기 버그로 기록한다.

    python3 -m pipeline.verify.pyequiv <ir.json...> --top <모듈> -o <build_dir> [--cells <셀.sv>...]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pipeline.ir.model import Module
from pipeline.ir.pysim import supported
from pipeline.tb.gen import comb_view, pad_top
from pipeline.verify.run import sim

TB_DIR = Path(__file__).resolve().parent
CYCLES = 2000
RESET_CYCLES = 3
RESET_PROB = 0.01
HALF_NS = [5, 7, 11, 13, 17, 19, 23]   # 그룹 기준 클럭의 반주기 후보 (홀수, 서로소)
PHASES = 3


def phases(top: Module, n: int = PHASES) -> list[dict[str, int]]:
    """그룹 기준 도메인 → 반주기(ns) 조합. 그룹이 하나면 한 가지(10ns 주기)."""
    roots = list(dict.fromkeys(top.clock_root(d)[0] for d in top.domains))
    if len(roots) <= 1:
        return [{r: HALF_NS[0] for r in roots}]
    return [{r: HALF_NS[(i * 2 + j) % len(HALF_NS)] for j, r in enumerate(roots)} for i in range(n)]


def check(top: str, library: dict[str, Module], files: list[Path], build_dir: Path, cycles: int = CYCLES,
          seed: int = 1) -> dict:
    """{"ok", "skipped"?, "mismatch"?, "error"?}. 실행기가 모르는 구성이면 건너뛴다 (skipped에 이유)."""
    if (why := supported(library[top], library)) is not None:
        return {"ok": True, "skipped": why}
    build_dir = Path(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)
    out = build_dir / "pyequiv_result.json"
    out.unlink(missing_ok=True)
    cfg = build_dir / "pyequiv.json"
    ph = phases(Module(comb_view(library[top].raw)))   # 조합 top은 가상 클럭 하나
    cfg.write_text(json.dumps({"top": top, "library": {n: m.source_raw for n, m in library.items()},
                               "cycles": max(1, cycles // len(ph)), "phases": ph, "seed": seed, "reset_cycles": RESET_CYCLES, "reset_prob": RESET_PROB, "out": str(out.resolve())},
                              ensure_ascii=False), encoding="utf-8")
    pads, sim_top = pad_top(library[top].raw, build_dir / "pad")   # inout top은 하니스와 같은 패드 래퍼를 top으로
    r = sim([*map(Path, files), *pads], sim_top, "pyequiv_tb", TB_DIR, build_dir, env={"PYEQUIV_CFG": str(cfg.resolve())})
    if out.exists():
        return json.loads(out.read_text(encoding="utf-8"))
    return {"ok": False, "error": "; ".join(f.message[:300] for f in r.findings) or "결과 파일이 없음"}


def main(argv: list[str] | None = None) -> int:
    from pipeline.emitter.sv import emit
    from pipeline.ir.model import load_library

    ap = argparse.ArgumentParser(description="번역기 검사 (설계 모델 대 RTL)")
    ap.add_argument("ir", nargs="+")
    ap.add_argument("--top", required=True)
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--cells", nargs="*", default=[])
    ap.add_argument("--cycles", type=int, default=CYCLES)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args(argv)
    lib = load_library(a.ir)
    out = Path(a.out)
    (out / "rtl").mkdir(parents=True, exist_ok=True)
    files = [Path(c) for c in a.cells]
    for n, m in lib.items():
        (out / "rtl" / f"{n}.sv").write_text(emit(m, lib).text, encoding="utf-8")
        files.append(out / "rtl" / f"{n}.sv")
    res = check(a.top, lib, files, out, a.cycles, a.seed)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
