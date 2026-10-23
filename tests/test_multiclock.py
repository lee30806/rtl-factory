"""P10 다중 클럭과 리셋: 분주 클럭, 추가 동기 리셋, 블록 안 리셋 동기화, sync_pulse, cdc_fifo, 단일 리셋 방식.
시험용 Block Top은 tests/fixtures/multiclock (비동기 리셋 설계, 시뮬레이션 시험은 tests/sim/test_multiclock_sim.py).
추가 동기 리셋은 동기 리셋 설계에만 두므로 같은 픽스처를 동기 리셋으로 바꾼 것(_sync_lib)으로 본다."""
import copy
import json

import pytest

from pipeline.emitter.sdc import emit_sdc
from pipeline.emitter.sv import emit
from pipeline.ir.model import Module, frozen_l1, load_library
from pipeline.ir.pysim import PySim, schedule
from pipeline.spec import check as spec_check
from pipeline.spec.l1 import block_top_l1
from pipeline.spec.parse import load as load_spec
from pipeline.tb.gen import harness_config
from pipeline.validator.checks import errors, validate
from pipeline.validator.structure import frozen_scope
from test_pipeline import LIB, ROOT

FIX = ROOT / "tests" / "fixtures" / "multiclock"


def _raw(name):
    return json.loads((FIX / f"{name}.ir.json").read_text())


def _lib(top=None, src=None):
    lib = {"mc_src": Module(src or _raw("mc_src"))}
    lib["mc_top"] = Module(top or _raw("mc_top"))
    return lib


def _sync_lib():
    """픽스처를 동기 리셋 설계로 바꾼 것: 리셋 동기화를 빼고, 소프트 리셋을 추가 동기 리셋(reset.extra)으로 둔다."""
    src, top = _raw("mc_src"), _raw("mc_top")
    for raw in (src, top):
        for d in raw["domains"]:
            d["reset"]["async"] = False
            d["reset"].pop("synchronize", None)
    src["domains"][0]["reset"]["extra"] = [{"port": "soft_rst", "active": "high"}]
    src["ports"] = [p for p in src["ports"] if p.get("name") != "soft_rst"]
    k_buf = next(b for b in src["blocks"] if b["id"] == "k_buf")
    k_buf["body"] = k_buf["body"].replace("if soft_rst:\n    buf_v = False\n    buf_d = 0\nelif", "if")
    top["domains"][0]["reset"]["extra"] = [{"port": "a_soft_rst", "active": "high"}]
    top["ports"] = [p for p in top["ports"] if p.get("name") != "a_soft_rst"]
    top["connections"] = [c for c in top["connections"] if c["id"] != "c_soft"]
    return top, src


def _msgs(m, lib=None):
    return [str(i) for i in validate(m, lib or {m.name: m})]


def test_fixture_passes_validator_with_rdc_warning_only():
    lib = load_library([FIX / "mc_src.ir.json", FIX / "mc_top.ir.json"])
    assert validate(lib["mc_top"], lib) == []
    issues = validate(lib["mc_src"], lib)
    assert errors(issues) == []
    assert [i.message for i in issues] == ["리셋 도메인 교차: fire_q는 비동기 리셋 rst_n로 바뀌는 sys의 레지스터 (slow의 리셋은 rst_slow_n)"]


@pytest.mark.parametrize("edit, want", [
    (lambda d: d[1].update(sync_to={"domain": "b", "div": 2}, freq_mhz=37.5) or d[2].update(sync_to={"domain": "a", "div": 1}),
     "분주의 사슬"),
    (lambda d: d[1].update(freq_mhz=40), "freq_mhz(40)가 a(100) / 2와 다름"),
    (lambda d: d[2].update(clock="a_clk"), "클럭 포트 a_clk를 함께 쓰는 도메인"),
    (lambda d: d[1].update(async_to=["a"]), "같은 클럭 그룹의 도메인을 async_to에 넣음: a"),
    (lambda d: d[2]["reset"].update(synchronize=True, **{"async": False}), "비동기 리셋(async: true)에만"),
    (lambda d: d[2]["reset"].update(synchronize=False, **{"async": False}), "모두 비동기이거나 모두 동기여야 함"),
    (lambda d: d[0]["reset"].update(extra=[{"port": "a_soft_rst", "active": "high"}]), "False schema does not allow"),
])
def test_domain_relation_errors(edit, want):
    top = _raw("mc_top")
    edit(top["domains"])
    msgs = _msgs(Module(top), _lib(top))
    assert any(want in s for s in msgs), msgs


