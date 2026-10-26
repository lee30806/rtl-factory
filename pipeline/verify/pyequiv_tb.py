"""번역기 검사용 cocotb 테스트 (사람이 검증하는 부분). `pyequiv.check`가 부른다.

같은 무작위 입력(가끔 리셋 포함)을 RTL과 설계 모델 실행기(`pysim`)에 넣고, 사건마다 모든 출력을 비교한다.
클럭은 이 테스트가 직접 움직여 RTL과 실행기의 에지 시각을 맞춘다 (상승 에지에서 실행기도 그 도메인들을 진행).

클럭 그룹(기준 도메인과 그 분주 도메인)마다 기준 클럭의 반주기를 2h ns(h는 홀수, `phases`의 값)로 둔다. 모든 에지는
시작 시각(짝수 ns)에서 짝수 ns 떨어진 곳에 있고, 입력은 그룹 기준 클럭의 하강 에지에서 h ns 뒤(홀수 ns)에 바꾸므로 어느
클럭의 상승·하강 에지와도 겹치지 않는다 (겹치면 RTL에서 경쟁이 되어 비교할 수 없음. 하강 에지 도메인도 있으므로 하강 에지도 피함).
분주 도메인은 기준과 같은 시각에 처음 오르고, 반주기는 기준 × 분주비다. 상승 에지에서는 상승 에지 도메인을, 하강 에지에서는
하강 에지 도메인(`edge: neg`)을 실행기에서도 진행한다.
`phases`(그룹 기준 도메인 → h)를 차례로 바꿔 가며 도메인 사이 위상을 바꾸고, 실행기와 RTL은 이어서 돌린다.
inout 포트가 있는 top은 하니스와 같은 패드 모델 포트(P_pad_o, P_pad_oe, P_pad_val)로 비교한다 (RTL 쪽은 패드 래퍼).
도메인이 없는 조합 top은 하니스와 같은 가상 클럭 뷰(`comb_view`)로 돌린다 (RTL 쪽은 가상 클럭 포트를 받는 래퍼).
첫 불일치를 결과 파일에 쓰고 실패한다.
"""
import json
import os
import random
from pathlib import Path

import cocotb
from cocotb.triggers import ReadOnly, Timer

from pipeline.ir.model import Module
from pipeline.ir.pysim import PySim
from pipeline.tb.gen import comb_view


def _groups(top: Module) -> dict[str, list[str]]:
    """클럭 그룹 기준 도메인 → 그룹의 도메인."""
    out: dict[str, list[str]] = {}
    for d in top.domains:
        out.setdefault(top.clock_root(d)[0], []).append(d)
    return out


