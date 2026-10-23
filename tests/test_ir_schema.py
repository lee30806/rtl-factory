import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
V = Draft202012Validator(json.loads((ROOT / "schema" / "ir.schema.json").read_text(encoding="utf-8")))
FIX = ROOT / "tests" / "fixtures"


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def errors(ir):
    return list(V.iter_errors(ir))


@pytest.mark.parametrize("name", ["pkt_parser.ir.json", "pkt_rx_top.ir.json"])
def test_design_doc_examples_are_valid(name):
    assert errors(load(name)) == []


def test_single_port_requires_domain():
    ir = load("pkt_parser.ir.json")
    del ir["ports"][1]["domain"]
    assert errors(ir)


def test_async_input_port_domain_null_is_allowed():
    ir = load("pkt_parser.ir.json")
    ir["ports"].append({"name": "ext_irq", "dir": "input", "type": "logic", "domain": None})
    assert errors(ir) == []


def test_input_port_cannot_have_kind_or_reset():
    ir = load("pkt_parser.ir.json")
    ir["ports"][0]["signals"][0]["kind"] = "reg"
    assert errors(ir)


def test_wire_output_cannot_have_reset():
    ir = load("pkt_parser.ir.json")
    ir["ports"][0]["signals"][1]["reset"] = "1'b0"
    assert errors(ir)


def test_unknown_field_is_rejected():
    ir = load("pkt_parser.ir.json")
    ir["blocks"][0]["sensitivity"] = "*"
    assert errors(ir)


def test_block_top_allows_only_cdc_sync_blocks():
    ir = load("pkt_rx_top.ir.json")
    ok = copy.deepcopy(ir)
    ok["signals"] = [{"name": "irq_sync", "kind": "wire", "type": "logic"}]
    ok["blocks"] = [{"id": "s1", "kind": "cdc_sync", "src": "irq", "src_domain": None,
                     "dst": "irq_sync", "dst_domain": "sys", "style": "2ff"}]
    assert errors(ok) == []
    ir["blocks"] = [{"id": "k1", "kind": "comb", "reads": [], "writes": ["x"], "body": "x = 1'b0;"}]
    assert errors(ir)


def test_external_has_l1_only_and_needs_rtl_files():
    ext = {"module": "legacy_ip", "role": "external", "params": [], "domains": [],
           "ports": [{"name": "a", "dir": "input", "type": "logic", "domain": None}]}
    assert errors(ext)  # rtl_files 없음
    ext["rtl_files"] = ["ip/legacy_ip.sv"]
    assert errors(ext) == []
    ext["blocks"] = []
    assert errors(ext)


def test_rtl_files_only_on_external():
    ir = load("pkt_parser.ir.json")
    ir["rtl_files"] = ["x.sv"]
    assert errors(ir)


def test_identifier_rules():
    ir = load("pkt_parser.ir.json")
    ir["signals"][0]["name"] = "1bad"
    assert errors(ir)