def test_sync_design_extra_reset_checks():
    top, src = _sync_lib()
    lib = _lib(top, src)
    assert errors(validate(lib["mc_src"], lib)) == [] and validate(lib["mc_top"], lib) == []
    top["domains"][0]["reset"]["extra"] = [{"port": "a_rst_n", "active": "low"}]
    assert any("추가 리셋에 주 리셋 포트를 다시 씀" in s for s in _msgs(Module(top), _lib(top, src)))
    top, src = _sync_lib()
    del top["domains"][0]["reset"]["extra"]
    msgs = _msgs(Module(top), _lib(top, src))
    assert any("추가 리셋(['high'])이 연결한 a의 추가 리셋([])과 맞지 않음" in s for s in msgs), msgs


def test_single_reset_style_across_hierarchy():
    """한 설계의 리셋은 한 방식: 동기 리셋 자식을 비동기 리셋 부모 도메인에 연결하면 V2."""
    _, src = _sync_lib()
    msgs = _msgs(Module(_raw("mc_top")), _lib(src=src))
    assert any("자식 도메인 sys(동기 리셋)를 비동기 리셋 도메인 a에 연결함" in s for s in msgs), msgs


def test_synchronize_only_in_tops_and_domain_map_keeps_relations():
    src = _raw("mc_src")
    src["domains"][0]["reset"]["synchronize"] = True
    assert any("top과 block_top에만" in s for s in _msgs(Module(src)))
    top = _raw("mc_top")
    top["instances"][0]["domain_map"] = {"sys": "a", "slow": "a"}   # 분주 도메인을 기준 도메인에 연결
    msgs = _msgs(Module(top), _lib(top))
    assert any("자식 도메인 slow는 sys에서 2분주인데" in s for s in msgs), msgs
    src = _raw("mc_src")
    src["domains"][1]["reset"]["port"] = "rst_n"   # 두 자식 도메인이 리셋 포트 하나를 함께 씀 → 부모는 넷 하나를 줘야 함
    msgs = _msgs(Module(_raw("mc_top")), _lib(src=src))
    assert any("자식 포트 rst_n를 함께 쓰는 도메인들에 서로 다른 넷(a_rst_sync_n, a2_rst_sync_n)" in s for s in msgs), msgs


def test_same_group_reads_and_reset_port_reads():
    src = _raw("mc_src")
    m = Module(src)
    assert "rst_n" in m.readable() and m.same_clock("sys", "slow") and m.clock_root("slow") == ("sys", 2)
    k_cnt = next(b for b in src["blocks"] if b["id"] == "k_cnt")
    k_cnt["body"] = "if fire_q and rst_n:\n    cnt = cnt + 1"   # seq는 리셋 포트를 읽지 않음
    assert any("리셋 포트는 comb 블록에서만 읽음" in s for s in _msgs(Module(src)))
    src = _raw("mc_src")
    del src["domains"][1]["sync_to"]   # 그룹이 다르면 동기화 없이 읽을 수 없음
    assert any("다른 클럭 그룹의 신호를 읽음: fire_q" in s for s in _msgs(Module(src)))


