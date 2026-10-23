"""axis_adapter (32비트 → 8비트): 생성된 하니스 + 손으로 쓴 참조 모델 (Verifier 역할). SPEC과 L1만 본다."""
import random

import cocotb
from axis_adapter_harness import make

LANES = 4


class Model:
    """ADP-01, 02, 03, 04: 입력 비트의 유효 바이트를 낮은 쪽부터 낸다."""

    def on_input(self, stream, t):
        n = max(i + 1 for i in range(LANES) if (t["s_axis_tkeep"] >> i) & 1)
        out = []
        for i in range(n):
            last = int(t["s_axis_tlast"] == 1 and i == n - 1)
            reqs = ["ADP-01", "ADP-03", "ADP-04"] + (["ADP-02"] if last else [])
            out.append(("m_axis", {"m_axis_tdata": (t["s_axis_tdata"] >> (8 * i)) & 0xFF, "m_axis_tkeep": 1,
                                   "m_axis_tlast": last, "m_axis_tid": t["s_axis_tid"], "m_axis_tdest": t["s_axis_tdest"],
                                   "m_axis_tuser": t["s_axis_tuser"]}, reqs))
        return out


def beats(frame, tid, tdest, tuser):
    out = []
    for k in range(0, len(frame), LANES):
        chunk = frame[k:k + LANES]
        last = k + LANES >= len(frame)
        out.append({"s_axis_tdata": int.from_bytes(bytes(chunk), "little"), "s_axis_tkeep": (1 << len(chunk)) - 1,
                    "s_axis_tlast": int(last), "s_axis_tid": tid, "s_axis_tdest": tdest, "s_axis_tuser": tuser if last else 0})
    return out


async def run(dut, name, nframes, idle=0, backpressure=None, seed=1):
    rng = random.Random(seed)
    h = make(dut, Model(), name=name)
    h.cover_protocol("m_axis", "ADP-05")
    if backpressure:
        h.set_ready("m_axis", backpressure)
    await h.start()
    for f in range(nframes):
        frame = [rng.randrange(256) for _ in range(rng.randint(1, 37))]
        for b in beats(frame, rng.randrange(256), rng.randrange(256), rng.randint(0, 1)):
            h.send("s_axis", b, idle=rng.randint(0, idle))
    await h.drain(extra=10)
    h.finish()


@cocotb.test()
async def frames_streaming(dut):
    await run(dut, "frames_streaming", 40)


@cocotb.test()
async def frames_idle_backpressure(dut):
    await run(dut, "frames_idle_backpressure", 40, idle=3, backpressure=[1, 0, 0, 1, 1], seed=2)
