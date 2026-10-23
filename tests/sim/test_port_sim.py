"""P11 원본 RTL에서 IR과 SPEC 만들기: 시뮬레이션이 필요한 부분 (R4 사이클 등가, R6 트랜잭션 비교, pl.py porteq, 실행기 전체 흐름).

R4와 R6의 '이식 IR'과 '재생성 IR' 자리에는 손으로 쓴 IR(golden, 시험용 원본의 ported/)과 그것을 일부러 틀리게 바꾼 IR을 쓴다.
실행기 시험은 가짜 에이전트(tests/fake_port_agent.py)와 가짜 재생성 함수로 단계 전환만 본다.
"""
import copy
import json
import shutil
import subprocess
import sys

import pytest

pytest.importorskip("cocotb_tools.runner")
if not shutil.which("verilator"):
    pytest.skip("verilator 없음", allow_module_level=True)

from pipeline.agents import porter  # noqa: E402
from pipeline.agents.roles import Roles  # noqa: E402
from pipeline.agents.runner import AgentConfig  # noqa: E402
from pipeline.importer import r1 as r1_mod, r4, r6  # noqa: E402
from pipeline.importer.sources import Inputs, read_filelist  # noqa: E402
from pipeline.scheduler.port import PortRun  # noqa: E402
from pipeline.scheduler.run import STUB_CELLS  # noqa: E402
from test_pipeline import ROOT  # noqa: E402

G = ROOT / "tests" / "golden"
FX = ROOT / "tests" / "fixtures" / "port"
FAST = {"seeds": (1,), "cycles": 600}


def _r1(tmp_path, files, top, params=None, defines=None, incs=None):
    return r1_mod.run(Inputs(files, top, params or {}, incs or [], defines or {}), tmp_path)


def _golden_r1(tmp_path, blk):
    g = json.loads((G / blk / "golden.json").read_text())
    top = "uart" if blk == "uart" else g["runs"][0]["top"]
    files = sorted([G / blk / f for f in g["upstream_rtl"]], key=lambda f: f.stem == top)
    return _r1(tmp_path, files, top, {k: str(v) for k, v in g.get("upstream_params", {}).get(top, {}).items()})


def _lib(dirpath, names):
    return {n: json.loads((dirpath / f"{n}.ir.json").read_text()) for n in names}


def test_r4_golden_adapter_equal_and_mutation(tmp_path):
    r1 = _golden_r1(tmp_path, "axis_adapter")
    lib = _lib(G / "axis_adapter" / "ir", ["axis_adapter"])
    assert r4.run_module("axis_adapter", lib, r1, tmp_path / "r4", STUB_CELLS, **FAST)["status"] == "equal"
    bad = copy.deepcopy(lib)
    text = json.dumps(bad["axis_adapter"])
    assert "m_axis_tlast <= s_axis_tlast_reg;" in text
    bad["axis_adapter"] = json.loads(text.replace("m_axis_tlast <= s_axis_tlast_reg;", "m_axis_tlast <= !s_axis_tlast_reg;", 1))
    res = r4.run_module("axis_adapter", bad, r1, tmp_path / "r4b", STUB_CELLS, **FAST)
    assert res["status"] == "differ" and res["first"]["port"] == "m_axis_tlast"


def test_r4_reset_only_and_hierarchy(tmp_path):
    """uart: 원본의 초기값만 있는 레지스터(txd = 1)를 IR은 리셋으로 옮겨 리셋 구간에서만 다름. 아래에서 위로 비교."""
    r1 = _golden_r1(tmp_path, "uart")
    lib = _lib(G / "uart" / "ir", r1["order"])
    res = r4.run_all(r1["order"], lib, r1, tmp_path / "r4", STUB_CELLS, **FAST)
    assert {n: r["status"] for n, r in res.items()} == {"uart_tx": "reset_only", "uart_rx": "reset_only", "uart": "reset_only"}
    assert res["uart_tx"]["first"]["in_window"]


