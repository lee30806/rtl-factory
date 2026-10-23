"""P11 원본 RTL에서 IR과 SPEC 만들기: 결정론 부분 (R1 추출, R2 요청과 반영, R5 SPEC 표, 게이트). 시뮬레이션 없음."""
import json
from pathlib import Path

import pytest

from pipeline.agents import porter, spec_writer
from pipeline.importer import r2, spec_tables
from pipeline.importer.extract import Extractor
from pipeline.importer.sources import Design, Inputs, SourceError, read_filelist
from pipeline.ir.model import Module, frozen_l1
from pipeline.spec.l1 import block_top_l1
from pipeline.spec.parse import parse
from pipeline.validator.checks import errors, validate

ROOT = Path(__file__).resolve().parents[1]
G = ROOT / "tests" / "golden"
FX = ROOT / "tests" / "fixtures" / "port"


def _golden(blk: str):
    g = json.loads((G / blk / "golden.json").read_text())
    top = "uart" if blk == "uart" else g["runs"][0]["top"]
    params = {k: str(v) for k, v in g.get("upstream_params", {}).get(top, {}).items()}
    files = [G / blk / f for f in g["upstream_rtl"]]
    if blk == "uart":   # 하위 모듈 먼저
        files = sorted(files, key=lambda f: f.name != "uart_tx.v" and f.name != "uart_rx.v")
    return Extractor(Design(Inputs(files, top, params))).run()


@pytest.mark.parametrize("blk", ["uart", "axis_adapter", "axis_cobs_decode"])
def test_r1_l1_matches_hand_ported_golden(blk):
    """원본에서 뽑은 L1이 P1에서 손으로 옮긴 golden IR의 L1과 같다 (주파수와 도메인 관계는 R2에서 채움)."""
    res = _golden(blk)
    assert not res["rejects"]
    for name, m in res["modules"].items():
        gold = json.loads((G / blk / "ir" / f"{name}.ir.json").read_text())
        mine, want = frozen_l1(m.ir), frozen_l1(gold)
        for k in ("domains", "params", "ports"):
            assert mine[k] == want[k], (name, k)
    if blk == "uart":
        top = res["modules"]["uart"].ir
        assert top["role"] == "block_top" and {i["name"] for i in top["instances"]} == {"uart_tx_inst", "uart_rx_inst"}
        assert res["order"][-1] == "uart"


def test_r1_async_fifo_domains_and_reviews():
    res = _golden("axis_async_fifo")
    ir = res["modules"]["axis_async_fifo"].ir
    assert [(d["name"], d["clock"], d["reset"]["port"], d["reset"]["async"]) for d in ir["domains"]] == \
        [("s", "s_clk", "s_rst", False), ("m", "m_clk", "m_rst", False)]
    assert [p.get("bundle") for p in ir["ports"][:2]] == ["s_axis", "m_axis"]
    assert not res["rejects"]   # 리셋 동기화 플롭도 동기 리셋으로 고친 원본 (한 설계의 리셋은 한 방식)
    kinds = {r["kind"] for r in res["reviews"]}
    assert {"unused_param", "reset_at_end"} <= kinds
    assert "PAUSE_ENABLE" not in [p["name"] for p in ir["params"]]


def _mixed(defines=None):
    files, incs, defs = read_filelist(FX / "mixed" / "files.f")
    return Extractor(Design(Inputs(files, "mix_top", {}, incs, {**defs, **(defines or {})}))).run()


