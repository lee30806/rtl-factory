"""v1 제외 항목 확장 (2026-10-19): 하강 에지 도메인, inout 패드 (실행기 tristate, 동시 구동 충돌), function/task, SVA.
시뮬레이션 없는 부분 (Validator, Emitter, 번역기, 실행기, SPEC 표)."""
import copy
import json

import pytest

from pipeline.ir.model import Module
from pipeline.ir.pysim import schedule
from pipeline.tb.gen import harness_config
from pipeline.validator.checks import errors, validate
from test_pipeline import ROOT

NE = ROOT / "tests" / "fixtures" / "negedge"


def _ne(name="ported/ne_top"):
    return json.loads((NE / f"{name}.ir.json").read_text())


def _msgs(raw, lib=None):
    m = Module(raw)
    return [str(i) for i in errors(validate(m, {n: Module(r) for n, r in (lib or {}).items()}))]


# ---------------------------------------------------------------- 하강 에지 도메인
def test_neg_domain_fixture_passes_and_harness_marks_neg_edge():
    for name in ("ported/ne_top", "ne_top_py"):
        assert _msgs(_ne(name)) == []
    cfg = harness_config(Module(_ne()))
    assert cfg["neg_edge"] == ["clk_neg"]
    assert "neg_edge" not in harness_config(Module(json.loads((ROOT / "tests/fixtures/multiclock/mc_top.ir.json").read_text())))


def test_neg_domain_relation_errors():
    raw = _ne()
    raw["domains"][1]["sync_to"]["div"] = 2   # 분주 관계는 상승 에지끼리만
    assert any("분주 관계는 상승 에지 도메인끼리만" in e for e in _msgs(raw))
    raw = _ne()
    raw["domains"][1]["reset"]["synchronize"] = True
    assert any("reset.synchronize는 상승 에지 도메인에만" in e for e in _msgs(raw))


def test_neg_domain_rejects_cells_and_edge_mismatch_in_domain_map():
    raw = _ne()
    raw["signals"].append({"name": "x_s", "kind": "wire", "type": "logic"})
    raw["blocks"].append({"id": "s_x", "kind": "cdc_sync", "src": "m_ready", "src_domain": None, "dst": "x_s",
                          "dst_domain": "clk_neg", "style": "2ff"})
    assert any("동기화 셀은 상승 에지 도메인 사이에만" in e for e in _msgs(raw))
    child = _ne()
    parent = {"module": "p", "role": "module", "params": [],
              "domains": [{"name": "sys", "clock": "clk", "edge": "pos", "reset": {"port": "rst_n", "active": "low", "async": True}}],
              "ports": [], "instances": [{"name": "u", "module": "ne_top", "domain_map": {"clk": "sys", "clk_neg": "sys"}}]}
    assert any("에지가 다른 도메인" in e for e in _msgs(parent, {"ne_top": child}))


def test_schedule_neg_domain_on_falling_edge():
    ev = schedule({"a": 10, "b": 10}, 30, falling={"b"})
    assert ev == [(0, {"a"}), (5, {"b"}), (10, {"a"}), (15, {"b"}), (20, {"a"}), (25, {"b"}), (30, {"a"})]


# ---------------------------------------------------------------- inout 패드: 실행기의 tristate
INOUT = ROOT / "tests" / "fixtures" / "port" / "inout" / "ported"


def _py_lib(top: str):
    """inout Block Top과 하위 모듈의 Python 조각 판 (실행기는 lang: py 모듈만 실행)."""
    lib = {n: json.loads((INOUT / f"{n}.ir.json").read_text()) for n in ("od_ctrl", "od_top", "pp_core", "pp_top")}
    bodies = {"od_ctrl": "shift = cat(shift[6:0], sda_i)\nsda_oe = shift[3] ^ shift[0]",
              "pp_core": "q = pad_i\npad_oe = ~pad_oe\npad_o = pad_o + 1"}
    for n, body in bodies.items():
        lib[n]["lang"] = "py"
        blk = lib[n]["blocks"][0]
        blk["body"] = body
        del blk["reads"], blk["writes"]
    child = "od_ctrl" if top == "od_top" else "pp_core"
    return {n: Module(lib[n]) for n in (child, top)}


