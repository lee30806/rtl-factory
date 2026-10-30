"""B1 RTLLM 2.0 어댑터 및 내장 파일럿 예제 로더 (BENCHMARK_PLAN B1)."""
from __future__ import annotations

import re
from pathlib import Path

import pyslang

from experiments.bench.common.schema import BenchItem, EvalSpec
from experiments.bench.common.to_spec import CLK_RE

MODULE_RE = re.compile(r"^\s*module\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:#\s*\([\s\S]*?\)\s*)?\([\s\S]*?\)\s*;", re.M)

# BENCHMARK_PLAN B1 파일럿 대표 동기식 블록 1: pulse_detect (RTLLM 2.0 표준 명세·헤더·testbench.v)
PILOT_PULSE_DETECT_DESC = """\
Please act as a professional verilog designer.

Implement a pulse detection module (`pulse_detect`) that detects a single-cycle positive pulse (`0 -> 1 -> 0`) on input `data_in` and asserts `data_out = 1` on the cycle where `data_in` transitions back from `1` to `0`.

Module Name:
    pulse_detect

Input Ports:
    - clk: Clock signal (positive-edge triggered).
    - rst_n: Active-low asynchronous reset signal. When `rst_n == 0`, the module resets to its initial state and `data_out` is `0`.
    - data_in: 1-bit serial input signal sampled on each rising edge of `clk`.

Output Ports:
    - data_out: 1-bit output indicator. In normal operation, when `data_in` follows the sequence `0` -> `1` -> `0` across consecutive rising clock edges, `data_out` becomes `1` in the cycle where `data_in` is `0` (immediately after a single cycle of `1`), and `0` in all other cycles. If `data_in` stays `1` for two or more consecutive cycles (`0 -> 1 -> 1 ... -> 0`), it is NOT a single-cycle pulse and `data_out` must remain `0`. Overlapping sequences such as `0 -> 1 -> 0 -> 1 -> 0` produce two pulses on the two `0` cycles following each `1`.

Module Header:
module pulse_detect(
    input clk,
    input rst_n,
    input data_in,
    output reg data_out
);
"""

PILOT_PULSE_DETECT_HEADER = """\
module pulse_detect(
    input clk,
    input rst_n,
    input data_in,
    output reg data_out
);
endmodule
"""

PILOT_PULSE_DETECT_TB = """\
`timescale 1ns/1ps

module tb_pulse_detect;
    reg clk;
    reg rst_n;
    reg data_in;
    wire data_out;

    pulse_detect uut (
        .clk(clk),
        .rst_n(rst_n),
        .data_in(data_in),
        .data_out(data_out)
    );

    initial begin
        clk = 0;
        forever #5 clk = ~clk;
    end

    // Test vector: 0 -> 1 -> 0 -> 1 -> 1 -> 0 -> 1 -> 0 -> 0 -> 1 -> 0
    reg [0:10] stim   = 11'b01011010010;
    reg [0:10] expect_out = 11'b00100001001;
    integer i;
    integer err_cnt;

    initial begin
        err_cnt = 0;
        rst_n = 0;
        data_in = 0;
        #12;
        if (data_out !== 1'b0) begin
            $display("FAIL at reset: data_out=%b, expected=0", data_out);
            err_cnt = err_cnt + 1;
        end
        rst_n = 1;
        for (i = 0; i < 11; i = i + 1) begin
            @(negedge clk);
            data_in = stim[i];
            @(posedge clk);
            #1;
            if (data_out !== expect_out[i]) begin
                $display("FAIL at step %0d: data_in=%b, data_out=%b, expected=%b", i, data_in, data_out, expect_out[i]);
                err_cnt = err_cnt + 1;
            end
        end

        if (err_cnt == 0) begin
            $display("Your Design Passed");
        end else begin
            $display("Total errors: %0d", err_cnt);
        end
        $finish;
    end
endmodule
"""