def test_r1_mixed_verilog_sv_sources():
    res = _mixed()
    assert not res["rejects"], res["rejects"]
    acc, top = res["modules"]["acc"], res["modules"]["mix_top"]
    # SV 예약어 포트 이름은 바꾸고, 부모 연결도 바꾼 이름을 쓴다
    assert acc.renames == {"logic": "logic_"}
    assert [p["name"] for p in acc.ir["ports"]] == ["logic_", "en", "sum"]
    assert acc.ir["ports"][0]["type"] == "logic [DW-1:0]"
    assert any(c["to"] == "u_acc_0.logic_" for c in top.ir["connections"])
    # 매크로와 package 상수는 localparam, 헤더의 parameter는 모듈 파라미터
    p = {x["name"]: x for x in acc.ir["params"]}
    assert p["DW"] == {"name": "DW", "type": "int", "default": "8", "local": True} and p["INIT"]["default"] == "0"
    assert {x["name"]: x["default"] for x in top.ir["params"]}["K"] == "3"
    # generate for는 인스턴스로 펼치고 genvar 바인딩은 값으로, defparam은 인스턴스 파라미터로
    inst = {i["name"]: i.get("params") for i in top.ir["instances"]}
    assert inst == {"u_acc_0": {"INIT": "0"}, "u_acc_1": {"INIT": "6"}, "u_extra": {"INIT": "5"}}
    # 배열 원소 연결은 Porter가 마무리
    assert {(x["instance"], x["expr"]) for x in top.pending} == {("u_acc_0", "s[i]"), ("u_acc_1", "s[i]")}
    kinds = [r["kind"] for r in res["reviews"]]
    assert "unused_param" in kinds and "rename" in kinds   # MODE는 generate 조건에서만 쓰여 지움
    assert "MODE" not in [x["name"] for x in top.ir["params"]]
    lib = {n: Module(m.ir) for n, m in res["modules"].items()}
    for n in lib:
        assert not errors(validate(lib[n], lib, upto=3)), n


def test_r1_ifdef_picks_branch_and_macro_order(tmp_path):
    a, b = _mixed(), _mixed({"USE_SUB": "1"})
    assert not a["rejects"] and not b["rejects"]
    files, incs, defs = read_filelist(FX / "mixed" / "files.f")
    wrong = [files[2], files[0], files[1], files[3]]   # acc.v가 매크로 파일보다 앞
    with pytest.raises(SourceError) as e:
        Design(Inputs(wrong, "mix_top", {}, incs, defs))
    assert any(i["kind"] == "macro_order" and "defs.v" in i["message"] for i in e.value.items)


def test_r1_inout_split_and_tristate():
    res = Extractor(Design(Inputs([FX / "inout" / "pp.v"], "pp_top"))).run()
    assert not res["rejects"]
    core, top = res["modules"]["pp_core"], res["modules"]["pp_top"]
    assert [p["name"] for p in core.ir["ports"]] == ["pad_o", "pad_oe", "pad_i", "q"]
    assert core.tri_split["pad"]["style"] == "push_pull"
    assert top.ir["role"] == "block_top"
    assert top.ir["ports"][0] == {"name": "pad", "dir": "inout", "type": "logic [3:0]", "domain": "sys"}
    assert top.ir["blocks"] == [{"id": "t_pad", "kind": "tristate", "pad": "pad", "oe": "pad_oe", "i": "pad_i",
                                 "domain": "sys", "style": "push_pull", "o": "pad_o"}]
    od = Extractor(Design(Inputs([FX / "inout" / "od.v"], "od_top"))).run()
    t = od["modules"]["od_top"].ir
    assert t["role"] == "block_top" and t["blocks"][0]["style"] == "open_drain" and "o" not in t["blocks"][0]
    assert t["blocks"][0]["oe"] == "sda_oe" and t["blocks"][0]["i"] == "sda_in"


def test_r1_rejects(tmp_path):
    f = tmp_path / "bad.v"
    f.write_text("module sub(input a, inout p); assign p = a ? 1'b1 : 1'bz; endmodule\n"
                 "module bad(input clk, input en, output reg q, inout p);\n"
                 "  always @(posedge clk) if (en) q <= ~q;\n"
                 "  wire w; sub u1(.a(en), .p(w)); sub u2(.a(q), .p(w)); assign p = w;\nendmodule\n")
    res = Extractor(Design(Inputs([f], "bad"))).run()
    kinds = {r["kind"] for r in res["rejects"]}
    assert "tristate_bus" in kinds
    assert {"no_reset", "comb_module"} <= {r["kind"] for r in res["reviews"]}   # 리셋 없는 도메인, 조합 모듈은 옮김
    g = tmp_path / "neg.v"
    g.write_text("module neg(input clk, input rst, output reg q); always @(negedge clk) if (rst) q <= 0; else q <= ~q; endmodule\n")
    res = Extractor(Design(Inputs([g], "neg"))).run()   # 하강 에지 클럭은 하강 에지 도메인으로 옮김
    assert not res["rejects"] and res["modules"]["neg"].ir["domains"][0]["edge"] == "neg"