def test_cell_checks():
    top = _raw("mc_top")
    p = next(b for b in top["blocks"] if b["id"] == "p_x")
    p["src_domain"] = None
    assert any("pulse는 보내는 쪽 클럭으로" in s for s in _msgs(Module(top), _lib(top)))
    top = _raw("mc_top")
    next(b for b in top["blocks"] if b["id"] == "p_x").update(src="in_data")
    assert any("pulse는 1비트만 동기화함" in s for s in _msgs(Module(top), _lib(top)))
    top = _raw("mc_top")
    next(s for s in top["signals"] if s["name"] == "x_data")["type"] = "logic [6:0]"
    assert any("src.data와 dst.data의 타입이 다름" in s for s in _msgs(Module(top), _lib(top)))
    top = _raw("mc_top")
    next(b for b in top["blocks"] if b["id"] == "f_x")["src_domain"] = "b"   # src 신호는 a 도메인
    msgs = _msgs(Module(top), _lib(top))
    assert any("src.valid(x_valid, ['a'])는 b 도메인 신호여야 함" in s for s in msgs), msgs
    assert any("같은 클럭 그룹(b, b) 사이에는 동기화 셀이 필요 없음" in s for s in msgs)
    top = _raw("mc_top")
    next(b for b in top["blocks"] if b["id"] == "f_x")["depth"] = 3
    assert any(s.startswith("[V1]") for s in _msgs(Module(top), _lib(top)))


def test_frozen_scope_keys_unchanged_without_cdc_fifo():
    assert not any(k.endswith("/cdc_fifo") for k in frozen_scope(LIB))
    lib = load_library([FIX / "mc_src.ir.json", FIX / "mc_top.ir.json"])
    assert "mc_top/cdc_fifo" in frozen_scope(lib)


def test_emitter_rst_sync_and_cells():
    lib = load_library([FIX / "mc_src.ir.json", FIX / "mc_top.ir.json"])
    src = emit(lib["mc_src"], lib).text
    assert "    if (!rst_n) begin\n      fire_q <= 1'b0;" in src and "else if (soft_rst)" not in src
    assert "always_ff @(posedge clk_slow or negedge rst_slow_n) begin" in src
    top = emit(lib["mc_top"], lib).text
    assert "rst_sync u_rst_sync_a (.clk(a_clk), .rst_n(a_rst_n), .rst_n_sync(a_rst_sync_n));" in top
    assert "rst_sync u_rst_sync_a2 (.clk(a2_clk), .rst_n(a_rst_n), .rst_n_sync(a2_rst_sync_n));" in top
    assert "cdc_fifo #(.WIDTH($bits(out_data)), .DEPTH(4)) u_f_x (" in top
    assert ".src_clk(a_clk), .src_rst_n(a_rst_sync_n), .src_valid(x_valid)" in top and ".dst_rst_n(b_rst_n)" in top
    assert ("sync_pulse u_p_x (.src_clk(a2_clk), .src_rst_n(a2_rst_sync_n), .dst_clk(b_clk), .dst_rst_n(b_rst_n), "
            ".d(pulse_in), .q(pulse_out));") in top
    assert ".rst_n(a_rst_sync_n),\n    .clk_slow(a2_clk),\n    .rst_slow_n(a2_rst_sync_n)," in top and "ASYNC_RST" not in top


def test_emitter_sync_design_extra_reset_and_sync_cells():
    """동기 리셋 설계: 추가 동기 리셋 분기, 리셋 없는 sensitivity, 표준 셀도 동기 리셋 (ASYNC_RST=0)."""
    top, src = _sync_lib()
    lib = _lib(top, src)
    text = emit(lib["mc_src"], lib).text
    assert "    end else if (soft_rst) begin\n      fire_q <= 1'b0;" in text
    assert "always_ff @(posedge clk_slow) begin" in text and "negedge" not in text
    text = emit(lib["mc_top"], lib).text
    assert "rst_sync" not in text and "negedge" not in text
    assert "cdc_fifo #(.WIDTH($bits(out_data)), .DEPTH(4), .ASYNC_RST(1'b0)) u_f_x (" in text
    assert "sync_pulse #(.ASYNC_RST(1'b0)) u_p_x (.src_clk(a2_clk), .src_rst_n(a_rst_n)," in text
    assert ".soft_rst(a_soft_rst)," in text


