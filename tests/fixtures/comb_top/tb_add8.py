"""조합 Block Top(add8)의 Verifier 테스트. 하니스는 가상 클럭(tb_comb_clk) 하나의 도메인 comb로 돈다.
하강 에지에서 입력을 바꾸고, 같은 사이클의 표본에서 출력이 입력의 함수인지 속성(T4)으로 본다."""
import random

import cocotb
from cocotb.triggers import FallingEdge
from add8_harness import make


class Model:
    """출력은 스코어보드로 비교하지 않는다 (입력 번들이 없음). 속성으로 본다."""
    streams = set()

    def on_input(self, stream, txn):
        return []


def add_ok(s, c):
    want = s["a"] + s["b"] + s["cin"]
    if (s["sum"], s["cout"]) != (want & 0xFF, want >> 8):
        return f'a={s["a"]} b={s["b"]} cin={s["cin"]}: sum={s["sum"]} cout={s["cout"]}, 기대 {want & 0xFF}, {want >> 8}'
    return None


async def _run(dut, name, vectors):
    h = make(dut, Model(), name=name)
    h.add_property("sum_is_a_plus_b", "comb", add_ok, ["ADD-01", "ADD-T01"])
    await h.start()
    for a, b, cin in vectors:
        await FallingEdge(dut.tb_comb_clk)
        dut.a.value, dut.b.value, dut.cin.value = a, b, cin
    await h.drain(extra=2)
    h.finish()


@cocotb.test()
async def random_vectors(dut):
    rng = random.Random(1)
    await _run(dut, "random_vectors", [(rng.randrange(256), rng.randrange(256), rng.randrange(2)) for _ in range(200)])


@cocotb.test()
async def corners(dut):
    await _run(dut, "corners", [(1, 2, 0), (0x80, 0x80, 1), (0xFF, 0xFF, 1), (0, 0, 0), (0xFF, 0, 1)])
