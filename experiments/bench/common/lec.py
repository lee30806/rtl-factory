"""Yosys 형식 등가 채점 (BENCHMARK_PLAN B5).

1. `equiv_make` + `equiv_simple` + `equiv_induct`: 상태 이름이 맞는 부분을 귀납으로 증명한다. 전부 증명되면 무한 깊이 등가.
2. 안 되면 miter를 만들어 `sat`으로 반례를 찾는다. 플롭이 없으면 한 사이클 증명이 곧 완전한 증명이고,
   플롭이 있으면 첫 사이클에 리셋을 건 뒤 `depth` 사이클까지의 유한 깊이 BMC다 (`bounded`). 리셋이 없는 원본의 X는 비교하지 않는다.
   깊이는 2(리셋 사이클 다음 한 사이클), 5, 10, 20, `depth`로 늘려 가며, 한 단계가 `timeout` 안에 끝나지 않으면 판정 불가(`inconclusive`)로 남긴다.

상용 SEC(JasperGold)처럼 상태 대응을 찾아 무한 깊이를 증명하지는 못하므로, BMC 통과는 깊이 안에서 반례가 없다는 뜻이다.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

FF_RE = re.compile(r"\$_?(a?dff|sdff|dffe|adffe|sdffe|sdffce|aldff|aldffe|dffsr|dffsre|dlatch|ff)\w*")


def _load(files: list[Path], top: str, name: str) -> str:
    srcs = " ".join(str(Path(p).resolve()) for p in files)
    return (f"read_verilog -sv {srcs}; prep -flatten -top {top}; memory_map; opt_clean; rename {top} {name}; "
            f"design -stash {name}; ")


def _both(gold: list[Path], gate: list[Path], top: str) -> str:
    return (_load(gold, top, "gold") + _load(gate, top, "gate")
            + "design -copy-from gold -as gold gold; design -copy-from gate -as gate gate; ")


def _yosys(script: str, log: Path, timeout: int) -> tuple[str, bool]:
    try:
        subprocess.run(["yosys", "-q", "-l", str(log), "-p", script], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return (log.read_text(errors="replace") if log.exists() else ""), True
    return (log.read_text(errors="replace") if log.exists() else ""), False


def yosys_lec(gold: list[Path], gate: list[Path], top: str, out: Path, resets: list[dict] | None = None,
              depth: int = 30, timeout: int = 300) -> dict:
    """resets: [{"port": 이름, "active": "high"|"low"}]. 반환 ok, method(equiv|comb_sat|bmc), bounded, depth."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    if not shutil.which("yosys"):
        return {"ok": False, "kind": "yosys_lec", "error": "yosys가 설치되어 있지 않음"}
    log, timed_out = _yosys(_both(gold, gate, top) + "equiv_make gold gate equiv; hierarchy -top equiv; async2sync; "
                            "equiv_simple -seq 5; equiv_induct -seq 5; equiv_status", out / "lec.log", timeout)
    m = re.search(r"Of those cells (\d+) are proven and (\d+) are unproven", log)
    if m and int(m.group(2)) == 0:
        return {"ok": True, "kind": "yosys_lec", "method": "equiv", "bounded": False, "proven_cells": int(m.group(1))}

    sets = " ".join(f"-set-at 1 in_{r['port']} {1 if r['active'] == 'high' else 0}" for r in resets or [])
    miter = (_both(gold, gate, top) + "miter -equiv -flatten -make_outputs -ignore_gold_x gold gate miter; hierarchy -top miter; "
             f"async2sync; dffunmap; tee -o {out / 'miter_stat.txt'} stat; ")
    # 깊이를 늘려 가며 돈다: 큰 메모리가 있으면 목표 깊이까지 못 가도 어디까지 반례가 없었는지 남는다
    reached, res = 0, {}
    for d in [x for x in (2, 5, 10, 20) if x < depth] + [depth]:
        blog, timed_out = _yosys(miter + f"sat -verify -seq {d} {sets} {'-prove-skip 1' if sets else ''} "
                                 "-set-init-undef -set-def-inputs -prove trigger 0 miter", out / "lec_bmc.log", timeout)
        stat = (out / "miter_stat.txt").read_text(errors="replace") if (out / "miter_stat.txt").exists() else ""
        seq = bool(FF_RE.search(stat))
        res = {"kind": "yosys_lec", "method": "bmc" if seq else "comb_sat", "bounded": seq, "resets": resets or []}
        if timed_out:
            return {**res, "ok": False, "inconclusive": True, "depth": reached, "error": f"깊이 {d}가 {timeout}초 안에 끝나지 않음"}
        if "SAT proof finished - no model found: SUCCESS" not in blog:
            err = re.findall(r"ERROR:.*", log + blog)
            cex = "proof did fail" in blog
            return {**res, "ok": False, "counterexample": cex, "depth": d,
                    "log_tail": (blog[-800:] if cex or not err else err[0])[:800]}
        reached = d
        if not seq:
            return {**res, "ok": True, "depth": 0}
    return {**res, "ok": True, "depth": reached}