def test_emitter_shared_child_ports_once():
    """자식 도메인 둘이 클럭과 리셋 포트를 함께 쓰면(같은 클럭, div 1) 인스턴스 핀은 한 번만."""
    src = _raw("mc_src")
    src["domains"][1].update(clock="clk", sync_to={"domain": "sys", "div": 1})
    src["domains"][1]["reset"]["port"] = "rst_n"
    top = _raw("mc_top")
    top["domains"][1].update(clock="a_clk", sync_to={"domain": "a", "div": 1}, freq_mhz=100)
    top["domains"][1]["reset"] = copy.deepcopy(top["domains"][0]["reset"])
    top["instances"][0]["domain_map"] = {"sys": "a", "slow": "a"}
    lib = _lib(top, src)
    assert errors(validate(lib["mc_src"], lib)) == [] and validate(lib["mc_top"], lib) == []
    text = emit(lib["mc_top"], lib).text
    inst = text[text.index("mc_src u_src"):]
    assert inst.count(".clk(a_clk)") == 1 and inst.count(".rst_n(") == 1
    sdc = emit_sdc(lib["mc_top"], lib)
    assert sdc.count("[get_ports a_clk]") == 1 and "# domains/a2: 클럭 포트 a_clk를 a와 함께 씀 (같은 클럭)" in sdc


def test_sdc_divided_clock_groups_and_rst_sync():
    lib = load_library([FIX / "mc_src.ir.json", FIX / "mc_top.ir.json"])
    sdc = emit_sdc(lib["mc_top"], lib)
    assert "# domains/a2: a에서 2분주 (같은 원천, 상승 에지 정렬)\ncreate_clock -name a2 -period 20.0 [get_ports a2_clk]" in sdc
    assert "set_clock_groups -asynchronous -group [get_clocks {a a2}] -group [get_clocks {b}]" in sdc
    assert "# a -> b: 비동기 클럭 그룹이 덮음 (u_f_x/wq1/D)" in sdc and "# b -> a: 비동기 클럭 그룹이 덮음 (u_f_x/rq1/D)" in sdc
    assert "# a2 -> b: 비동기 클럭 그룹이 덮음 (u_p_x/s1/D)" in sdc
    assert "set_false_path -from [get_ports a_rst_n] -through [get_pins u_rst_sync_a/rst_n]" in sdc
    # 비동기 그룹이 없으면 셀의 동기화 핀까지 목적지 한 주기로 제한한다 (FIFO는 양쪽 방향)
    top = _raw("mc_top")
    for d in top["domains"]:
        d.pop("async_to", None)
    sdc = emit_sdc(Module(top), _lib(top))
    assert "set_clock_groups" not in sdc
    assert "set_max_delay 13.333 -from [get_clocks a] -to [get_pins u_f_x/wq1/D]" in sdc
    assert "set_max_delay 10.0 -from [get_clocks b] -to [get_pins u_f_x/rq1/D]" in sdc
    assert "set_max_delay 13.333 -from [get_clocks a2] -to [get_pins u_p_x/s1/D]" in sdc


def test_spec_to_l1_matches_fixture_and_passes_spec_check():
    spec = load_spec(FIX / "mc_top.spec.md")
    ir, prov = block_top_l1(spec)
    assert frozen_l1(ir) == frozen_l1(_raw("mc_top"))
    assert ir["domains"][1]["sync_to"] == {"domain": "a", "div": 2} and ir["domains"][0]["reset"]["synchronize"] is True
    assert spec_check.run(spec)["gate"] == "pass"


def test_spec_same_clock_port_is_synchronous():
    spec = load_spec(FIX / "mc_top.spec.md")
    t = spec.sections[2].tables[0]
    row = next(r for r in t.rows if r[0] == "a2")
    row[1], row[2], row[6] = "a_clk", "100 MHz", "–"
    ir, _ = block_top_l1(spec)
    assert ir["domains"][1]["sync_to"] == {"domain": "a", "div": 1}


def _top_sim():
    lib = load_library([FIX / "mc_src.ir.json", FIX / "mc_top.ir.json"])
    return PySim(lib["mc_top"], lib)


