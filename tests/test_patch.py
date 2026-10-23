import copy
import json
import subprocess
import sys

import pytest

from pipeline.ir.model import Module
from pipeline.patch.apply import apply, ir_hash
from pipeline.validator.checks import validate
from test_pipeline import LIB, ROOT

RAW = LIB["pkt_parser"].raw
FIX_BODY = next(b for b in RAW["blocks"] if b["id"] == "k_cnt")["body"]


def _blk(raw, bid):
    return next(b for b in raw["blocks"] if b["id"] == bid)


def test_set_add_remove_and_original_untouched():
    before = copy.deepcopy(RAW)
    res = apply(RAW, [
        {"op": "set", "target": "blocks/k_fsm/transitions/tr2", "field": "cond", "value": "s_valid && s_data != 8'd0"},
        {"op": "set", "target": "blocks/k_fsm", "field": "reads", "value": ["sync_hit", "s_valid", "s_data", "byte_cnt"]},
        {"op": "add", "target": "signals", "value": {"name": "spare", "kind": "wire", "type": "logic"}},
        {"op": "remove", "target": "signals/spare"},
        {"op": "set", "target": "blocks/k_cnt", "field": "body", "value": FIX_BODY},
    ])
    assert res.ok, res.rejected
    assert RAW == before
    assert next(t for t in _blk(res.ir, "k_fsm")["transitions"] if t["id"] == "tr2")["cond"] == "s_valid && s_data != 8'd0"
    assert [s["name"] for s in res.ir["signals"]] == ["byte_cnt", "sync_hit"]
    assert validate(Module(res.ir), LIB) == []


def test_patch_fixes_injected_bug_and_hash_tracks_it():
    bad = copy.deepcopy(RAW)
    _blk(bad, "k_cnt")["body"] = FIX_BODY.replace("byte_cnt <= s_data;", "byte_cnt <= {s_data, 1'b0};")
    assert validate(Module(bad), LIB) != []
    res = apply(bad, [{"op": "set", "target": "blocks/k_cnt", "field": "body", "value": FIX_BODY}])
    assert res.ok and validate(Module(res.ir), LIB) == []
    assert ir_hash(res.ir) == ir_hash(RAW) != ir_hash(bad)


@pytest.mark.parametrize("patch, reason", [
    ({"op": "set", "target": "ports/pkt_valid", "field": "reset", "value": "1'b1"}, "쓰기 범위 밖"),
    ({"op": "set", "target": "params/SYNC_BYTE", "field": "default", "value": "8'h5A"}, "쓰기 범위 밖"),
    ({"op": "set", "target": "domains/sys", "field": "edge", "value": "neg"}, "쓰기 범위 밖"),
    ({"op": "add", "target": "instances", "value": {"name": "u_x", "module": "pkt_parser"}}, "쓰기 범위 밖"),
    ({"op": "set", "target": "role", "field": "x", "value": 1}, "쓰기 범위 밖"),
    ({"op": "set", "target": "blocks/k_cnt", "field": "body", "value": "/* verilator lint_off WIDTH */ " + FIX_BODY}, "waiver"),
    ({"op": "set", "target": "blocks/k_hit", "field": "body", "value": "// spyglass disable_block W164a\ns_ready = 1'b1;"}, "waiver"),
    ({"op": "set", "target": "blocks/k_hit", "field": "waivers", "value": ["W164a"]}, "waiver"),
    ({"op": "set", "target": "blocks/k_hit", "field": "body", "value": "`ifndef VERILATOR\ns_ready = 1'b1;\n`endif"}, "전처리"),
    ({"op": "set", "target": "blocks/k_hit", "field": "body", "value": "`line 1 \"x.sv\" 0\ns_ready = 1'b1;"}, "전처리"),
    ({"op": "add", "target": "types", "value": {"name": "t", "kind": "enum", "members": ["`define A 1"]}}, "전처리"),
    ({"op": "set", "target": "blocks/nope", "field": "body", "value": ""}, "노드가 없음"),
    ({"op": "set", "target": "blocks/k_cnt", "field": "id", "value": "k_x"}, "이름"),
    ({"op": "set", "target": "blocks/k_cnt/transitions/tr1", "field": "cond", "value": "1"}, "fsm 블록이 없음"),
    ({"op": "add", "target": "signals", "value": {"name": "byte_cnt", "kind": "reg", "type": "logic"}}, "이미 있음"),
    ({"op": "remove", "target": "signals"}, "노드여야"),
    ({"op": "rewrite", "target": "blocks/k_cnt"}, "형식"),
])
def test_out_of_scope_and_malformed_patches_are_rejected_atomically(patch, reason):
    ok = {"op": "set", "target": "blocks/k_hit", "field": "body", "value": "s_ready = 1'b1; sync_hit = 1'b0;"}
    res = apply(RAW, [ok, patch])
    assert not res.ok and res.ir is RAW
    assert [r["index"] for r in res.rejected] == [1] and reason in res.rejected[0]["reason"], res.rejected


