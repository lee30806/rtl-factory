import copy
import glob
import json
from pathlib import Path

import pyslang
import pytest

from pipeline.emitter.sv import emit
from pipeline.ir.model import Module, load_library
from pipeline.validator.checks import validate

ROOT = Path(__file__).resolve().parents[1]
LIB = load_library(sorted(glob.glob(str(ROOT / "tests" / "fixtures" / "*.ir.json"))))
STUB = (ROOT / "pipeline" / "emitter" / "stubs" / "sync_2ff.sv").read_text()


def mutate(f):
    raw = copy.deepcopy(LIB["pkt_parser"].raw)
    f(raw)
    return [str(i) for i in validate(Module(raw), LIB)]


@pytest.mark.parametrize("name", sorted(LIB))
def test_fixtures_pass_v1_to_v5(name):
    assert validate(LIB[name], LIB) == []


def _blk(raw, bid):
    return next(b for b in raw["blocks"] if b["id"] == bid)


MUTATIONS = [
    ("V1", "signals/wire", lambda r: r["signals"][1].__setitem__("name", "wire")),
    ("V1", "blocks/1", lambda r: _blk(r, "k_fsm").__setitem__("writes", [])),
    ("V1", "이름 중복", lambda r: r["signals"].append({"name": "byte_cnt", "kind": "wire", "type": "logic"})),
    ("V2", "signals/byte_cnt: 정의되지 않은 도메인", lambda r: r["signals"][0].__setitem__("domain", "fast")),
    ("V2", "reads의 신호가 없음", lambda r: _blk(r, "k_hit")["reads"].append("nope")),
    ("V2", "상태 타입에 없는 상태", lambda r: _blk(r, "k_fsm").__setitem__("reset_state", "BOOT")),
    ("V3", "signals/byte_cnt/reset", lambda r: r["signals"][0].__setitem__("reset", "9'h1FF")),
    ("V3", "params/SYNC_BYTE", lambda r: r["params"][0].__setitem__("default", "8'hA5 +")),
    ("V4", "blocking 대입", lambda r: _blk(r, "k_cnt").__setitem__("body", _blk(r, "k_cnt")["body"].replace("pkt_valid <= 1'b0", "pkt_valid = 1'b0"))),
    ("V4", "non-blocking 대입", lambda r: _blk(r, "k_hit").__setitem__("body", "s_ready <= 1'b1; sync_hit <= s_valid && (s_data == SYNC_BYTE);")),
    ("V4", "reads에 없는 신호를 읽음: ['s_data']", lambda r: _blk(r, "k_hit")["reads"].remove("s_data")),
    ("V4", "쓰지 않은 선언: ['pkt_valid']", lambda r: _blk(r, "k_hit")["writes"].append("pkt_valid")),
    ("V4", "금지 구문: 지연(#)", lambda r: _blk(r, "k_hit").__setitem__("body", "s_ready = #1 1'b1; sync_hit = s_valid && (s_data == SYNC_BYTE);")),
    ("V4", "금지 구문: procedural assign", lambda r: _blk(r, "k_hit").__setitem__("body", "assign s_ready = 1'b1; sync_hit = s_valid && (s_data == SYNC_BYTE);")),
    ("V4", "transitions/tr3", lambda r: _blk(r, "k_fsm")["reads"].remove("byte_cnt")),
    ("V5", "구동하는 곳이 여러 개", lambda r: r["blocks"].append({"id": "k_dup", "kind": "comb", "reads": [], "writes": ["sync_hit"], "body": "sync_hit = 1'b0;"})),
    ("V5", "구동하는 곳이 없음: dangling", lambda r: r["signals"].append({"name": "dangling", "kind": "wire", "type": "logic"})),
    ("V5", "reg는 seq 블록 하나만", lambda r: (_blk(r, "k_cnt")["writes"].remove("byte_cnt"),
                                         _blk(r, "k_cnt").__setitem__("body", "pkt_valid <= 1'b0;"),
                                         _blk(r, "k_hit")["writes"].append("byte_cnt"),
                                         _blk(r, "k_hit").__setitem__("body", "s_ready = 1'b1; byte_cnt = '0; sync_hit = s_valid && (s_data == SYNC_BYTE);"),
                                         _blk(r, "k_fsm")["reads"].remove("byte_cnt"),
                                         _blk(r, "k_fsm")["transitions"][2].__setitem__("cond", "s_valid"))),
]


@pytest.mark.parametrize("stage,expect,f", MUTATIONS, ids=[f"{s}-{e[:20]}" for s, e, _ in MUTATIONS])
def test_validator_catches(stage, expect, f):
    issues = mutate(f)
    assert issues, "오류를 잡지 못함"
    assert any(i.startswith(f"[{stage}]") and expect in i for i in issues), issues


def test_block_top_requires_known_child():
    raw = copy.deepcopy(LIB["pkt_rx_top"].raw)
    raw["instances"][0]["module"] = "missing_mod"
    assert any("라이브러리에 없는 모듈" in str(i) for i in validate(Module(raw), LIB))


def test_emitted_rtl_elaborates_cleanly():
    texts = [emit(m, LIB).text for m in LIB.values()] + [STUB]
    comp = pyslang.Compilation()
    trees = [pyslang.SyntaxTree.fromText(t) for t in texts]
    for t in trees:
        comp.addSyntaxTree(t)
    diags = [d for d in comp.getAllDiagnostics()]
    assert diags == [], [pyslang.DiagnosticEngine.reportAll(trees[0].sourceManager, [d]) for d in diags]
    assert comp.getRoot().topInstances[0].name == "pkt_rx_top"


def test_emit_is_deterministic_and_has_sourcemap():
    a, b = emit(LIB["pkt_parser"], LIB), emit(LIB["pkt_parser"], LIB)
    assert a.text == b.text
    lines = a.text.split("\n")
    for entry in a.sourcemap:
        if not entry.get("fragment"):
            assert lines[entry["start"] - 2].strip() == f"// @ir {entry['node']}"
    k_cnt = next(e for e in a.sourcemap if e["node"] == "blocks/k_cnt")
    block = "\n".join(lines[k_cnt["start"] - 1:k_cnt["end"]])
    assert "always_ff @(posedge clk or negedge rst_n)" in block
    assert "byte_cnt  <= '0;" in block and "pkt_valid <= 1'b0;" in block


def test_fsm_priority_follows_array_order():
    raw = copy.deepcopy(LIB["pkt_parser"].raw)
    _blk(raw, "k_fsm")["transitions"].insert(1, {"id": "tr0", "from": "IDLE", "to": "PAYLOAD", "cond": "s_valid"})
    text = emit(Module(raw), LIB).text
    assert text.index("if (sync_hit) state_d = HEADER") < text.index("else if (s_valid) state_d = PAYLOAD")


def test_cdc_sync_emits_standard_cell_on_dst_domain_clock():
    raw = copy.deepcopy(LIB["pkt_rx_top"].raw)
    raw["ports"].append({"name": "irq", "dir": "input", "type": "logic", "domain": None})
    raw["ports"].append({"name": "irq_s", "dir": "output", "type": "logic", "domain": "sys", "kind": "wire"})
    raw["blocks"] = [{"id": "s_irq", "kind": "cdc_sync", "src": "irq", "src_domain": None,
                      "dst": "irq_s", "dst_domain": "sys", "style": "2ff"}]
    m = Module(raw)
    assert validate(m, LIB) == []
    assert "sync_2ff u_s_irq (.clk(clk), .rst_n(rst_n), .d(irq), .q(irq_s));" in emit(m, LIB).text