def test_r1_rejects_mixed_reset_styles(tmp_path):
    """한 설계의 리셋은 모두 비동기이거나 모두 동기다. 모듈 안에서 섞여도, 모듈 사이에서 달라도 거부한다."""
    f = tmp_path / "mix.v"
    f.write_text("module mix(input clk, input rst, input d, output reg a, output reg b);\n"
                 "  always @(posedge clk or posedge rst) if (rst) a <= 0; else a <= d;\n"
                 "  always @(posedge clk) if (rst) b <= 0; else b <= a;\nendmodule\n")
    res = Extractor(Design(Inputs([f], "mix"))).run()
    assert [r["kind"] for r in res["rejects"]] == ["mixed_reset"] and "mix.v:2" in res["rejects"][0]["message"]
    g = tmp_path / "hier.v"
    g.write_text("module sub(input clk, input rst, input d, output reg q); always @(posedge clk) if (rst) q <= 0; else q <= d; endmodule\n"
                 "module hier(input clk, input rst, input d, output q, output reg r);\n"
                 "  sub u(.clk(clk), .rst(rst), .d(d), .q(q));\n"
                 "  always @(posedge clk or posedge rst) if (rst) r <= 0; else r <= q;\nendmodule\n")
    res = Extractor(Design(Inputs([g], "hier"))).run()
    assert "mixed_reset" in [r["kind"] for r in res["rejects"]]
    h = tmp_path / "soft.v"   # 비동기 리셋 블록의 소프트 리셋은 추가 리셋으로 올리지 않음 (동작으로 남김)
    h.write_text("module sr(input clk, input rst, input soft_rst, input d, output reg q);\n"
                 "  always @(posedge clk or posedge rst) if (rst) q <= 0; else if (soft_rst) q <= 0; else q <= d;\nendmodule\n")
    res = Extractor(Design(Inputs([h], "sr"))).run()
    assert not res["rejects"] and "extra" not in res["modules"]["sr"].ir["domains"][0]["reset"]


def test_tristate_ir_validator_and_emitter():
    from pipeline.emitter.sv import emit
    lib = {n: Module(json.loads((FX / "inout" / "ported" / f"{n}.ir.json").read_text())) for n in ("pp_core", "pp_top", "od_ctrl", "od_top")}
    for n, m in lib.items():
        assert not errors(validate(m, lib)), (n, validate(m, lib))
    text = emit(lib["pp_top"], lib).text
    assert "inout  wire [3:0] pad" in text and "assign pad = pad_oe ? pad_o : 'z;" in text and "assign pad_i = pad;" in text
    assert "assign sda = sda_oe ? '0 : 'z;" in emit(lib["od_top"], lib).text
    # inout은 하위 모듈에 둘 수 없고, tristate는 하나만
    bad = json.loads((FX / "inout" / "ported" / "pp_core.ir.json").read_text())
    bad["ports"].append({"name": "x", "dir": "inout", "type": "logic", "domain": "sys"})
    assert any("inout 포트는 top과 block_top에만" in str(i) for i in validate(Module(bad), {}))


def test_r2_request_answers_and_propagation():
    res = _golden("uart")
    irs = {n: m.ir for n, m in res["modules"].items()}
    r1 = {"top": res["top"], "order": res["order"], "reviews": res["reviews"]}
    req = r2.make_request(r1, irs)
    ids = [i["id"] for i in req["items"]]
    assert ids[0] == "freq.sys" and "proto.uart.s_axis" in ids
    assert r2.set_answers(req, {"freq.sys": "abc"}) and r2.set_answers(req, {"nope": "1"})
    ans = {i["id"]: "valid_ready" for i in req["items"] if i["kind"] == "protocol"}
    ans["freq.sys"] = "125"
    ans["proto.uart_tx.s_axis"] = "none"
    assert r2.set_answers(req, ans) == []
    out, errs = r2.apply(req, r1, irs)
    assert errs == []
    assert out["uart"]["domains"][0]["freq_mhz"] == 125 and out["uart_tx"]["domains"][0]["freq_mhz"] == 125
    assert "bundle" not in json.dumps(out["uart_tx"]["ports"]) and any(c["from"] == "uart_rx_inst.m_axis" for c in out["uart"]["connections"])


