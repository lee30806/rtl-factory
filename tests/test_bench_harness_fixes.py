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


def test_clock_reset_from_code_annotations_and_port_lines():
    """클럭·리셋은 쓰임새(감응 목록, 헤더 주석, 포트 설명 첫 마디)로 먼저 찾고 이름은 마지막 수단이다."""
    from experiments.bench.common.to_spec import CLK_RE, RST_RE, clock_reset_evidence
    code = "always @(posedge clk or negedge rst_n) begin : P\n if (rst_n == 1'b0) q <= 0; else q <= d; end"
    ev = clock_reset_evidence(["rst_n", "d"], "", [code])               # 셀의 clk는 최상위 포트가 아니어도 rst_n은 리셋
    assert ev["resets"] == {"rst_n": {"active": "low", "async": True}} and ev["clocks"] == {}
    ev = clock_reset_evidence(["hclk", "hrst"], "", ["always @(posedge hclk) if (hrst) q <= 0;"])
    assert ev["clocks"] == {"hclk": "pos"} and ev["resets"]["hrst"] == {"active": "high", "async": False}
    # 본문 없는 예시 감응 목록은 어느 것이 리셋인지 몰라 쓰지 않는다
    assert clock_reset_evidence(["clk", "rst"], "Use `always @(posedge clk or negedge rst)`). - State", [])["clocks"] == {}
    hdr = "endmodule\n// @clock pclk posedge\n// @reset presetn active-low async\n"
    ev = clock_reset_evidence(["pclk", "presetn"], "", [], hdr)
    assert ev["clocks"] == {"pclk": "pos"} and ev["resets"]["presetn"] == {"active": "low", "async": True}
    prose = ("Inputs:\n    sys_c: System clock.\n    rb: Asynchronous active-low reset.\n"
             "    scl_i: External I2C SCL clock line input.\n    ena: Core enable. Cleared by reset.\n")
    ev = clock_reset_evidence(["sys_c", "rb", "scl_i", "ena"], prose)
    assert set(ev["clocks"]) == {"sys_c"} and set(ev["resets"]) == {"rb"}
    assert all(CLK_RE.search(n) for n in ("pclk", "hclk", "aclk")) and all(RST_RE.search(n) for n in ("presetn", "hresetn"))
    assert not RST_RE.search("preset")                                    # 값 적재 신호와 구분


def test_reset_lesson_matches_final_judgment(tmp_path: Path):
    """헤더 주석이 비동기 리셋을 정하면 '동기로 정했다' 같은 문장 추정 LESSON을 남기지 않는다."""
    from experiments.bench.common.schema import BenchItem
    from experiments.bench.common.to_spec import extract_header_l1
    hdr = ("module t (input pclk, input presetn, input [7:0] d, output reg [7:0] q);\nendmodule\n"
           "// @clock pclk posedge\n// @reset presetn active-low async\n")
    l1, _, notes = extract_header_l1(BenchItem("cvdp", "t", "cid003", "t", "A register.", hdr), tmp_path)
    assert l1["domains"][0]["reset"] == {"port": "presetn", "active": "low", "async": True}
    resets = [x for x in notes["lessons"] if "presetn" in x]
    assert resets and all("비동기" in x and "동기로 정했다" not in x for x in resets)


