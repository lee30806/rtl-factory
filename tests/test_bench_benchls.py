"""B5 RTL-BenchLS 왕복: 레코드 로더, R2 무인 응답, Yosys 등가 채점, 요약 (에이전트 호출 없음)."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.bench.adapters.rtl_benchls import gold_keys, load_benchls  # noqa: E402
from experiments.bench.common.lec import yosys_lec  # noqa: E402
from experiments.bench.common.metrics import write_summary  # noqa: E402
from experiments.bench.common.roundtrip import auto_answers  # noqa: E402
from experiments.bench.common.schema import BenchResult  # noqa: E402

needs_yosys = pytest.mark.skipif(not shutil.which("yosys"), reason="yosys 없음")

GOLD = """module c(input clk, input rst, input en, output reg [3:0] q, output z);
  always @(posedge clk) if (rst) q <= 0; else if (en) q <= q + 1;
  assign z = (q == 4'd9);
endmodule
"""
RENAMED = """module c(input logic clk, input logic rst, input logic en, output logic [3:0] q, output logic z);
  logic [3:0] cnt_r;
  always_ff @(posedge clk) begin if (rst) cnt_r <= '0; else if (en) cnt_r <= cnt_r + 4'd1; end
  assign q = cnt_r; assign z = cnt_r == 4'd9;
endmodule
"""
# 13에서 0으로 돌아감: 리셋 뒤 14사이클이 지나야 원본(16에서 돌아감)과 달라진다
WRAP13 = RENAMED.replace("cnt_r <= cnt_r + 4'd1", "cnt_r <= (cnt_r == 4'd12) ? 4'd0 : cnt_r + 4'd1")
RESETS = [{"port": "rst", "active": "high"}]


def _w(p: Path, text: str) -> list[Path]:
    p.write_text(text, encoding="utf-8")
    return [p]


def test_gold_keys_prefers_combined_then_top_then_spec_side():
    assert gold_keys({"design_name": "a", "files": {"a.v": "", "a_combined.v": "", "lib.v": ""}}) == ["a_combined.v"]
    assert gold_keys({"design_name": "a", "files": {"a.v": "", "sec_a.tcl": ""}}) == ["a.v"]
    assert gold_keys({"design_name": "rtl", "files": {"m.v0.v": "", "m.v1.v": ""}}) == ["m.v0.v"]


def test_load_benchls_writes_gold_and_uses_lec(tmp_path: Path):
    rec = {"task_id": "t1", "design_name": "c", "top_module": "c", "design_type": "sequential", "files": {"c.v": GOLD}}
    data = tmp_path / "s.jsonl"
    data.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    [it] = load_benchls(data, tmp_path / "gold")
    assert it.eval.kind == "yosys_lec" and it.category == "sequential"
    assert Path(it.reference_rtl[0]).read_text(encoding="utf-8") == GOLD


def test_auto_answers_keep_original_behavior():
    req = {"items": [
        {"id": "freq.sys", "kind": "freq", "answer": None},
        {"id": "rel.a.b", "kind": "relation", "answer": None},
        {"id": "proto.m.s", "kind": "protocol", "answer": None},
        {"id": "review.1", "kind": "review", "options": ["ok", "stop"], "answer": None},
        {"id": "review.2", "kind": "review", "port": "x", "options": ["sys", "async", "stop"], "answer": None},
        {"id": "review.3", "kind": "review", "options": ["ok", "stop"], "answer": "stop"},
    ]}
    assert auto_answers(req) == {"freq.sys": "100", "rel.a.b": "async", "proto.m.s": "none", "review.1": "ok", "review.2": "sys"}


@needs_yosys
def test_lec_proves_renamed_state(tmp_path: Path):
    r = yosys_lec(_w(tmp_path / "g.v", GOLD), _w(tmp_path / "r.sv", RENAMED), "c", tmp_path / "o", RESETS)
    assert r["ok"] and not r["bounded"]


@needs_yosys
def test_lec_bmc_past_twelve_cycles_finds_late_difference(tmp_path: Path):
    """예전 12사이클 BMC는 이 차이를 놓쳤다. 첫 사이클 리셋 + 깊이 30이면 반례가 나온다."""
    r = yosys_lec(_w(tmp_path / "g.v", GOLD), _w(tmp_path / "w.sv", WRAP13), "c", tmp_path / "o", RESETS)
    assert not r["ok"] and r["method"] == "bmc" and r["counterexample"]


@needs_yosys
def test_lec_combinational_difference(tmp_path: Path):
    g = _w(tmp_path / "g.v", "module a(input [3:0] x, y, output [4:0] s); assign s = x + y; endmodule\n")
    b = _w(tmp_path / "b.v", "module a(input [3:0] x, y, output [4:0] s); assign s = x - y; endmodule\n")
    assert yosys_lec(g, g, "a", tmp_path / "o1")["ok"]
    r = yosys_lec(g, b, "a", tmp_path / "o2")
    assert not r["ok"] and r["method"] == "comb_sat"


def test_roundtrip_summary_labels(tmp_path: Path):
    ev = {"ok": True, "kind": "yosys_lec", "method": "bmc", "bounded": True, "depth": 30}
    rs = [BenchResult(benchmark="rtl_benchls", id="a", category="sequential", top="a", spec_gate_ok=True, pipeline_status="pass",
                      chosen={"a_core": 0}, candidates={"c0": {"bench_eval": ev}}, bench_pass_chosen=True, bench_pass_any=True,
                      bench_pass_chosen_final=True, bench_pass_any_final=True, round_trip=[{"round": 0}]),
          BenchResult(benchmark="rtl_benchls", id="b", category="combinational", top="b", spec_gate_ok=False, pipeline_status="rejected")]
    s = write_summary(rs, tmp_path)
    md = (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert s["counts"]["bench_pass_chosen"] == 1
    assert "1회 왕복 등가 (`pass@round1`, 선택 후보)**: 1/2" in md and "유한 깊이 BMC로만 확인 1개" in md
    assert "| `a` | sequential | `pass` | 1 | PASS | PASS | - | BMC 30 | PASS |" in md
