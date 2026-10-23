"""생성된 하니스(pkt_parser_harness)에 손으로 쓴 참조 모델을 붙인 테스트. Verifier가 쓸 테스트의 형태다.

Verifier는 SPEC과 L1(하니스)만 본다. 내부 신호 이름은 이 파일에 나오지 않는다.
"""
import random

import cocotb
from pkt_parser_harness import make

SYNC = 0xA5


class Model:
    """SPEC FR-01~03: sync 바이트 → 길이 N → 페이로드 N바이트. 마지막 바이트를 받으면 pkt_valid 한 번."""

    def __init__(self):
        self.state, self.left = "IDLE", 0

    def on_input(self, stream, txn):
        b = txn["s_data"]
        if self.state == "IDLE":
            if b == SYNC:
                self.state = "HEADER"
        elif self.state == "HEADER":
            self.state, self.left = "PAYLOAD", b
        elif self.state == "PAYLOAD":
            self.left = (self.left - 1) & 0xFF
            if self.left == 0:
                self.state = "IDLE"
                return [("pkt_valid", {"pkt_valid": 1}, "FR-03")]
        return []


def properties(h):
    """T4. FR-04: s_ready는 항상 1. FR-05: pkt_valid는 한 사이클 펄스."""
    last = {"pv": 0}

    def ready_always(s, c):
        return None if s["s_ready"] == 1 else f"s_ready={s['s_ready']}"

    def pulse(s, c):
        bad = last["pv"] == 1 and s["pkt_valid"] == 1
        last["pv"] = s["pkt_valid"]
        return "pkt_valid가 두 사이클 연속 1" if bad else None

    h.add_property("ready_always", "sys", ready_always, "FR-04")
    h.add_property("pkt_valid_pulse", "sys", pulse, "FR-05")
    # 표본에는 리셋 포트도 있다 (표본은 리셋을 푼 사이클부터 읽으므로 이 테스트에서는 늘 비활성)
    h.add_property("reset_in_sample", "sys", lambda s, c: None if s.get("rst_n") == 1 else f"rst_n={s.get('rst_n')}")


def packet(n, rng=None, gap=0):
    out = [(SYNC, 0), (n, 0)]
    for _ in range(n):
        out.append((rng.randrange(256) if rng else 0x5A, rng.randint(0, gap) if rng else gap))
    return out


async def run(dut, name, items):
    h = make(dut, Model(), name=name)
    properties(h)
    await h.start()
    for data, idle in items:
        h.send("s", {"s_data": data}, idle=idle)
    await h.drain()
    h.finish()


@cocotb.test()
async def single_packet(dut):
    await run(dut, "single_packet", packet(3))


@cocotb.test()
async def noise_and_gaps(dut):
    await run(dut, "noise_and_gaps", [(0x00, 0), (0x13, 1)] + packet(1) + packet(4, gap=2) + [(0x77, 0)])


@cocotb.test()
async def properties_only(dut):
    """참조 모델 없이 속성만 보는 테스트. 출력은 스코어보드로 가지 않는다."""
    h = make(dut, None, name="properties_only")
    properties(h)
    await h.start()
    for data, idle in packet(2):
        h.send("s", {"s_data": data}, idle=idle)
    await h.drain()
    h.finish()


@cocotb.test()
async def cocotb_seeded_stimulus(dut):
    """모듈 수준 random은 cocotb 시드로 정해진다. T1 비교가 이것을 초기값 차이로 오인하면 안 된다."""
    items = []
    for _ in range(10):
        items += packet(random.randint(1, 4), random, gap=1)
    await run(dut, "cocotb_seeded_stimulus", items)


@cocotb.test()
async def random_traffic(dut):
    rng = random.Random(7)
    items = []
    for _ in range(30):
        items += packet(rng.randint(1, 6), rng, gap=2) if rng.random() < 0.6 else [(rng.randrange(256), rng.randint(0, 2))]
    await run(dut, "random_traffic", items)
