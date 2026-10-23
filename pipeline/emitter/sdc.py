"""Block Top과 Top의 SDC 골격. L1에서 유도할 수 있는 제약만 방출한다.

| 방출 항목         | 출처                                               |
| 클럭 정의         | `domains`의 클럭 포트와 `freq_mhz`, 분주(`sync_to`) |
| 비동기 클럭 그룹  | `domains`의 `async_to` (SPEC 2번 섹션)             |
| CDC 경로 제약     | 계층 전체의 `cdc_sync`, `cdc_fifo` 노드            |
| 리셋 동기화       | `reset.synchronize` 도메인의 `rst_sync` 셀         |

분주 도메인은 기준 주기 × 분주비로 `create_clock`한다. 같은 원천이라 비동기 그룹에 넣지 않는다 (기본 동기 해석).
클럭 포트가 같은 도메인(리셋만 다른 같은 클럭)은 클럭을 한 번만 정의하고 그 이름을 함께 쓴다.
비동기 그룹은 클럭 그룹(기준 도메인과 그 분주 도메인) 단위로 묶는다.

입출력 지연과 예외 경로는 방출하지 않는다. 사람이 쓴 제약과 SDC Lint에서 대조한다.
CDC 경로 (셀의 동기화 플립플롭 핀. 임시 표준 셀의 이름이라 사내 셀을 받으면 바꾼다):
- 출발과 도착 도메인이 `async_to`로 비동기 그룹에 묶여 있으면 클럭 그룹이 경로를 이미 덮으므로 주석만 남긴다
  (클럭 그룹이 set_max_delay보다 우선한다).
- 그렇지 않으면 동기화 셀 데이터 핀까지 목적지 클럭 한 주기로 `set_max_delay`를 둔다.
- 비동기 입력 포트에서 오는 경로는 false path로 둔다. 하위 모듈의 포트는 시작점이 될 수 없으므로 `-through`로 쓴다.
"""
from __future__ import annotations

from pipeline.emitter.sv import SYNC_CELL, EmitError
from pipeline.ir.model import Module

# 셀 → (방향, 동기화 첫 단의 핀). 방향 "fwd"는 src → dst, "back"은 dst → src
SYNC_PINS = {"2ff": [("fwd", SYNC_CELL["d"])], "gray": [("fwd", SYNC_CELL["d"])], "pulse": [("fwd", "s1/D")],
             "cdc_fifo": [("fwd", "wq1/D"), ("back", "rq1/D")]}


def _period(dom: dict, m: Module | None = None) -> float | None:
    """클럭 주기 (ns). 분주 도메인은 기준 주기 × 분주비 (기준에 freq_mhz가 없으면 자기 freq_mhz)."""
    if m is not None and dom.get("sync_to") and dom["sync_to"]["domain"] in m.domains:
        root = m.domains[dom["sync_to"]["domain"]]
        if root.get("freq_mhz"):
            return round(1000.0 / root["freq_mhz"] * dom["sync_to"]["div"], 3)
    f = dom.get("freq_mhz")
    return round(1000.0 / f, 3) if f else None


def clock_names(m: Module) -> dict[str, str]:
    """도메인 → SDC 클럭 이름. 클럭 포트가 같은 도메인은 처음 나온 도메인의 클럭을 함께 쓴다."""
    first: dict[str, str] = {}
    return {d["name"]: first.setdefault(d["clock"], d["name"]) for d in m.domains.values()}


def _walk(m: Module, library: dict[str, Module], path: str, dmap: dict[str, str]):
    """(인스턴스 경로, 모듈, 이 모듈의 도메인 → 최상위 도메인)을 깊이 우선으로 돌려준다."""
    yield path, m, dmap
    for inst in m.instances:
        child = library.get(inst["module"])
        if child is None:
            raise EmitError(f'instances/{inst["name"]}: 라이브러리에 없는 모듈 {inst["module"]}')
        cmap = {cd: dmap[pd] for cd, pd in inst.get("domain_map", {}).items() if pd in dmap}
        yield from _walk(child, library, f'{path}{inst["name"]}/', cmap)


