import copy
import shutil
from pathlib import Path

import pytest

pytest.importorskip("cocotb_tools.runner")
if not shutil.which("verilator"):
    pytest.skip("verilator 없음", allow_module_level=True)

from pipeline.classifier.classify import classify_findings  # noqa: E402
from pipeline.emitter.sv import emit  # noqa: E402
from pipeline.ir.model import Module  # noqa: E402
from pipeline.verify.run import sim  # noqa: E402
from test_pipeline import LIB  # noqa: E402

HERE = Path(__file__).parent


def _run(tmp_path, raw, tests="tb_pkt_parser"):
    e = emit(Module(raw), LIB)
    src = tmp_path / "pkt_parser.sv"
    src.write_text(e.text)
    return sim([src], "pkt_parser", tests, HERE, tmp_path / "build", seed=1), e


def test_e2_pass(tmp_path):
    r, _ = _run(tmp_path, LIB["pkt_parser"].raw)
    assert r.ok and r.findings == [] and r.stage == "E2"


def test_e2_mismatch_is_classified_for_repair(tmp_path):
    raw = copy.deepcopy(LIB["pkt_parser"].raw)
    k = next(b for b in raw["blocks"] if b["id"] == "k_cnt")
    k["body"] = k["body"].replace("pkt_valid <= (byte_cnt == 8'd1)", "pkt_valid <= (byte_cnt == 8'd2)")
    r, e = _run(tmp_path, raw)
    assert not r.ok and {f.rule for f in r.findings} == {"AssertionError"}
    assert all("pkt_valid RTL=" in f.message and f.test for f in r.findings)
    assert {c.type for c in classify_findings(r.findings, {"pkt_parser": e.sourcemap}, {"pkt_parser": "module"})} == {"sim_mismatch"}


def test_e2_missing_test_module_is_tb_error(tmp_path):
    r, e = _run(tmp_path, LIB["pkt_parser"].raw, tests="tb_does_not_exist")
    assert not r.ok
    assert {c.type for c in classify_findings(r.findings, {"pkt_parser": e.sourcemap}, {})} == {"tb_error"}
