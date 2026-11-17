"""PAPER_EVAL_PLAN 6절 하네스 결함 수정: 자체 검사 TB 판정(D6), 채점 환경 결함 분류(D7), ChipVerilog 원문 입력(D8), 토큰 기록(D9)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.bench.adapters import chipverilog as cv  # noqa: E402
from experiments.bench.common.ext_fix import chipverilog_tb_files, tb_is_self_checking  # noqa: E402
from experiments.bench.common.runner import add_usage, dir_usage, task_usage  # noqa: E402
from pipeline.agents.runner import parse_usage  # noqa: E402

CV_JSONL = ROOT / "build" / "cvdp_datasets" / "cvdp_chipverilog.jsonl"
needs_cv = pytest.mark.skipif(not (CV_JSONL.exists() and (cv.CV_ROOT / "Src").exists()), reason="ChipVerilog 데이터셋 없음")


def test_tb_self_checking_matches_official_rule():
    assert tb_is_self_checking('initial $display("Error! out is incorrect %h", out);')
    assert tb_is_self_checking('$display("MISMATCH at %0t", $time);')
    assert not tb_is_self_checking('initial $display("out = %h", out);')          # 출력만 함
    assert not tb_is_self_checking('// $display("fail")\n/* "error" */ $display("ok");')   # 주석 속 문구는 세지 않음


@needs_cv
def test_chipverilog_self_checking_tbs_are_the_official_twelve(tmp_path: Path):
    items = cv.load_chipverilog(CV_JSONL, tmp_path)
    with_tb = sorted(it.id.rsplit("_", 1)[0].removeprefix("cvdp_chipverilog_") for it in items if chipverilog_tb_files(it))
    assert len(with_tb) == 12
    # 출력만 하는 TB 4개는 판정 근거에서 빠진다
    assert not {"if_stage", "mips_16_core_top", "register_file", "or1200_top"} & set(with_tb)


def test_grader_defect_only_for_tool_verdicts():
    assert "등가 검사 도구" in cv.grader_defect({"status": "equivalence_error", "reason": "No SAT model for $mem_v2"})
    assert cv.grader_defect({"status": "reference_fail"})
    for st in ("pass", "compile_fail", "function_fail", "interface_fail", "elaboration_fail"):
        assert cv.grader_defect({"status": st}) == ""                     # 후보를 고쳐야 하는 실패는 Repair로 간다


def test_scheduler_stops_repair_on_grader_defect():
    """외부 채점 결과의 grader_defect가 E2 항목에 실리고, 모든 실패가 채점 결함이면 Repair 전에 멈추는 분기가 있다."""
    src = (ROOT / "pipeline" / "scheduler" / "run.py").read_text(encoding="utf-8")
    assert '"grader_defect": "" if ext_ok else str(ext_r.get("grader_defect") or "")' in src
    i_stop, i_repair = src.index('self.review("GRADER_DEFECT"'), src.index('if st["repairs"] >= limit:')
    assert i_stop < i_repair


@needs_cv
def test_chipverilog_raw_prompt_is_paper_description(tmp_path: Path):
    raw = {it.id: it for it in cv.load_chipverilog(CV_JSONL, tmp_path, "raw")}
    conv = {it.id: it for it in cv.load_chipverilog(CV_JSONL, tmp_path, "cvdp")}
    assert len(raw) == 64 and raw.keys() == conv.keys()
    fpu = raw["cvdp_chipverilog_fpu_0004"]
    assert fpu.prompt_text == (cv.CV_ROOT / "Src/double_fpu/des/verilog/fpu_double/description.txt").read_text(encoding="utf-8")
    alu = raw["cvdp_chipverilog_alu_0021"]
    assert alu.prompt_text == (cv.CV_ROOT / "Src/mips_16/des/alu/description.txt").read_text(encoding="utf-8")
    assert any(raw[k].prompt_text != conv[k].prompt_text for k in raw)   # 변환본은 추가 안내가 붙어 다르다
    with pytest.raises(ValueError):
        cv.load_chipverilog(CV_JSONL, tmp_path, "other")


def test_parse_usage_from_claude_json():
    out = json.dumps({"type": "result", "result": "done", "total_cost_usd": 0.02,
                      "usage": {"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 100,
                                "cache_creation_input_tokens": 50, "service_tier": "standard"}})
    assert parse_usage(out) == {"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 100,
                                "cache_creation_input_tokens": 50, "cost_usd": 0.02}
    assert parse_usage("plain text reply") == {} and parse_usage("{not json") == {}


def test_usage_sums_task_records(tmp_path: Path):
    rec = {"runs": [{"usage": {"input_tokens": 1, "output_tokens": 2}}, {"attempt": 2}, {"usage": {"input_tokens": 3, "output_tokens": 4}}]}
    assert task_usage(rec) == {"input_tokens": 4, "output_tokens": 6, "runs_with_usage": 2}
    for sub in ("run/tasks", "ext1/run/tasks"):
        d = tmp_path / sub
        d.mkdir(parents=True)
        (d / "author.json").write_text(json.dumps(rec), encoding="utf-8")
    (tmp_path / "run" / "tasks" / "broken.json").write_text("{", encoding="utf-8")
    assert dir_usage(tmp_path) == {"input_tokens": 8, "output_tokens": 12, "runs_with_usage": 4}
    assert add_usage({"cost_usd": 0.1}, {"cost_usd": 0.2}, {}) == {"cost_usd": 0.3}


DESC = """Module m is a byte controller.