@cocotb.test()
async def pyequiv(dut):
    cfg = json.loads(Path(os.environ["PYEQUIV_CFG"]).read_text(encoding="utf-8"))
    lib = {n: Module(r) for n, r in cfg["library"].items()}
    lib[cfg["top"]] = Module(comb_view(cfg["library"][cfg["top"]]))   # 조합 top: 가상 클럭 하나 (RTL 쪽은 pad_top 래퍼)
    top = lib[cfg["top"]]
    sim = PySim(top, lib)
    rng = random.Random(cfg["seed"])
    groups = _groups(top)
    ins, in_dom = sim.inputs(), sim.input_domains()
    first_root = next(iter(groups))
    in_root = {n: (top.clock_root(d)[0] if d else first_root) for n, d in in_dom.items()}   # 비동기 입력은 첫 그룹에서 바꿈
    resets = {}   # 리셋 포트 → (활성 값, 주 리셋인지)
    for d, dd in top.domains.items():
        r = dd.get("reset")
        if r is None:
            continue
        resets.setdefault(r["port"], (0 if r["active"] == "low" else 1, True))
        for x in r.get("extra", []):
            resets.setdefault(x["port"], (0 if x["active"] == "low" else 1, False))
    outs = list(sim.outputs())
    ports = {}   # 클럭 포트 → (그룹 기준 도메인, 분주비, 그 클럭의 상승 에지 도메인, 하강 에지 도메인)
    for d, dd in top.domains.items():
        root, div = top.clock_root(d)
        ports.setdefault(dd["clock"], (root, div, [], []))[3 if dd["edge"] == "neg" else 2].append(d)

    # 리셋은 비활성으로 시작한다. 2-state RTL에서 처음부터 활성 값이면 에지가 없어 비동기 리셋이 동작하지 않는다
    # (RTLLM traffic_light: 리셋값 10인 레지스터가 첫 클럭 전까지 0이라 설계 모델과 다르다고 나옴)
    for n, (act, _) in resets.items():
        getattr(dut, n).value = 1 - act
    mismatch = None
    t0, now, cyc = 0, 0, {r: 0 for r in groups}
    for phase in cfg["phases"]:
        half = {p: 2 * phase[root] * div for p, (root, div, _, _) in ports.items()}   # ns
        fast = min(phase[r] for r in groups)
        end = t0 + 4 * fast * cfg["cycles"]
        events: dict[int, tuple[set, set, set]] = {}   # 시각 → (오르는 포트, 내리는 포트, 입력을 바꾸는 그룹)
        for p, (root, div, _, _) in ports.items():
            h0 = 2 * phase[root]
            t = t0 + h0                 # 첫 상승: 기준과 같은 시각
            while t <= end:
                events.setdefault(t, (set(), set(), set()))[0].add(p)
                if t + half[p] <= end:
                    events.setdefault(t + half[p], (set(), set(), set()))[1].add(p)
                t += 2 * half[p]
        for r in groups:   # 기준 클럭의 하강 에지(짝수 ns)에서 h ns 뒤 (홀수 ns)
            for t in range(t0 + phase[r], end + 1, 4 * phase[r]):
                events.setdefault(t, (set(), set(), set()))[2].add(r)
        for p in ports:
            getattr(dut, p).value = 0
        for t in sorted(events):
            up, down, drive = events[t]
            if t > now:
                await Timer(t - now, unit="ns")
                now = t
            for p in down:
                getattr(dut, p).value = 0
            for p in up:
                getattr(dut, p).value = 1
            if drive:
                vals = {}
                for n, w in ins.items():
                    if in_root[n] not in drive:
                        continue
                    if n in resets:
                        act, main = resets[n]
                        hit = (main and cyc[in_root[n]] < cfg["reset_cycles"]) or rng.random() < cfg["reset_prob"]
                        vals[n] = act if hit else 1 - act
                    else:
                        vals[n] = rng.getrandbits(w)
                for r in drive:
                    cyc[r] += 1
                for n, v in vals.items():
                    getattr(dut, n).value = v
                sim.set(vals)
                sim.settle()
            active = {d for p in up for d in ports[p][2]} | {d for p in down for d in ports[p][3]}
            if active:
                sim.step(active)
            await ReadOnly()
            for o in outs:
                got = int(getattr(dut, o).value)
                if got != sim.val[o]:
                    mismatch = {"time_ns": t, "phase": phase, "output": o, "rtl": got, "py": sim.val[o],
                                "rising": sorted(active),
                                "inputs": {n: sim.val[n] for n in ins}}
                    break
            if mismatch:
                break
        if mismatch:
            break
        await Timer(1, unit="ns")   # 다음 위상은 짝수 ns에서 시작 (위의 겹치지 않는 조건)
        now += 1
        t0 = now + (now % 2)
        for r in groups:
            cyc[r] = 0
    Path(cfg["out"]).write_text(json.dumps({"ok": mismatch is None, "cycles": cfg["cycles"], "phases": cfg["phases"],
                                            "mismatch": mismatch}, ensure_ascii=False, indent=1), encoding="utf-8")
    assert mismatch is None, f"RTL과 설계 모델이 다름: {mismatch}"