def test_r4_mixed_sources_two_define_sets(tmp_path):
    files, incs, defs = read_filelist(FX / "mixed" / "files.f")
    for combo, extra in (("default", {}), ("use_sub", {"USE_SUB": "1"})):
        r1 = _r1(tmp_path / combo, files, "mix_top", {}, {**defs, **extra}, incs)
        assert r1["ok"], r1["rejects"]
        lib = _lib(FX / "mixed" / "ported" / combo, r1["order"])
        res = r4.run_all(r1["order"], lib, r1, tmp_path / combo / "r4", STUB_CELLS, **FAST)
        assert {n: r["status"] for n, r in res.items()} == {"acc": "equal", "mix_top": "equal"}, (combo, res)
    lib = _lib(FX / "mixed" / "ported" / "default", r1["order"])   # USE_SUB 원본에 기본 설정 IR: 달라야 함
    res = r4.run_all(r1["order"], lib, r1, tmp_path / "cross", STUB_CELLS, **FAST)
    assert res["acc"]["status"] == "differ" and res["mix_top"]["status"] == "skipped"


@pytest.mark.parametrize("name,top,mod", [("pp", "pp_top", "pp_core"), ("od", "od_top", "od_ctrl")])
def test_r4_inout_pad_model(tmp_path, name, top, mod):
    r1 = _r1(tmp_path, [FX / "inout" / f"{name}.v"], top)
    lib = _lib(FX / "inout" / "ported", r1["order"])
    res = r4.run_all(r1["order"], lib, r1, tmp_path / "r4", STUB_CELLS, **FAST)
    assert {n: r["status"] for n, r in res.items()} == {mod: "equal", top: "equal"}
    if name == "pp":   # 나눈 포트를 가진 하위 모듈도 원본처럼 합쳐 비교하므로 틀리면 잡힘
        bad = copy.deepcopy(lib)
        bad[mod]["blocks"][0]["body"] = bad[mod]["blocks"][0]["body"].replace("~pad_oe", "pad_oe")
        assert r4.run_module(mod, bad, r1, tmp_path / "r4b", STUB_CELLS, **FAST)["status"] == "differ"


def test_r6_compare_and_minimize(tmp_path):
    r1 = _golden_r1(tmp_path, "axis_cobs_decode")
    ir = _lib(G / "axis_cobs_decode" / "ir", ["axis_cobs_decode"])["axis_cobs_decode"]
    seeds = (("steady", 1), ("reset", 3))
    res = r6.run(ir, {"axis_cobs_decode": ir}, r1, tmp_path / "eq", STUB_CELLS, cycles=800, seeds=seeds)
    assert res["status"] == "equal" and res["timing"][0]["delta_max"] == 0
    bad = json.loads(json.dumps(ir).replace("m_axis_tuser <= m_axis_tuser_int;", "m_axis_tuser <= !m_axis_tuser_int;", 1))
    res = r6.run(ir, {"axis_cobs_decode": bad}, r1, tmp_path / "bad", STUB_CELLS, cycles=800, seeds=seeds, budget=12)
    assert res["status"] == "differ"
    j = r6.judge_input(res)
    assert j["difference"]["stream"] == "m_axis" and "s_axis" in j["inputs"]


def test_porteq_tool(tmp_path):
    r1 = _golden_r1(tmp_path / "r1", "axis_cobs_decode")
    work = tmp_path / "w"
    porter.make_ref_dir(r1, work / "ref")
    shutil.copy(G / "axis_cobs_decode" / "ir" / "axis_cobs_decode.ir.json", work / "m.ir.json")
    r = subprocess.run([sys.executable, str(ROOT / "pipeline" / "agents" / "pl.py"), "porteq", "m.ir.json", "--ref", "ref",
                        "--cycles", "500"], cwd=work, capture_output=True, text=True, env={"PL_NO_LOG": "1", "PATH": "/usr/bin:/bin:/opt/homebrew/bin"})
    out = json.loads(r.stdout)
    assert out["ok"] and out["status"] == "equal", r.stdout + r.stderr