module m (
    input             clk,
    input             nReset,
    input      [ 3:0] cmd,      // command
           cmd_ack,  // ack
    input             rst
);

Input ports:
    clk: System clock.
    nReset: Asynchronous active-low reset. Resets state immediately.
    cmd[3:0]: Command.
    rst: Synchronous active-high reset.

Output ports:
    cmd_ack: One-cycle acknowledge.
"""


def test_header_from_description_trusts_port_sections():
    """원문 선언이 ANSI 상속으로 출력 포트를 input [3:0]로 만들어도 Output ports 절을 따른다 (ChipVerilog i2c_master_bit_ctrl)."""
    hdr, fixed = cv.header_from_description(DESC, "m")
    assert "output cmd_ack" in hdr and "input [ 3:0] cmd" in hdr and hdr.index("clk") < hdr.index("rst")
    assert fixed == ["cmd_ack: 선언 input[ 3:0] → 절 output"]
    nonansi = "module n (a, b, y);\nInput ports:\n    a: x\n    b[7:0]: y\nOutput ports:\n    y[7:0]: z\n"
    assert cv.header_from_description(nonansi, "n") == ("module n (\n    input a,\n    input [7:0] b,\n    output [7:0] y\n);\nendmodule\n", [])
    assert cv.header_from_description("no declaration here", "n")[0] == ""


@needs_cv
def test_chipverilog_rebuilds_only_broken_i2c_headers(tmp_path: Path):
    items = cv.load_chipverilog(CV_JSONL, tmp_path)
    fixed = sorted(it.id for it in items if it.eval.extra.get("header_lessons"))
    assert fixed == ["cvdp_chipverilog_i2c_master_bit_ctrl_0013", "cvdp_chipverilog_i2c_master_byte_ctrl_0014",
                     "cvdp_chipverilog_i2c_master_top_0015"]


def test_reset_traits_follow_port_line_and_multibit_is_not_clock(tmp_path: Path):
    from experiments.bench.common.to_spec import infer_reset_and_clock_traits, port_reset_traits
    assert port_reset_traits(DESC, "nReset") == ("low", True)
    assert port_reset_traits(DESC, "rst") == ("high", False)
    assert port_reset_traits(DESC, "clk") == (None, None)
    # 문서 어딘가의 "Asynchronous"가 rst에 붙지 않는다: 포트 설명 줄이 동기라 하면 동기
    assert infer_reset_and_clock_traits(DESC, "clk", "rst")[1:] == ("high", False)


@needs_cv
def test_i2c_domain_reset_is_the_async_one(tmp_path: Path):
    from experiments.bench.common.to_spec import extract_header_l1
    it = next(i for i in cv.load_chipverilog(CV_JSONL, tmp_path / "src") if i.id == "cvdp_chipverilog_i2c_master_bit_ctrl_0013")
    l1, _, notes = extract_header_l1(it, tmp_path / "x")
    assert [(d["clock"], d["reset"]["port"], d["reset"]["active"], d["reset"]["async"]) for d in l1["domains"]] == [("clk", "nReset", "low", True)]
    assert any("리셋이 여럿" in x for x in notes["lessons"])


@needs_cv
def test_fpu_header_extracts_without_rejected_cell(tmp_path: Path):
    from experiments.bench.common.to_spec import extract_header_l1
    it = next(i for i in cv.load_chipverilog(CV_JSONL, tmp_path / "src") if i.id == "cvdp_chipverilog_fpu_0004")
    l1, _, notes = extract_header_l1(it, tmp_path / "x")
    assert len(l1["ports"]) >= 10 and any("셀 없이 헤더를 추출" in x for x in notes["lessons"])


def test_header_directives_go_to_top_file_end_not_grader(tmp_path: Path):
    """헤더의 `define은 채점기가 아니라 rtl-factory 방출 단계가 Block Top 파일 끝에 넣는다 (소스맵 줄 번호 유지)."""
    from experiments.bench.common.runner import header_directives
    from experiments.bench.common.schema import BenchItem
    from pipeline.scheduler.run import with_directives
    it = BenchItem("chipverilog", "x", "standalone", "t", "", "`define W 16\n`ifdef A\nmodule t(input [`W:0] a);\nendmodule\n")
    assert header_directives(it) == "`define W 16"
    body = "module t;\nendmodule\n"
    out = with_directives(body, True, "`define W 16")
    assert out.startswith(body.rstrip("\n")) and out.rstrip().endswith("`define W 16")
    assert with_directives(body, False, "`define W 16") == body and with_directives(body, True, "") == body
    src = (ROOT / "experiments" / "bench" / "adapters" / "chipverilog.py").read_text(encoding="utf-8")
    assert "hdr_defs" not in src                       # 채점기는 후보를 고치지 않는다