def test_prompt_doc_structure_reset_extraction_and_domain_refine(tmp_path: Path):
    """완성된 RTL이 없을 때 마크다운 문서 블록·자연어 문장에서 클럭/리셋 구조를 추출하고, SpecWriter의 2절 리셋 속성 보정을 허용한다."""
    from experiments.bench.common.schema import BenchItem
    from experiments.bench.common.to_spec import extract_header_l1, infer_reset_and_clock_traits
    from pipeline.agents import spec_writer
    from pipeline.importer import spec_tables
    from pipeline.spec.l1 import block_top_l1
    from pipeline.spec.parse import load as load_spec

    # 1) RealBench 마크다운 블록 ("**Reset:`RST`**\n- Type: Asynchronous\n- Active Level: High (`1`)")
    rb_crc = (
        "## Interface\n- CLK (input): Clock.\n- RST (input): The reset signal. When asserted high (1), initializes CRC.\n"
        "## Clock and Reset\n**Clock :`CLK`**\n- Type: Synchronous\n\n**Reset:`RST`**\n- Type: Asynchronous\n- Active Level: High (`1`)\n"
    )
    lessons: list[str] = []
    assert infer_reset_and_clock_traits(rb_crc, "CLK", "RST", lessons=lessons) == ("pos", "high", True)
    assert lessons == []

    # 2) 마크다운 강조("**asynchronous** reset method") 및 무관한 포트의 "active low" 오염 방지
    rb_wb = "This module uses a synchronous design and an **asynchronous** reset method. When `wb_rst_i` is asserted, it resets."
    assert infer_reset_and_clock_traits(rb_wb, "wb_clk_i", "wb_rst_i", lessons=lessons) == ("pos", "high", True)
    rb_sd = "Data start bit is active low.\n**Reset Condition (`rst` High)**:\n**Normal Operation (`rst` Low)**:\n"
    assert infer_reset_and_clock_traits(rb_sd, "sd_clk", "rst")[1] == "high"

    # 3) RTLLM traffic_light / calendar / freq_divbyfrac / LFSR 자연어 문장
    tl = "rst_n: Reset signal (active low).\nThe counter is decremented by 1 on every positive edge of the clock or negative edge of the reset signal."
    assert infer_reset_and_clock_traits(tl, "clk", "rst_n", lessons=lessons) == ("pos", "low", True)
    assert infer_reset_and_clock_traits("The active low reset signal rst_n asynchronously initializes the counter.", "clk", "rst_n") == ("pos", "low", True)
    assert lessons == []
    # 동작 서술("클럭 에지에서 rst가 high이면")은 동기 명시가 아니다: 비동기 기본값 + LESSON (ChipVerilog register_file 정답은 비동기)
    assert infer_reset_and_clock_traits("On the rising edge of the clock (clk), if the reset (rst) is high, clear.", "clk", "rst", lessons=lessons) == ("pos", "high", True)
    assert len(lessons) == 1 and "기본값" in lessons[0]
    lessons.clear()

    # 4) 완성된 RTL 코드가 있는 경우(조합 assign 포함) 더미 순차 블록을 주입하지 않고 코드 구조 그대로 반환
    from experiments.bench.common.to_spec import _inject_dummy_seq_if_empty
    comb_rtl = "module comb_m(input clk, input rst, input a, output y);\n  assign y = a;\nendmodule\n"
    (tmp_path / "comb").mkdir(exist_ok=True)
    aug = _inject_dummy_seq_if_empty(comb_rtl, "comb_m", rb_crc, [], {}, {}, tmp_path / "comb")
    assert "always" not in aug.read_text(encoding="utf-8")

    # 5) interface_diff 및 allow_domain_refine: 외부 포트·파라미터는 고정하되 2절 리셋 동기/극성 변경은 허용
    base_spec = ROOT / "tests" / "golden" / "axis_cobs_decode" / "block_spec.md"
    ir = json.loads((ROOT / "tests" / "golden" / "axis_cobs_decode" / "ir" / "axis_cobs_decode.ir.json").read_text())
    head, rest = base_spec.read_text().split("\n## 2.", 1)
    async_md = spec_tables.tables_md(ir).replace("| high | 동기 |", "| high | 비동기 |")
    spec_text = head + "\n" + async_md + rest[rest.index("\n## 5."):].replace("rst=1을 본 다음 사이클부터", "rst=1인 동안")
    p = tmp_path / "refined.md"
    p.write_text(spec_text, encoding="utf-8")
    assert spec_writer.gate(p, ir, allow_domain_refine=False) != []
    assert spec_writer.gate(p, ir, allow_domain_refine=True) == []
    bad_port = tmp_path / "bad_port.md"
    bad_port.write_text(spec_text.replace("| sys | clk |", "| sys | clk2 |", 1), encoding="utf-8")
    assert any(e.startswith("[2~4절]") for e in spec_writer.gate(bad_port, ir, allow_domain_refine=True))


