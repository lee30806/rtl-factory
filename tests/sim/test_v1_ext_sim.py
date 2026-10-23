"""v1 제외 항목 확장 (2026-10-19): 시뮬레이션이 필요한 부분.

- 하강 에지 도메인: 원본(ne.v) R1 → 이식 IR R4 등가, 생성 하니스로 반주기 단 실행, Python 조각 판의 번역기 검사.
- inout 패드: 실행기의 tristate를 번역기 검사(패드 래퍼)로 RTL과 비교, 하니스 패드 체커의 동시 구동 충돌.
"""
import copy
import json
import shutil

import pytest

pytest.importorskip("cocotb_tools.runner")
if not shutil.which("verilator"):
    pytest.skip("verilator 없음", allow_module_level=True)

from pipeline.emitter.sv import emit  # noqa: E402
from pipeline.importer import r1 as r1_mod, r4  # noqa: E402
from pipeline.importer.sources import Inputs  # noqa: E402
from pipeline.ir.model import Module  # noqa: E402
from pipeline.scheduler.run import STUB_CELLS  # noqa: E402
from pipeline.tb.gen import harness_source  # noqa: E402
from pipeline.verify.pyequiv import check  # noqa: E402
from pipeline.verify.run import lint, sim  # noqa: E402
from test_pipeline import ROOT  # noqa: E402

FX = ROOT / "tests" / "fixtures" / "negedge"
FAST = {"seeds": (1,), "cycles": 600}


def _ir(name="ported/ne_top"):
    return json.loads((FX / f"{name}.ir.json").read_text())


def test_r1_negedge_domain_and_r4_equal(tmp_path):
    r1 = r1_mod.run(Inputs([FX / "ne.v"], "ne_top", {}, [], {}), tmp_path)
    assert r1["ok"], r1
    doms = json.loads((tmp_path / "r1" / "ir" / "ne_top.ir.json").read_text())["domains"]
    assert [(d["name"], d["edge"], d.get("sync_to")) for d in doms] == \
        [("clk", "pos", None), ("clk_neg", "neg", {"domain": "clk", "div": 1})]
    lib = {"ne_top": _ir()}
    assert r4.run_module("ne_top", lib, r1, tmp_path / "r4", STUB_CELLS, **FAST)["status"] == "equal"
    bad = copy.deepcopy(lib)   # 하강 에지 단을 상승 에지로 옮기면 그 값을 받는 상승 에지 단(p_q)이 한 사이클 늦어진다
    bad["ne_top"]["blocks"][2]["domain"] = "clk"
    bad["ne_top"]["signals"][1]["domain"] = "clk"
    assert r4.run_module("ne_top", bad, r1, tmp_path / "r4b", STUB_CELLS, **FAST)["status"] == "differ"


def test_harness_drives_neg_domain_on_rising_edge(tmp_path):
    m = Module(_ir())
    (tmp_path / "rtl").mkdir()
    f = tmp_path / "rtl" / "ne_top.sv"
    f.write_text(emit(m).text)
    assert lint([f], "ne_top").ok
    (tmp_path / "h").mkdir()
    (tmp_path / "h" / "ne_top_harness.py").write_text(harness_source(m))
    r = sim([f], "ne_top", "tb_ne_top_h", ROOT / "tests" / "sim", tmp_path / "build", harness_dir=tmp_path / "h")
    assert r.ok, [x.message for x in r.findings]
    res = json.loads(next((tmp_path / "build" / "tb_out").glob("*/neg_edge_domains.result.json")).read_text())
    assert res["matched"] == 41 and res["properties"] == [] and res["scoreboard"] is None


def test_translation_of_neg_domain_matches_model(tmp_path):
    lib = {"ne_top": Module(_ir("ne_top_py"))}
    (tmp_path / "rtl").mkdir()
    f = tmp_path / "rtl" / "ne_top.sv"
    f.write_text(emit(lib["ne_top"]).text)
    res = check("ne_top", lib, [f], tmp_path / "b", cycles=1500)
    assert res["ok"] and res["mismatch"] is None, res
    # RTL의 하강 에지 단을 상승 에지로 바꾸면 번역기 검사가 잡는다 (실행기가 하강 에지 시각에 그 도메인을 진행함)
    f.write_text(emit(lib["ne_top"]).text.replace("always_ff @(negedge clk or negedge rst_n) begin\n    if (!rst_n) begin\n      conv",
                                                  "always_ff @(posedge clk or negedge rst_n) begin\n    if (!rst_n) begin\n      conv"))
    res = check("ne_top", lib, [f], tmp_path / "b2", cycles=600)
    assert not res["ok"] and res["mismatch"]["output"] in ("m_data", "p_q"), res


