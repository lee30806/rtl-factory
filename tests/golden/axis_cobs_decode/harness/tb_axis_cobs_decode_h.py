"""axis_cobs_decode: 생성된 하니스 + 손으로 쓴 참조 모델 (Verifier 역할). SPEC(spec.md)과 L1만 본다."""
import itertools
import random

import cocotb
from axis_cobs_decode_harness import make


def cobs_encode(block):
    enc, seg, code, new_data = bytearray(), bytearray(), 1, True
    for b in block:
        if b == 0:
            enc.append(code); enc.extend(seg); code, seg, new_data = 1, bytearray(), True
        else:
            code += 1; seg.append(b); new_data = True
            if code == 255:
                enc.append(code); enc.extend(seg); code, seg, new_data = 1, bytearray(), False
    if new_data:
        enc.append(code); enc.extend(seg)
    return bytes(enc)


def cobs_decode(block):
    dec, i = bytearray(), 0
    while i < len(block):
        code = block[i]; i += 1
        dec.extend(block[i:i + code - 1]); i += code - 1
        if code < 255 and i < len(block):
            dec.append(0)
    return bytes(dec)


class Model:
    """COBS-01, 02, 03: 프레임 끝(tlast)에서 디코딩해 출력 바이트를 기대한다."""

    def __init__(self):
        self.buf = []

    def on_input(self, stream, t):
        self.buf.append(t["s_axis_tdata"])
        if not t["s_axis_tlast"]:
            return []
        dec, self.buf = cobs_decode(bytes(self.buf)), []
        return [("m_axis", {"m_axis_tdata": b, "m_axis_tlast": int(i == len(dec) - 1), "m_axis_tuser": 0},
                 ["COBS-01", "COBS-02"] if i == len(dec) - 1 else "COBS-01") for i, b in enumerate(dec)]


def setup(dut, name):
    h = make(dut, Model(), name=name)
    h.add_property("no_error_flag", "sys",
                   lambda s, c: "tuser=1" if s["m_axis_tvalid"] == 1 and s["m_axis_tuser"] == 1 else None, "COBS-03")
    h.cover_protocol("m_axis", "COBS-04")
    return h


async def run(dut, name, frames, idle=0, backpressure=None, seed=1):
    rng = random.Random(seed)
    h = setup(dut, name)
    if backpressure:
        h.set_ready("m_axis", backpressure)
    await h.start()
    for f in frames:
        enc = cobs_encode(f)
        for i, b in enumerate(enc):
            h.send("s_axis", {"s_axis_tdata": b, "s_axis_tlast": int(i == len(enc) - 1), "s_axis_tuser": 0},
                   idle=rng.randint(0, idle))
    await h.drain(extra=10)
    h.finish()


def frames(kind):
    sizes = list(range(1, 20)) + [253, 254, 255, 256, 300]
    if kind == "zero":
        return [bytes(n) for n in sizes]
    if kind == "inc":
        return [bytes(itertools.islice(itertools.cycle(range(1, 256)), n)) for n in sizes]
    rng = random.Random(3)
    return [bytes(rng.randrange(256) for _ in range(n)) for n in sizes]


@cocotb.test()
async def zero_payload(dut):
    await run(dut, "zero_payload", frames("zero"))


@cocotb.test()
async def nonzero_payload(dut):
    await run(dut, "nonzero_payload", frames("inc"))


@cocotb.test()
async def random_payload_idle_backpressure(dut):
    await run(dut, "random_payload_idle_backpressure", frames("rand"), idle=2, backpressure=[1, 1, 1, 0])
