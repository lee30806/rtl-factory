import copy

import pytest

from pipeline.ir.model import Module
from pipeline.tb.gen import GenError, harness_config, harness_source, trace_names
from pipeline.tb.report import fanin, fsm_coverage, mismatch_report
from test_pipeline import LIB, ROOT

SPEC = (ROOT / "tests" / "fixtures" / "pkt_parser.spec.md").read_text()


def test_harness_config_uses_l1_only():
    cfg = harness_config(LIB["pkt_parser"])
    assert cfg["clocks"] == {"sys": ("clk", 10.0)} and cfg["resets"] == {"sys": ("rst_n", 0)}
    assert cfg["bundles"] == [{"name": "s", "protocol": "valid_ready", "domain": "sys", "dut_drives": False,
                               "valid": "s_valid", "ready": "s_ready", "data": ["s_data"]}]
    assert cfg["outputs"] == [{"name": "pkt_valid", "domain": "sys", "width": 1}]
    src = harness_source(LIB["pkt_parser"])
    for internal in ("byte_cnt", "sync_hit", "state_q", "state_e"):  # Verifier가 보는 파일에 L2가 새지 않음
        assert internal not in src
    assert harness_config(LIB["pkt_rx_top"])["clocks"] == {"sys": ("clk", 2.0)}
    assert "def make(dut, model=None, *, name):" in src  # 테스트마다 결과 파일이 겹치지 않게 이름은 필수


def test_trace_names_include_internals():
    assert trace_names(LIB["pkt_parser"]) == {
        "names": {"s_valid": "sys", "s_ready": "sys", "s_data": "sys", "pkt_valid": "sys",
                  "byte_cnt": "sys", "sync_hit": None, "state_q": "sys"},
        "outputs": ["s_ready", "pkt_valid"], "qualifiers": {}, "domains": ["sys"]}


def test_source_bundle_and_unknown_protocol():
    raw = copy.deepcopy(LIB["pkt_parser"].raw)
    raw["ports"].append({"bundle": "m", "protocol": "valid_ready", "role": "source", "domain": "sys", "signals": [
        {"name": "m_valid", "dir": "output", "type": "logic", "kind": "reg", "reset": "1'b0"},
        {"name": "m_ready", "dir": "input", "type": "logic"},
        {"name": "m_len", "dir": "output", "type": "logic [7:0]", "kind": "reg", "reset": "'0"}]})
    b = harness_config(Module(raw))["bundles"][1]
    assert (b["dut_drives"], b["valid"], b["ready"], b["data"]) == (True, "m_valid", "m_ready", ["m_len"])
    raw["ports"][-1]["protocol"] = "axi_stream"
    with pytest.raises(GenError):
        harness_config(Module(raw))


def test_fanin_stops_at_inputs():
    nodes, sigs = fanin(LIB["pkt_parser"], ["s_ready"])
    assert nodes == ["blocks/k_hit"] and sigs == ["s_data", "s_ready", "s_valid"]
    nodes, _ = fanin(LIB["pkt_parser"], ["pkt_valid"])
    assert nodes == ["blocks/k_cnt", "blocks/k_fsm", "blocks/k_hit"]
    assert fanin(LIB["pkt_rx_top"], ["pkt_valid"], LIB)[0] == ["instances/u_parser"]
    assert fanin(LIB["pkt_rx_top"], ["s_ready"], LIB)[0] == ["instances/u_parser"]  # 번들 연결


def _trace(states):
    return [{"domain": "sys", "cycle": i, "values": {"state_q": s, "byte_cnt": i, "pkt_valid": 0}} for i, s in enumerate(states)]


def test_mismatch_report_bundles_four_parts():
    result = {"test": "t", "scoreboard": {"kind": "missing", "stream": "pkt_valid", "expected": {"pkt_valid": 1},
                                          "got": None, "cycle": 30, "req": "FR-03", "expected_since": 4},
              "protocol": [], "properties": []}
    rep = mismatch_report(LIB["pkt_parser"], result, _trace([0, 1, 2, 2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]), SPEC)
    assert rep["first_failure"]["anchor"] == 4  # missing은 기대가 생긴 시점 기준
    assert [x["state"] for x in rep["fsm_trace"]["state_q"]][:5] == ["IDLE", "HEADER", "PAYLOAD", "PAYLOAD", "IDLE"]
    assert "blocks/k_fsm/transitions/tr3" in rep["fanin"]["fsm_transitions"] and "blocks/k_cnt" in rep["fanin"]["nodes"]
    assert rep["fanin"]["values"][0] == {"cycle": 0, "byte_cnt": 0, "pkt_valid": 0, "state_q": 0}
    assert rep["spec"]["clauses"] == ["- FR-03: 페이로드 N바이트를 모두 받은 다음 사이클에 pkt_valid를 한 사이클 1로 만든다."]
    assert mismatch_report(LIB["pkt_parser"], {"scoreboard": None, "protocol": [], "properties": []}, [], SPEC) is None


