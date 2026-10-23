"""생성된 하니스(ne_top_harness)로 하강 에지 도메인이 있는 블록을 돌린다.

상승 에지 도메인(clk)은 하강 에지에서 입력을 바꾸고, 하강 에지 도메인(clk_neg)은 상승 에지에서 입력을 바꾼다.
- m_data(clk_neg 출력)는 받은 바이트 ^ 0x5A를 반주기 뒤에 낸다 (값이 바뀔 때마다 트랜잭션).
- n_cnt(clk_neg)는 하강 에지마다 n_in을 더한다. 표본이 하강 에지 도메인의 사이클을 따르면 n_cnt(c) = n_cnt(c-1) + n_in(c-1).
"""
import random

import cocotb
from cocotb.triggers import FallingEdge, RisingEdge
from ne_top_harness import make


class Model:
    """NE-01: 받은 바이트 ^ 0x5A. 리셋 뒤 첫 하강 에지에서 0(리셋값) → 0x5A(보관값 0의 변환)로 한 번 바뀐다."""
    streams = {"m_data"}

    def __init__(self):
        self.first = True

    def on_input(self, stream, txn):
        out = [("m_data", {"m_data": 0x5A}, "NE-01")] if self.first else []
        self.first = False
        return out + [("m_data", {"m_data": txn["s_data"] ^ 0x5A}, "NE-01")]


@cocotb.test()
async def neg_edge_domains(dut):
    h = make(dut, Model(), name="neg_edge_domains")
    prev = {}

    def acc(s, c):
        if s["rst_n"] == 0:
            prev.clear()
            return None
        bad = None
        if prev and s["n_cnt"] != (prev["n_cnt"] + prev["n_in"]) & 0xFF:
            bad = f'n_cnt={s["n_cnt"]}, 앞 사이클 n_cnt={prev["n_cnt"]} n_in={prev["n_in"]}'
        prev.update(n_cnt=s["n_cnt"], n_in=s["n_in"])
        return bad
    h.add_property("acc_on_falling_edge", "clk_neg", acc, "NE-02")
    await h.start()
    rng = random.Random(3)

    async def drive_n_in():
        while True:
            await RisingEdge(dut.clk)   # 하강 에지 도메인의 입력은 상승 에지에서 바꾼다
            dut.n_in.value = rng.randrange(16)
    async def drive_m_ready():   # m은 번들이 아니라(m_data가 하강 에지 도메인) 테스트가 ready를 직접 구동
        pattern = [1, 1, 0, 1, 0, 0, 1]
        for i in range(10**6):
            await FallingEdge(dut.clk)
            dut.m_ready.value = pattern[i % len(pattern)]
    cocotb.start_soon(drive_n_in())
    cocotb.start_soon(drive_m_ready())
    last = 0
    for _ in range(40):
        d = rng.randrange(1, 256)
        while d == last:   # 같은 값이 이어지면 m_data가 바뀌지 않아 트랜잭션이 생기지 않음
            d = rng.randrange(1, 256)
        last = d
        h.send("s", {"s_data": d}, idle=rng.randint(0, 2))
    await h.drain()
    h.finish()
