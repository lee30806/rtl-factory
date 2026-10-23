import copy
import glob
from pathlib import Path

import pytest

from pipeline.ir.model import Module, load_library
from pipeline.validator.checks import errors, validate
from pipeline.validator.structure import comb_paths, frozen_scope, v11_frozen

ROOT = Path(__file__).resolve().parents[1]
LIB = load_library(sorted(glob.glob(str(ROOT / "tests" / "fixtures" / "*.ir.json"))))


def blk(raw, bid):
    return next(b for b in raw["blocks"] if b["id"] == bid)


def issues_for(name, f):
    raw = copy.deepcopy(LIB[name].raw)
    f(raw)
    lib = dict(LIB)
    lib[name] = Module(raw)
    return [str(i) for i in validate(lib[name], lib)]


def latch(r):
    blk(r, "k_hit")["body"] = "if (s_valid) sync_hit = (s_data == SYNC_BYTE); s_ready = 1'b1;"


def loop(r):
    r["signals"] += [{"name": "a", "kind": "wire", "type": "logic"}, {"name": "b", "kind": "wire", "type": "logic"}]
    r["blocks"] += [{"id": "k_a", "kind": "comb", "reads": ["b"], "writes": ["a"], "body": "a = b;"},
                    {"id": "k_b", "kind": "comb", "reads": ["a"], "writes": ["b"], "body": "b = a;"}]


def mixed_reset(r):
    r["signals"].append({"name": "tmp", "kind": "reg", "type": "logic", "domain": "sys"})
    k = blk(r, "k_cnt")
    k["writes"].append("tmp")
    k["body"] += " tmp <= s_valid;"


def two_domains(r):
    r["domains"].append({"name": "fast", "clock": "clk2", "edge": "pos", "reset": {"port": "rst2_n", "active": "low", "async": True}})
    r["signals"].append({"name": "f_reg", "kind": "reg", "type": "logic", "domain": "fast", "reset": "1'b0"})
    r["blocks"].append({"id": "k_f", "kind": "seq", "domain": "fast", "reads": ["f_reg"], "writes": ["f_reg"], "body": "f_reg <= ~f_reg;"})


def cross_read(r):
    two_domains(r)
    k = blk(r, "k_cnt")
    k["reads"].append("f_reg")
    k["body"] += " if (f_reg) pkt_valid <= 1'b1;"


def async_read(r):
    r["ports"].append({"name": "irq", "dir": "input", "type": "logic", "domain": None})
    k = blk(r, "k_hit")
    k["reads"].append("irq")
    k["body"] = "s_ready = irq; sync_hit = s_valid && (s_data == SYNC_BYTE);"


def unreachable(r):
    r["types"][0]["members"].append("ERR")
    blk(r, "k_fsm")["transitions"].append({"id": "tr9", "from": "ERR", "to": "IDLE", "cond": "1'b1"})


def dead_end(r):
    blk(r, "k_fsm")["transitions"].pop()  # PAYLOAD에서 나가는 전이 제거


def double_input(r):
    r["ports"].append({"name": "x_valid", "dir": "input", "type": "logic", "domain": "sys"})
    r["connections"].append({"id": "c3", "from": "self.x_valid", "to": "u_parser.s_valid"})


def bundle_role(r):
    r["ports"][0]["role"] = "source"


def domain_cross(r):
    r["domains"].append({"name": "fast", "clock": "clk2", "edge": "pos", "reset": {"port": "rst2_n", "active": "low", "async": True}})
    r["instances"][0]["domain_map"] = {"sys": "fast"}


CASES = [
    ("pkt_parser", latch, "[V6] blocks/k_hit: 래치 위험: sync_hit"),
    ("pkt_parser", loop, "[V6] signals/a: 조합 루프: a → b → a"),
    ("pkt_parser", mixed_reset, "[V7] blocks/k_cnt: 리셋 유무가 섞임"),
    ("pkt_parser", cross_read, "[V8] blocks/k_cnt: 다른 클럭 그룹의 신호를 읽음: f_reg"),
    ("pkt_parser", async_read, "[V8] blocks/k_hit: 비동기 신호는 cdc_sync의 src로만"),
    ("pkt_parser", unreachable, "[V9] blocks/k_fsm: 리셋 상태에서 도달할 수 없는 상태: ERR"),
    ("pkt_parser", dead_end, "[V9 경고] blocks/k_fsm: 나가는 전이가 없는 상태: PAYLOAD"),
    ("pkt_rx_top", double_input, "[V10] instances/u_parser: 입력 s_valid의 연결 수가 2"),
    ("pkt_rx_top", bundle_role, "[V10] connections/c1: 번들 역할이 같아야 함"),
    ("pkt_rx_top", domain_cross, "[V10] connections/c1: 도메인이 다른 연결"),
]