def test_pysim_runs_tristate_with_pad_model():
    from pipeline.ir.pysim import PySim
    lib = _py_lib("pp_top")
    sim = PySim(lib["pp_top"], lib)
    assert set(sim.inputs()) == {"rst_n", "pad_pad_o", "pad_pad_oe"} and set(sim.outputs()) == {"q", "pad_pad_val"}
    sim.set({"rst_n": 0, "pad_pad_oe": 0})
    sim.step()
    sim.settle()
    assert sim.val["pad_pad_val"] == 0xF                       # 아무도 구동하지 않음: 풀업
    sim.set({"pad_pad_o": 0x5, "pad_pad_oe": 1})
    sim.settle()
    assert sim.val["pad_pad_val"] == 0x5 and sim.val["pad_i"] == 0x5   # 바깥 장치 구동
    sim.set({"rst_n": 1, "pad_pad_oe": 0})
    sim.step()                                                  # 블록이 pad_oe=1, pad_o=1을 냄
    assert sim.val["u_core.pad_oe"] == 1 and sim.val["pad_pad_val"] == 0x1
    sim.set({"pad_pad_o": 0x8, "pad_pad_oe": 1})
    sim.settle()
    assert sim.val["pad_pad_val"] == 0x1                        # 블록 구동이 이김 (약한 바깥 구동)
    od = _py_lib("od_top")
    sim = PySim(od["od_top"], od)
    sim.set({"rst_n": 0, "sda_pad_oe": 0})
    sim.step()
    sim.settle()
    assert sim.val["sda_pad_val"] == 1                          # open-drain: 놓으면 풀업


# ---------------------------------------------------------------- function/task
V1X = ROOT / "tests" / "fixtures" / "v1ext"


def _fn(name="fn_mod"):
    return json.loads((V1X / f"{name}.ir.json").read_text())


def test_function_fixture_passes_and_emits_functions_with_sourcemap():
    from pipeline.emitter.sv import emit
    from pipeline.validator.structure import comb_paths
    for name in ("fn_mod", "fn_mod_py"):
        assert _msgs(_fn(name)) == []
    m = Module(_fn("fn_mod_py"))
    e = emit(m)
    assert "function automatic logic [7:0] mix(input logic [7:0] x, input logic [7:0] w);" in e.text
    assert "task" not in e.text and "return mix(acc, 8'd1);" in e.text
    nodes = {x["node"] for x in e.sourcemap if x.get("fragment")}
    assert {"functions/mix", "functions/mix/body", "assertions/p_z"} <= nodes
    assert any(x["node"] == "functions/rot/body" and "py_line" in x for x in e.sourcemap)   # Python 조각 줄 대응
    assert ("a", "z") in comb_paths(m, {})


@pytest.mark.parametrize("edit, want", [
    (lambda r: r["functions"][0].update(body="t = x + y;\nreturn t;"), "모듈 신호를 직접 읽거나 씀"),
    (lambda r: r["functions"][0].update(body="t <= x;\nreturn t;"), "blocking(=)만"),
    (lambda r: r["functions"][0].update(body="x = 8'd0;\nreturn x;"), "입력 인자에 씀"),
    (lambda r: r["functions"][0].update(body="t = rot(x, 3'd1);\nreturn t;"), "앞에 정의한 function만"),
    (lambda r: r["functions"][0].update(body="t = x;\n@(posedge t[0]);\nreturn t;"), "타이밍 제어"),
    (lambda r: r["functions"][0]["vars"].append({"name": "a", "type": "logic"}), "모듈의 이름과 겹침"),
])
def test_function_errors(edit, want):
    raw = _fn()
    edit(raw)
    msgs = _msgs(raw)
    assert any(want in x for x in msgs), msgs