def test_refsim_tool(tmp_path):
    """SpecWriter의 refsim: 원본에 정한 자극을 넣고 출력 트랜잭션을 본다. 오류로 끝나는 프레임의 출력 data(직전 바이트)도
    원본 그대로 나온다 (실제 실행 R6 1회차에서 SPEC이 놓친 것)."""
    r1 = _golden_r1(tmp_path / "r1", "axis_cobs_decode")
    work = tmp_path / "w"
    porter.make_ref_dir(r1, work / "ref")
    shutil.copy(G / "axis_cobs_decode" / "ir" / "axis_cobs_decode.ir.json", work / "m.ir.json")
    byte = lambda d, last=0: {"tdata": d, "tlast": last, "tuser": 0}
    stim = {"inputs": {"s_axis": [byte(3), byte(17), byte(34), byte(2), byte(51), byte(0, 1), byte(5), byte(133), byte(218, 1)]}}
    (work / ".refsim").mkdir()
    (work / ".refsim" / "s.json").write_text(json.dumps(stim))
    env = {"PL_NO_LOG": "1", "PATH": "/usr/bin:/bin:/opt/homebrew/bin"}
    pl = str(ROOT / "pipeline" / "agents" / "pl.py")
    r = subprocess.run([sys.executable, pl, "refsim", "m.ir.json", "--ref", "ref", "--stim", ".refsim/s.json"],
                       cwd=work, capture_output=True, text=True, env=env)
    out = json.loads(r.stdout)
    assert out["ok"] and out["inputs_accepted"] == {"s_axis": 9}, r.stdout + r.stderr
    got = [(t["m_axis_tdata"], t["m_axis_tlast"], t["m_axis_tuser"]) for t in out["outputs"]["m_axis"]]
    assert got == [(17, 0, 0), (34, 0, 0), (0, 0, 0), (51, 1, 0), (133, 1, 1)]
    assert out["outputs"]["m_axis"][0]["cycle"] >= 0
    (work / ".refsim" / "b.json").write_text(json.dumps({"inputs": {"nope": []}}))
    r = subprocess.run([sys.executable, pl, "refsim", "m.ir.json", "--ref", "ref", "--stim", ".refsim/b.json"],
                       cwd=work, capture_output=True, text=True, env=env)
    out = json.loads(r.stdout)
    assert not out["ok"] and "nope" in out["errors"][0] and "s_axis" in out["bundles"]


VTB_RESET = """import cocotb
from cocotb.triggers import ClockCycles, FallingEdge
from axis_cobs_decode_harness import make


class NoOutput:   # SPEC을 '출력 없음'으로 읽은 Verifier: 원본은 출력을 내므로 실패
    def on_input(self, stream, txn):
        return []


@cocotb.test()
async def mid_reset(dut):
    h = make(dut, NoOutput(), name="mid_reset")
    h.set_ready("m_axis", [1, 0, 1])
    await h.start()
    for d in (3, 17, 34):
        h.send("s_axis", {"s_axis_tdata": d, "s_axis_tlast": 0, "s_axis_tuser": 0})
    await h.drain()
    h.send("s_axis", {"s_axis_tdata": 90, "s_axis_tlast": 0, "s_axis_tuser": 0})
    await FallingEdge(dut.clk)
    dut.rst.value = 1
    await ClockCycles(dut.clk, 2)
    dut.rst.value = 0
    for d, last in ((2, 0), (0, 0), (2, 0), (51, 0), (0, 1)):
        h.send("s_axis", {"s_axis_tdata": d, "s_axis_tlast": last, "s_axis_tuser": 0}, idle=1)
    await h.drain()
    h.finish()
"""


