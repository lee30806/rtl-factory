"""생성된 하니스(mc_top_harness)로 다중 클럭 Block Top을 돌린다 (P10). 분주 클럭의 에지 정렬, 추가 동기 리셋, 블록 안 리셋 동기화를 본다."""
import random

import cocotb
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge
from cocotb.utils import get_sim_time
from mc_top_harness import make


class Model:
    """MC-01: 받은 바이트 + 1을 같은 순서로 낸다."""
    streams = {"out"}

    def on_input(self, stream, txn):
        return [("out", {"out_data": (txn["in_data"] + 1) & 0xFF}, "MC-01")]


@cocotb.test()
async def bytes_and_soft_reset(dut):
    h = make(dut, Model(), name="bytes_and_soft_reset")
    h.add_property("ready_low_in_soft_reset", "a",
                   lambda s, c: "a_soft_rst=1인데 in_ready=1" if s["a_soft_rst"] == 1 and s["in_ready"] == 1 else None, "MC-03")
    rises = {"a": [], "a2": []}

    async def watch(name, clk):
        for _ in range(40):
            await RisingEdge(clk)
            rises[name].append(get_sim_time("ps"))
    cocotb.start_soon(watch("a", dut.a_clk))
    cocotb.start_soon(watch("a2", dut.a2_clk))
    await h.start()
    rng = random.Random(1)
    h.set_ready("out", [1, 0, 1, 1, 0])
    for _ in range(30):
        h.send("in", {"in_data": rng.randrange(256)}, idle=rng.randint(0, 2))
    await h.drain()
    await FallingEdge(dut.a_clk)
    dut.a_soft_rst.value = 1
    await ClockCycles(dut.a_clk, 4)
    await FallingEdge(dut.a_clk)
    dut.a_soft_rst.value = 0
    for _ in range(10):
        h.send("in", {"in_data": rng.randrange(256)})
    await h.drain()
    # 분주 클럭(a2 = a / 2)의 상승 에지는 모두 a의 상승 에지와 같은 시각이다
    a2 = [x for x in rises["a2"] if x <= rises["a"][-1]]
    assert len(a2) >= 10 and set(a2) <= set(rises["a"]), rises
    assert rises["a2"][1] - rises["a2"][0] == 2 * (rises["a"][1] - rises["a"][0])
    h.finish()