def test_r2_relations_and_stop():
    res = _golden("axis_async_fifo")
    irs = {n: m.ir for n, m in res["modules"].items()}
    r1 = {"top": res["top"], "order": res["order"], "reviews": res["reviews"]}
    req = r2.make_request(r1, irs)
    assert r2.set_answers(req, {"rel.s.m": "x/2"})
    ans = {i["id"]: "ok" for i in req["items"] if i["kind"] == "review"}
    ans.update({i["id"]: "valid_ready" for i in req["items"] if i["kind"] == "protocol"})
    ans.update({"freq.s": "100", "rel.s.m": "s/2"})
    assert r2.set_answers(req, ans) == []
    out, errs = r2.apply(req, r1, irs)
    assert errs == [] and out["axis_async_fifo"]["domains"][1]["sync_to"] == {"domain": "s", "div": 2}
    assert out["axis_async_fifo"]["domains"][1]["freq_mhz"] == 50
    r2.set_answers(req, {"review.1": "stop"})
    _, errs = r2.apply(req, r1, irs)
    assert errs and errs[0].startswith("사용자가 멈춤")


@pytest.mark.parametrize("path", ["tests/golden/axis_async_fifo/ir/axis_async_fifo.ir.json", "tests/golden/uart/ir/uart.ir.json",
                                  "tests/fixtures/multiclock/mc_top.ir.json", "tests/fixtures/port/inout/ported/od_top.ir.json",
                                  "tests/fixtures/negedge/ported/ne_top.ir.json"])
def test_spec_tables_roundtrip(path):
    ir = json.loads((ROOT / path).read_text())
    l1, _ = block_top_l1(parse(f"# {ir['module']} SPEC\n\n" + spec_tables.tables_md(ir)))
    assert spec_tables.roundtrip_diff(ir, l1) == []


def test_spec_writer_gate(tmp_path):
    ir = json.loads((G / "axis_cobs_decode" / "ir" / "axis_cobs_decode.ir.json").read_text())
    base = (G / "axis_cobs_decode" / "block_spec.md").read_text()
    head, rest = base.split("\n## 2.", 1)
    good = head + "\n" + spec_tables.tables_md(ir) + rest[rest.index("\n## 5."):]
    (tmp_path / "good.md").write_text(good)
    assert spec_writer.gate(tmp_path / "good.md", ir) == []
    (tmp_path / "bad.md").write_text(good.replace("| sys | clk |", "| sys | clk2 |", 1))
    assert any(e.startswith("[2~4절]") for e in spec_writer.gate(tmp_path / "bad.md", ir))


def test_porter_gate_keeps_draft_structure(tmp_path):
    draft = json.loads((FX / "mixed" / "ported" / "default" / "mix_top.ir.json").read_text())
    acc = json.loads((FX / "mixed" / "ported" / "default" / "acc.ir.json").read_text())
    (tmp_path / "out").mkdir()
    out = tmp_path / "out" / "mix_top.ir.json"
    out.write_text(json.dumps(draft))
    check = porter.check(draft, {"acc": acc})
    assert check(tmp_path) == []
    moved = json.loads(json.dumps(draft))
    moved["instances"][0]["params"]["INIT"] = "9"
    moved["connections"] = moved["connections"][1:]
    out.write_text(json.dumps(moved))
    errs = check(tmp_path)
    assert any("instances" in e for e in errs) and any("연결이 빠지거나" in e for e in errs)