def _extract_header_from_desc(desc: str, fallback_file: Path | None = None, fallback_name: str = "") -> tuple[str, str]:
    """design_description.txt 또는 참조 .v 파일의 포트·파라미터 심볼에서 (top 모듈 이름, 표준 ANSI 모듈 헤더 SV)를 뽑는다."""
    m_name = re.search(r"Module\s+name\s*:\s*([A-Za-z_][A-Za-z0-9_]*)", desc, re.I)
    desc_top = m_name.group(1) if m_name else fallback_name
    m = MODULE_RE.search(desc)
    if m:
        top = desc_top or m.group(1)
        hdr = re.sub(rf"^\s*module\s+{re.escape(m.group(1))}\b", f"module {top}", m.group(0), count=1, flags=re.M)
        return top, hdr + "\nendmodule\n"
    if fallback_file and fallback_file.exists():
        text = fallback_file.read_text(encoding="utf-8", errors="replace")
        tree = pyslang.SyntaxTree.fromText(text)
        comp = pyslang.Compilation()
        comp.addSyntaxTree(tree)
        inst = comp.getRoot().topInstances[0] if comp.getRoot().topInstances else None
        if inst is not None:
            top = desc_top or inst.name
            params: list[tuple[str, str]] = []
            for sym in inst.body:
                if sym.kind == pyslang.SymbolKind.Parameter and not sym.isLocalParam:
                    val_str = str(sym.value).strip()
                    # 내부 FSM 상태 상수(비트 리터럴) 제외
                    if "'" in val_str:
                        continue
                    params.append((sym.name, val_str))
            ports: list[str] = []
            for sym in inst.body.portList:
                if sym.kind == pyslang.SymbolKind.Port:
                    d = {"In": "input", "Out": "output", "InOut": "inout"}[str(sym.direction).split(".")[-1]]
                    t = str(sym.type)
                    t = re.sub(r"^(logic|reg|wire|bit)\s*\[", r"logic [", t)
                    if t in ("logic", "reg", "wire", "bit"):
                        t = "logic"
                    syn = sym.syntax
                    if syn is not None:
                        syn_s = str(syn)
                        m_rng = re.search(r"\[[^\]]+\]", syn_s)
                        if m_rng and any(p[0] in m_rng.group(0) for p in params):
                            t = f"logic {m_rng.group(0)}"
                    ports.append(f"    {d} {t} {sym.name}")
            p_clause = ""
            if params:
                p_lines = [f"    parameter int {k} = {v}" for k, v in params]
                p_clause = " #(\n" + ",\n".join(p_lines) + "\n)"
            hdr = f"module {top}{p_clause} (\n" + ",\n".join(ports) + "\n);\nendmodule\n"
            return top, hdr
    raise ValueError("모듈 헤더 선언(module ... (...);)을 찾을 수 없음")


def is_sequential_header(header_sv: str, top: str) -> bool:
    """클럭 입력 포트가 있는 동기식(Sequential) 설계인지 판별한다."""
    tree = pyslang.SyntaxTree.fromText(header_sv if "endmodule" in header_sv else header_sv + "\nendmodule\n")
    comp = pyslang.Compilation()
    comp.addSyntaxTree(tree)
    inst = comp.getRoot().topInstances[0] if comp.getRoot().topInstances else None
    if inst is None:
        return bool(CLK_RE.search(header_sv))
    for p in inst.body.portList:
        if p.kind == pyslang.SymbolKind.Port and p.direction == pyslang.ArgumentDirection.In and CLK_RE.search(p.name):
            return True
    return False


def load_rtllm_dir(dataset_dir: Path, subset: str = "sequential") -> list[BenchItem]:
    """RTLLM 2.0 디렉터리를 순회하며 문제별 design_description.txt와 testbench.v를 BenchItem으로 로드한다."""
    dataset_dir = Path(dataset_dir).resolve()
    items: list[BenchItem] = []
    for desc_path in sorted(dataset_dir.rglob("design_description.txt")):
        prob_dir = desc_path.parent
        tb_path = prob_dir / "testbench.v"
        if not tb_path.exists():
            continue
        desc = desc_path.read_text(encoding="utf-8", errors="replace")
        v_files = [p for p in (*prob_dir.glob("*.v"), *prob_dir.glob("*.sv")) if p.name != "testbench.v"]
        ref_file = v_files[0] if v_files else None
        try:
            top, header_sv = _extract_header_from_desc(desc, ref_file, fallback_name=prob_dir.name)
        except ValueError:
            continue
        seq = is_sequential_header(header_sv, top)
        cat = "sequential" if seq else "combinational"
        if subset == "sequential" and not seq:
            continue
        if subset == "combinational" and seq:
            continue
        items.append(
            BenchItem(
                benchmark="rtllm",
                id=prob_dir.name,
                category=cat,
                top=top,
                prompt_text=desc,
                header_sv=header_sv,
                reference_rtl=[str(ref_file)] if ref_file else [],
                eval=EvalSpec(
                    kind="iverilog",
                    tb_files=[str(tb_path)],
                    pass_pattern="Your Design Passed|PASS",
                ),
            )
        )
    return items


def materialize_builtin_pilot(work_dir: Path) -> BenchItem:
    """외부 데이터셋 폴더가 없을 때도 즉시 실행·검증할 수 있는 RTLLM 2.0 `pulse_detect` 파일럿 예제를 생성한다."""
    prob_dir = Path(work_dir).resolve() / "rtllm_pilot" / "pulse_detect"
    prob_dir.mkdir(parents=True, exist_ok=True)
    desc_path = prob_dir / "design_description.txt"
    tb_path = prob_dir / "testbench.v"
    hdr_path = prob_dir / "pulse_detect_header.sv"
    desc_path.write_text(PILOT_PULSE_DETECT_DESC, encoding="utf-8")
    tb_path.write_text(PILOT_PULSE_DETECT_TB, encoding="utf-8")
    hdr_path.write_text(PILOT_PULSE_DETECT_HEADER, encoding="utf-8")
    return BenchItem(
        benchmark="rtllm",
        id="pulse_detect",
        category="sequential",
        top="pulse_detect",
        prompt_text=PILOT_PULSE_DETECT_DESC,
        header_sv=PILOT_PULSE_DETECT_HEADER,
        eval=EvalSpec(
            kind="iverilog",
            tb_files=[str(tb_path)],
            pass_pattern="Your Design Passed|PASS",
        ),
    )