def test_internal_pipeline_bugfixes_and_sv_testbench_mode(tmp_path: Path):
    """1) Verifier 외부 셀(cells/) 가시성, 2) Arbiter 시나리오 ID(SC-01) 허용 및 other_verifiers_passed,
    3) Verifier SV 테스트벤치(tb_lang='sv') 게이트 및 sim_sv E2 실행 검증."""
    import shutil
    from pipeline.agents import arbiter, roles as roles_mod, verifier
    from pipeline.verify.run import sim_sv

    l1 = {
        "module": "crc1",
        "role": "block_top",
        "params": [],
        "domains": [{"name": "sys", "clock": "clk", "edge": "pos", "reset": {"port": "rst", "active": "high", "async": True}}],
        "ports": [
            {"name": "clk", "dir": "input", "type": "logic", "domain": "sys"},
            {"name": "rst", "dir": "input", "type": "logic", "domain": "sys"},
            {"name": "d", "dir": "input", "type": "logic", "domain": "sys"},
            {"name": "q", "dir": "output", "type": "logic", "domain": "sys"},
        ],
    }
    spec = tmp_path / "spec.md"
    spec.write_text(
        "# crc1 SPEC\n\n## 1. 목적과 범위\n1비트 플립플롭.\n\n## 2. 클럭, 리셋, 전력\n"
        "| 도메인 | 클럭 포트 | 에지 | 주파수(MHz) | 리셋 포트 | 극성 | 동기/비동기 | 기준 도메인 | 분주비 |\n|---|---|---|---|---|---|---|---|---|\n"
        "| sys | clk | pos | 100 | rst | high | 비동기 | - | - |\n\n"
        "## 3. 인터페이스\n| 포트 | 방향 | 타입 | 도메인 | 설명 |\n|---|---|---|---|---|\n"
        "| clk | input | logic | sys | 클럭 |\n| rst | input | logic | sys | 리셋 |\n| d | input | logic | sys | 입력 |\n| q | output | logic | sys | 출력 |\n\n"
        "## 4. 파라미터\n없음\n\n## 5. 기능 요구사항\n- FR-01 (필수) rst=1이면 q=0, 아니면 상승 에지에서 q<=d.\n\n"
        "## 6. 타이밍과 성능\n- TIM-01 (필수) 입력 뒤 1사이클에 출력한다.\n\n## 7. 예외와 코너 케이스\n없음\n\n"
        "## 8. 예시 시나리오\n### SC-01 정상: 기본 동작\n| 사이클 | d | q |\n|---|---|---|\n| 1 | 1 | 1 |\n\n"
        "## 9. 구현 제약\n없음\n\n## 10. 열린 질문\n없음\n",
        encoding="utf-8",
    )
    # 1) 외부 제공 셀(cells/data_mem.v)이 Verifier 입력에 포함되는지 확인
    ext_cell = tmp_path / "data_mem.v"
    ext_cell.write_text("module data_mem(input clk); endmodule\n", encoding="utf-8")
    vt_py = verifier.make_task("verifier/v0", l1, spec, tb_lang="cocotb", cells=[ext_cell])
    assert "cells/data_mem.v" in vt_py.inputs and "제공 외부 서브모듈" in vt_py.text

    # 2) Arbiter가 SC-01(시나리오 ID)도 근거 요구사항(reqs)으로 허용하고 test.sv도 입력에 넣는지 확인
    arb_work = tmp_path / "arb"
    (arb_work / "out").mkdir(parents=True, exist_ok=True)
    (arb_work / "out" / "verdict.json").write_text(
        json.dumps({"verdict": "test_wrong", "reqs": ["SC-01"], "reason": "클럭 2번 구동", "proposal": "1클럭만 구동"}),
        encoding="utf-8",
    )
    assert arbiter.check(spec)(arb_work) == []

    # 3) Verifier SV 테스트벤치 모드(tb_lang='sv') 및 sim_sv 실행 검증
    vt_sv = verifier.make_task("verifier/v0", l1, spec, tb_lang="sv", cells=[ext_cell])
    assert vt_sv.outputs == ["out/test_crc1.sv"] and "cells/data_mem.v" in vt_sv.inputs
    if shutil.which("iverilog") and shutil.which("vvp"):
        sv_work = tmp_path / "sv_v0"
        (sv_work / "out").mkdir(parents=True, exist_ok=True)
        tb_sv = sv_work / "out" / "test_crc1.sv"
        tb_sv.write_text(
            "`timescale 1ns/1ps\n"
            "module test_crc1;\n"
            "  logic clk = 0, rst = 1, d = 0, q;\n"
            "  always #5 clk = ~clk;\n"
            "  crc1 dut(.clk(clk), .rst(rst), .d(d), .q(q));\n"
            "  initial begin\n"
            "    #12; if (q !== 1'b0) begin $display(\"TEST FAILED: [req=FR-01] reset q=%b\", q); $finish; end\n"
            "    @(negedge clk); rst = 0; d = 1;\n"
            "    @(posedge clk); #1; if (q !== 1'b1) begin $display(\"TEST FAILED: [req=SC-01] q=%b\", q); $finish; end\n"
            "    $display(\"TEST PASSED\"); $finish;\n"
            "  end\n"
            "endmodule\n",
            encoding="utf-8",
        )
        assert verifier.check(l1, tb_lang="sv")(sv_work) == []

        good_dut = tmp_path / "crc1_good.sv"
        good_dut.write_text(
            "`timescale 1ns/1ps\nmodule crc1(input logic clk, input logic rst, input logic d, output logic q);\n"
            "  always_ff @(posedge clk or posedge rst) if (rst) q <= 1'b0; else q <= d;\nendmodule\n",
            encoding="utf-8",
        )
        bad_dut = tmp_path / "crc1_bad.sv"
        bad_dut.write_text(
            "`timescale 1ns/1ps\nmodule crc1(input logic clk, input logic rst, input logic d, output logic q);\n"
            "  assign q = 1'b0;\nendmodule\n",
            encoding="utf-8",
        )
        r_ok = sim_sv([good_dut], "crc1", tb_sv, tmp_path / "sim_ok")
        assert r_ok.ok and r_ok.findings == []
        r_fail = sim_sv([bad_dut], "crc1", tb_sv, tmp_path / "sim_fail")
        assert not r_fail.ok and r_fail.findings[0].rule == "AssertionError" and "SC-01" in r_fail.findings[0].message


