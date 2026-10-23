import copy
import shutil

import pytest

from pipeline.classifier.classify import TYPES, classify_findings, classify_issues, classify_rejections, locate
from pipeline.emitter.sv import emit
from pipeline.ir.model import Module
from pipeline.patch.apply import apply
from pipeline.validator.checks import Issue, validate
from pipeline.verify.run import Finding, lint, parse_verilator
from test_pipeline import LIB

RAW = LIB["pkt_parser"].raw
EM = emit(LIB["pkt_parser"], LIB)
LINES = EM.text.split("\n")
MAPS, ROLES = {"pkt_parser": EM.sourcemap}, {"pkt_parser": "module"}


def _line(text):
    return next(i + 1 for i, ln in enumerate(LINES) if text in ln)


def _f(line, rule="WIDTH"):
    return Finding("verilator", rule, "out/pkt_parser.sv", line, "msg", "warning")


def test_every_type_has_a_route():
    assert len(TYPES) == 11
    assert {r for _, r in TYPES.values()} == {"retry", "repair", "human", "verifier", "arbiter", "architect"}


def test_validator_issues():
    out = classify_issues("pkt_parser", [Issue("V1", "blocks/k_cnt", "x"), Issue("V5", "ports/pkt_valid", "x"),
                                         Issue("V11", "pkt_parser/l1", "x"), Issue("V9", "blocks/k_fsm", "x", "warning")])
    assert [(c.type, c.route, c.node) for c in out] == [
        ("schema", "retry", "blocks/k_cnt"), ("ir_semantic", "repair", "ports/pkt_valid"),
        ("interface_change", "architect", "pkt_parser/l1")]


@pytest.mark.parametrize("text, node, type_", [
    ("byte_cnt <= s_data", "blocks/k_cnt/body", "lint_fragment"),
    ("if (s_valid && byte_cnt == 8'd1) state_d = IDLE", "blocks/k_fsm/transitions/tr3", "lint_fragment"),
    ("logic [7:0] byte_cnt;", "signals/byte_cnt", "lint_fragment"),
    ("state_q <= IDLE;", "blocks/k_fsm/reset_state", "lint_fragment"),
    ("pkt_valid <= 1'b0;", "ports/pkt_valid", "interface_change"),   # 리셋값이 동결된 L1에 있음
    ("always_ff @(posedge clk", "blocks/k_fsm", "lint_skeleton"),     # Emitter가 쓴 줄
    ("module pkt_parser", None, "lint_skeleton"),
    ("input  logic [7:0] s_data", "ports/s", "interface_change"),   # 포트 타입 자체는 L1

])
def test_lint_lines_map_to_ir_nodes(text, node, type_):
    (c,) = classify_findings([_f(_line(text))], MAPS, ROLES)
    assert (c.type, c.node) == (type_, node)


def test_unknown_file_and_external_report_go_to_human():
    ext = Finding("superlint", "W123", "vendor/sync_2ff.sv", 3, "x")
    (c,) = classify_findings([ext], MAPS, ROLES)
    assert (c.type, c.route, c.module) == ("lint_skeleton", "human", None)
    ext = Finding("dclint", "R9", "out/pkt_parser.sv", _line("sync_hit = s_valid"), "x")
    (c,) = classify_findings([ext], MAPS, ROLES)
    assert (c.type, c.node) == ("lint_fragment", "blocks/k_hit/body")


def test_sim_results():
    out = classify_findings([Finding("cocotb", "AssertionError", "tb.py", 3, "cycle 5: pkt_valid", test="t1"),
                             Finding("cocotb", "AttributeError", "tb.py", 7, "no child", test="t2"),
                             Finding("cocotb", "NO_RESULTS", None, None, "x")], MAPS, ROLES)
    assert [(c.type, c.route) for c in out] == [("sim_mismatch", "repair"), ("tb_error", "verifier"), ("tb_error", "verifier")]


