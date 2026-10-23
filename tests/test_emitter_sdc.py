import copy
import json
import shutil
import subprocess
import sys

import pytest

from pipeline.emitter.sdc import emit_sdc
from pipeline.emitter.sv import EmitError, emit
from pipeline.ir.model import Module
from pipeline.validator.checks import validate
from test_pipeline import LIB, ROOT, STUB


def _parser_with_sync():
    """pkt_parser에 비동기 입력 irq와 동기화 셀을 더한 모듈 (계층 아래쪽 CDC)."""
    raw = copy.deepcopy(LIB["pkt_parser"].raw)
    raw["ports"].append({"name": "irq", "dir": "input", "type": "logic", "domain": None})
    raw["ports"].append({"name": "irq_s", "dir": "output", "type": "logic", "domain": "sys", "kind": "wire"})
    raw["blocks"].append({"id": "s_irq", "kind": "cdc_sync", "src": "irq", "src_domain": None,
                          "dst": "irq_s", "dst_domain": "sys", "style": "2ff"})
    return raw


def _two_domain_top():
    top = copy.deepcopy(LIB["pkt_rx_top"].raw)
    top["domains"][0]["async_to"] = ["io"]
    top["domains"].append({"name": "io", "clock": "io_clk", "freq_mhz": 125, "edge": "pos",
                           "reset": {"port": "io_rst_n", "active": "low", "async": True}, "async_to": ["sys"]})
    top["ports"] += [{"name": "flag_io", "dir": "input", "type": "logic", "domain": "io"},
                     {"name": "flag_sys", "dir": "output", "type": "logic", "domain": "sys", "kind": "wire"},
                     {"name": "irq", "dir": "input", "type": "logic", "domain": None},
                     {"name": "irq_s", "dir": "output", "type": "logic", "domain": "sys", "kind": "wire"}]
    top["blocks"] = [{"id": "s_flag", "kind": "cdc_sync", "src": "flag_io", "src_domain": "io",
                      "dst": "flag_sys", "dst_domain": "sys", "style": "2ff"}]
    top["connections"] += [{"id": "c3", "from": "self.irq", "to": "u_parser.irq"},
                           {"id": "c4", "from": "u_parser.irq_s", "to": "self.irq_s"}]
    return top


def test_sdc_skeleton_clocks_groups_and_cdc():
    child = Module(_parser_with_sync())
    lib = {"pkt_parser": child}
    top = Module(_two_domain_top())
    assert validate(child, lib) == [] and validate(top, lib) == []
    sdc = emit_sdc(top, lib)
    assert "create_clock -name sys -period 2.0 [get_ports clk]" in sdc
    assert "create_clock -name io -period 8.0 [get_ports io_clk]" in sdc
    assert sdc.count("set_clock_groups -asynchronous -group [get_clocks io] -group [get_clocks sys]") == 1
    assert "# io -> sys: 비동기 클럭 그룹이 덮음 (u_s_flag/d)" in sdc and "set_max_delay" not in sdc
    assert "set_false_path -through [get_pins u_parser/irq] -to [get_pins u_parser/u_s_irq/d]" in sdc
    assert "set_false_path -from [get_ports irq]" not in sdc
    assert emit_sdc(top, lib) == sdc
    # 비동기 그룹으로 묶지 않은 도메인 사이의 CDC는 목적지 클럭 한 주기로 제한한다.
    raw = _two_domain_top()
    for d in raw["domains"]:
        d.pop("async_to")
    sdc = emit_sdc(Module(raw), lib)
    assert "set_clock_groups" not in sdc
    assert "set_max_delay 2.0 -from [get_clocks io] -to [get_pins u_s_flag/d]" in sdc


def test_sdc_only_for_tops_and_missing_freq_is_reported():
    with pytest.raises(EmitError):
        emit_sdc(LIB["pkt_parser"], LIB)
    top = copy.deepcopy(LIB["pkt_rx_top"].raw)
    del top["domains"][0]["freq_mhz"]
    sdc = emit_sdc(Module(top), LIB)
    assert "create_clock" not in sdc and "freq_mhz 없음" in sdc


def _external_parser(tmp_path):
    """pkt_parser를 사람이 쓴 기존 블록(external)으로 등록한다. RTL은 방출 결과로 대신한다."""
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "pkt_parser.sv").write_text(emit(LIB["pkt_parser"], LIB).text)
    raw = {k: copy.deepcopy(v) for k, v in LIB["pkt_parser"].raw.items() if k in ("module", "params", "domains", "ports")}
    raw.update(role="external", rtl_files=["rtl/pkt_parser.sv"])
    return raw


def test_external_is_referenced_not_emitted(tmp_path):
    ext = Module(_external_parser(tmp_path))
    assert validate(ext, {}) == [] and validate(LIB["pkt_rx_top"], {"pkt_parser": ext}) == []
    with pytest.raises(EmitError):
        emit(ext)
    (tmp_path / "ir").mkdir()
    (tmp_path / "pkt_parser.ir.json").write_text(json.dumps(ext.raw))
    (tmp_path / "ir" / "pkt_rx_top.ir.json").write_text(json.dumps(LIB["pkt_rx_top"].raw))
    out = tmp_path / "out"
    p = subprocess.run([sys.executable, "-m", "pipeline.emitter.sv", str(tmp_path / "pkt_parser.ir.json"),
                        str(tmp_path / "ir" / "pkt_rx_top.ir.json"), "-o", str(out)], cwd=ROOT, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert sorted(x.name for x in out.iterdir()) == ["filelist.f", "pkt_rx_top.sdc", "pkt_rx_top.srcmap.json", "pkt_rx_top.sv"]
    assert (out / "filelist.f").read_text().split() == [str((tmp_path / "rtl" / "pkt_parser.sv").resolve()),
                                                         str((out / "pkt_rx_top.sv").resolve())]
    if shutil.which("verilator"):  # 절대 경로라 다른 디렉터리에서 -f로 읽어도 된다
        r = subprocess.run(["verilator", "--lint-only", "-Wall", "-f", str(out / "filelist.f"), "--top-module", "pkt_rx_top"],
                           cwd=ROOT, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_emitted_two_domain_design_lints_clean(tmp_path):
    if not shutil.which("verilator"):
        pytest.skip("verilator 없음")
    lib = {"pkt_parser": Module(_parser_with_sync())}
    top = Module(_two_domain_top())
    for m in (lib["pkt_parser"], top):
        (tmp_path / f"{m.name}.sv").write_text(emit(m, {**lib, top.name: top}).text)
    (tmp_path / "sync_2ff.sv").write_text(STUB)
    # io 도메인은 동기화 셀의 출발점으로만 쓰여 io_clk/io_rst_n이 미사용으로 나온다. 시험용 설계의 한계라 끈다.
    r = subprocess.run(["verilator", "--lint-only", "-Wall", "-Wno-UNUSEDSIGNAL", "--top-module", "pkt_rx_top", "sync_2ff.sv",
                        "pkt_parser.sv", "pkt_rx_top.sv"], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