def test_pysim_rst_sync_and_soft_reset():
    sim = _top_sim()
    sim.set({"a_rst_n": 0, "b_rst_n": 0, "in_valid": 1, "in_data": 7})
    sim.step()
    assert sim.outputs()["in_ready"] == 0
    sim.set({"a_rst_n": 1, "b_rst_n": 1})
    sim.settle()
    assert sim.val["u_rst_sync_a.out"] == 0 and sim.outputs()["in_ready"] == 0
    sim.step({"a"})
    assert sim.val["u_rst_sync_a.s1"] == 1 and sim.outputs()["in_ready"] == 0   # 동기 deassert: 두 번째 에지
    sim.step({"a"})
    assert sim.outputs()["in_ready"] == 1 and sim.val["u_src.rst_n"] == 1
    sim.step({"a"})   # in_valid가 1이라 바이트 하나를 받음
    assert sim.val["u_src.buf_v"] == 1 and sim.val["u_src.buf_d"] == 8
    sim.set({"a_soft_rst": 1})
    sim.settle()
    assert sim.outputs()["in_ready"] == 0   # comb가 추가 리셋을 읽음
    sim.step({"a"})
    assert sim.val["u_src.buf_v"] == 0 and sim.val["u_src.buf_d"] == 0   # 소프트 리셋(동작)은 에지에서


def test_pysim_sync_design_extra_reset_and_cell_reset():
    """동기 리셋 설계: 추가 동기 리셋과 셀 리셋은 에지에서 들어간다 (리셋이 걸려도 에지 전에는 값을 유지)."""
    top, src = _sync_lib()
    sim = PySim(Module(top), _lib(top, src))
    sim.set({"a_rst_n": 0, "b_rst_n": 0})
    sim.step()
    sim.set({"a_rst_n": 1, "b_rst_n": 1, "in_valid": 1, "in_data": 7, "out_ready": 0})
    sim.settle()
    assert sim.outputs()["in_ready"] == 1   # 리셋 동기화 없이 바로
    for _ in range(3):
        sim.step({"a"})
    assert sim.val["u_src.buf_v"] == 1 and sim.val["u_f_x.wbin"] != 0
    sim.set({"a_rst_n": 0})
    sim.settle()
    assert sim.val["u_f_x.wbin"] != 0   # 동기 리셋: 에지 전에는 그대로
    sim.step({"a"})
    assert sim.val["u_f_x.wbin"] == 0 and sim.val["u_src.buf_v"] == 0
    sim.set({"a_rst_n": 1})
    sim.step({"a"})
    sim.set({"a_soft_rst": 1})
    sim.settle()
    assert sim.outputs()["in_ready"] == 0   # comb가 추가 리셋을 읽음
    sim.step({"a"})
    assert sim.val["u_src.buf_v"] == 0 and sim.val["u_src.buf_d"] == 0   # 추가 동기 리셋은 에지에서


def test_pysim_pulse_and_fifo_cross_domains():
    sim = _top_sim()
    per = {"a": 10000, "a2": 20000, "b": 13000}
    ev = schedule(per, 2_000_000)
    assert ev[0] == (0, {"a", "a2", "b"}) and ev[1] == (10000, {"a"}) and ev[2] == (13000, {"b"})
    sim.set({"a_rst_n": 0, "b_rst_n": 0})
    for _, r in ev[:6]:
        sim.step(r)
    sim.set({"a_rst_n": 1, "b_rst_n": 1, "out_ready": 1, "in_valid": 1, "in_data": 0x41})
    sent, got, pulses, a2_edges = 0, [], 0, 0
    for _, r in ev[6:]:
        push = "a" in r and sim.outputs()["in_ready"]   # 이 에지에서 받는 바이트
        sim.step(r)
        if push:
            sent += 1
            sim.set({"in_data": 0x41 + sent})
        if "a2" in r:
            a2_edges += 1
            sim.set({"pulse_in": int(a2_edges == 3)})   # a2 리셋 동기화(2에지)가 풀린 뒤 한 사이클 펄스
        if "b" in r:
            pulses += sim.outputs()["pulse_out"]
            if sim.outputs()["out_valid"]:
                got.append(sim.outputs()["out_data"])
    assert pulses == 1
    assert len(got) >= 10 and got == [0x42 + i for i in range(len(got))]


def test_harness_config_multiclock_keys():
    cfg = harness_config(Module(_sync_lib()[0]))
    assert cfg["clock_sync"] == {"a2": ("a", 2)} and cfg["extra_resets"] == {"a": [("a_soft_rst", 1)]}
    assert "extra_resets" not in harness_config(Module(_raw("mc_top")))
    single = harness_config(LIB["pkt_parser"])
    assert "clock_sync" not in single and "extra_resets" not in single
