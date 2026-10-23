"""이식 시험 회귀: 손으로 옮긴 IR의 방출 RTL이 원본 블록의 기존 테스트벤치를 통과하고, E1 결과가 골격을 가리키지 않는다."""
import json
import shutil
import sys
from pathlib import Path

import pytest

pytest.importorskip("cocotbext.axi")
if not shutil.which("verilator"):
    pytest.skip("verilator 없음", allow_module_level=True)

GOLDEN = Path(__file__).resolve().parents[1] / "golden"
sys.path.insert(0, str(GOLDEN))
import run_golden  # noqa: E402

from pipeline.classifier.classify import classify_findings  # noqa: E402
from pipeline.ir.model import load_library  # noqa: E402
from pipeline.verify.run import Finding  # noqa: E402

BLOCKS = sorted(p.parent.name for p in GOLDEN.glob("*/golden.json"))


@pytest.mark.parametrize("block", BLOCKS)
def test_emitted_rtl_passes_upstream_testbench(tmp_path, block):
    r = run_golden.run_block(block, tmp_path)
    assert r["ok"], json.dumps(r, ensure_ascii=False, indent=1)
    assert all(x["tests"] > 0 for x in r["e2"])
    # E1 결과는 모두 IR 노드(L1 선언 등)로 돌아가야 한다. 골격(Emitter가 쓴 줄)을 가리키면 생성기 버그다.
    cfg = json.loads((GOLDEN / block / "golden.json").read_text())
    lib = load_library([GOLDEN / block / f for f in cfg["ir"]])
    maps = {n: json.loads((tmp_path / block / "ir" / f"{n}.srcmap.json").read_text()) for n in lib if lib[n].role != "external"}
    for top, lines in r["e1"].items():
        for ln in lines:
            rule, loc = ln.split(" ", 2)[:2]
            f = Finding("verilator", rule, loc.split(":")[0], int(loc.split(":")[1]), ln)
            (c,) = classify_findings([f], maps, {n: m.role for n, m in lib.items()})
            assert c.type != "lint_skeleton", ln