def test_r1_comb_module_and_no_reset_domain():
    res = Extractor(Design(Inputs([FX / "comb" / "cr.v"], "cr_top"))).run()
    assert not res["rejects"]
    dec, pipe, top = (res["modules"][n].ir for n in ("dec", "pipe", "cr_top"))
    assert dec["domains"] == [] and {p["domain"] for p in dec["ports"]} == {None}
    assert pipe["domains"] == [{"name": "sys", "clock": "clk", "edge": "pos"}]
    assert {i["name"]: i["domain_map"] for i in top["instances"]} == {"u_dec": {}, "u_pipe": {"sys": "sys"}}
    assert {p["name"]: p["domain"] for p in top["ports"]} == {"sel": "sys", "en": "sys", "q": "sys", "cnt": "sys"}
    lib = {n: Module(json.loads((FX / "comb" / "ported" / f"{n}.ir.json").read_text())) for n in ("dec", "pipe", "cr_top")}
    for n, m in lib.items():
        assert not errors(validate(m, lib)), (n, validate(m, lib))



def test_comb_block_top_spec_and_harness_view(tmp_path):
    """조합 Block Top: SPEC 2절의 "없음" 행은 도메인이 아니고, 표 왕복이 같으며, 하니스와 시뮬레이션 top은 가상 클럭 래퍼를 쓴다."""
    from pipeline.spec.parse import load
    from pipeline.tb.gen import COMB_CLK, harness_config, pad_top, trace_names

    l1, _ = block_top_l1(load(ROOT / "tests" / "fixtures" / "comb_top" / "add8.spec.md"))
    assert l1["domains"] == [] and {p["domain"] for p in l1["ports"]} == {None}
    md = spec_tables.tables_md(l1)
    assert "| 없음 | 없음 |" in md and "비동기" not in md
    assert spec_tables.roundtrip_diff(l1, block_top_l1(parse("# add8 SPEC\n\n" + md))[0]) == []
    cfg = harness_config(Module(l1))
    assert cfg["clocks"] == {"comb": (COMB_CLK, 10.0)} and set(cfg["port_domains"].values()) == {"comb"}
    assert trace_names(Module(l1))["domains"] == ["comb"]
    files, top = pad_top(l1, tmp_path)
    assert top == "add8_pad_top" and COMB_CLK in files[0].read_text() and f".{COMB_CLK}(" not in files[0].read_text()

def test_no_reset_domain_rules():
    pipe = json.loads((FX / "comb" / "ported" / "pipe.ir.json").read_text())
    bad = json.loads(json.dumps(pipe))
    bad["signals"][0]["reset"] = "'0"
    bad["blocks"][0]["writes"] = ["s"]
    bad["blocks"][0]["body"] = "s <= d;"
    bad["blocks"].append({"id": "k_q", "kind": "seq", "domain": "sys", "reads": ["s"], "writes": ["q"], "body": "q <= s;"})
    assert any("리셋 없는 도메인 sys의 레지스터에 리셋값" in str(i) for i in validate(Module(bad), {}))
    # 리셋 있는 자식 도메인을 리셋 없는 부모 도메인에 연결할 수 없음
    top = json.loads((FX / "comb" / "ported" / "cr_top.ir.json").read_text())
    top["domains"] = [{"name": "sys", "clock": "clk", "edge": "pos"}]
    top["ports"][3].pop("reset")
    top["blocks"] = []
    top["ports"] = top["ports"][:3]
    core = json.loads((FX / "rsync" / "ported" / "rs_core.ir.json").read_text())
    top["instances"][1] = {"name": "u_pipe", "module": "rs_core", "domain_map": {"sys": "sys"}}
    top["connections"] = [c for c in top["connections"] if "u_pipe" not in c["from"] + c["to"]]
    lib = {"rs_core": Module(core), "dec": Module(json.loads((FX / "comb" / "ported" / "dec.ir.json").read_text()))}
    assert any("리셋 없는 도메인 sys에 연결함" in str(i) for i in validate(Module(top), lib))
    # SPEC 2절: 리셋 포트 "없음" 왕복
    l1, _ = block_top_l1(parse("# pipe SPEC\n\n" + spec_tables.tables_md(pipe)))
    assert "reset" not in l1["domains"][0] and spec_tables.roundtrip_diff(pipe, l1) == []