# ---------------------------------------------------------------- inout 패드
@pytest.mark.parametrize("top", ["pp_top", "od_top"])
def test_translation_with_tristate_top_matches_model(tmp_path, top):
    from test_v1_ext import _py_lib
    lib = _py_lib(top)
    (tmp_path / "rtl").mkdir()
    files = []
    for n, m in lib.items():
        files.append(tmp_path / "rtl" / f"{n}.sv")
        files[-1].write_text(emit(m, lib).text)
    res = check(top, lib, files, tmp_path / "b", cycles=1500)
    assert res["ok"] and res["mismatch"] is None, res
    bad = files[-1].read_text().replace("? pad_o : 'z", "? ~pad_o : 'z") if top == "pp_top" else None
    if bad:   # 블록의 패드 구동을 틀리게 바꾸면 패드 값이 달라진다
        files[-1].write_text(bad)
        res = check(top, lib, files, tmp_path / "b2", cycles=600)
        assert not res["ok"] and res["mismatch"]["output"] == "pad_pad_val", res


def test_harness_reports_pad_conflict(tmp_path):
    from pipeline.scheduler.run import emit_lib
    from pipeline.tb.gen import pad_top
    lib = {n: json.loads((ROOT / "tests/fixtures/port/inout/ported" / f"{n}.ir.json").read_text()) for n in ("pp_core", "pp_top")}
    files, _ = emit_lib(lib, tmp_path / "rtl", STUB_CELLS)
    (tmp_path / "h").mkdir()
    (tmp_path / "h" / "pp_top_harness.py").write_text(harness_source(Module(lib["pp_top"])))
    pads, top = pad_top(lib["pp_top"], tmp_path / "pad")
    r = sim([*files, *pads], top, "tb_pp_top_h", ROOT / "tests" / "sim", tmp_path / "build", harness_dir=tmp_path / "h")
    assert r.ok, [x.message for x in r.findings]
    res = json.loads(next((tmp_path / "build" / "tb_out").glob("*/pad_conflict_is_reported.result.json")).read_text())
    assert res["protocol"] and res["protocol"][0]["bundle"] == "pad"


# ---------------------------------------------------------------- function/task, SVA
V1X = ROOT / "tests" / "fixtures" / "v1ext"


def test_function_task_translation_matches_model(tmp_path):
    lib = {"fn_mod": Module(_ir_v1x("fn_mod_py"))}
    (tmp_path / "rtl").mkdir()
    f = tmp_path / "rtl" / "fn_mod.sv"
    f.write_text(emit(lib["fn_mod"]).text)
    assert lint([f], "fn_mod").ok
    res = check("fn_mod", lib, [f], tmp_path / "b", cycles=1500)
    assert res["ok"] and res["mismatch"] is None, res
    f.write_text(emit(lib["fn_mod"]).text.replace("acc = {acc[6:0], acc[7]};", "acc = {acc[0], acc[7:1]};"))
    res = check("fn_mod", lib, [f], tmp_path / "b2", cycles=600)
    assert not res["ok"] and res["mismatch"]["output"] == "q", res


def _ir_v1x(name):
    return json.loads((V1X / f"{name}.ir.json").read_text())


def test_sva_failures_are_findings_of_the_test(tmp_path):
    from pipeline.classifier.classify import classify_findings
    from pipeline.emitter.sv import emit as emit_
    m = Module(_ir_v1x("fn_mod"))
    (tmp_path / "rtl").mkdir()
    f = tmp_path / "rtl" / "fn_mod.sv"
    e = emit_(m)
    f.write_text(e.text)
    r = sim([f], "fn_mod", "tb_fn_mod", ROOT / "tests" / "sim", tmp_path / "ok")
    # 두 cocotb 테스트 모두 통과하지만, 가정을 어긴 테스트에는 assume 실패가 붙는다
    assert [(x.tool, x.rule, x.test, x.message.split()[0]) for x in r.findings] == \
        [("sva", "SVA_ASSUME", "breaks_assumption", "assertions/m_b_small")], r.findings
    # z 상위 니블을 틀리게 만든 RTL: 두 테스트 모두 assert p_z 실패. Classifier는 모듈과 노드를 짚어 Repair로 보낸다
    f.write_text(e.text.replace("z_hi = 4'((a ^ b) >> 4);", "z_hi = 4'((a ^ b) >> 3);"))
    r = sim([f], "fn_mod", "tb_fn_mod", ROOT / "tests" / "sim", tmp_path / "bad")
    p_z = [x for x in r.findings if x.rule == "SVA_ASSERT"]
    assert {x.test for x in p_z} == {"within_assumption", "breaks_assumption"} and all("assertions/p_z" in x.message for x in p_z)
    c = classify_findings(p_z, {"fn_mod": e.sourcemap}, {"fn_mod": "module"})
    assert {(x.type, x.route, x.module, x.node) for x in c} == {("sim_mismatch", "repair", "fn_mod", "assertions/p_z")}