# --- 6.2 2차 수정 (E1~E6) ---

def test_mixed_timescale_gets_default_without_waiver(tmp_path: Path):
    """셀에만 `timescale이 있으면 그 값을 timescale 없는 모듈의 기본값으로 줘 TIMESCALEMOD를 없앤다 (경고 예외 없이). E1"""
    import shutil
    from pipeline.verify.run import LINT_ARGS, lint, mixed_timescale
    (tmp_path / "cellx.v").write_text("`timescale 1ns / 100ps\nmodule cellx(input a, output y); assign y = a; endmodule\n")
    (tmp_path / "top.sv").write_text("module top(input a, output y); cellx u(.a(a), .y(y)); endmodule\n")
    files = [tmp_path / "top.sv", tmp_path / "cellx.v"]
    assert mixed_timescale(files) == ("1ns", "100ps") and mixed_timescale(files[:1]) is None
    assert "-Wno-TIMESCALEMOD" not in LINT_ARGS
    if shutil.which("verilator"):
        assert lint(files, "top").ok


@needs_cv
def test_header_gets_cell_includes_when_macros_used(tmp_path: Path):
    """헤더가 매크로를 쓰면 셀의 찾을 수 있는 `include를 헤더 앞에 둔다: 셀 없이 추출해도 같은 포트. E2"""
    from experiments.bench.common.to_spec import extract_header_l1
    it = next(i for i in cv.load_chipverilog(CV_JSONL, tmp_path / "src") if "if_stage" in i.id)
    with_cells = [p["name"] for p in extract_header_l1(it, tmp_path / "a")[0]["ports"]]
    it.cells = []
    assert [p["name"] for p in extract_header_l1(it, tmp_path / "b")[0]["ports"]] == with_cells


def _tree(conns):
    top = {"module": "t", "role": "block_top", "instances": [{"name": "u_a", "module": "a"}, {"name": "u_b", "module": "b"}],
           "connections": conns}
    kids = {m: {"module": m, "role": "module", "ports": [{"name": "i", "dir": "input", "type": "logic"},
                                                          {"name": "o", "dir": "output", "type": "logic"}]} for m in ("a", "b")}
    return top, kids