def test_top_connections_and_sync_cells_are_frozen():
    top = LIB["pkt_rx_top"].raw
    cid = top["connections"][0]["id"]
    res = apply(top, [{"op": "set", "target": f"connections/{cid}", "field": "to", "value": "x"}])
    assert not res.ok and "쓰기 범위 밖" in res.rejected[0]["reason"]
    res = apply(top, [{"op": "add", "target": "blocks", "value": {"id": "s_x", "kind": "cdc_sync"}}])
    assert not res.ok


def test_cli_checks_without_writing(tmp_path):
    bad = copy.deepcopy(RAW)
    _blk(bad, "k_cnt")["body"] = FIX_BODY.replace("byte_cnt <= s_data;", "byte_cnt <= {s_data, 1'b0};")
    ir, patches, out = tmp_path / "ir.json", tmp_path / "p.json", tmp_path / "out.json"
    ir.write_text(json.dumps(bad))
    run = lambda ps, *extra: subprocess.run([sys.executable, "-m", "pipeline.patch.apply", str(ir), str(patches), *extra],
                                            cwd=ROOT, capture_output=True, text=True)
    patches.write_text(json.dumps([{"op": "set", "target": "blocks/k_hit", "field": "body",
                                    "value": "s_ready = 1'b1; sync_hit = s_valid && (s_data == SYNC_BYTE);"}]))
    p = run(patches, "-o", str(out))
    assert p.returncode == 1 and json.loads(p.stdout)["errors"] and not out.exists()  # 버그가 남아 있음
    patches.write_text(json.dumps([{"op": "set", "target": "blocks/k_cnt", "field": "body", "value": FIX_BODY}]))
    p = run(patches, "-o", str(out))
    assert p.returncode == 0, p.stdout
    assert json.loads(out.read_text()) == RAW


def test_frozen_hash_survives_malformed_ir():
    from pipeline.validator.structure import frozen_scope
    top = copy.deepcopy(LIB["pkt_rx_top"].raw)
    top["blocks"] = [{"id": "s_irq", "kind": "cdc_sync", "src": "irq", "src_domain": None,
                      "dst": "irq_s", "dst_domain": "sys", "style": "2ff"}]
    bad = copy.deepcopy(top)
    bad["signals"] = [{"name": "x"}]  # Module 해석이 실패하는 형식 오류
    assert Module(bad).blocks == []
    a, b = frozen_scope({"t": Module(top)}), frozen_scope({"t": Module(bad)})
    assert a["t/cdc_sync"] == b["t/cdc_sync"] and a["t/signals"] != b["t/signals"]


def test_block_top_signals_and_types_are_frozen():
    top = LIB["pkt_rx_top"].raw
    for p in ({"op": "add", "target": "signals", "value": {"name": "x", "kind": "wire", "type": "logic"}},
              {"op": "add", "target": "types", "value": {"name": "t", "kind": "enum", "members": ["A"]}}):
        res = apply(top, [p])
        assert not res.ok and "쓰기 범위 밖" in res.rejected[0]["reason"]
