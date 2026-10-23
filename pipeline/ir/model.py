"""IR 모듈을 읽고 이름을 해석한다.

IR 파일 하나가 모듈 하나다. 구조(L0~L2)는 JSON이고 동작(L3)은 슬롯 안의 조각이다.
조각 언어는 모듈의 `lang`으로 정한다: `sv`(기본, SystemVerilog) 또는 `py`(Python, `pyfrag`).
`py` 모듈은 읽을 때 SV 조각으로 낮춘다. 검사, 방출, 하니스는 낮춘 IR(`raw`)을 보고, 원문은 `source_raw`에 남는다.
이 모듈은 검사와 방출이 공통으로 쓰는 이름 해석만 맡는다.
- 번들 포트를 개별 포트로 펼친다.
- 클럭과 리셋 포트(주 리셋, 추가 동기 리셋)는 `domains`에서 유도한다. IR의 `ports`에는 적지 않는다.
  `reset`이 없는 도메인은 리셋 없는 도메인이다 (`reset_of`가 None).
- 리셋 포트는 `comb` 조각에서만 읽을 수 있다 (예: 리셋 중 ready를 0으로). 클럭 포트는 읽지 않는다.
- 클럭 그룹: 기준 도메인과 그 분주 도메인(`sync_to`)은 같은 원천의 동기 클럭이라, 그룹 안에서는 동기화 셀 없이 읽고 연결한다.
- `fsm` 블록은 `<state_name>_q` 레지스터를 노출한다.
- `memory` 블록은 배열 `name`을 노출한다. 쓰기 포트 도메인의 seq 블록이 쓰고, 읽기 포트 도메인의 블록이 읽는다.
- inout 포트(top, block_top만)는 `tristate` 블록만 구동하고 읽는다. 조각에서 읽거나 쓸 수 없다.
- `functions`(function, task)는 조각에서 부르는 이름이다. 신호가 아니라 reads에 적지 않는다. task의 출력 인자로 넘긴 신호는 쓰기다.
- `assertions`(SVA)는 신호를 읽기만 한다. 구동이나 동작에 영향이 없다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Port:
    name: str
    dir: str                 # input | output | inout
    type: str
    domain: str | None       # None이면 비동기 입력
    kind: str | None = None  # 출력만: reg | wire
    reset: str | None = None
    bundle: str | None = None
    derived: bool = False    # domains에서 유도한 클럭/리셋 포트


@dataclass(frozen=True)
class Signal:
    name: str
    kind: str                # reg | wire
    type: str
    domain: str | None = None
    reset: str | None = None
    origin: str = "signal"   # signal | port | fsm | memory


class Module:
    def __init__(self, raw: dict, path: Path | None = None):
        self.source_raw, self.path = raw, path
        self.lang: str = raw.get("lang", "sv") if isinstance(raw, dict) else "sv"
        self.lower_errors: list[tuple[str, str]] = []           # (노드, 오류): Validator가 V4로 보고
        self.py_lines: dict[str, list[tuple[int, int, int]]] = {}  # 조각 노드 → (SV 시작, 끝, Python 줄), 조각 안 줄 번호
        if self.lang == "py":
            from pipeline.ir.pyfrag import lower_module
            raw, self.lower_errors, self.py_lines = lower_module(raw)
        self.raw = raw
        # 형식이 틀린 IR도 V1이 보고할 수 있도록, 해석에 실패한 부분은 비워 둔다.
        self.name: str = raw.get("module", "") if isinstance(raw, dict) else ""
        self.role: str = raw.get("role", "") if isinstance(raw, dict) else ""
        try:
            self._init(raw)
        except (KeyError, TypeError, AttributeError):
            self.params, self.domains, self.types, self.blocks = [], {}, [], []
            self.functions, self.assertions = [], []
            self.instances, self.connections, self.bundles, self.ports, self.signals = [], [], {}, [], []

    def _init(self, raw: dict) -> None:
        self.params: list[dict] = raw.get("params", [])
        self.domains: dict[str, dict] = {d["name"]: d for d in raw.get("domains", [])}
        self.types: list[dict] = raw.get("types", [])
        self.blocks: list[dict] = raw.get("blocks", [])
        self.functions: list[dict] = raw.get("functions", [])
        self.assertions: list[dict] = raw.get("assertions", [])
        self.instances: list[dict] = raw.get("instances", [])
        self.connections: list[dict] = raw.get("connections", [])
        self.bundles: dict[str, dict] = {}
        self.ports: list[Port] = self._ports()
        self.signals: list[Signal] = [Signal(s["name"], s["kind"], s["type"], s.get("domain"), s.get("reset"))
                                      for s in raw.get("signals", [])]

    # -- 포트 --
    def _ports(self) -> list[Port]:
        out: list[Port] = []
        seen: set[str] = set()
        for d in self.raw.get("domains", []):
            r = d.get("reset") or {}
            for name in (d["clock"], *([r["port"]] if r else []), *(x["port"] for x in r.get("extra", []))):
                if name not in seen:
                    out.append(Port(name, "input", "logic", d["name"], derived=True))
                    seen.add(name)
        for p in self.raw.get("ports", []):
            if "bundle" in p:
                self.bundles[p["bundle"]] = p
                for s in p["signals"]:
                    out.append(Port(s["name"], s["dir"], s["type"], p["domain"], s.get("kind"), s.get("reset"), p["bundle"]))
            else:
                out.append(Port(p["name"], p["dir"], p["type"], p.get("domain"), p.get("kind"), p.get("reset")))
        return out

    def port(self, name: str) -> Port | None:
        return next((p for p in self.ports if p.name == name), None)

    # -- 클럭 그룹과 리셋 --
    def clock_root(self, dom: str) -> tuple[str, int]:
        """(기준 도메인, 분주비). 기준 도메인은 (자기, 1). 사슬(분주의 분주)은 V2가 막으므로 한 번만 따라간다."""
        s = self.domains[dom].get("sync_to")
        return (s["domain"], s["div"]) if s and s["domain"] in self.domains else (dom, 1)

    def same_clock(self, a: str | None, b: str | None) -> bool:
        """같은 클럭 그룹인지 (같은 도메인 포함)."""
        if a == b:
            return True
        if a not in self.domains or b not in self.domains:
            return False
        return self.clock_root(a)[0] == self.clock_root(b)[0]

    def reset_ports(self, dom: str) -> list[str]:
        """도메인의 리셋 포트: 주 리셋, 추가 동기 리셋 순."""
        r = self.domains[dom].get("reset")
        return [r["port"], *(x["port"] for x in r.get("extra", []))] if r else []

    def reset_of(self, dom: str) -> dict | None:
        """도메인의 리셋 설정. 리셋 없는 도메인은 None."""
        return self.domains[dom].get("reset")

    def all_reset_ports(self) -> set[str]:
        return {p for d in self.domains for p in self.reset_ports(d)}

    def reset_async(self) -> bool | None:
        """모듈의 리셋 방식: 비동기면 True, 동기면 False, 리셋 있는 도메인이 없으면 None.
        한 설계의 리셋은 모두 한 방식이다 (V2). 섞여 있으면 첫 도메인의 방식."""
        styles = [d["reset"]["async"] for d in self.domains.values() if d.get("reset")]
        return styles[0] if styles else None

    @staticmethod
    def synced_reset(dom: str) -> str:
        """`reset.synchronize` 도메인에서 rst_sync 셀이 내는 동기화한 리셋 (active-low) 넷 이름."""
        return f"{dom}_rst_sync_n"

    # -- 이름 공간 --
    def state_regs(self) -> list[Signal]:
        return [Signal(f'{b["state_name"]}_q', "reg", b["state_type"], b["domain"], b["reset_state"], origin="fsm")
                for b in self.blocks if b["kind"] == "fsm"]

    def memories(self) -> dict[str, dict]:
        """배열 이름 → memory 블록."""
        return {b["name"]: b for b in self.blocks if b["kind"] == "memory"}

    def tristates(self) -> dict[str, dict]:
        """inout 포트 이름 → tristate 블록."""
        return {b["pad"]: b for b in self.blocks if b["kind"] == "tristate"}

    def memory_domains(self, name: str, kind: str) -> set[str]:
        """메모리 포트 중 kind(read|write)를 할 수 있는 포트의 도메인."""
        b = self.memories()[name]
        return {p["domain"] for p in b["ports"] if p["kind"] in (kind, "rw")}

    def storage(self) -> dict[str, Signal]:
        """쓸 수 있는 모든 이름: 출력 포트, 내부 신호, FSM 상태 레지스터, 메모리 배열."""
        out = {p.name: Signal(p.name, p.kind or "wire", p.type, p.domain, p.reset, origin="port")
               for p in self.ports if p.dir == "output"}
        out.update({s.name: s for s in self.signals})
        out.update({s.name: s for s in self.state_regs()})
        out.update({n: Signal(n, "mem", b["word_type"], None, None, origin="memory") for n, b in self.memories().items()})
        return out

    def readable(self) -> set[str]:
        """조각에서 읽을 수 있는 신호 이름 (상수 제외). 리셋 포트는 comb만 읽는다 (V8)."""
        return {p.name for p in self.ports if not p.derived and p.dir != "inout"} | set(self.storage()) | self.all_reset_ports()

    def constants(self) -> set[str]:
        """파라미터와 enum 멤버. 조각의 reads 목록에 적지 않아도 된다."""
        out = {p["name"] for p in self.params}
        for t in self.types:
            if t["kind"] == "enum":
                out |= set(t["members"])
        return out

    def type_names(self) -> set[str]:
        return {t["name"] for t in self.types}

    def tasks(self) -> dict[str, list[str]]:
        """task 이름 → 인자 방향 목록 (input | output). 출력 인자로 넘긴 신호는 그 조각의 쓰기다."""
        return {f["name"]: [a["dir"] for a in f["args"]] for f in self.functions if f["kind"] == "task"}


IMPL_PORT_FIELDS = ("kind", "reset")


def frozen_l1(raw: dict) -> dict:
    """동결 범위의 L1. 출력 포트의 kind(reg/wire)와 리셋값, local 파라미터는 구현이 정하는 값이라 뺀다.

    SPEC 표에는 이 값들이 없고 Author가 L2와 함께 정한다 (2026-10-11 결정).
    """
    def port(p: dict) -> dict:
        if "signals" in p:
            return {**p, "signals": [{k: v for k, v in s.items() if k not in IMPL_PORT_FIELDS} for s in p["signals"]]}
        return {k: v for k, v in p.items() if k not in IMPL_PORT_FIELDS}

    out = {k: raw[k] for k in ("module", "role", "domains", "power_domains") if k in raw}  # 없는 키는 넣지 않음
    out["params"] = [p for p in raw.get("params", []) if not p.get("local")]
    out["ports"] = [port(p) for p in raw.get("ports", [])] if isinstance(raw.get("ports"), list) else raw.get("ports")
    return out


def load(path: str | Path) -> Module:
    path = Path(path)
    return Module(json.loads(path.read_text(encoding="utf-8")), path)


def load_library(paths) -> dict[str, Module]:
    mods = [load(p) for p in paths]
    return {m.name: m for m in mods}