def test_r1_reset_synchronizer_in_top():
    res = Extractor(Design(Inputs([FX / "rsync" / "rs.v"], "rs_top"))).run()
    assert not res["rejects"]
    top = res["modules"]["rs_top"].ir
    assert top["role"] == "block_top"
    assert top["domains"][0]["reset"] == {"port": "arst_n", "active": "low", "async": True, "synchronize": True}


def test_r1_reset_synchronizer_not_in_top(tmp_path):
    f = tmp_path / "x.v"
    f.write_text((FX / "rsync" / "rs.v").read_text().replace("module rs_top", "module rs_mid") +
                 "module x_top(input clk, input arst_n, input [7:0] d, output [7:0] q);\n"
                 "  rs_mid u_mid(.clk(clk), .arst_n(arst_n), .d(d), .q(q));\nendmodule\n")
    res = Extractor(Design(Inputs([f], "x_top"))).run()
    assert "reset_sync" in {r["kind"] for r in res["rejects"]}


def test_simcopy_moves_arrayed_defparam(tmp_path):
    from pipeline.importer import simcopy
    files, incs, defs = read_filelist(FX / "mixed" / "files.f")
    d = Design(Inputs(files, "mix_top", {}, incs, defs))
    out = simcopy.make(d, tmp_path)
    text = Path(out[str((FX / "mixed" / "mix_top.sv").resolve())]).read_text()
    assert "defparam g_acc[1]" not in text and "defparam u_extra.INIT = 5;" in text
    assert "acc #(.INIT(((i == 0) ? 0 : 6)))" in text


def test_spec_writer_revise_gets_failed_test_stimuli(tmp_path):
    """R6 고치기: 원본에서 실패한 Verifier 테스트의 자극을 `r6_stim/`으로 받고, refsim으로 다시 넣어 보라는 지시를 받는다."""
    prev = tmp_path / "spec_v0.md"
    prev.write_text("# m SPEC\n")
    st = tmp_path / "v0_t.json"
    st.write_text("{}")
    t = spec_writer.make_task("spec_writer/v1", "m", "", {}, {}, {}, prev_spec=prev, r6={}, stims={"v0_t.json": st})
    assert t.inputs["r6_stim/v0_t.json"] == st and "r6_stim/" in t.text
    t = spec_writer.make_task("spec_writer/v1", "m", "", {}, {}, {}, prev_spec=prev, r6={})
    assert not any(k.startswith("r6_stim/") for k in t.inputs) and "r6_stim/" not in t.text


def test_isolated_work_dirs_of_same_named_runs_do_not_collide(tmp_path):
    """P11 R6의 재생성 실행은 블록마다 r6/regen_0이다. 동시에 돌아도 격리 작업 디렉터리가 겹치면 안 된다."""
    from pipeline.scheduler.run import Run
    dirs = []
    for blk in ("a", "b"):
        r = Run.__new__(Run)
        r.dir, r.isolate = tmp_path / blk / "r6" / "regen_0", True
        dirs.append(r.work_dir("verifier/v0"))
    assert dirs[0] != dirs[1] and all(d.name == "verifier__v0" and d.parent.name.startswith("regen_0-") for d in dirs)


def _two_clock_cfg(mode="reset", joint=True):
    h = {"clocks": {"m": ["m_clk", 11.001], "s": ["s_clk", 10.0]}, "resets": {"m": ["m_rst", 1], "s": ["s_rst", 1]},
         "bundles": [], "inputs": [], "port_domains": {}}
    return {"harness": h, "widths": {}, "mode": mode, "seed": 3, "cycles": 3000, "drain": 200, "model": None,
            "harness_dir": None} | ({"joint_reset": True} if joint else {})