def test_realbench_cell_module_deduplication(tmp_path: Path):
    """RealBench 어댑터 및 runner에서 동일 모듈이 여러 셀 파일(rtl/submodules vs tb/)에 있을 때 중복 제거 검증."""
    from experiments.bench.common.modules import module_names

    f1 = tmp_path / "cell1.v"
    f1.write_text("module shared_fifo(input clk); endmodule\n", encoding="utf-8")
    f2 = tmp_path / "cell2.sv"
    f2.write_text("module shared_fifo(input clk); // duplicate\nendmodule\n", encoding="utf-8")
    f3 = tmp_path / "cell3.v"
    f3.write_text("module unique_cell(input clk); endmodule\n", encoding="utf-8")

    raw_cells = [f1, f2, f3]
    seen_mods: set[str] = set()
    deduped = []
    for f in raw_cells:
        mods = module_names(f.read_text(encoding="utf-8"))
        if mods and all(m in seen_mods for m in mods):
            continue
        seen_mods.update(mods)
        deduped.append(f)

    assert deduped == [f1, f3]
    assert seen_mods == {"shared_fifo", "unique_cell"}



def test_architect_gate_rejects_cell_port_names_not_in_cell(tmp_path: Path):
    """제공 셀과 이름이 같은 하위 모듈 L1이 셀에 없는 포트(도메인 클럭 포함)를 쓰면 Architect 게이트가 실제 포트를 알려 준다
    (RealBench sd_fifo_tx_filler: 셀 sd_tx_fifo의 wclk/rclk를 clk/sd_clk로 적어 컴파일 실패)."""
    from pipeline.agents.architect import cell_port_errors, cell_port_names
    cell = tmp_path / "sd_tx_fifo.v"
    cell.write_text("module sd_tx_fifo(input [31:0] d, input wr, input wclk, output full, input rclk, input rst);\nendmodule\n"
                    "module old_style(a, b);\n input a; output b;\nendmodule\n", encoding="utf-8")
    assert cell_port_names(cell) == {"sd_tx_fifo": ["d", "wr", "wclk", "full", "rclk", "rst"], "old_style": ["a", "b"]}
    ir = {"module": "sd_tx_fifo", "params": [], "role": "module",
          "domains": [{"name": "w", "clock": "clk"}, {"name": "r", "clock": "sd_clk"}],
          "ports": [{"name": "d", "dir": "input", "type": "logic [31:0]", "domain": "w"},
                    {"name": "full", "dir": "output", "type": "logic", "domain": "w"}]}
    errs = cell_port_errors({"sd_tx_fifo": ir}, [cell])
    assert len(errs) == 1 and "['clk', 'sd_clk']" in errs[0] and "wclk" in errs[0]
    ir["domains"] = [{"name": "w", "clock": "wclk"}, {"name": "r", "clock": "rclk"}]
    assert cell_port_errors({"sd_tx_fifo": ir}, [cell]) == []
    assert cell_port_errors({"other": ir}, [cell]) == []