def emit_sdc(m: Module, library: dict[str, Module] | None = None) -> str:
    if m.role not in ("top", "block_top"):
        raise EmitError(f"SDC 골격은 top과 block_top에만 방출함 ({m.name}은 {m.role})")
    library = {**(library or {}), m.name: m}
    out = [f"# SDC skeleton for '{m.name}', generated from IR L1. Do not edit; IO delays and exceptions go in the",
           "# hand-written constraints.", ""]

    out.append("# @ir domains")
    cname = clock_names(m)
    for d in m.domains.values():
        if cname[d["name"]] != d["name"]:
            out.append(f'# domains/{d["name"]}: 클럭 포트 {d["clock"]}를 {cname[d["name"]]}와 함께 씀 (같은 클럭)')
            continue
        per = _period(d, m)
        if per is None:
            out.append(f'# domains/{d["name"]}: freq_mhz 없음. 클럭 정의를 방출하지 않음')
            continue
        if d.get("sync_to"):
            out.append(f'# domains/{d["name"]}: {d["sync_to"]["domain"]}에서 {d["sync_to"]["div"]}분주 (같은 원천, 상승 에지 정렬)')
        out.append(f'create_clock -name {d["name"]} -period {per} [get_ports {d["clock"]}]')

    def group(dom: str) -> str:
        """클럭 그룹의 클럭 이름들 (기준 도메인과 그 분주 도메인)."""
        names = sorted({cname[x] for x in m.domains if m.same_clock(x, dom)})
        return " ".join(names)

    pairs = sorted({tuple(sorted((group(d["name"]), group(o)))) for d in m.domains.values() for o in d.get("async_to", [])
                    if o in m.domains})
    async_pairs = {tuple(sorted((a, b))) for a in m.domains for b in m.domains
                   if tuple(sorted((group(a), group(b)))) in pairs}
    if pairs:
        out += ["", "# @ir domains/async_to"]
        for a, b in pairs:
            out.append(f"set_clock_groups -asynchronous -group [get_clocks {{{a}}}] -group [get_clocks {{{b}}}]"
                       if " " in a + b else f"set_clock_groups -asynchronous -group [get_clocks {a}] -group [get_clocks {b}]")

    cdc, rsts = [], []
    for path, mod, dmap in _walk(m, library, "", {d: d for d in m.domains}):
        for b in mod.blocks:
            if b["kind"] in ("cdc_sync", "cdc_fifo"):
                cdc.append((path, mod, dmap, b))
        rsts += [(path, d) for d in mod.domains.values() if (d.get("reset") or {}).get("synchronize")]
    if cdc:
        out += ["", "# CDC paths into standard sync cells"]
    for path, mod, dmap, b in cdc:
        out.append(f'# @ir {mod.name}/blocks/{b["id"]}')
        for way, p in SYNC_PINS[b.get("style", b["kind"])]:
            pin = f'{path}u_{b["id"]}/{p}'
            if b["src_domain"] is None:
                if not path:
                    out.append(f"set_false_path -from [get_ports {b['src']}] -to [get_pins {pin}]")
                else:
                    out.append(f"set_false_path -through [get_pins {path}{b['src']}] -to [get_pins {pin}]")
                continue
            src, dst = dmap.get(b["src_domain"], b["src_domain"]), dmap.get(b["dst_domain"])
            if way == "back":
                src, dst = dst, src
            if tuple(sorted((src, dst))) in async_pairs:
                out.append(f"# {src} -> {dst}: 비동기 클럭 그룹이 덮음 ({pin})")
                continue
            per = _period(m.domains[dst], m) if dst in m.domains else None
            if per is None:
                out.append(f"# 목적지 도메인의 주기를 알 수 없음: {pin}")
                continue
            out.append(f"set_max_delay {per} -from [get_clocks {cname.get(src, src)}] -to [get_pins {pin}]")
    if rsts:
        out += ["", "# Reset synchronizers: asynchronous assertion into standard rst_sync cells"]
    for path, d in rsts:
        pin = f'{path}u_rst_sync_{d["name"]}/rst_n'
        out.append(f'# @ir domains/{d["name"]}/reset')
        src = f'[get_ports {d["reset"]["port"]}]' if not path else f'[get_pins {path}{d["reset"]["port"]}]'
        out.append(f"set_false_path {'-from' if not path else '-through'} {src} -through [get_pins {pin}]")
    return "\n".join(out) + "\n"