def test_r6_joint_reset_plan_overlaps_in_time():
    """R6 함께 리셋: 모든 도메인의 리셋이 같은 시각 구간을 덮고, 가장 느린 클럭 4사이클 이상이며 프로브 전에 끝난다."""
    from pipeline.tb.diffsim import INIT_RESET, make_plan
    plan = make_plan(_two_clock_cfg())
    groups = plan["reset_groups"]
    assert groups and plan["after_reset"]
    per = {"m": 11.001, "s": 10.0}
    for parts in groups:
        spans = {d: ((c0 + 0.5) * per[d], (c0 + n - 0.5) * per[d]) for d, c0, n in parts}
        assert all(n >= 4 for _, _, n in parts)
        assert max(a for a, _ in spans.values()) <= min(b for _, b in spans.values())   # 시각 구간이 겹침
        for d, c0, n in parts:
            assert plan["domains"][d]["reset"][c0:c0 + n] == [1] * n
            assert c0 + n < plan["probe_at"] - 100
    for d, dp in plan["domains"].items():   # 그룹 밖 리셋은 처음 리셋뿐
        inside = {c for parts in groups for gd, c0, n in parts if gd == d for c in range(c0, c0 + n)}
        assert {c for c, v in enumerate(dp["reset"]) if v} == set(range(INIT_RESET)) | inside
    assert "reset_groups" not in make_plan(_two_clock_cfg(joint=False))


def test_r6_joint_reset_episodes_remove_whole_group():
    from pipeline.tb.diffsim import describe, episodes, make_plan, truncate, without
    plan = make_plan(_two_clock_cfg())
    eps = [e for e in episodes(plan) if e["kind"] == "reset"]
    assert len(eps) == len(plan["reset_groups"])
    p2 = without(plan, eps[:1])
    for d, c0, n in eps[0]["parts"]:
        assert p2["domains"][d]["reset"][c0:c0 + n] == [0] * n
    assert len([e for e in episodes(p2) if e["kind"] == "reset"]) == len(eps) - 1
    h = _two_clock_cfg()["harness"]
    assert any("함께 리셋" in x for x in describe(plan, h))
    d, c0, n = eps[0]["parts"][0]
    p3 = truncate(plan, c0 + 1)   # 그룹 중간에서 자르면 그룹 전체를 지운다 (한쪽 리셋이 남지 않음)
    for gd, gc, gn in eps[0]["parts"]:
        assert 1 not in p3["domains"][gd]["reset"][gc:gc + gn]


def test_r6_compare_after_last_reset_only():
    from pipeline.tb.diffsim import compare
    a = {"outputs": {"o": [["m", 5, {"x": 1}], ["m", 50, {"x": 7}]]}, "reset_end": {"m": 20, "s": 18}}
    b = {"outputs": {"o": [["m", 6, {"x": 2}], ["m", 9, {"x": 3}], ["m", 51, {"x": 7}]]}, "reset_end": {"m": 20, "s": 18}}
    assert compare(a, b) is None
    b["outputs"]["o"][-1][2] = {"x": 8}
    assert compare(a, b)["kind"] == "data"
    a.pop("reset_end"), b.pop("reset_end")
    assert compare(a, b) is not None


def test_r6_compare_tail_count_follows_inputs_accepted():
    """함께 리셋 비교: 입력을 더 받은 쪽이 같은 앞부분 뒤에 더 낸 것은 차이가 아니다. 덜 받고 더 내면 차이다."""
    from pipeline.tb.diffsim import compare
    a = {"outputs": {"o": [["m", 30, {"x": 1}]]}, "inputs": {"i": 1}, "reset_end": {"m": 9}}
    b = {"outputs": {"o": [["m", 30, {"x": 1}], ["m", 31, {"x": 2}]]}, "inputs": {"i": 2}, "reset_end": {"m": 9}}
    assert compare(a, b) is None
    b["inputs"] = {"i": 1}
    assert compare(a, b)["kind"] == "count"
    b["inputs"] = {"i": 2}
    a.pop("reset_end")
    assert compare(a, b)["kind"] == "count"
    a["tail_tolerant"] = True   # steady 모드라도 리셋 도메인이 둘 이상이면 끝 개수 허용 (처음 리셋 뒤 ready 지연도 미정)
    assert compare(a, b) is None


