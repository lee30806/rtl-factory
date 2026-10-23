"""P10 다중 클럭과 리셋: 시험용 Block Top(mc_top)의 RTL을 하니스로 돌리고, Python 조각 판을 번역기 검사(서로 다른 주기 조합 셋)로 본다."""
import json
import shutil

import pytest

pytest.importorskip("cocotb_tools.runner")
if not shutil.which("verilator"):
    pytest.skip("verilator 없음", allow_module_level=True)

from pipeline.emitter.sv import emit  # noqa: E402
from pipeline.ir.model import load_library  # noqa: E402
from pipeline.tb.gen import harness_source  # noqa: E402
from pipeline.verify.pyequiv import check  # noqa: E402
from pipeline.verify.run import sim  # noqa: E402
from test_pipeline import ROOT  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "multiclock"
STUBS = sorted((ROOT / "pipeline" / "emitter" / "stubs").glob("*.sv"))
HERE = ROOT / "tests" / "sim"


def _lib():
    return load_library([FIX / "mc_src.ir.json", FIX / "mc_top.ir.json"])


def _files(tmp_path, lib, edit=None):
    """표준 셀과 방출한 RTL. edit: (파일 이름, 바꿀 문자열, 새 문자열)."""
    (tmp_path / "rtl").mkdir(parents=True, exist_ok=True)
    texts = {p.name: p.read_text() for p in STUBS} | {f"{n}.sv": emit(m, lib).text for n, m in lib.items()}
    out = []
    for name, text in texts.items():
        if edit and name == edit[0]:
            assert edit[1] in text
            text = text.replace(edit[1], edit[2])
        (tmp_path / "rtl" / name).write_text(text)
        out.append(tmp_path / "rtl" / name)
    return out


def test_harness_runs_multiclock_block(tmp_path):
    lib = _lib()
    (tmp_path / "h").mkdir()
    (tmp_path / "h" / "mc_top_harness.py").write_text(harness_source(lib["mc_top"]))
    r = sim(_files(tmp_path, lib), "mc_top", "tb_mc_top_h", HERE, tmp_path / "build", harness_dir=tmp_path / "h")
    assert r.ok, r.findings
    res = json.loads(next((tmp_path / "build" / "tb_out").glob("*/bytes_and_soft_reset.result.json")).read_text())
    assert res["matched"] == 40 and res["properties"] == [] and res["protocol"] == []


def test_translation_matches_model_over_three_clock_phases(tmp_path):
    lib = _lib()
    res = check("mc_top", lib, _files(tmp_path, lib), tmp_path / "b", cycles=3000)
    assert res["ok"] and res["mismatch"] is None, res
    assert len(res["phases"]) == 3 and len({tuple(sorted(p.items())) for p in res["phases"]}) == 3


@pytest.mark.parametrize("edit, output", [
    (("cdc_fifo.sv", "assign dst_valid = rgray != wq2;", "assign dst_valid = rgray != wq1;"), "out_valid"),
    (("sync_pulse.sv", "assign q = s2 ^ s3;", "assign q = s1 ^ s2;"), "pulse_out"),
    (("rst_sync.sv", "rst_n_sync <= s1;", "rst_n_sync <= 1'b1;"), "in_ready"),
    (("mc_src.sv", "posedge clk_slow", "posedge clk"), "cnt_out"),
])
def test_wrong_cell_or_clock_is_caught(tmp_path, edit, output):
    lib = _lib()
    res = check("mc_top", lib, _files(tmp_path, lib, edit), tmp_path / "b", cycles=1500)
    assert not res["ok"] and res["mismatch"]["output"] == output, res
