"""테스트 도중 리셋: 하니스가 그 도메인 입력 큐에 남은 전송을 버리는지 본다."""
import cocotb
from cocotb.triggers import ClockCycles
from pkt_parser_harness import make


class Count:
    def __init__(self):
        self.n = 0

    def on_input(self, stream, txn):
        self.n += 1
        return []


@cocotb.test()
async def reset_flushes_inputs(dut):
    m = Count()
    h = make(dut, m, name="reset_flushes_inputs")
    await h.start()
    for i in range(40):
        h.send("s", {"s_data": i}, idle=1)
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 2)
    dut.rst_n.value = 1
    accepted = m.n
    await h.drain()
    assert 0 < accepted < 40 and m.n == accepted, (accepted, m.n)   # 리셋 뒤에는 더 들어가지 않음
    h.send("s", {"s_data": 7})   # 리셋을 푼 뒤 다시 넣은 것은 들어감
    await h.drain()
    assert m.n == accepted + 1
    h.finish()
