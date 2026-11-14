"""평가 데이터셋 4종(RTLLM, ChipVerilog, RealBench, CVDP) 준비 시험: 로더, 채점기, 헤더 작성, 그림 첨부, 구성 매크로 규칙, 결함 제외."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.bench.adapters import cvdp as cvdp_mod, realbench as rb_mod  # noqa: E402
from experiments.bench.common.modules import drop_modules, merge_candidate, modules_in_files  # noqa: E402
from experiments.bench.common.runner import evaluate_rtl, has_external_tb  # noqa: E402
from experiments.bench.common.schema import BenchItem  # noqa: E402
from experiments.bench.common.to_spec import config_macros, ensure_header, extract_header_l1, header_check, make_bench_spec_task  # noqa: E402
from experiments.bench.run_bench import MANIFEST, manifest  # noqa: E402
from pipeline.agents.roles import Roles  # noqa: E402
from pipeline.agents.runner import AgentConfig  # noqa: E402

FAKE = str(ROOT / "tests" / "fake_agent.py")


def _roles(tmp_path: Path, files: dict) -> Roles:
    env = {"FAKE_MODE": "roles", "FAKE_FILES": json.dumps(files), "FAKE_LOG": str(tmp_path / "calls.log")}
    cfg = AgentConfig("fake", "Test", "fake", [sys.executable, FAKE], "m", env=env, timeout_s=120)
    return Roles({r: cfg for r in ("spec_writer", "verifier", "author")}, {}, {"agent_retries": 0})


# ---------------------------------------------------------------- 모듈 텍스트 도우미
def test_merge_candidate_drops_duplicates_and_provided(tmp_path: Path):
    a = tmp_path / "a.sv"
    a.write_text("module top;\n endmodule\nmodule cell;\n endmodule\n", encoding="utf-8")
    b = tmp_path / "b.sv"
    b.write_text("module cell;\n endmodule\n", encoding="utf-8")
    text, dropped = merge_candidate([a, b], {"cell"})
    assert "module top" in text and dropped == ["cell", "cell"]
    assert modules_in_files([a]) == {"top", "cell"}
    assert drop_modules("module x;\n endmodule\n", set()) == ("module x;\n endmodule\n", [])


# ---------------------------------------------------------------- RealBench
_RB_TB = """\
module tb();
  reg clk = 0; always #5 clk = ~clk;
  reg [7:0] a; wire [7:0] b_ref, b_dut;
  integer errors = 0, samples = 0, first = -1;
  ref_inc good(.a(a), .b(b_ref));
  inc dut(.a(a), .b(b_dut));
  initial begin
    a = 0; repeat (20) begin @(posedge clk); a = a + 3; end
    $finish;
  end
  always @(negedge clk) begin
    samples = samples + 1;
    if (b_ref !== b_dut) begin errors = errors + 1; if (first < 0) first = $time; end
  end
  final begin
    if (errors) $display("Hint: Output 'b' has %0d mismatches. First mismatch occurred at time %0d.", errors, first);
    else $display("Hint: Output 'b' has no mismatches.");
    $display("Hint: Total mismatched samples is %0d out of %0d samples", errors, samples);
  end