def test_verifier_on_ref_stimulus_replays_with_refsim(tmp_path):
    """원본에서 실패한 Verifier 테스트의 자극(하니스 기록)을 refsim에 넣으면 원본이 그 테스트에서 낸 출력이 그대로 나온다.
    도중 리셋과 리셋 사이클의 핸드셰이크, 출력 backpressure 포함 (R6 5회차에서 SPEC이 같은 오류를 세 번 놓친 것)."""
    blk = "axis_cobs_decode"
    r1 = _golden_r1(tmp_path / "r1", blk)
    top = json.loads((G / blk / "ir" / f"{blk}.ir.json").read_text())
    tb = tmp_path / "vtb" / "test_mid_reset.py"
    tb.parent.mkdir()
    tb.write_text(VTB_RESET)
    res = r6.verifier_on_ref([str(tb)], top, r1, tmp_path / "vref")
    assert not res[0]["ok"] and res[0]["findings"][0]["test"] == "mid_reset"
    stim_path = res[0]["findings"][0]["stim"]
    stim = json.loads(open(stim_path).read())
    assert 1 in stim["reset"]["sys"] and 0 in stim["ready"]["m_axis"]
    seen = json.loads(next((tmp_path / "vref" / "sim_v0" / "tb_out").glob("*/mid_reset.result.json")).read_text())["outputs"]["m_axis"]
    work = tmp_path / "w"
    porter.make_ref_dir(r1, work / "ref")
    (work / "m.ir.json").write_text(json.dumps(top))
    r = subprocess.run([sys.executable, str(ROOT / "pipeline" / "agents" / "pl.py"), "refsim", "m.ir.json", "--ref", "ref",
                        "--stim", stim_path], cwd=work, capture_output=True, text=True,
                       env={"PL_NO_LOG": "1", "PATH": "/usr/bin:/bin:/opt/homebrew/bin"})
    out = json.loads(r.stdout)
    assert out["ok"], r.stdout + r.stderr
    assert [{k: t[k] for k in seen[0]} for t in out["outputs"]["m_axis"]] == seen and len(seen) >= 3

def test_port_runner_flow_with_fake_agents(tmp_path, monkeypatch):
    """start → R2 대기 → answer → resume: Porter(첫 시도 틀림 → R4 차이 → 고치기) → SpecWriter → R6(1차 차이 → 판정 spec_gap
    → SPEC 고치기 → 2차 같음) → 묶음. R6 첫 재생성은 사람 결정을 기다리다 멈추고(waiting_regen), resume에서 이어 간다."""
    blk = "axis_cobs_decode"
    cfg = AgentConfig("fake", "Test", "fake", [sys.executable, str(ROOT / "tests" / "fake_port_agent.py")], "m",
                      env={"FAKE_PORTED": str(G / blk / "ir"), "FAKE_SPEC_BASE": str(G / blk / "block_spec.md"),
                           "FAKE_BAD_FIRST": "m_axis_tuser <= m_axis_tuser_int;=>m_axis_tuser <= !m_axis_tuser_int;"}, timeout_s=120)
    roles = Roles({"porter": cfg, "spec_writer": cfg, "arbiter": cfg}, {}, {"agent_retries": 0, "porter_fix": 2, "spec_revise": 2})
    good = json.loads((G / blk / "ir" / f"{blk}.ir.json").read_text())
    bad = json.loads(json.dumps(good).replace("m_axis_tuser <= m_axis_tuser_int;", "m_axis_tuser <= !m_axis_tuser_int;", 1))
    calls = []

    def regen(spec, regen_dir):
        calls.append(spec.read_text())
        if len(calls) == 1:
            return "waiting_approval", {}, [], 3
        return "pass", {blk: bad if len(calls) == 2 else good}, [], 0

    run_dir = tmp_path / "run"
    pr = PortRun(run_dir)
    st = pr.start(Inputs([G / blk / "upstream" / "rtl" / f"{blk}.v"], blk))
    assert st["stage"] == "r2" and st["status"] == "waiting_user"
    qs = {q["id"] for q in st["questions"]}
    assert "freq.sys" in qs and f"proto.{blk}.s_axis" in qs
    out = pr.answer({"freq.sys": "100"})
    assert out["unanswered"]
    ans = {q: ("ok" if q.startswith("review") else "valid_ready") for q in qs if q != "freq.sys"}
    out = PortRun(run_dir).answer(ans)
    assert out["errors"] == [] and out["stage"] == "r3"
    pr = PortRun(run_dir, roles, isolate=False, regen_fn=regen, r4_kw=FAST,
                 r6_kw={"cycles": 800, "seeds": (("steady", 1),), "budget": 8})
    st = pr.resume()
    assert st["status"] == "waiting_regen" and pr.state["r6"]["rounds"][-1]["regen_status"] == "waiting_approval", st
    assert pr.state["agent_runs"] >= 3
    pr = PortRun(run_dir, roles, isolate=False, regen_fn=regen, r4_kw=FAST,
                 r6_kw={"cycles": 800, "seeds": (("steady", 1),), "budget": 8})
    st = pr.resume()
    assert st["status"] == "pass", st
    assert st["modules"][blk] == {"status": "equal", "attempts": 2}
    rounds = pr.state["r6"]["rounds"]
    assert [r["compare"] for r in rounds] == ["differ", "equal"] and rounds[0]["verdict"]["verdict"] == "spec_gap"
    assert [r["round"] for r in rounds] == [0, 1]   # 기다리던 재생성은 같은 회차로 이어 감
    assert "FAKE-99" in calls[2] and "FAKE-99" not in calls[1]
    pkg = run_dir / "package"
    assert (pkg / "spec.md").exists() and (pkg / "ir" / f"{blk}.ir.json").exists() and (pkg / "harness" / f"{blk}_harness.py").exists()
    g = json.loads((pkg / "golden.json").read_text())
    assert g["r4"] == {blk: "equal"} and len(g["r6"]) == 2