def test_architect_rejects_instance_with_no_connected_output():
    from pipeline.agents.architect import dangling_instances
    top, kids = _tree([{"from": "self.x", "to": "u_a.i"}, {"from": "u_a.o", "to": "u_b.i"}, {"from": "self.x", "to": "u_b.i"}])
    errs = dangling_instances(top, kids)
    assert len(errs) == 1 and "u_b" in errs[0]                                  # u_b의 결과를 아무도 받지 않음 (E3)
    top["connections"].append({"from": "u_b.o", "to": "self.y"})
    assert dangling_instances(top, kids) == []


def test_stage_budget_must_match_spec_latency(tmp_path: Path):
    """모듈 경계로 단 수를 바꾸지 않는다: 직렬 연결이 있고 SPEC 6절이 고정 지연을 정하면 stage_budget 합 = 지연. E4"""
    from pipeline.agents.architect import CONTRACTS, spec_latency, stage_budget_errors
    spec = tmp_path / "spec.md"
    spec.write_text("# t SPEC\n\n## 6. 타이밍과 성능\n\n- TIM-01 (필수) 입력을 받은 에지부터 3사이클 뒤 출력이 바뀐다.\n", encoding="utf-8")
    assert spec_latency(spec) == 3
    spec.write_text("# t SPEC\n\n## 6. 타이밍과 성능\n\n- TIM-01 (필수) 지연은 구현에 따름(검증하지 않음).\n", encoding="utf-8")
    assert spec_latency(spec) is None
    top, _ = _tree([{"from": "self.x", "to": "u_a.i"}, {"from": "u_a.o", "to": "u_b.i"}, {"from": "u_b.o", "to": "self.y"}])
    (tmp_path / CONTRACTS).parent.mkdir(parents=True, exist_ok=True)
    assert "stage_budget" in stage_budget_errors(top, tmp_path, 3)[0]
    (tmp_path / CONTRACTS).write_text(json.dumps({"contracts": [], "stage_budget": {"u_a": 2, "u_b": 2}}))
    assert "≠" in stage_budget_errors(top, tmp_path, 3)[0]
    (tmp_path / CONTRACTS).write_text(json.dumps({"contracts": [], "stage_budget": {"u_a": 2, "u_b": 1}}))
    assert stage_budget_errors(top, tmp_path, 3) == [] and stage_budget_errors(top, tmp_path, None) == []


def test_latency_hint_is_a_lower_bound_from_stage_annotations():
    """원문이 레지스터에 Stage-N을 붙이면 최소 단 수를 힌트로 주고, 6절이 정하지 않거나 더 작으면 다시 쓰게 한다. 같거나 크면 통과. E5"""
    from experiments.bench.common.to_spec import latency_hint, latency_hint_errors
    prompt = "reg out_0: Stage-0 pipeline copy of the output.\nreg out_1: Stage-1 pipeline copy of the output.\n"
    assert latency_hint(prompt)[0] == 2
    assert latency_hint("source_1, source_2, source_3 feed the pipeline hazard check")[0] is None   # 단 번호 없는 접미만으로는 정하지 않음
    head = "# t SPEC\n\n## 6. 타이밍과 성능\n\n"
    assert latency_hint_errors(head + "- TIM-01 (필수) 지연은 구현에 따름(검증하지 않음).\n", prompt)
    assert latency_hint_errors(head + "- TIM-01 (필수) 입력 뒤 1사이클에 출력한다.\n", prompt)
    assert latency_hint_errors(head + "- TIM-01 (필수) 입력 뒤 3사이클에 출력한다.\n", prompt) == []


def test_summary_counts_grader_defect_items():
    from experiments.bench.common.metrics import summarize
    from experiments.bench.common.schema import BenchResult
    bad = BenchResult("chipverilog", "a", "s", "a", True, candidates={
        "c0": {"bench_eval": {"ok": False, "status": "equivalence_error", "grader_defect": "x"}},
        "c1": {"bench_eval": {"ok": False, "status": "equivalence_timeout", "grader_defect": "y"}}})
    mixed = BenchResult("chipverilog", "b", "s", "b", True, candidates={
        "c0": {"bench_eval": {"ok": False, "status": "equivalence_error", "grader_defect": "x"}},
        "c1": {"bench_eval": {"ok": False, "status": "function_fail", "grader_defect": ""}}})
    assert summarize([bad, mixed])["counts"]["grader_defect_items"] == 1
