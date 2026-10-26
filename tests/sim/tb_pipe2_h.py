"""번들이 없는 블록의 하니스 도우미: valid 플래그 스트림, 입력 열(drive), 속성 표본의 __reset__."""
import cocotb
from cocotb.triggers import ClockCycles, FallingEdge, Timer
from pipe2_harness import make


class Model:
    streams = set()   # 단독 출력의 기본 비교는 끔. 결과는 flag_output으로만 비교

    def on_input(self, stream, txn):
        return [("res", {"sum": txn["a"] + txn["b"]})]


@cocotb.test()
async def flag_streams_ignore_latency(dut):
    """i_en이 1인 사이클의 a, b가 o_en이 1인 사이클의 sum으로 나오는지 지연(2사이클)을 몰라도 순서대로 비교한다."""
    h = make(dut, Model(), name="flag_streams")
    h.flag_input("op", "i_en", ["a", "b"])
    h.flag_output("res", "o_en", ["sum"])
    await h.start()
    h.drive("i_en", [1, 1, 0, 1, 0])
    h.drive("a", [1, 200, 0, 255, 0])
    h.drive("b", [2, 100, 0, 255, 0])
    await h.drain()
    assert h.sb.matched == 3, h.sb.matched
    h.finish()


@cocotb.test()
async def drive_starts_at_cycle_zero(dut):
    """start() 바로 뒤에 넣은 첫 값은 사이클 0의 표본에서 보인다."""
    h = make(dut, None, name="drive_cycle0")
    seen = {}
    h.add_property("a_seq", "sys", lambda s, c: seen.setdefault(c, s["a"]) and None)
    await h.start()
    h.drive("a", [11, 22, 33])
    await h.drain()
    assert [seen[c] for c in range(4)] == [11, 22, 33, 33], seen
    h.finish()


@cocotb.test()
async def mid_cycle_reset_is_flagged(dut):
    """사이클 중간에 걸었다 푼 비동기 리셋도 다음 표본의 __reset__으로 보인다. 시작 리셋은 넣지 않는다."""
    h = make(dut, None, name="mid_reset")
    flagged = []
    h.add_property("rst", "sys", lambda s, c: flagged.append(c) if s["__reset__"] else None)
    await h.start()
    await ClockCycles(dut.clk, 5)
    await FallingEdge(dut.clk)
    await Timer(1, unit="ns")   # 표본 뒤, 다음 상승 에지 전
    dut.rst_n.value = 0
    await Timer(1, unit="ns")
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 3)
    assert len(flagged) == 1 and flagged[0] > 0, flagged
    h.finish()