@pytest.mark.parametrize("name,f,expect", CASES, ids=[e[:30] for _, _, e in CASES])
def test_structure_checks(name, f, expect):
    got = issues_for(name, f)
    assert any(g.startswith(expect) for g in got), got


def test_two_domains_alone_is_clean():
    assert issues_for("pkt_parser", two_domains) == []


def test_dead_end_is_warning_not_error():
    raw = copy.deepcopy(LIB["pkt_parser"].raw)
    dead_end(raw)
    issues = validate(Module(raw), LIB)
    assert issues and errors(issues) == []


def test_comb_path_summary():
    # pkt_parser: s_ready는 상수이고 sync_hit는 내부 신호라 출력으로 가는 조합 경로가 없다
    assert comb_paths(LIB["pkt_parser"], LIB) == set()
    raw = copy.deepcopy(LIB["pkt_parser"].raw)
    blk(raw, "k_hit")["reads"] = ["s_valid", "s_data"]
    blk(raw, "k_hit")["body"] = "s_ready = s_valid; sync_hit = s_valid && (s_data == SYNC_BYTE);"
    assert comb_paths(Module(raw), LIB) == {("s_valid", "s_ready")}


def test_v11_frozen_detects_l1_change_but_not_body_change():
    base = frozen_scope(LIB)
    lib = dict(LIB)
    raw = copy.deepcopy(LIB["pkt_parser"].raw)
    blk(raw, "k_cnt")["body"] = blk(raw, "k_cnt")["body"].replace("8'd1", "8'd01")
    lib["pkt_parser"] = Module(raw)
    assert v11_frozen(lib, base) == []
    raw["ports"][1]["type"] = "logic [1:0]"
    lib["pkt_parser"] = Module(raw)
    assert [str(i) for i in v11_frozen(lib, base)] == ["[V11] pkt_parser/l1: 동결 범위가 바뀜"]


def test_feedthrough_rule_and_paths():
    """SPEC 9절의 조합 통과 금지를 읽고, 입력→출력 조합 경로를 찾는다 (P8 회고: E1에서 검토 항목으로)."""
    from pipeline.ir.model import load
    from pipeline.validator.structure import feedthrough_paths, feedthrough_rule

    sec = "## 9. 구현 제약\n\n### 금지 사항\n- {}\n\n## 10. 열린 질문\n- 출력 레지스터는 나중에\n"
    assert feedthrough_rule(sec.format("래치")) is None   # 10절의 문장은 보지 않음
    assert feedthrough_rule(sec.format("s_axis_* 포트에서 m_axis_* 포트로의 순수 조합 통과 경로")) == [("s_axis_", "m_axis_")]
    assert feedthrough_rule(sec.format("입출력 조합 통과 경로(feedthrough)")) == [("", "")]
    m = load(Path(__file__).parent / "fixtures" / "py" / "py_ops.ir.json")
    paths = feedthrough_paths(m, {}, [("", "")])
    assert ("a", "sum9") in paths and not any(o in ("acc", "par") for _, o in paths)   # 레지스터 출력은 경로 없음
    assert feedthrough_paths(m, {}, [("b", "inv")]) == []


def test_combinational_loop_across_instances():
    """하위 모듈 둘이 각자 입력을 출력으로 조합 통과하고 서로 이어 고리를 만들면 V6 조합 루프 (부모 IR에서 잡힘)."""
    dom = [{"name": "sys", "clock": "clk", "edge": "pos", "reset": {"port": "rst", "active": "high", "async": False}}]

    def thru(name):
        return Module({"module": name, "role": "module", "params": [], "domains": dom,
                       "ports": [{"name": "i", "dir": "input", "type": "logic", "domain": "sys"},
                                 {"name": "o", "dir": "output", "type": "logic", "domain": "sys", "kind": "wire"}],
                       "blocks": [{"id": "k", "kind": "comb", "reads": ["i"], "writes": ["o"], "body": "o = ~i;"}]})
    lib = {"ta": thru("ta"), "tb": thru("tb")}
    top = Module({"module": "top", "role": "block_top", "params": [], "domains": dom,
                  "ports": [{"name": "y", "dir": "output", "type": "logic", "domain": "sys"}],
                  "instances": [{"name": "u_a", "module": "ta", "domain_map": {"sys": "sys"}},
                                {"name": "u_b", "module": "tb", "domain_map": {"sys": "sys"}}],
                  "connections": [{"id": "c1", "from": "u_a.o", "to": "u_b.i"}, {"id": "c2", "from": "u_b.o", "to": "u_a.i"},
                                  {"id": "c3", "from": "u_b.o", "to": "self.y"}]})
    issues = [str(i) for i in validate(top, lib)]
    assert any("[V6]" in i and "조합 루프" in i for i in issues), issues