TASK = {"name": "split", "kind": "task", "args": [{"name": "x", "dir": "input", "type": "logic [7:0]"},
                                                  {"name": "hi", "dir": "output", "type": "logic [3:0]"}],
        "body": "hi = x[7:4];"}


def test_task_and_unsynthesizable_system_calls_are_rejected():
    """합성 RTL에는 task와 PLI(시스템 task, $display, $random 등)를 쓰지 않는다 (2026-10-22 사용자 규칙).
    상수 함수($clog2, $bits, $signed 등)는 쓴다. SVA는 Emitter가 SYNTHESIS 밖으로 빼므로 보지 않는다."""
    raw = _fn()
    raw["functions"].append(TASK)
    assert any("task를 쓰지 않음" in x for x in _msgs(raw))
    for body, bad in [("y = mix(a, b);\n$display(\"%d\", y);", "$display"), ("y = 8'($random);", "$random"),
                      ("y = 8'($time);", "$time")]:
        raw = _fn()
        raw["blocks"][0]["body"] = body
        assert any("합성되지 않는 시스템 함수" in x and bad in x for x in _msgs(raw)), (body, _msgs(raw))
    raw = _fn()
    raw["blocks"][0]["body"] = "y = mix(a, b) ^ 8'($clog2(16)) ^ 8'($bits(a));"
    assert _msgs(raw) == []
    raw = _fn()
    raw["functions"][0]["body"] = "t = x + w;\n$display(t);\nreturn t;"
    assert any("합성되지 않는 시스템 함수" in x for x in _msgs(raw))


def test_python_fragment_function_errors():
    raw = _fn("fn_mod_py")
    raw["functions"][0]["body"] = "t = x + a\nreturn t"          # 함수 안에서 모듈 신호
    assert "모르는 이름 a" in Module(raw).lower_errors[0][1]


def test_repair_can_patch_functions_and_assertions():
    from pipeline.patch.apply import apply
    raw = _fn()
    res = apply(raw, [{"op": "set", "target": "functions/mix", "field": "body", "value": "t = x - w;\nreturn t;"},
                      {"op": "set", "target": "assertions/p_y", "field": "expr", "value": "y == mix(a, b)"}])
    assert res.ok and res.ir["functions"][0]["body"].startswith("t = x - w")
    top = json.loads((INOUT / "od_top.ir.json").read_text())
    assert not apply(top, [{"op": "add", "target": "functions", "value": {"name": "f"}}]).ok   # top은 동결


# ---------------------------------------------------------------- SVA
@pytest.mark.parametrize("edit, want", [
    (lambda r: r["assertions"][0].update(reads=["y"]), "reads에 없는 신호를 읽음"),
    (lambda r: r["assertions"][0].update(expr="y = 8'd0"), "SVA"),
    (lambda r: r["assertions"][0].update(domain="nope"), "정의되지 않은 도메인"),
    (lambda r: r["assertions"][1].update(id="p_y"), "id 중복"),
])
def test_assertion_errors(edit, want):
    raw = _fn()
    edit(raw)
    msgs = _msgs(raw)
    assert any(want in x for x in msgs), msgs


def test_assertion_domain_and_role_rules():
    raw = copy.deepcopy(_ne())   # 같은 클럭 그룹(하강 에지 도메인)의 신호는 읽음
    raw["assertions"] = [{"id": "p_conv", "kind": "assert", "domain": "clk", "reads": ["m_data", "n_in"], "expr": "m_data != 8'd0 || n_in == 4'd0"}]
    assert _msgs(raw) == []
    from pipeline.emitter.sv import emit
    text = emit(Module(raw)).text
    assert "wire clk_sva_rst = !rst_n;" in text and "p_conv: assert property (@(posedge clk) disable iff (clk_sva_rst)" in text
    raw["assertions"][0].update(reset_disable=False, domain="clk_neg")
    assert "p_conv: assert property (@(negedge clk) (m_data" in emit(Module(raw)).text
    top = json.loads((INOUT / "od_top.ir.json").read_text())
    top["assertions"] = [{"id": "p", "kind": "assert", "domain": "sys", "reads": [], "expr": "1'b1"}]
    assert any("[V1]" in x for x in _msgs(top))   # top과 block_top에는 두지 않음