def test_port_runner_regen_failed_but_verifier_fails_on_original(tmp_path):
    """재생성이 끝나지 않았어도, 그 Verifier 테스트가 원본에서 실패하면 Arbiter 판정(spec_gap) → SPEC 고치기로 넘긴다."""
    blk = "axis_cobs_decode"
    cfg = AgentConfig("fake", "Test", "fake", [sys.executable, str(ROOT / "tests" / "fake_port_agent.py")], "m",
                      env={"FAKE_PORTED": str(G / blk / "ir"), "FAKE_SPEC_BASE": str(G / blk / "block_spec.md")}, timeout_s=120)
    roles = Roles({"porter": cfg, "spec_writer": cfg, "arbiter": cfg}, {}, {"agent_retries": 0, "porter_fix": 1, "spec_revise": 2})
    good = json.loads((G / blk / "ir" / f"{blk}.ir.json").read_text())
    tb = tmp_path / "vtb" / f"test_{blk}.py"   # SPEC을 원본과 다르게 읽은 테스트: 원본은 출력을 내므로 실패
    tb.parent.mkdir()
    tb.write_text("import cocotb\n\n"
                  "@cocotb.test()\nasync def never_outputs(dut):\n    assert False, 'SPEC 해석: 출력이 없어야 함'\n")
    calls = []

    def regen(spec, regen_dir):
        calls.append(spec.read_text())
        return ("failed", {}, [str(tb)], 5) if len(calls) == 1 else ("pass", {blk: good}, [], 0)

    run_dir = tmp_path / "run"
    pr = PortRun(run_dir)
    st = pr.start(Inputs([G / blk / "upstream" / "rtl" / f"{blk}.v"], blk))
    PortRun(run_dir).answer({q["id"]: ("100" if q["id"] == "freq.sys" else "ok" if q["id"].startswith("review") else "valid_ready")
                             for q in st["questions"]})
    pr = PortRun(run_dir, roles, isolate=False, regen_fn=regen, r4_kw=FAST, r6_kw={"cycles": 800, "seeds": (("steady", 1),)})
    st = pr.resume()
    assert st["status"] == "pass", st
    rounds = pr.state["r6"]["rounds"]
    assert rounds[0]["regen_status"] == "failed" and not rounds[0]["verifier_on_ref"][0]["ok"]
    assert rounds[0]["verdict"]["verdict"] == "spec_gap" and rounds[1]["compare"] == "equal"
    judge = json.loads((run_dir / "r6" / "judge_0.json").read_text())
    assert judge["regen_status"] == "failed" and judge["verifier_failures_on_original"]
    assert len(calls) == 2 and "FAKE-99" in calls[1]


