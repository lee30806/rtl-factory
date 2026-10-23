"""생성된 하니스(pp_top_harness)의 패드 체커: 바깥 장치가 0을 구동하는 동안 블록(push-pull)이 1을 구동하면 동시 구동 충돌(T2)."""
import cocotb
from cocotb.triggers import ClockCycles, FallingEdge
from pp_top_harness import make


@cocotb.test()
async def pad_conflict_is_reported(dut):
    h = make(dut, None, name="pad_conflict_is_reported")
    await h.start()
    await FallingEdge(dut.clk)
    dut.pad_pad_o.value = 0
    dut.pad_pad_oe.value = 1          # 블록은 사이클마다 pad_oe를 뒤집으며 pad_o(1, 2, ...)를 구동함
    await ClockCycles(dut.clk, 6)
    try:
        h.finish()
    except AssertionError as e:
        assert "패드 동시 구동 충돌" in str(e), e
    else:
        raise AssertionError("충돌을 보고하지 않음")