def test_r6_compare_skips_contiguous_extra_from_side_that_accepted_more():
    """다중 리셋 도메인: 프로브 앞에서 입력 순서가 다른 위치에서 끊기면, 더 받은 쪽이 한 곳에 낸 항목은 차이가 아니다
    (2026-10-23 P11 axis_async_fifo reset 시드 4). 받은 입력이 같거나 덜 받은 쪽이 더 냈으면 차이다."""
    from pipeline.tb.diffsim import compare
    o = lambda *xs: [["m", c, {"x": x}] for c, x in enumerate(xs)]
    a = {"outputs": {"o": o(1, 2, 9, 3, 4)}, "inputs": {"i": 6}, "tail_tolerant": True}
    b = {"outputs": {"o": o(1, 2, 3, 4)}, "inputs": {"i": 5}, "tail_tolerant": True}
    assert compare(a, b) is None and compare(b, a) is None
    b["inputs"] = {"i": 6}
    assert compare(a, b)["kind"] == "data"
    b["inputs"], a["outputs"]["o"] = {"i": 5}, o(1, 2, 9, 8, 3, 4)   # 받은 차이(1)보다 많이 더 냄
    assert compare(a, b)["kind"] == "data"
    a["outputs"]["o"], b["outputs"]["o"] = o(1, 2, 9, 3, 4), o(1, 2, 3, 5)   # 건너뛴 뒤에도 다름
    assert compare(a, b)["kind"] == "data"
    a.pop("tail_tolerant"), b.pop("tail_tolerant")
    b["outputs"]["o"] = o(1, 2, 3, 4)
    assert compare(a, b)["kind"] == "data"


def test_r6_steady_plan_tail_tolerant_only_with_joint_reset():
    from pipeline.tb.diffsim import make_plan
    cfg = _two_clock_cfg()
    cfg["mode"] = "steady"
    assert make_plan(cfg).get("tail_tolerant") is True
    assert "tail_tolerant" not in make_plan(_two_clock_cfg(joint=False))


def test_r6_plan_opts_joint_only_for_multiple_reset_domains():
    from pipeline.importer.r6 import plan_opts
    assert plan_opts({"resets": {"m": ["m_rst", 1], "s": ["s_rst", 1]}}) == {"joint_reset": True}
    assert plan_opts({"resets": {"clk": ["rst", 1]}}) == {}


def test_ref_params_keep_removed_fixed_params():
    """R1이 쓰이지 않는다고 지운 파라미터도 원본 빌드에는 고정 값으로 준다. 빼면 원본이 기본값으로 다른 구조를 골라
    이식 IR과 함께 틀린 채로 R4가 같다고 본다 (2026-10-22 axis_async_fifo PAUSE_ENABLE: 고정 1, 기본 0)."""
    from pipeline.importer.r4 import ref_params
    ir = {"module": "f", "params": [{"name": "W", "default": "8"}, {"name": "AW", "default": "3", "local": True}]}
    r1 = {"top": "f", "inputs": {"params": {"W": "8", "PAUSE_ENABLE": "1"}},
          "modules": {"f": {"renames": {}, "removed_params": {"PAUSE_ENABLE": "1"}}}}
    assert ref_params(ir, r1) == {"W": "8", "PAUSE_ENABLE": "1"}
    r1["modules"]["f"].pop("removed_params")   # 기록이 생기기 전의 R1: top은 고정 설정에서
    assert ref_params(ir, r1) == {"W": "8", "PAUSE_ENABLE": "1"}
    r1["top"] = "other"
    assert ref_params(ir, r1) == {"W": "8"}


def test_r1_records_removed_params_with_fixed_values():
    """golden axis_async_fifo를 PAUSE_ENABLE=1로 고정해 추출하면 PAUSE_ENABLE은 generate 조건에서만 쓰여 지워지고, 그 값이 남는다."""
    ext = _golden("axis_async_fifo")
    m = ext["modules"]["axis_async_fifo"]
    assert m.removed_params.get("PAUSE_ENABLE") == "1"
    assert "PAUSE_ENABLE" not in {p["name"] for p in m.ir["params"]}
