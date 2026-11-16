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