def test_unused_input_port_goes_to_repair_in_module_but_architect_in_top():
    (c,) = classify_findings([_f(_line("input  logic [7:0] s_data"), "UNUSEDSIGNAL")], MAPS, ROLES)
    assert (c.type, c.route, c.node) == ("ir_semantic", "repair", "ports/s")
    (c,) = classify_findings([_f(_line("input  logic [7:0] s_data"), "UNUSEDSIGNAL")], MAPS, {"pkt_parser": "top"})
    assert c.type == "interface_change"


def test_instance_pins_map_to_connections():
    top = emit(LIB["pkt_rx_top"], LIB)
    lines = top.text.split("\n")
    ln = next(i + 1 for i, x in enumerate(lines) if ".pkt_valid(" in x)
    f = Finding("verilator", "PINCONNECTEMPTY", "pkt_rx_top.sv", ln, "x", "warning")
    (c,) = classify_findings([f], {"pkt_rx_top": top.sourcemap}, {"pkt_rx_top": "module"})
    assert (c.type, c.node) == ("lint_fragment", "connections/c2")
    (c,) = classify_findings([f], {"pkt_rx_top": top.sourcemap}, {"pkt_rx_top": "block_top"})
    assert c.type == "interface_change"  # block_top의 연결은 동결 범위


def test_patch_rejections():
    res = apply(RAW, [{"op": "set", "target": "ports/pkt_valid", "field": "reset", "value": "1'b1"},
                      {"op": "set", "target": "blocks/k_hit", "field": "body", "value": "// lint_off\n"}])
    out = classify_rejections("pkt_parser", res.rejected)
    assert [(c.type, c.node) for c in out] == [("interface_change", "ports/pkt_valid"), ("schema", "blocks/k_hit")]
    for typo in ("signal/x", "transitions/t1"):
        res = apply(RAW, [{"op": "remove", "target": typo}])
        assert [c.type for c in classify_rejections("pkt_parser", res.rejected)] == ["schema"], typo


def test_locate_picks_narrowest():
    m = [{"node": "blocks/a", "start": 1, "end": 10}, {"node": "blocks/a/body", "start": 3, "end": 4, "fragment": True}]
    assert locate(m, 3) == ("blocks/a/body", True) and locate(m, 9) == ("blocks/a", False) and locate(m, 11) == (None, False)


def test_parse_verilator_messages():
    text = """%Warning-WIDTHTRUNC: out/pkt_parser.sv:53:69: Operator ASSIGNDLY expects 8 bits
                     ... note
%Error: out/pkt_parser.sv:12:3: syntax error, unexpected end
%Error-NEEDTIMINGOPT: out/x.sv:4:5: Use --timing
%Error: Exiting due to 2 warning(s)"""
    assert [(f.rule, f.line, f.severity) for f in parse_verilator(text)] == [
        ("WIDTHTRUNC", 53, "warning"), ("ERROR", 12, "error"), ("NEEDTIMINGOPT", 4, "error")]


@pytest.mark.skipif(not shutil.which("verilator"), reason="verilator 없음")
def test_e1_lint_bug_is_traced_to_fragment_and_fixed_by_patch(tmp_path):
    # Validator를 거치지 않은 폭 버그를 E1이 잡고, Classifier가 조각 노드로 되돌리고, patch로 고친다.
    bad = copy.deepcopy(RAW)
    k = next(b for b in bad["blocks"] if b["id"] == "k_cnt")
    good_body, k["body"] = k["body"], k["body"].replace("byte_cnt <= s_data;", "byte_cnt <= {s_data, 1'b0};")
    src = tmp_path / "pkt_parser.sv"
    e = emit(Module(bad), LIB)
    src.write_text(e.text)
    r = lint([src], "pkt_parser")
    assert not r.ok and [f.rule for f in r.findings] == ["WIDTHTRUNC"]
    (c,) = classify_findings(r.findings, {"pkt_parser": e.sourcemap}, ROLES)
    assert (c.type, c.route, c.node) == ("lint_fragment", "repair", "blocks/k_cnt/body")
    fixed = apply(bad, [{"op": "set", "target": c.node.rsplit("/", 1)[0], "field": "body", "value": good_body}])
    assert fixed.ok and validate(Module(fixed.ir), LIB) == []
    src.write_text(emit(Module(fixed.ir), LIB).text)
    assert lint([src], "pkt_parser").ok