def test_mismatch_trace_summary_and_realbench_hint_formats():
    """신호별 첫 불일치와 전후 창. 기대값의 x 비트는 비교하지 않는다. RealBench TB의 두 가지 불일치 문구를 모두 읽는다."""
    from experiments.bench.adapters.realbench import diagnose
    from experiments.bench.common import mismatch_trace as mt
    rows = [{"cyc": i, "time": str(10 * i + 5), "ins": {"en": "1", "ack": "1" if i == 3 else "0"},
             "exp": {"cyc_o": "0" if i >= 4 else "1", "dat": "x" * 4},
             "act": {"cyc_o": "1", "dat": "0101"}} for i in range(8)]
    text = mt.summarize(rows, "clk", "공식 TB")
    assert "- cyc_o: 사이클 4 (시각 45), 기대 0 실제 1, 불일치 표본 4개" in text and "dat" not in text.split("\n")[1]
    assert "\ncyc | ack en | cyc_o\n" in text and "\n4 | 0 1 | 0/1*" in text and "\n3 | 1 1 | 1/1" in text
    assert mt.summarize(rows[:4], "clk") == ""
    log = ("Hint: Output 'a' has 3 mismatches. First mismatch occurred at time 25.\n"
           "Hint: Output m_wb_cyc_o: 473 errors, first at time 265\nHint: Output `'signal`' has no mismatches.\n"
           "Hint: Total mismatched samples is 476 out of 900 samples\n")
    bad, total = diagnose(log)
    assert [(b["output"], b["mismatches"], b["first_time"]) for b in bad] == [("a", 3, 25), ("m_wb_cyc_o", 473, 265)]
    assert total == (476, 900)


@pytest.mark.skipif(not __import__("shutil").which("iverilog"), reason="iverilog 없음")
def test_equivalence_counterexample_shows_async_reset_difference(tmp_path: Path):
    """등가로만 채점하는 문제의 반례: 정답(비동기 리셋)과 후보(동기 리셋)를 같은 입력으로 돌려 첫 불일치를 찾는다 (register_file)."""
    from experiments.bench.common.schema import BenchItem, EvalSpec
    ref = tmp_path / "des" / "r.v"
    ref.parent.mkdir()
    ref.write_text("module r(input clk, input rst, input [3:0] d, output reg [3:0] q);\n"
                   "always @(posedge clk or posedge rst) if (rst) q <= 0; else q <= d;\nendmodule\n", encoding="utf-8")
    header = "module r(input clk, input rst, input [3:0] d, output [3:0] q);\nendmodule\n"
    item = BenchItem("chipverilog", "r", "standalone", "r", "rst is an active-high reset.", header,
                     reference_rtl=[str(ref)], eval=EvalSpec(kind="chipverilog"))
    cand = "module r(input clk, input rst, input [3:0] d, output reg [3:0] q);\nalways @(posedge clk) if (rst) q <= 0; else q <= d;\nendmodule\n"
    text = cv.counterexample(item, cand, tmp_path / "cex")
    assert "- q: 사이클 0" in text and "기대 0 실제 x" in text
    assert cv.counterexample(item, ref.read_text(), tmp_path / "cex2") == ""


def test_edge_detection_hint_and_micro_timing_rules():
    """에지 검출 또는 펄스 관련 프롬프트에서 표준 동기 1사이클 등록 펄스 힌트가 생성되는지 검증한다."""
    if (ROOT / "experiments").exists():
        from experiments.bench.common.to_spec import edge_detection_hint
    else:
        from src.rtl_factory.bench.common.to_spec import edge_detection_hint

    hint = edge_detection_hint("Design a synchronous edge detector that detects positive and negative edges on an input signal. Assert for one clock cycle.")
    assert hint is not None
    assert "에지 검출 타이밍" in hint
    assert "1사이클 동기 등록 펄스" in hint
    assert "불필요한 2단 쉬프트 지연" in hint

    # 일반 모듈은 힌트가 없음
    assert edge_detection_hint("Design a 32-bit ALU.") is None
