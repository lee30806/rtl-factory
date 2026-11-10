"""B5: RTL-BenchLS Task 1 (Round-Trip) 로더.

레코드(`data/slice_XX.jsonl` 한 줄)의 원본 RTL을 파일로 꺼내 BenchItem으로 만든다. 원본은 `reference_rtl`에 두고,
P11 왕복(원본 → IR·SPEC → SPEC만으로 재생성)의 입력과 Yosys 등가 채점의 기준으로 쓴다. 공식 채점 스크립트(JasperGold SEC,
Conformal LEC)는 쓰지 않는다.
"""
from __future__ import annotations

import json
from pathlib import Path

from experiments.bench.common.schema import BenchItem, EvalSpec


def gold_keys(rec: dict) -> list[str]:
    """원본 RTL 파일: 하위 모듈까지 합친 `<이름>_combined.v`, 없으면 `<이름>.v`, 둘 다 없으면 짝 파일 중 spec 쪽(`.v0.v`)."""
    fs = rec["files"]
    if comb := [k for k in fs if k.endswith("_combined.v")]:
        return comb
    if (k := rec["design_name"] + ".v") in fs:
        return [k]
    if v0 := [k for k in fs if k.endswith(".v0.v")]:
        return v0
    return [k for k in fs if k.endswith(".v") and k != "lib.v"]


def load_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def to_item(rec: dict, gold_root: Path) -> BenchItem:
    d = Path(gold_root) / rec["task_id"]
    d.mkdir(parents=True, exist_ok=True)
    files = []
    for k in gold_keys(rec):
        p = d / k
        p.write_text(rec["files"][k], encoding="utf-8")
        files.append(str(p))
    return BenchItem(benchmark="rtl_benchls", id=rec["task_id"], category=rec["design_type"], top=rec["top_module"],
                     prompt_text="", header_sv="", reference_rtl=files, eval=EvalSpec(kind="yosys_lec"))


def load_benchls(path: Path, gold_root: Path) -> list[BenchItem]:
    return [to_item(r, gold_root) for r in load_records(path)]