def test_missing_expected_at_cycle_zero_anchors_at_zero():
    result = {"scoreboard": {"kind": "missing", "stream": "pkt_valid", "expected": {"pkt_valid": 1}, "got": None,
                             "cycle": 0, "req": None, "expected_since": 0, "domain": "sys", "expected_domain": "sys"},
              "protocol": [], "properties": []}
    rep = mismatch_report(LIB["pkt_parser"], result, _trace([0] * 40), SPEC)
    assert rep["first_failure"]["anchor"] == 0 and [v["cycle"] for v in rep["fanin"]["values"]] == [0, 1, 2]


def test_window_follows_anchor_domain():
    trace = [{"domain": d, "cycle": i, "values": {"state_q": 0}} for i in range(5) for d in ("sys", "io")]
    result = {"scoreboard": None, "protocol": [],
              "properties": [{"property": "p", "cycle": 2, "domain": "io", "reason": "x", "req": None}]}
    rep = mismatch_report(LIB["pkt_parser"], result, trace, SPEC)
    assert {v["cycle"] for v in rep["fanin"]["values"]} == {0, 1, 2, 3, 4} and len(rep["fanin"]["values"]) == 5


def test_earliest_failure_wins():
    result = {"scoreboard": {"kind": "mismatch", "stream": "pkt_valid", "expected": {}, "got": {}, "cycle": 9, "req": None},
              "protocol": [], "properties": [{"property": "ready_always", "cycle": 3, "reason": "x", "req": "FR-04"}]}
    rep = mismatch_report(LIB["pkt_parser"], result, _trace([0] * 12), SPEC)
    assert rep["first_failure"]["check"] == "T4" and rep["spec"]["req"] == "FR-04"


def test_fsm_coverage():
    cov = fsm_coverage(LIB["pkt_parser"], [_trace([0, 1, 2, 2]), _trace([0, 0])])
    assert cov == {"blocks/k_fsm": {"covered": ["tr1", "tr2"], "uncovered": ["tr3"], "unobservable": []}}


def test_valid_ready_checker_ignores_reset():
    """T2: 리셋이 걸린 표본과 그 다음 표본에서 valid가 내려가는 것은 위반이 아니다 (P8a crc reset_mid). 리셋 밖에서는 잡는다."""
    pytest.importorskip("cocotb")
    from types import SimpleNamespace as N

    from pipeline.tb.lib import ValidReadyChecker

    dut = N(m_valid=N(value=0), m_ready=N(value=0), m_data=N(value=0), rst=N(value=0))
    b = {"name": "m", "domain": "sys", "valid": "m_valid", "ready": "m_ready", "data": ["m_data"]}
    c = ValidReadyChecker(dut, b, ("rst", 1))

    def step(v, r, d, rst=0):
        dut.m_valid.value, dut.m_ready.value, dut.m_data.value, dut.rst.value = v, r, d, rst
        c.sample(step.n)
        step.n += 1
    step.n = 0
    step(1, 0, 5)
    step(1, 0, 5, rst=1)   # 리셋이 걸림
    step(0, 0, 0)          # 동기 리셋이 반영되어 valid가 내려감
    step(1, 0, 7)
    step(0, 0, 0)          # 리셋 밖: 위반
    assert [x["cycle"] for x in c.violations] == [4]


def test_requirement_ids_accept_lists():
    """cover_protocol, add_property의 req는 ID 하나나 목록 (P8a async_fifo: 목록을 넘겨 TB 실행 오류)."""
    pytest.importorskip("cocotb")
    from pipeline.tb.lib import req_ids

    assert req_ids({"A-01"}, "A-02", ["A-03", "A-01"], ("A-04",)) == ["A-01", "A-02", "A-03", "A-04"]


def test_ready_pattern_repeats_or_holds():
    """set_ready의 pattern은 되풀이하고, then을 주면 한 번 쓴 뒤 그 값을 유지한다 (P8a async_fifo fill_up 오해)."""
    pytest.importorskip("cocotb")
    from types import SimpleNamespace as N

    from pipeline.tb.lib import ReadyDriver

    def run(then):
        dut = N(m_ready=N(value=0))
        d = ReadyDriver(dut, {"ready": "m_ready"}, [0, 0, 1])
        d.then = then
        seen = []
        for _ in range(7):
            d.on_falling()
            seen.append(dut.m_ready.value)
        return seen
    assert run(None) == [0, 0, 1, 0, 0, 1, 0]
    assert run(1) == [0, 0, 1, 1, 1, 1, 1]
