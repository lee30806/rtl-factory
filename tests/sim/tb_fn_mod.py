"""fn_mod(function/task, SVA) 시험 TB. 하니스 없이 입력만 넣는다. 판정은 IR의 SVA가 한다 (Verify가 로그에서 실패를 읽음).
- within_assumption: b < 8 (설계 가정 m_b_small을 지킴)
- breaks_assumption: b를 0~255에서 고름 (assume 실패)"""
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge


async def _run(dut, bmax: int, seed: int):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst_n.value = 0
    dut.a.value = 0
    dut.b.value = 0
    await ClockCycles(dut.clk, 3)
    await FallingEdge(dut.clk)
    dut.rst_n.value = 1
    rng = random.Random(seed)
    for _ in range(60):
        dut.a.value = rng.randrange(256)
        dut.b.value = rng.randrange(bmax)
        await FallingEdge(dut.clk)


@cocotb.test()
async def within_assumption(dut):
    await _run(dut, 8, 1)


@cocotb.test()
async def breaks_assumption(dut):
    await _run(dut, 256, 2)
