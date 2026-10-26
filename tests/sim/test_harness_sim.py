"""생성된 하니스 + 손으로 쓴 참조 모델(tb_pkt_parser_h)로 T1~T5를 돌리고, 버그를 넣었을 때 리포트가 원인 노드를 가리키는지 본다."""
import copy
import json
import shutil
from pathlib import Path

import pytest

pytest.importorskip("cocotb_tools.runner")
if not shutil.which("verilator"):
    pytest.skip("verilator 없음", allow_module_level=True)

from pipeline.classifier.classify import classify_findings  # noqa: E402
from pipeline.emitter.sv import emit  # noqa: E402
from pipeline.ir.model import Module  # noqa: E402
from pipeline.tb.gen import harness_source, trace_names  # noqa: E402
from pipeline.tb.report import fsm_coverage, mismatch_report  # noqa: E402
from pipeline.verify.run import sim  # noqa: E402
from test_pipeline import LIB, ROOT  # noqa: E402

HERE = Path(__file__).parent
SPEC = (ROOT / "tests" / "fixtures" / "pkt_parser.spec.md").read_text()


def _run(tmp_path, raw, seeds=(1, 2)):
    m = Module(raw)
    e = emit(m, LIB)
    (tmp_path / "pkt_parser.sv").write_text(e.text)
    (tmp_path / "pkt_parser_harness.py").write_text(harness_source(m))
    (tmp_path / "verify_only").mkdir()
    (tmp_path / "verify_only" / "pkt_parser_trace.json").write_text(json.dumps(trace_names(m)))
    r = sim([tmp_path / "pkt_parser.sv"], "pkt_parser", "tb_pkt_parser_h", HERE, tmp_path / "build", seeds=list(seeds),
            harness_dir=tmp_path, trace=tmp_path / "verify_only" / "pkt_parser_trace.json")
    return r, e, m, tmp_path / "build" / "tb_out" / str(seeds[0])


def _blk(raw, bid):
    return next(b for b in raw["blocks"] if b["id"] == bid)


def test_golden_passes_t1_to_t5(tmp_path):
    r, _, m, out = _run(tmp_path, LIB["pkt_parser"].raw)
    assert r.ok, r.findings
    results = {p.name: json.loads(p.read_text()) for p in out.glob("*.result.json")}
    assert len(results) == 5
    assert results["properties_only.result.json"]["matched"] == 0
    assert all(x["matched"] > 0 for n, x in results.items() if n != "properties_only.result.json")
    cov = fsm_coverage(m, [json.loads(p.read_text()) for p in out.glob("*.trace.json")])
    assert cov["blocks/k_fsm"]["uncovered"] == []


@pytest.mark.parametrize("block, edit, cause", [
    ("k_fsm", lambda b: b["transitions"][2].update(cond="s_valid && byte_cnt == 8'd2"), "blocks/k_fsm/transitions/tr3"),
    ("k_cnt", lambda b: b.update(body=b["body"].replace("byte_cnt - 8'd1", "byte_cnt - 8'd2")), "blocks/k_cnt"),
])
def test_injected_bug_report_contains_cause_node(tmp_path, block, edit, cause):
    raw = copy.deepcopy(LIB["pkt_parser"].raw)
    edit(_blk(raw, block))
    r, e, m, out = _run(tmp_path, raw)
    assert not r.ok
    assert {c.type for c in classify_findings(r.findings, {"pkt_parser": e.sourcemap}, {"pkt_parser": "module"})} == {"sim_mismatch"}
    test = r.findings[0].test
    rep = mismatch_report(m, json.loads((out / f"{test}.result.json").read_text()),
                          json.loads((out / f"{test}.trace.json").read_text()), SPEC)
    assert cause in rep["fanin"]["nodes"] + rep["fanin"]["fsm_transitions"]
    assert rep["fsm_trace"]["state_q"] and rep["spec"]["clauses"]


def test_t1_random_init_exposes_missing_reset(tmp_path):
    # byte_cnt의 리셋을 빼고 길이를 누적하게 바꾸면 첫 패킷의 길이가 초기값에 따라 달라진다.
    # (Verilator 5.052는 1비트 reg와 enum 상태 레지스터의 초기값은 시드와 관계없이 0으로 두었다.)
    raw = copy.deepcopy(LIB["pkt_parser"].raw)
    del next(s for s in raw["signals"] if s["name"] == "byte_cnt")["reset"]
    k = _blk(raw, "k_cnt")
    k["body"] = k["body"].replace("byte_cnt <= s_data;", "byte_cnt <= s_data + byte_cnt;")
    r, e, _, _ = _run(tmp_path, raw, seeds=(1, 2, 3, 4, 5, 6))
    assert not r.ok and r.findings[0].rule == "T1_INIT", r.findings
    assert classify_findings(r.findings[:1], {"pkt_parser": e.sourcemap}, {"pkt_parser": "module"})[0].type == "sim_mismatch"


def test_hung_sim_is_stopped(tmp_path):
    """테스트가 끝나지 않으면(RTL이 멈춤) 시간 초과로 시뮬레이터를 끝내고 마지막 테스트를 낸다."""
    tb = tmp_path / "tb"
    tb.mkdir()
    (tb / "tb_hang.py").write_text(
        "import cocotb\nfrom cocotb.clock import Clock\nfrom cocotb.triggers import ClockCycles\n\n"
        "@cocotb.test()\nasync def never_ends(dut):\n"
        "    cocotb.start_soon(Clock(dut.clk, 10, 'ns').start())\n"
        "    await ClockCycles(dut.clk, 10**12)\n")
    (tmp_path / "pkt_parser.sv").write_text(emit(LIB["pkt_parser"], LIB).text)
    r = sim([tmp_path / "pkt_parser.sv"], "pkt_parser", "tb_hang", tb, tmp_path / "build", timeout_s=15)
    assert [(f.rule, f.test) for f in r.findings] == [("SIM_TIMEOUT", "never_ends")]


def test_mid_test_reset_flushes_input_queue(tmp_path):
    m = Module(LIB["pkt_parser"].raw)
    (tmp_path / "pkt_parser.sv").write_text(emit(m, LIB).text)
    (tmp_path / "pkt_parser_harness.py").write_text(harness_source(m))
    (tmp_path / "verify_only").mkdir()
    (tmp_path / "verify_only" / "pkt_parser_trace.json").write_text(json.dumps(trace_names(m)))
    r = sim([tmp_path / "pkt_parser.sv"], "pkt_parser", "tb_pkt_parser_reset", HERE, tmp_path / "build", seeds=[1],
            harness_dir=tmp_path, trace=tmp_path / "verify_only" / "pkt_parser_trace.json")
    assert r.ok, r.findings


def test_flag_streams_drive_and_reset_flag(tmp_path):
    """번들이 없는 블록: valid 플래그 스트림은 지연과 무관하게 비교하고, drive의 첫 값은 사이클 0, 사이클 중간 리셋은
    __reset__으로 보인다 (RTLLM adder_pipe_64bit, sequence_detector, up_down_counter의 Verifier 결함)."""
    m = Module(json.loads((ROOT / "tests" / "fixtures" / "flag_top" / "pipe2.ir.json").read_text()))
    (tmp_path / "pipe2.sv").write_text(emit(m).text)
    (tmp_path / "pipe2_harness.py").write_text(harness_source(m))
    r = sim([tmp_path / "pipe2.sv"], "pipe2", "tb_pipe2_h", HERE, tmp_path / "build", harness_dir=tmp_path)
    assert r.ok, r.findings
