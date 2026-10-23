"""axis_async_fifo: 생성된 하니스 + 손으로 쓴 참조 모델 (Verifier 역할). SPEC과 L1만 본다. 두 클럭 도메인."""
import random

import cocotb
from cocotb.triggers import ClockCycles
from axis_async_fifo_harness import make


class Model:
    """AFIFO-01, 02: 같은 순서로 그대로 나온다. 출력 tkeep은 1. 상태 출력은 예측하지 않는다 (속성으로 봄)."""

    streams = {"m_axis"}

    def on_input(self, stream, t):
        return [("m_axis", {"m_axis_tdata": t["s_axis_tdata"], "m_axis_tkeep": 1, "m_axis_tlast": t["s_axis_tlast"],
                            "m_axis_tid": t["s_axis_tid"], "m_axis_tdest": t["s_axis_tdest"], "m_axis_tuser": t["s_axis_tuser"]},
                 ["AFIFO-01", "AFIFO-02"])]


def setup(dut, name, backpressure=None):
    h = make(dut, Model(), name=name)
    h.add_property("no_overflow", "m", lambda s, c: "overflow" if s["m_status_overflow"] == 1 else None, "AFIFO-03")
    h.add_property("paused_no_valid", "m",
                   lambda s, c: "valid while paused" if s["m_pause_ack"] == 1 and s["m_axis_tvalid"] == 1 else None, "AFIFO-05")
    h.cover_protocol("m_axis", "AFIFO-04")
    if backpressure:
        h.set_ready("m_axis", backpressure)
    return h


def send_frames(h, rng, n, idle):
    for _ in range(n):
        length = rng.randint(1, 20)
        for i in range(length):
            h.send("s_axis", {"s_axis_tdata": rng.randrange(256), "s_axis_tkeep": 1, "s_axis_tlast": int(i == length - 1),
                              "s_axis_tid": rng.randrange(256), "s_axis_tdest": rng.randrange(256),
                              "s_axis_tuser": rng.randint(0, 1)}, idle=rng.randint(0, idle))


@cocotb.test()
async def streaming(dut):
    h = setup(dut, "streaming")
    await h.start()
    send_frames(h, random.Random(1), 30, 0)
    await h.drain(extra=20)
    h.finish()


@cocotb.test()
async def idle_and_backpressure(dut):
    h = setup(dut, "idle_and_backpressure", backpressure=[1, 0, 0, 0, 1])
    await h.start()
    send_frames(h, random.Random(2), 30, 3)
    await h.drain(extra=20)
    h.finish()


@cocotb.test()
async def pause_between_frames(dut):
    h = setup(dut, "pause_between_frames")
    await h.start()
    send_frames(h, random.Random(3), 10, 1)
    await ClockCycles(dut.m_clk, 30)
    dut.m_pause_req.value = 1
    await ClockCycles(dut.m_clk, 60)
    dut.m_pause_req.value = 0
    await h.drain(extra=20)
    h.finish()
