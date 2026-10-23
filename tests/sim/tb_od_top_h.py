"""생성된 하니스(od_top_harness)로 inout Block Top(open-drain 패드)을 돌린다 (P11). 하니스는 패드를 sda_pad_o/sda_pad_oe(바깥 장치)와
sda_pad_val(패드 값)로 다룬다."""
import cocotb
from cocotb.triggers import ClockCycles, FallingEdge, ReadOnly
from od_top_harness import make


@cocotb.test()
async def pad_pullup_and_external_drive(dut):
    h = make(dut, None, name="pad_pullup_and_external_drive")
    await h.start()
    await FallingEdge(dut.clk)
    dut.sda_pad_o.value = 0
    dut.sda_pad_oe.value = 1          # 바깥 장치가 0을 구동
    await ClockCycles(dut.clk, 10)
    await ReadOnly()
    assert int(dut.shift.value) == 0 and int(dut.sda_pad_val.value) == 0
    await FallingEdge(dut.clk)
    dut.sda_pad_oe.value = 0          # 바깥 장치 놓음: 블록도 구동하지 않으므로(shift[3]^shift[0] = 0) 풀업으로 1
    await ReadOnly()
    assert int(dut.sda_pad_val.value) == 1
    h.finish()