def test_port_runner_spec_gap_at_limit_then_limit_raised(tmp_path):
    """spec_gap 판정이 고치기 상한에 걸려 멈춘 뒤 상한을 올려 resume하면, 다시 만들기 전에 그 판정으로 SPEC부터 고친다."""
    blk = "axis_cobs_decode"
    cfg = AgentConfig("fake", "Test", "fake", [sys.executable, str(ROOT / "tests" / "fake_port_agent.py")], "m",
                      env={"FAKE_PORTED": str(G / blk / "ir"), "FAKE_SPEC_BASE": str(G / blk / "block_spec.md")}, timeout_s=120)
    good = json.loads((G / blk / "ir" / f"{blk}.ir.json").read_text())
    bad = json.loads(json.dumps(good).replace("m_axis_tuser <= m_axis_tuser_int;", "m_axis_tuser <= !m_axis_tuser_int;", 1))
    calls = []

    def regen(spec, regen_dir):
        calls.append(spec.read_text())
        return "pass", {blk: bad if len(calls) == 1 else good}, [], 0

    run_dir = tmp_path / "run"
    st = PortRun(run_dir).start(Inputs([G / blk / "upstream" / "rtl" / f"{blk}.v"], blk))
    PortRun(run_dir).answer({q["id"]: ("100" if q["id"] == "freq.sys" else "ok" if q["id"].startswith("review") else "valid_ready")
                             for q in st["questions"]})
    kw = dict(isolate=False, regen_fn=regen, r4_kw=FAST, r6_kw={"cycles": 800, "seeds": (("steady", 1),)})
    lim = {"agent_retries": 0, "porter_fix": 1}
    pr = PortRun(run_dir, Roles({"porter": cfg, "spec_writer": cfg, "arbiter": cfg}, {}, {**lim, "spec_revise": 0}), **kw)
    st = pr.resume()
    assert st["status"] == "failed" and any(r["code"] == "R6_SPEC_GAP" for r in st["review"]), st
    pr = PortRun(run_dir, Roles({"porter": cfg, "spec_writer": cfg, "arbiter": cfg}, {}, {**lim, "spec_revise": 1}), **kw)
    st = pr.resume()
    assert st["status"] == "pass", st
    rounds = pr.state["r6"]["rounds"]
    assert [(r["round"], r["spec_version"], r["compare"]) for r in rounds] == [(0, 0, "differ"), (1, 1, "equal")]
    assert len(calls) == 2 and "FAKE-99" in calls[1] and "FAKE-99" not in calls[0]


def test_pipeline_inout_block_top_harness_and_diffsim(tmp_path):
    """일반 파이프라인이 inout Block Top을 다룸: 생성 하니스(패드 모델 뷰)와 패드 래퍼 top으로 E2 시뮬레이션, run_diffsim."""
    from pipeline.scheduler.run import emit_lib, run_diffsim
    from pipeline.tb.gen import harness_source, pad_top
    from pipeline.verify.run import sim
    from pipeline.ir.model import Module

    lib = _lib(FX / "inout" / "ported", ["od_ctrl", "od_top"])
    files, _ = emit_lib(lib, tmp_path / "rtl", STUB_CELLS)
    (tmp_path / "h").mkdir()
    (tmp_path / "h" / "od_top_harness.py").write_text(harness_source(Module(lib["od_top"])))
    pads, top = pad_top(lib["od_top"], tmp_path / "pad")
    assert top == "od_top_pad_top"
    r = sim([*files, *pads], top, "tb_od_top_h", ROOT / "tests" / "sim", tmp_path / "build", harness_dir=tmp_path / "h")
    assert r.ok, [f.message for f in r.findings]
    rec = run_diffsim(files, lib["od_top"], "reset", 1, tmp_path / "diff")
    assert rec is not None and rec["probe_at"] is not None


@pytest.mark.parametrize("name,top,mods", [("comb/cr", "cr_top", ["dec", "pipe", "cr_top"]), ("rsync/rs", "rs_top", ["rs_core", "rs_top"])])
def test_r4_comb_noreset_and_reset_sync(tmp_path, name, top, mods):
    """조합 모듈(가상 클럭 래퍼로 모듈 단위 비교), 리셋 없는 도메인, top의 리셋 동기화기(reset.synchronize)."""
    r1 = _r1(tmp_path, [FX / f"{name}.v"], top)
    assert r1["ok"], r1
    lib = _lib(FX / name.split("/")[0] / "ported", r1["order"])
    res = r4.run_all(r1["order"], lib, r1, tmp_path / "r4", STUB_CELLS, **FAST)
    assert {n: r["status"] for n, r in res.items()} == {m: "equal" for m in mods}
    if name.startswith("comb"):
        bad = copy.deepcopy(lib)
        bad["dec"]["blocks"][0]["body"] = bad["dec"]["blocks"][0]["body"].replace("1'b1", "1'b0")
        assert r4.run_module("dec", bad, r1, tmp_path / "r4b", STUB_CELLS, **FAST)["status"] == "differ"
