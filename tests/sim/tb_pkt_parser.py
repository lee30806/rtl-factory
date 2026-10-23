"""방출된 pkt_parser RTL을 SPEC 기반 참조 모델과 사이클 단위로 비교한다 (cocotb)."""
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, FallingEdge, RisingEdge

SYNC = 0xA5


class RefModel:
    """SPEC FR-01~05: sync 바이트 → 헤더(길이 N) → 페이로드 N바이트, 마지막 바이트 다음 사이클에 pkt_valid 1사이클."""

    def __init__(self):
        self.state, self.cnt, self.pkt_valid = "IDLE", 0, 0

    def step(self, valid: int, data: int) -> None:
        nxt_valid = 0
        if self.state == "IDLE":
            if valid and data == SYNC:
                self.state = "HEADER"
        elif self.state == "HEADER":
            if valid:
                self.cnt, self.state = data, "PAYLOAD"
        elif self.state == "PAYLOAD":
            if valid:
                if self.cnt == 1:
                    nxt_valid, self.state = 1, "IDLE"
                self.cnt = (self.cnt - 1) & 0xFF
        self.pkt_valid = nxt_valid


async def run_stream(dut, stream):
    """stream: (valid, data) 목록. 매 사이클 RTL과 모델의 pkt_valid와 s_ready를 비교한다."""
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.s_valid.value, dut.s_data.value, dut.rst_n.value = 0, 0, 0
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    model, pulses = RefModel(), 0
    await FallingEdge(dut.clk)
    for cycle, (v, d) in enumerate(stream + [(0, 0)] * 3):
        # 하강 에지에서 입력을 바꾸고, 상승 에지에서 RTL과 모델이 함께 한 사이클 진행한다.
        dut.s_valid.value, dut.s_data.value = v, d
        await RisingEdge(dut.clk)
        model.step(v, d)
        await FallingEdge(dut.clk)
        assert int(dut.s_ready.value) == 1, f"cycle {cycle}: s_ready는 항상 1 (FR-04)"
        got = int(dut.pkt_valid.value)
        assert got == model.pkt_valid, f"cycle {cycle}: pkt_valid RTL={got} model={model.pkt_valid}"
        pulses += got
    return pulses


def packet(n, gap=0, rng=None):
    out = [(1, SYNC), (1, n)]
    for _ in range(n):
        out += [(0, 0)] * (rng.randint(0, gap) if rng else gap)
        out.append((1, rng.randrange(256) if rng else 0x5A))
    return out


@cocotb.test()
async def single_packet(dut):
    assert await run_stream(dut, packet(3)) == 1


@cocotb.test()
async def noise_then_packets_with_gaps(dut):
    stream = [(1, 0x00), (1, 0x13), (0, SYNC)] + packet(1) + packet(4, gap=2) + [(1, 0x77)]
    assert await run_stream(dut, stream) == 2


@cocotb.test()
async def random_traffic(dut):
    rng = random.Random(1)
    stream = []
    for _ in range(40):
        stream += packet(rng.randint(1, 6), gap=2, rng=rng) if rng.random() < 0.5 else [(rng.randint(0, 1), rng.randrange(256))]
    await run_stream(dut, stream)