endmodule
"""


def _rb_fixture(tmp_path: Path) -> tuple[Path, Path]:
    raw = tmp_path / "raw" / "aes" / "inc"
    (raw / "figures").mkdir(parents=True)
    (raw / "inc.md").write_text("**inc design specification**\n\n![](./figures/inc%20mod.png)\n\n| a | input | 8 |\n", encoding="utf-8")
    (raw / "figures" / "inc mod.png").write_bytes(b"\x89PNG")
    rec = {"id": "cvdp_realbench_inc_0001",
           "metadata": {"original_id": "inc", "ip_domain": "aes", "level": "module_level", "top_module": "wrong_name",
                        "header_sv": "module wrong_name(input [7:0] a, output [7:0] b);\nendmodule\n"},
           "harness": {"rtl_target": "rtl/inc.sv", "submodule_files": [], "files": {
               "tb/inc_testbench.sv": _RB_TB,
               "tb/inc_stimulus_gen.sv": "module stimulus_gen; endmodule\n",
               "tb/inc_ref.sv": "module ref_inc(input [7:0] a, output [7:0] b); assign b = a + 1; endmodule\n",
               "tb/lib_cells.v": "module lib_cell(input x, output y); assign y = x; endmodule\n",
               "ref/gold.v": "module inc(input [7:0] a, output [7:0] b); assign b = a + 1; endmodule\n",
               "run_sim.sh": "#!/bin/bash\n", "src/.env": "TOP_MODULE=inc\n"}}}
    conv = tmp_path / "conv.jsonl"
    conv.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    return conv, tmp_path / "raw"


def test_realbench_loader_uses_raw_doc_figures_and_gold_header(tmp_path: Path):
    conv, raw = _rb_fixture(tmp_path)
    items = rb_mod.load_realbench(conv, tmp_path / "src", raw)
    assert len(items) == 1
    it = items[0]
    assert it.benchmark == "realbench" and it.top == "inc" and it.category == "module_level"
    assert it.prompt_text.startswith("**inc design specification**")
    assert [Path(f).name for f in it.attachments] == ["inc mod.png"]
    assert it.header_sv.endswith("ref/gold.v")           # 변환본 헤더의 모듈 이름이 틀리면 정답 포트로 헤더를 만든다
    assert {Path(p).name for p in it.reference_rtl} == {"inc_ref.sv", "gold.v"}
    assert [Path(p).name for p in it.eval.tb_files] == ["inc_testbench.sv", "inc_stimulus_gen.sv"]
    assert [Path(c).name for c in it.cells] == ["lib_cells.v"]
    assert has_external_tb(it)
    top_l1, _, notes = extract_header_l1(it, tmp_path / "l1")
    assert [p["name"] for p in top_l1["ports"]] == ["a", "b"] and notes["figures"] == ["inc mod.png"]
    task = make_bench_spec_task(it, "", "module inc; endmodule", notes)
    assert "figures/inc mod.png" in task.inputs and not any("gold" in k or "_ref" in k for k in task.inputs)


@pytest.mark.skipif(not shutil.which("verilator"), reason="verilator 필요")
def test_realbench_evaluate_pass_and_mismatch(tmp_path: Path):
    conv, raw = _rb_fixture(tmp_path)
    it = rb_mod.load_realbench(conv, tmp_path / "src", raw)[0]
    good = tmp_path / "good.sv"
    good.write_text("module inc(input [7:0] a, output [7:0] b); assign b = a + 8'd1; endmodule\nmodule lib_cell(input x, output y); assign y = x; endmodule\n")
    r = evaluate_rtl(it, [good], tmp_path / "eval_good")
    assert r["ok"] and r["stage"] == "sim" and r["total"][0] == 0 and r["deduped_modules"] == ["lib_cell"]
    bad = tmp_path / "bad.sv"
    bad.write_text("module inc(input [7:0] a, output [7:0] b); assign b = a + 8'd2; endmodule\n")
    r = evaluate_rtl(it, [bad], tmp_path / "eval_bad")
    assert not r["ok"] and r["mismatches"][0]["output"] == "b" and "틀린 출력: b" in r["diagnosis"]


# ---------------------------------------------------------------- CVDP
_COCOTB_TEST = """\
import cocotb
from cocotb.triggers import Timer

@cocotb.test()
async def test_inc(dut):
    for a in (0, 1, 7, 200):
        dut.a.value = a
        await Timer(1, "ns")
        assert dut.b.value.to_unsigned() == (a + 1) & 0xff, f"a={a}"
"""
_RUNNER = """\
import os
from cocotb_tools.runner import get_runner

def test_runner():
    r = get_runner(os.getenv("SIM", "icarus"))
    r.build(sources=os.getenv("VERILOG_SOURCES").split(), hdl_toplevel=os.getenv("TOPLEVEL"), always=True, timescale=("1ns", "1ps"))
    r.test(hdl_toplevel=os.getenv("TOPLEVEL"), test_module=os.getenv("MODULE"))