def test_sva_log_parsing_and_classification():
    from pipeline.classifier.classify import classify_findings
    from pipeline.verify.run import parse_sva
    log = ("0.00ns INFO cocotb.regression running tb_x.t_a (1/2)\n"
           "[SVA] FAIL module=core id=p_hold kind=assert at top.u_core.p_hold\n"
           "[SVA] FAIL module=core id=p_hold kind=assert at top.u_core.p_hold\n"
           "90.00ns INFO cocotb.regression running tb_x.t_b (2/2)\n"
           "[SVA] FAIL module=core id=m_in kind=assume at top.u_core.m_in\n")
    fs = parse_sva(log)
    assert [(f.test, f.rule, f.file, f.message) for f in fs] == [
        ("t_a", "SVA_ASSERT", "core.sv", "assertions/p_hold (core, top.u_core.p_hold) assert 실패 2회"),
        ("t_b", "SVA_ASSUME", "core.sv", "assertions/m_in (core, top.u_core.m_in) assume 실패 1회")]
    c = classify_findings(fs, {}, {})
    assert [(x.type, x.route, x.module, x.node) for x in c] == [
        ("sim_mismatch", "repair", "core", "assertions/p_hold"), ("tb_error", "verifier", "core", "assertions/m_in")]


# ---------------------------------------------------------------- 하니스: SPEC이 허용한 덮어쓰기
def test_valid_ready_checker_allow_overwrite():
    pytest.importorskip("cocotb")
    from types import SimpleNamespace as NS
    from pipeline.tb.lib import ValidReadyChecker
    dut = NS(m_valid=NS(value=0), m_ready=NS(value=0), m_data=NS(value=0))
    b = {"name": "m", "domain": "sys", "valid": "m_valid", "ready": "m_ready", "data": ["m_data"]}

    def run(overwrite):
        c = ValidReadyChecker(dut, b)
        c.overwrite_ok = overwrite
        for cyc, (v, d) in enumerate([(1, 0xA1), (1, 0x3C), (0, 0x3C)]):   # ready 0: data가 바뀐 뒤 valid가 내려감
            dut.m_valid.value, dut.m_data.value = v, d
            c.sample(cyc)
        return [x["rule"] for x in c.violations]
    assert run(False) == ["ready 전에 data가 바뀜", "valid가 ready 전에 내려감"]
    assert run(True) == ["valid가 ready 전에 내려감"]   # 덮어쓰기는 허용해도 valid 유지는 계속 본다


def test_mismatch_report_without_failure_cycle():
    """테스트가 사이클 없이 넣은 기댓값이 끝까지 오지 않으면(missing, 사이클 None) 리포트가 죽지 않고 트레이스 끝을 본다
    (2026-10-20 axis_async_fifo R6)."""
    from pipeline.tb.report import mismatch_report
    m = Module(_fn())
    res = {"test": "t", "scoreboard": {"kind": "missing", "stream": "y", "expected": {"y": 1}, "got": None, "cycle": None,
                                       "domain": None, "req": None, "expected_since": None, "expected_domain": None},
           "protocol": [], "properties": []}
    trace = [{"domain": "sys", "cycle": c, "values": {"y": c}} for c in range(30)]
    rep = mismatch_report(m, res, trace)
    assert rep["first_failure"]["anchor"] == 29 and rep["fanin"]["values"][-1]["cycle"] == 29
