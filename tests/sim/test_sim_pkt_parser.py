import glob
import shutil
from pathlib import Path

import pytest

cocotb_tools = pytest.importorskip("cocotb_tools.runner")
if not shutil.which("verilator"):
    pytest.skip("verilator 없음", allow_module_level=True)

from pipeline.emitter.sv import emit  # noqa: E402
from pipeline.ir.model import load_library  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def _simulate(tmp_path, module):
    """방출된 RTL을 Verilator로 빌드해 cocotb 테스트를 돌리고 (전체 수, 실패 이름)을 돌려준다."""
    import xml.etree.ElementTree as ET

    src = tmp_path / "pkt_parser.sv"
    src.write_text(emit(module, LIB).text)
    runner = cocotb_tools.get_runner("verilator")
    runner.build(sources=[src], hdl_toplevel="pkt_parser", build_dir=tmp_path / "build",
                 build_args=["-Wall", "-Wno-DECLFILENAME", "-Wno-UNUSEDSIGNAL"], always=True)
    results = tmp_path / "results.xml"
    try:
        runner.test(hdl_toplevel="pkt_parser", test_module="tb_pkt_parser", test_dir=Path(__file__).parent,
                    build_dir=tmp_path / "build", results_xml=str(results))
    except (Exception, SystemExit):
        pass  # cocotb runner는 테스트가 실패하면 SystemExit를 던진다. 판정은 결과 파일로 한다.
    cases = ET.parse(results).getroot().findall(".//testcase")
    return len(cases), [c.get("name") for c in cases if c.find("failure") is not None or c.find("error") is not None]


LIB = load_library(sorted(glob.glob(str(ROOT / "tests" / "fixtures" / "*.ir.json"))))


def test_emitted_pkt_parser_matches_spec_model(tmp_path):
    total, failed = _simulate(tmp_path, LIB["pkt_parser"])
    assert total == 3 and not failed, failed


def test_testbench_catches_injected_bug(tmp_path):
    import copy

    from pipeline.ir.model import Module

    raw = copy.deepcopy(LIB["pkt_parser"].raw)
    k = next(b for b in raw["blocks"] if b["id"] == "k_cnt")
    k["body"] = k["body"].replace("pkt_valid <= (byte_cnt == 8'd1)", "pkt_valid <= (byte_cnt == 8'd2)")
    total, failed = _simulate(tmp_path, Module(raw))
    assert total == 3 and failed, "버그를 넣었는데 테스트가 통과함"