"""


def _cvdp_fixture(tmp_path: Path) -> Path:
    env = "SIM = icarus\nTOPLEVEL_LANG = verilog\nVERILOG_SOURCES = /code/rtl/inc.sv /code/rtl/mycell.sv\nTOPLEVEL = inc\nMODULE = test_inc\nPYTHONPATH = /src\n"
    nonagentic = {"id": "cvdp_copilot_inc_0001", "categories": ["cid005", "easy"],
                  "input": {"prompt": "Design module `inc` that adds one to `a` (8-bit) producing `b` (8-bit). Use `mycell`.",
                            "context": {"rtl/mycell.sv": "module mycell(input x, output y); assign y = x; endmodule\n",
                                        "docs/spec.md": "# inc\nb = a + 1\n"}},
                  "output": {"context": {"rtl/inc.sv": ""}},
                  "harness": {"files": {"src/.env": env, "src/test_runner.py": _RUNNER, "src/test_inc.py": _COCOTB_TEST,
                                        "docker-compose.yml": "services: {}\n"}}}
    agentic = {"id": "cvdp_agentic_inc_0002", "categories": ["cid003", "medium"],
               "prompt": "Implement `module inc(input [7:0] a, output [7:0] b);` at /code/rtl/inc.sv.",
               "context": {}, "patch": {"rtl/inc.sv": ""},
               "harness": {"src/.env": env.replace(" /code/rtl/mycell.sv", ""), "src/test_runner.py": _RUNNER, "src/test_inc.py": _COCOTB_TEST}}
    no_top = {"id": "cvdp_agentic_notop_0003", "categories": ["cid003", "easy"], "prompt": "x", "context": {}, "patch": {},
              "harness": {"src/test_runner.py": _RUNNER}}
    old = {"id": "cvdp_copilot_old_0009", "categories": ["cid003", "easy"], "input": {"prompt": "old"}, "output": {"context": {}},
           "harness": {"files": {"src/.env": "TOPLEVEL = old\n"}}}
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "cvdp_v1.1.0_nonagentic_code_generation_no_commercial.jsonl").write_text(json.dumps(nonagentic) + "\n", encoding="utf-8")
    (raw / "cvdp_v1.1.0_agentic_code_generation_no_commercial.jsonl").write_text(json.dumps(agentic) + "\n" + json.dumps(no_top) + "\n", encoding="utf-8")
    (raw / "cvdp_v1.0.3_nonagentic_code_generation_no_commercial.jsonl").write_text(json.dumps(old) + "\n", encoding="utf-8")
    return raw


def test_cvdp_loader_both_shapes_newest_version_only(tmp_path: Path, capsys):
    raw = _cvdp_fixture(tmp_path)
    items = cvdp_mod.load_cvdp(raw, tmp_path / "src")
    assert [it.id for it in items] == ["cvdp_agentic_inc_0002", "cvdp_copilot_inc_0001"]   # 예전 버전 파일과 TOPLEVEL 없는 레코드는 뺀다
    assert "TOPLEVEL이 없음" in capsys.readouterr().out
    ag, na = items
    assert ag.header_sv.startswith("module inc(") and ag.category == "cid003" and ag.eval.extra["track"] == "agentic"
    assert na.header_sv == "" and na.category == "cid005" and [Path(c).name for c in na.cells] == ["mycell.sv"]
    assert "## 제공 문서 `docs/spec.md`" in na.prompt_text and "b = a + 1" in na.prompt_text
    assert [Path(p).name for p in na.eval.tb_files] == ["test_inc.py", "test_runner.py"]
    assert has_external_tb(na) and na.eval.extra["env"]["TOPLEVEL"] == "inc"


@pytest.mark.skipif(not shutil.which("iverilog"), reason="iverilog 필요")
def test_cvdp_evaluate_runs_cocotb_natively(tmp_path: Path):
    pytest.importorskip("cocotb_tools.runner")
    raw = _cvdp_fixture(tmp_path)
    na = cvdp_mod.load_cvdp(raw, tmp_path / "src")[1]
    good = tmp_path / "good.sv"
    good.write_text("module inc(input [7:0] a, output [7:0] b); assign b = a + 8'd1; endmodule\nmodule mycell(input x, output y); assign y = x; endmodule\n")
    r = evaluate_rtl(na, [good], tmp_path / "eval_good")
    assert r["ok"] and r["stage"] == "sim" and r["deduped_modules"] == ["mycell"]
    bad = tmp_path / "bad.sv"
    bad.write_text("module inc(input [7:0] a, output [7:0] b); assign b = a; endmodule\n")
    r = evaluate_rtl(na, [bad], tmp_path / "eval_bad")
    assert not r["ok"] and r["stage"] == "sim" and r["failed_tests"] and "AssertionError" in r["log_tail"]
    broken = tmp_path / "broken.sv"
    broken.write_text("module inc(input [7:0] a, output [7:0] b); assign b = ; endmodule\n")
    assert evaluate_rtl(na, [broken], tmp_path / "eval_broken")["stage"] == "compile"


def test_ensure_header_writes_header_once_with_agent(tmp_path: Path):
    raw = _cvdp_fixture(tmp_path)
    na = cvdp_mod.load_cvdp(raw, tmp_path / "src")[1]
    files = {"HeaderWriter": {"out/header.sv": "module inc(input [7:0] a, output [7:0] b);\nendmodule\n"}}
    res = ensure_header(na, tmp_path / "spec", _roles(tmp_path, files), isolate=False)
    assert res is not None and res.ok and na.header_sv.endswith("header_writer/header.sv")
    assert ensure_header(na, tmp_path / "spec", _roles(tmp_path, {}), isolate=False) is None   # 두 번째는 저장된 헤더를 쓴다
    top_l1, _, _ = extract_header_l1(na, tmp_path / "l1")
    assert [p["name"] for p in top_l1["ports"]] == ["a", "b"]
    # 작업 디렉터리에는 프롬프트와 셀만 있고 테스트(src/)는 없다
    work = tmp_path / "spec" / "header_writer" / "agent_work"
    assert (work / "prompt.txt").exists() and (work / "cells" / "mycell.sv").exists() and not (work / "src").exists()


def test_ensure_header_rejects_bad_header(tmp_path: Path):
    raw = _cvdp_fixture(tmp_path)
    na = cvdp_mod.load_cvdp(raw, tmp_path / "src")[1]
    files = {"HeaderWriter": {"out/header.sv": "module other(input a);\nassign b = a;\nendmodule\n"}}
    with pytest.raises(RuntimeError, match="헤더 작성 실패"):
        ensure_header(na, tmp_path / "spec", _roles(tmp_path, files), isolate=False)
    w = tmp_path / "hc" / "out"
    w.mkdir(parents=True)
    (w / "header.sv").write_text("module inc #(parameter W = 8)(input [W-1:0] a, output reg [W-1:0] b);\nendmodule\n")
    assert header_check("inc")(tmp_path / "hc") == []


# ---------------------------------------------------------------- 구성 매크로 규칙 (ChipVerilog or1200_sb 유형)
def test_config_macros_follow_provided_includes(tmp_path: Path):
    inc = tmp_path / "include"
    inc.mkdir()
    (inc / "defs.v").write_text("`define OR1200_DU_IMPLEMENTED\n//`define OR1200_SB_IMPLEMENTED\n`ifdef OR1200_NO_BURSTS\n`endif\n", encoding="utf-8")
    prompt = ("When OR1200_SB_IMPLEMENTED is enabled the FIFO buffers stores. When OR1200_SB_IMPLEMENTED is not defined it is a pass-through. "
              "OR1200_DU_IMPLEMENTED enables the debug unit. Bursts follow OR1200_NO_BURSTS. The ACK_WIDTH constant is 4. "
              "Valid ports exist only when VALID_FLAG is defined.")
    cm = config_macros(prompt, [inc], {}, "module x; endmodule")
    assert cm["undefined"] == ["OR1200_SB_IMPLEMENTED", "OR1200_NO_BURSTS", "VALID_FLAG"] and cm["defined"] == ["OR1200_DU_IMPLEMENTED"]
    assert config_macros(prompt, [inc], {"OR1200_SB_IMPLEMENTED": ""}, "")["undefined"] == ["OR1200_NO_BURSTS", "VALID_FLAG"]
    item = BenchItem(benchmark="chipverilog", id="sb", category="standalone", top="sb", prompt_text=prompt,
                     header_sv="`include \"defs.v\"\nmodule sb(input clk, input rst, input [1:0] a, output [1:0] b);\nendmodule\n", incdirs=[str(inc)])
    _, _, notes = extract_header_l1(item, tmp_path / "l1")
    assert notes["config_macros"]["undefined"][0] == "OR1200_SB_IMPLEMENTED"
    assert any(l.startswith("구성 매크로: ") and "OR1200_SB_IMPLEMENTED" in l for l in notes["lessons"])


# ---------------------------------------------------------------- 데이터셋 명세와 결함 제외
def test_manifest_lists_four_benchmarks_and_defects():
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert {"rtllm", "chipverilog", "realbench", "cvdp"} <= set(m) and "verireason" not in {k.lower() for k in m}
    assert manifest("chipverilog")["exclude"]["cvdp_chipverilog_rotator_0002"]
    assert set(manifest("cvdp")["cids"]) == {"cid003", "cid005"} and manifest("nope") == {}
    assert len(manifest("chipverilog")["flagged"]["stub_graded"]) == 15
