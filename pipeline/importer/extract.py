"""P11 R1: elaborate한 원본에서 모듈마다 L1(params, domains, ports)과 L0(instances, connections) 초안을 만든다.

모듈 구조는 원본과 1:1이다 (모듈 이름, 계층, 인스턴스 이름, 포트). 동작(L2, L3)은 R3 Porter가 쓴다.

- 도메인: `always @(posedge clk [or posedge/negedge rst])`, `always_ff`의 sensitivity와 첫 `if`의 리셋 분기에서 찾는다.
  `negedge clk`는 하강 에지 도메인이다 (같은 클럭의 상승 에지 도메인과는 다른 도메인, 이름은 `<클럭>_neg`).
  비동기 리셋은 sensitivity에 있는 신호, 동기 리셋은 첫 `if` 조건이 입력 포트 하나이고 그 갈래가 상수만 대입하는 경우.
  한 설계의 리셋은 모두 비동기이거나 모두 동기다. 섞여 있으면(모듈 안이든 모듈 사이든) `mixed_reset`으로 거부한다.
  하위 인스턴스의 도메인도 부모 도메인을 만든다 (domain_map).
- 포트 도메인: 그 포트를 쓰거나 읽는 seq 블록(조합 경로와 하위 인스턴스를 거쳐)의 도메인.
- 번들: `<p>_[t]valid`와 `<p>_[t]ready`가 반대 방향으로 있으면 `<p>_`로 시작하는 포트를 valid_ready 번들 후보로 묶는다 (R2에서 확정).
- `generate`: 고정한 파라미터로 elaborate한 결과에서 고른 갈래만 본다. `generate for` 안의 인스턴스는 `<인스턴스>_<인덱스>`로 펼친다.
- 매크로와 `$unit`/package 상수: 숫자 상수는 쓰는 모듈의 `localparam`으로 옮기고, 그 밖의 매크로는 풀어 쓴다.
- 포트 연결이 이름 하나가 아니면(슬라이스, 상수, 식, 미연결 입력) Porter가 마무리할 연결(`pending`)로 남기고, 부모 role은 module이다.
- inout: 하위 모듈의 inout 포트 P는 P_o(출력, push-pull만), P_oe(출력), P_i(입력)로 나눈다. 원본의 `assign P = c ? x : 'z`에서
  x가 0이면 open-drain이다. top의 inout은 그대로 두고 `tristate` 블록과 연결용 wire 신호(P_o, P_oe, P_i)를 만든다.
  하위 모듈 사이를 오가는 3상태 버스는 거부한다.

표현할 수 없는 것은 `rejects`, 사람이 확인할 것은 `reviews`로 낸다 (파일과 줄 포함).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import pyslang

from pipeline.importer.sources import Design
from pipeline.validator.checks import SV_KEYWORDS

SK, EK, STK = pyslang.SymbolKind, pyslang.ExpressionKind, pyslang.StatementKind
IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_$]*\b")
PKG_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)::([A-Za-z_][A-Za-z0-9_]*)\b")
RESET_NAME = re.compile(r"rst|reset|clr|clear", re.I)


@dataclass
class Proc:
    kind: str                          # seq | comb
    reads: set[str]
    writes: set[str]
    loc: tuple[str, int]
    clock: str | None = None
    edge: str = "pos"
    reset: tuple[str, str, bool] | None = None   # (port, active, async)
    extra: list[tuple[str, str]] = field(default_factory=list)
    internal_reset: str | None = None


@dataclass
class Mod:
    """원본 모듈 하나의 추출 결과."""
    name: str
    ir: dict
    source: dict
    renames: dict[str, str] = field(default_factory=dict)
    pending: list[dict] = field(default_factory=list)
    macro_params: list[str] = field(default_factory=list)
    port_domains: dict[str, str | None] = field(default_factory=dict)
    signature: str = ""
    tri_split: dict[str, dict] = field(default_factory=dict)   # 나눈 inout 포트 → {style, o, oe, i, 원본 식}
    removed_params: dict[str, str] = field(default_factory=dict)   # 고른 구조에서 쓰이지 않아 지운 파라미터 → 고정 값


class Extractor:
    def __init__(self, design: Design):
        self.d = design
        self.mods: dict[str, Mod] = {}
        self.rejects: list[dict] = []
        self.reviews: list[dict] = []
        self.order: list[str] = []          # 하위 모듈부터
        self.genvars: dict[tuple, frozenset] = {}

    # ------------------------------------------------------------------ 진입점
    def run(self) -> dict:
        self._instance(self.d.top, top=True)
        styles = {}   # 리셋 방식 → 그 방식을 쓰는 모듈 (모듈 안에서 섞인 것은 _module이 거부함)
        for mn in self.order:
            own = {d["reset"]["async"] for d in self.mods[mn].ir.get("domains", []) if d.get("reset")}
            if len(own) == 1:
                styles.setdefault(own.pop(), mn)
        if len(styles) > 1:
            self._reject(self.d.top.definition.name, None, "mixed_reset",
                         f"모듈마다 리셋 방식이 다름: {styles[True]}은 비동기, {styles[False]}은 동기. "
                         "한 설계의 리셋은 한 방식이어야 하므로 원본을 모두 비동기나 모두 동기로 고친 뒤 옮김")
        for w in self.d.warnings():
            if w["code"] in ("ImplicitNet", "ImplicitNetPortNoDefault"):
                self._review(None, (w["file"], w["line"]), "implicit_net", "선언하지 않은 이름이 암묵적 1비트 wire가 됨 (오타일 수 있음)")
        return {"top": self.d.inp.top, "order": self.order, "modules": self.mods, "rejects": self.rejects, "reviews": self.reviews}

    def _reject(self, mod: str | None, loc, kind: str, msg: str) -> None:
        item = {"module": mod, "file": loc[0] if loc else None, "line": loc[1] if loc else None, "kind": kind, "message": msg}
        if item not in self.rejects:
            self.rejects.append(item)

    def _review(self, mod: str | None, loc, kind: str, msg: str) -> None:
        item = {"module": mod, "file": loc[0] if loc else None, "line": loc[1] if loc else None, "kind": kind, "message": msg}
        if item not in self.reviews:
            self.reviews.append(item)

    def _loc(self, node) -> tuple[str, int]:
        try:
            return self.d.where(node.sourceRange.start)
        except AttributeError:
            return self.d.where(node.location)

    # ------------------------------------------------------------------ 계층
    def _instance(self, inst, top: bool = False) -> Mod | None:
        body = inst.body
        name = inst.definition.name
        children = list(self._instances(body))
        for _, child in children:
            self._instance(child)
        sig = self._signature(body)
        if name in self.mods:
            if self.mods[name].signature != sig:
                self._reject(name, self._loc(inst), "param_structure",
                             f"모듈 {name}을 구조가 달라지는 서로 다른 파라미터로 인스턴스함 (IR은 설정마다 따로 두어야 함)")
            return self.mods[name]
        mod = self._module(inst, body, children, top)
        mod.signature = sig
        self.mods[name] = mod
        self.order.append(name)
        return mod

    def _instances(self, scope, prefix: list[str] | None = None, genvars: frozenset = frozenset()):
        """(IR 인스턴스 이름, 인스턴스 심볼). 고른 generate 갈래 안까지 본다. genvar 이름은 self.genvars에 남긴다."""
        for m in scope:
            if m.kind == SK.Instance:
                name = "_".join([m.name, *(prefix or [])]) if prefix else m.name
                self.genvars[(id(scope), m.name, name)] = genvars
                yield name, m
            elif m.kind == SK.InstanceArray:
                self._reject(None, self._loc(m), "instance_array", f"인스턴스 배열 {m.name}[...]은 generate for로 바꿔야 함")
            elif m.kind == SK.GenerateBlock and not m.isUninstantiated:
                yield from self._instances(m, prefix, genvars)
            elif m.kind == SK.GenerateBlockArray:
                for e in m:
                    if e.kind == SK.GenerateBlock and not e.isUninstantiated:
                        gv = genvars | {x.name for x in e if x.kind == SK.Parameter}
                        yield from self._instances(e, [*(prefix or []), str(e.arrayIndex)], frozenset(gv))

    def _signature(self, body) -> str:
        parts = []
        for p in body.portList:
            parts.append(f"{p.name}:{p.type}")
        for n, c in self._instances(body):
            parts.append(f"i:{n}:{c.definition.name}")

        def gens(scope):
            for m in scope:
                if m.kind == SK.GenerateBlock:
                    parts.append(f"g:{m.name}:{m.isUninstantiated}")
                    if not m.isUninstantiated:
                        gens(m)
                elif m.kind == SK.GenerateBlockArray:
                    parts.append(f"ga:{m.name}:{len(list(m))}")
        gens(body)
        return "|".join(parts)

    # ------------------------------------------------------------------ 모듈
    def _module(self, inst, body, children, top: bool) -> Mod:
        name = inst.definition.name
        src = self.d.module_source(inst.definition)
        mod = Mod(name, {}, src)
        self.cur = mod
        floc = (src["file"], src["line"])
        if name in SV_KEYWORDS:
            self._reject(name, floc, "keyword_module", f"모듈 이름이 SV 예약어: {name}")
        self.tri = self._tri_assigns(body, children, name, top)
        procs = self._procs(body, name)
        self._misc(body, name)

        # 도메인 키: (clock, edge, reset_port, active, async)
        input_ports = {p.name for p in body.portList if p.kind == SK.Port and p.direction == pyslang.ArgumentDirection.In}
        rsync, procs = self._reset_syncs(procs, input_ports, top, name, body)
        sync_keys: set[tuple] = set()
        keys: dict[tuple, list] = {}
        clocks: dict[tuple[str, str], None] = {}   # (클럭, 에지). 같은 클럭의 상승·하강 에지는 다른 도메인
        for p in procs:
            if p.kind != "seq":
                continue
            if p.clock not in input_ports:
                self._reject(name, p.loc, "internal_clock", f"클럭 {p.clock}이 입력 포트가 아님 (블록 안에서 만든 클럭)")
                continue
            clocks.setdefault((p.clock, p.edge))
            if p.reset:
                if p.reset[0] in input_ports:
                    keys.setdefault((p.clock, p.edge, *p.reset), []).append(p)
                else:
                    p.internal_reset = p.reset[0]
                    p.reset = None
            if p.internal_reset:
                self._review(name, p.loc, "internal_reset",
                             f"리셋 {p.internal_reset}이 입력 포트가 아닌 내부 신호 (IR 도메인 리셋으로 옮길 수 없어 동작으로 옮김)")
        child_maps: dict[str, dict[str, tuple]] = {}
        for iname, c in children:
            cm = self.mods.get(c.definition.name)
            if not cm:
                continue
            child_maps[iname] = {}
            for cd in cm.ir.get("domains", []):
                ck = self._net(c, cd["clock"])
                loc = self._loc(c)
                if ck not in input_ports:
                    self._reject(name, loc, "child_clock", f"{iname}의 클럭 {cd['clock']}에 입력 포트가 아닌 것이 연결됨: {ck or '식'}")
                    continue
                if not cd.get("reset"):   # 리셋 없는 자식 도메인: 같은 클럭의 부모 도메인에 붙임 (아래에서 정함)
                    clocks.setdefault((ck, cd["edge"]))
                    child_maps[iname][cd["name"]] = ("noreset", ck, cd["edge"])
                    continue
                rs = self._net(c, cd["reset"]["port"])
                if rs in rsync:   # top 안의 리셋 동기화기 출력 → reset.synchronize 도메인
                    sk = rsync[rs]
                    if sk[0] != ck:
                        self._reject(name, loc, "child_reset", f"{iname}의 리셋 {rs}을 동기화한 클럭({sk[0]})이 {iname}의 클럭({ck})과 다름")
                        continue
                    key = (ck, cd["edge"], sk[1], sk[2], True)
                    sync_keys.add(key)
                    keys.setdefault(key, [])
                    clocks.setdefault((ck, cd["edge"]))
                    child_maps[iname][cd["name"]] = key
                    continue
                if rs not in input_ports:
                    self._reject(name, loc, "child_reset",
                                 f"{iname}의 리셋 {cd['reset']['port']}에 입력 포트가 아닌 것이 연결됨: {rs or '식'} (블록 안에서 만든 리셋)")
                    continue
                key = (ck, cd["edge"], rs, cd["reset"]["active"], cd["reset"]["async"])
                keys.setdefault(key, [])
                clocks.setdefault((ck, cd["edge"]))
                child_maps[iname][cd["name"]] = key
                for x in cd["reset"].get("extra", []):
                    ex = self._net(c, x["port"])
                    if ex not in input_ports:
                        self._reject(name, loc, "child_reset", f"{iname}의 추가 리셋 {x['port']}에 입력 포트가 아닌 것이 연결됨")
        # 한 설계의 리셋은 모두 비동기이거나 모두 동기다 (ARCHITECTURE 도메인 리셋). 섞이면 원본을 한 방식으로 고친 뒤 옮긴다
        styles = {k[4] for k in keys}
        if len(styles) > 1:
            where = {asy: next((p.loc for k in keys if k[4] == asy for p in keys[k]), floc) for asy in styles}
            self._reject(name, where[True], "mixed_reset",
                         f"비동기 리셋({where[True][0]}:{where[True][1]})과 동기 리셋({where[False][0]}:{where[False][1]})이 섞임. "
                         "한 설계의 리셋은 한 방식이어야 하므로 원본을 모두 비동기나 모두 동기로 고친 뒤 옮김")
        for ck, edge in clocks:
            if not any(k[:2] == (ck, edge) for k in keys):   # 리셋 없는 도메인
                keys[(ck, edge, None, None, None)] = []
                self._review(name, floc, "no_reset", f"클럭 {ck}의 레지스터에 입력 포트 리셋이 없어 리셋 없는 도메인으로 둠")
        for cmap in child_maps.values():
            for cd, key in cmap.items():
                if key and key[0] == "noreset":
                    cmap[cd] = next(k for k in keys if k[:2] == key[1:3])
        if not clocks:   # 조합 모듈: 도메인 없이, 포트 도메인은 부모가 연결한 넷에서 물려받음
            self._review(name, floc, "comb_module", "클럭이 없는 조합 모듈 (도메인 없이 옮김. 포트 도메인은 연결한 넷을 따름)")
        order = {p.name: i for i, p in enumerate(body.portList)}
        keys = dict(sorted(keys.items(), key=lambda kv: (order.get(kv[0][0], 1e9), order.get(kv[0][2] or "", 1e9))))
        dnames = self._domain_names(list(keys))
        domains = []
        for key, dn in dnames.items():
            ck, edge, rs, act, asy = key
            dom = {"name": dn, "clock": ck, "edge": edge, "reset": {"port": rs, "active": act, "async": asy}}
            if rs is None:
                del dom["reset"]
            elif key in sync_keys:
                dom["reset"]["synchronize"] = True
                if any(keys[key]):
                    self._reject(name, floc, "reset_sync", f"리셋 {rs}을 블록 안에서 동기화하는 도메인에 top 자신의 로직도 있음 "
                                                           "(동기화 전 리셋을 쓰는 로직과 섞임)")
            extra = []
            for p in keys[key]:
                for x in p.extra:
                    if x not in extra:
                        extra.append(x)
            if extra and "reset" in dom:
                dom["reset"]["extra"] = [{"port": e, "active": a} for e, a in extra]
                self._review(name, keys[key][0].loc, "extra_reset", f"{dn} 도메인의 추가 동기 리셋 후보: {', '.join(e for e, _ in extra)}")
            same = next((x for x in domains if x["clock"] == ck), None)
            if same is not None:   # 클럭 포트가 같은 앞 도메인 (리셋이나 에지만 다름): 같은 클럭 (R2도 같게 둠)
                dom["sync_to"] = {"domain": same["name"], "div": 1}
            domains.append(dom)
        # 리셋 없는 seq 블록은 같은 클럭, 같은 에지의 첫 도메인에 붙인다
        def dom_of(p: Proc) -> str | None:
            if p.reset:
                return dnames.get((p.clock, p.edge, *p.reset))
            return next((dn for k, dn in dnames.items() if k[:2] == (p.clock, p.edge)), None)
        derived = {d["clock"] for d in domains} | {d["reset"]["port"] for d in domains if d.get("reset")} | \
                  {x["port"] for d in domains if d.get("reset") for x in d["reset"].get("extra", [])}

        # 포트 도메인
        pdom = self._port_domains(body, procs, dom_of, children, child_maps, dnames, derived, name)
        mod.port_domains = pdom
        for pad, info in self.tri.items():
            info["domain"] = pdom.get(pad)

        params = self._params(inst, body, top, name)
        mod.removed_params = self._removed_params
        ports = self._ports(body, pdom, derived, name)
        ir = {"module": name, "role": "module", "params": params, "domains": domains, "ports": ports}
        instances, connections, signals, pending = self._connections(body, children, child_maps, dnames, input_ports, name)
        if instances:
            ir["instances"] = instances
        if connections:
            ir["connections"] = connections
        if signals:
            ir["signals"] = signals
        mod.pending = pending
        if top and self.tri:
            blocks = []
            for pad, info in self.tri.items():
                blk = {"id": f"t_{pad}", "kind": "tristate", "pad": pad, "oe": info["oe"], "i": info["i"],
                       "domain": info["domain"] or next(iter(dnames.values()), "sys"), "style": info["style"]}
                if info["style"] == "push_pull":
                    blk["o"] = info["o"]
                blocks.append(blk)
                for k in ("o", "oe", "i"):
                    n = info.get(k)
                    if n and info.get(f"{k}_new") and not any(x["name"] == n for x in ir.get("signals", [])):
                        ir.setdefault("signals", []).append({"name": n, "kind": "wire",
                                                             "type": info["oe_type"] if k == "oe" else self._tri_type(body, pad, mod)})
                for c in info.get("conns", []):
                    ir.setdefault("connections", []).append(c)
            ir["blocks"] = blocks
            signals = ir.get("signals", [])
        logic = any(True for _ in procs)
        if top and children and not logic and not pending and all(s["name"] in self._tri_signal_names() for s in signals):
            ir["role"] = "block_top"
            for p in ir["ports"]:
                for s in (p["signals"] if "signals" in p else [p]):
                    if s["dir"] == "output":
                        s["kind"] = "wire"
        ir["params"] += [mp for mp in self._macro_localparams(name) if mp["name"] not in {p["name"] for p in ir["params"]}]
        mod.ir = ir
        mod.macro_params = [p["name"] for p in ir["params"] if p.get("_macro")]
        for p in ir["params"]:
            p.pop("_macro", None)
        self._apply_renames(mod)
        return mod

    def _domain_names(self, keys: list[tuple]) -> dict[tuple, str]:
        if len(keys) == 1:
            return {keys[0]: "sys"}
        out: dict[tuple, str] = {}
        used: set[str] = set()
        for k in keys:
            base = re.sub(r"(^clk_?|_?clk$|_?clock$|^clock_?)", "", k[0], flags=re.I) or k[0]
            if not re.match(r"^[A-Za-z_]", base):
                base = k[0]
            if k[1] == "neg":   # 같은 클럭의 하강 에지 도메인
                base += "_neg"
            n, i = base, 2
            while n in used or n in SV_KEYWORDS:
                n, i = f"{base}{i}", i + 1
            used.add(n)
            out[k] = n
        return out

    # ------------------------------------------------------------------ 블록 분석
    def _procs(self, scope, mod: str) -> list[Proc]:
        out: list[Proc] = []
        if not hasattr(self, "tri_skip"):
            self.tri_skip, self.top_mod = set(), False
        for m in scope:
            if m.kind == SK.ProceduralBlock:
                p = self._proc(m, mod)
                if p:
                    out.append(p)
            elif m.kind == SK.ContinuousAssign:
                r, w = rw(m.assignment)
                if self.top_mod and loc_key(m) in self.tri_skip:   # top의 3상태 assign은 tristate 블록이 됨
                    continue
                out.append(Proc("comb", r, w, self._loc(m)))
            elif m.kind == SK.GenerateBlock and not m.isUninstantiated:
                out += self._procs(m, mod)
            elif m.kind == SK.GenerateBlockArray:
                for e in m:
                    if e.kind == SK.GenerateBlock and not e.isUninstantiated:
                        out += self._procs(e, mod)
            elif m.kind in (SK.Net, SK.Variable) and m.initializer is not None and m.kind == SK.Net:
                r = names(m.initializer)
                out.append(Proc("comb", r, {m.name}, self._loc(m)))
        return out

    def _proc(self, m, mod: str) -> Proc | None:
        kind = m.procedureKind
        loc = self._loc(m)
        PK = pyslang.ProceduralBlockKind
        if kind in (PK.Initial, PK.Final):
            if kind == PK.Initial:
                self._review(mod, loc, "initial", "initial 블록 (리셋이 아닌 초기값은 IR에서 리셋값으로 옮김. 시뮬레이션 전용이면 무시)")
            return None
        if kind == PK.AlwaysLatch:
            self._reject(mod, loc, "latch", "래치 (always_latch)")
            return None
        if has_delay(m.body):
            self._reject(mod, loc, "delay", "지연(#)이 있는 절차 블록")
            return None
        body = m.body
        if body.kind == STK.Timed:
            t = body.timing
            events = []
            if t.kind == pyslang.TimingControlKind.SignalEvent:
                events = [t]
            elif t.kind == pyslang.TimingControlKind.EventList:
                events = list(t.events)
            elif t.kind == pyslang.TimingControlKind.ImplicitEvent:
                r, w = rw(body.stmt)
                return Proc("comb", r, w, loc)
            edged = [e for e in events if e.kind == pyslang.TimingControlKind.SignalEvent and e.edge != pyslang.EdgeKind.None_]
            if not edged:
                r, w = rw(body.stmt)
                listed = {sym_name(e.expr) for e in events if e.kind == pyslang.TimingControlKind.SignalEvent}
                missing = sorted(r - listed - w)
                if missing:
                    self._review(mod, loc, "sensitivity", f"조합 always의 sensitivity 목록에 빠진 신호: {', '.join(missing)} (원본과 시뮬레이션이 다를 수 있음)")
                return Proc("comb", r, w, loc)
            return self._seq(body.stmt, edged, loc, mod)
        if kind == PK.AlwaysComb:
            r, w = rw(body)
            return Proc("comb", r, w, loc)
        self._review(mod, loc, "always", "타이밍 제어가 없는 always 블록")
        return None

    def _seq(self, stmt, edged, loc, mod: str) -> Proc:
        sigs = [(sym_name(e.expr), "pos" if e.edge == pyslang.EdgeKind.PosEdge else "neg") for e in edged]
        first = first_stmt(stmt)
        reset = None
        rest_stmt = stmt
        if len(sigs) >= 2:
            cond = cond_signal(first) if first is not None else None
            if cond and cond[0] in [s for s, _ in sigs]:
                edge = dict(sigs)[cond[0]]
                active = cond[1]
                if (edge == "pos") != (active == "high"):
                    self._review(mod, loc, "reset_polarity", f"비동기 리셋 {cond[0]}의 에지({edge})와 조건 극성({active})이 맞지 않음")
                reset = (cond[0], active, True)
                rest_stmt = first.ifFalse
                clock = [(s, e) for s, e in sigs if s != cond[0]]
            else:
                clock = sigs[:1]
                self._review(mod, loc, "async_events", f"sensitivity에 에지 신호가 여럿이지만 리셋 분기를 찾지 못함: {', '.join(s for s, _ in sigs)}")
            if len(clock) != 1:
                self._reject(mod, loc, "multi_clock_block", f"절차 블록 하나에 클럭이 여럿: {', '.join(s for s, _ in clock)}")
                clock = clock[:1] or sigs[:1]
        else:
            clock = sigs
            # 동기 리셋: 첫 문장 `if (rst) 상수 else ...` 또는 끝 문장 `if (rst) 상수` (끝에서 덮어쓰는 Verilog 관행)
            stmts = top_stmts(stmt)
            cands = [(first, True)] + ([(stmts[-1], False)] if len(stmts) > 1 else [])
            for st, is_first in cands:
                cond = cond_signal(st) if st is not None else None
                if cond and st.ifTrue is not None and const_only(st.ifTrue) and (is_first or st.ifFalse is None):
                    reset = (cond[0], cond[1], False)
                    rest_stmt = st.ifFalse if is_first else None
                    if not RESET_NAME.search(cond[0]):
                        self._review(mod, loc, "sync_reset_guess", f"{cond[0]}을 동기 리셋으로 봄 (if 갈래가 상수만 대입). 리셋이 아니면 R2에서 고치세요")
                    if not is_first:
                        self._review(mod, loc, "reset_at_end", f"리셋 {cond[0]}이 블록 끝에서 일부 레지스터만 덮어씀 (리셋 없는 레지스터와 섞임. Porter가 seq 블록을 나눔)")
                    break
        extra = []   # 추가 동기 리셋은 동기 리셋 설계에만 둔다. 비동기 리셋 블록의 소프트 리셋은 동작으로 남긴다
        nxt = first_stmt(rest_stmt) if rest_stmt is not None and reset and not reset[2] else None
        while reset and nxt is not None:
            c2 = cond_signal(nxt)
            if not (c2 and nxt.ifTrue is not None and const_only(nxt.ifTrue)
                    and RESET_NAME.search(c2[0])):
                break
            extra.append(c2)
            nxt = first_stmt(nxt.ifFalse) if nxt.ifFalse is not None else None
        r, w = rw(stmt)
        ck = clock[0]
        r -= {ck[0]}
        return Proc("seq", r, w, loc, clock=ck[0], edge=ck[1], reset=reset, extra=extra)

    # ------------------------------------------------------------------ 리셋 동기화기
    def _reset_syncs(self, procs: list[Proc], input_ports: set[str], top: bool, mod: str, body) -> tuple[dict, list[Proc]]:
        """리셋 동기화 플롭 패턴 → ({출력 넷: (클럭, 리셋 포트, 극성)}, 남은 블록).
        입력 포트 리셋으로 비동기 리셋되고, 리셋 아닌 갈래가 자기 레지스터와 상수만 읽고(시프트 체인), 그 레지스터를
        다른 로직이 읽지 않는 블록이다. 그 레지스터를 그대로 옮긴 wire(assign x = r 또는 r[k])도 출력 넷이다.
        top에만 둘 수 있다 (IR의 reset.synchronize). 하위 모듈 리셋으로 쓰이는지는 호출한 쪽이 본다."""
        cands = [p for p in procs if p.kind == "seq" and p.reset and p.reset[2] and p.reset[0] in input_ports
                 and p.writes and (p.reads - {p.reset[0]}) <= p.writes]
        if not cands:
            return {}, procs
        out, drop = {}, []
        for p in cands:
            others = [q for q in procs if q is not p and q not in cands]
            aliases = [q for q in others if q.kind == "comb" and len(q.writes) == 1 and q.reads and q.reads <= p.writes]
            readers = [q for q in others if q not in aliases and q.reads & (p.writes | {w for a in aliases for w in a.writes})]
            if readers:
                continue
            if not top:
                self._reject(mod, p.loc, "reset_sync", "리셋 동기화 플롭이 top이 아닌 모듈에 있음 (IR은 top의 reset.synchronize만 지원)")
                continue
            info = (p.clock, p.reset[0], p.reset[1])
            for n in p.writes | {w for a in aliases for w in a.writes}:
                out[n] = info
            drop += [p, *aliases]
            depth = sum(getattr(getattr(body.find(n), "type", None), "bitWidth", 1) for n in p.writes)
            if depth != 2:
                self._review(mod, p.loc, "reset_sync_depth", f"리셋 동기화 {depth}단을 표준 셀 rst_sync(2단)로 옮김. "
                                                             "리셋 해제 시점이 원본과 다를 수 있음")
        return out, [p for p in procs if p not in drop]

    # ------------------------------------------------------------------ inout
    def _tri_assigns(self, body, children, mod: str, top: bool) -> dict[str, dict]:
        """inout 포트 → {style, o, oe, i, domain, oe_type, ...}. 원본의 `assign P = c ? x : 'z`와 `assign y = P`,
        하위 인스턴스의 나눈 inout 포트로 정한다. 하위 모듈은 tri_split(나눈 포트 이름)을 남긴다."""
        self.tri_skip, self.top_mod, self._tri_sigs = set(), top, set()
        pads = [p for p in body.portList if p.kind == SK.Port and p.direction == pyslang.ArgumentDirection.InOut]
        if not pads:
            return {}
        out: dict[str, dict] = {}
        names_ = {p.name for p in pads}
        for m in self._members(body):
            if m.kind != SK.ContinuousAssign:
                continue
            a = m.assignment
            lhs = strip_conv(a.left)
            rhs = a.right
            while rhs.kind == EK.Conversion:
                rhs = rhs.operand
            if lhs.kind == EK.NamedValue and lhs.symbol.name in names_ and rhs.kind == EK.ConditionalOp:
                hiz = self.d.text(strip_conv(rhs.right).syntax)
                if not re.search(r"'[sS]?[bBoOdDhH]?\s*[zZ?_]+$|^'[zZ]$", hiz):
                    continue
                o_txt = self.d.text(strip_conv(rhs.left).syntax)
                oe_txt = self.d.text(strip_conv(rhs.conditions[0].expr).syntax)
                zero = re.fullmatch(r"(\d*'[sS]?[bBoOdDhH]\s*0+|0|'0)", o_txt.replace("_", "")) is not None
                o_sym = strip_conv(rhs.left)
                oe_sym = strip_conv(rhs.conditions[0].expr)
                out[lhs.symbol.name] = {"style": "open_drain" if zero else "push_pull", "o_text": o_txt, "oe_text": oe_txt,
                                        "o": o_sym.symbol.name if o_sym.kind == EK.NamedValue else None,
                                        "oe": oe_sym.symbol.name if oe_sym.kind == EK.NamedValue else None,
                                        "oe_type": "logic" if rhs.conditions[0].expr.type.bitWidth == 1 else
                                        f"logic [{rhs.conditions[0].expr.type.bitWidth - 1}:0]",
                                        "domain": None, "assign": m}
                self.tri_skip.add(loc_key(m))
        for m in self._members(body):
            if m.kind == SK.ContinuousAssign:
                a = m.assignment
                r = strip_conv(a.right)
                if r.kind == EK.NamedValue and r.symbol.name in out and strip_conv(a.left).kind == EK.NamedValue:
                    out[r.symbol.name].setdefault("readers", []).append(strip_conv(a.left).symbol.name)
                    if top:
                        self.tri_skip.add(loc_key(m))
        # 하위 인스턴스로 내려가는 inout (원본은 3상태 assign이 하위 모듈 안에 있음)
        down: dict[str, list] = {}
        for iname, c in children:
            cm = self.mods.get(c.definition.name)
            for pc in c.portConnections:
                if cm and pc.port.name in cm.tri_split and pc.expression is not None:
                    e = strip_conv(pc.expression)
                    if e.kind == EK.NamedValue:
                        down.setdefault(e.symbol.name, []).append((iname, cm, pc.port.name))
                    else:
                        self._reject(mod, self._loc(c), "tristate_expr", f"{iname}.{pc.port.name}(inout)에 이름 하나가 아닌 것이 연결됨")
        for net, users in down.items():
            if len(users) > 1 or net in out:
                self._reject(mod, None, "tristate_bus", f"3상태 넷 {net}을 여러 곳이 구동함 (하위 모듈 사이의 3상태 버스): "
                                                        f"{[u[0] for u in users] + (['assign'] if net in out else [])}")
                continue
            if net not in names_:
                self._reject(mod, None, "tristate_bus", f"하위 모듈의 inout이 포트가 아닌 넷 {net}에 연결됨 (하위 모듈 사이의 3상태 버스)")
                continue
            iname, cm, cport = users[0]
            sp = cm.tri_split[cport]
            out[net] = {"style": sp["style"], "o": None, "oe": None, "oe_type": sp["oe_type"], "domain": None,
                        "down": (iname, cm, cport)}
        for p in pads:
            if p.name not in out:
                self._reject(mod, self._loc(p), "inout", f"inout 포트 {p.name}의 3상태 구동(assign {p.name} = c ? x : 'z)을 찾지 못함")
        for pad, info in out.items():
            if top:
                for k in ("o", "oe", "i"):
                    if k == "i":
                        rd = info.get("readers", [])
                        info["i"], info["i_new"] = (rd[0], False) if len(rd) == 1 and not info.get("down") else (f"{pad}_i", True)
                        if len(rd) > 1:
                            self._review(mod, None, "tristate_readers", f"패드 {pad}를 여러 곳에서 읽음 ({rd}). {pad}_i 하나로 읽음")
                    elif k == "o" and info["style"] == "open_drain":
                        continue
                    elif info.get(k) is None or info.get("down"):
                        info[k], info[f"{k}_new"] = f"{pad}_{k}", True
                        if not info.get("down") and info.get(f"{k}_text"):
                            self._reject(mod, None, "tristate_expr", f"패드 {pad}의 {k}가 이름 하나가 아닌 식: {info[f'{k}_text']}")
                    self._tri_sigs.add(info[k])
            else:
                self.cur.tri_split[pad] = {"style": info["style"], "o": f"{pad}_o" if info["style"] == "push_pull" else None,
                                           "oe": f"{pad}_oe", "i": f"{pad}_i", "oe_type": info["oe_type"],
                                           "orig_o": info.get("o_text"), "orig_oe": info.get("oe_text"),
                                           "readers": info.get("readers", []), "down": info["down"][0] if info.get("down") else None}
        return out

    def _tri_conns(self, iname: str, sp: dict, pc, mod: str, cid) -> list[dict]:
        """하위 인스턴스의 나눈 inout 포트를 부모에 잇는다 (부모가 top이면 tristate 신호, 아니면 부모의 나눈 포트)."""
        net = strip_conv(pc.expression).symbol.name if pc.expression is not None else None
        info = self.tri.get(net)
        if info is None:
            return []
        out = []
        for k, d in (("o", "out"), ("oe", "out"), ("i", "in")):
            child_p = sp.get(k)
            if not child_p:
                continue
            parent = info[k] if self.top_mod else self.cur.tri_split[net][k]
            frm, to = (f"{iname}.{child_p}", f"self.{parent}") if d == "out" else (f"self.{parent}", f"{iname}.{child_p}")
            out.append({"id": cid(f"c_{iname}_{child_p}"), "from": frm, "to": to})
        return out

    def _tri_type(self, body, pad: str, mod: str) -> str:
        p = next(x for x in body.portList if x.name == pad)
        return self._port_type(p, body, mod)

    def _tri_signal_names(self) -> set[str]:
        return set(getattr(self, "_tri_sigs", set()))

    def _members(self, scope):
        for m in scope:
            if m.kind == SK.GenerateBlock and not m.isUninstantiated:
                yield from self._members(m)
            elif m.kind == SK.GenerateBlockArray:
                for e in m:
                    if e.kind == SK.GenerateBlock and not e.isUninstantiated:
                        yield from self._members(e)
            else:
                yield m

    def _misc(self, scope, mod: str) -> None:
        for m in scope:
            if m.kind == SK.Port and m.direction == pyslang.ArgumentDirection.Ref:
                self._reject(mod, self._loc(m), "ref_port", f"ref 포트 {m.name}")
            elif m.kind == SK.InterfacePort:
                self._reject(mod, self._loc(m), "interface", f"SV interface 포트 {m.name}")
            elif m.kind == SK.Port and not m.type.isIntegral:
                self._reject(mod, self._loc(m), "type_port", f"정수형이 아닌 포트 {m.name}: {m.type}")
            elif m.kind == SK.Port and (m.type.isStruct if hasattr(m.type, "isStruct") else False):
                self._reject(mod, self._loc(m), "struct_port", f"struct 포트 {m.name}")
            elif m.kind == SK.Port and (m.type.isUnpackedUnion or m.type.isPackedUnion):
                self._reject(mod, self._loc(m), "union_port", f"union 포트 {m.name}")
            elif m.kind == SK.DefParam if hasattr(SK, "DefParam") else False:
                self._review(mod, self._loc(m), "defparam", f"defparam {self.d.text(m.syntax)} (인스턴스 파라미터 대입으로 옮김)")
            elif m.kind == SK.Subroutine and m.subroutineKind == pyslang.SubroutineKind.Task and has_delay(m.body):
                self._reject(mod, self._loc(m), "delay_task", f"지연이 있는 task {m.name}")
            elif m.kind == SK.GenerateBlock and not m.isUninstantiated:
                self._misc(m, mod)
            elif m.kind == SK.GenerateBlockArray:
                for e in m:
                    if e.kind == SK.GenerateBlock and not e.isUninstantiated:
                        self._misc(e, mod)

    # ------------------------------------------------------------------ 포트 도메인
    def _port_domains(self, body, procs, dom_of, children, child_maps, dnames, derived, mod) -> dict[str, str | None]:
        seqw: dict[str, set] = {}
        seqr: dict[str, set] = {}
        comb_deps: dict[str, set] = {}
        comb_fwd: dict[str, set] = {}
        for p in procs:
            if p.kind == "seq":
                d = dom_of(p)
                for w in p.writes:
                    seqw.setdefault(w, set()).add(d)
                for r in p.reads:
                    seqr.setdefault(r, set()).add(d)
            else:
                for w in p.writes:
                    comb_deps.setdefault(w, set()).update(p.reads)
                for r in p.reads:
                    comb_fwd.setdefault(r, set()).update(p.writes)
        child_out: dict[str, set] = {}
        child_in: dict[str, set] = {}
        for iname, c in children:
            cm = self.mods.get(c.definition.name)
            if not cm:
                continue
            if not cm.ir.get("domains"):   # 조합 하위 모듈은 입력 넷 → 출력 넷 조합 경로
                ins, outs = set(), set()
                for pc in c.portConnections:
                    if pc.expression is not None:
                        (outs if pc.port.direction == pyslang.ArgumentDirection.Out else ins).update(names(strip_conv(pc.expression)))
                for w in outs:
                    comb_deps.setdefault(w, set()).update(ins)
                for r in ins:
                    comb_fwd.setdefault(r, set()).update(outs)
                continue
            for pc in c.portConnections:
                if pc.expression is None:
                    continue
                cd = cm.port_domains.get(pc.port.name, "-")
                if cd == "-":
                    continue
                key = child_maps.get(iname, {}).get(cd) if cd else None
                pd = dnames.get(key) if key else None
                nets = names(strip_conv(pc.expression))
                if pc.port.direction == pyslang.ArgumentDirection.Out:
                    for n in nets:
                        child_out.setdefault(n, set()).add(pd)
                else:
                    for n in nets:
                        child_in.setdefault(n, set()).add(pd)

        def producers(n, seen):
            if n in seen:
                return set()
            seen.add(n)
            out = set(seqw.get(n, set())) | child_out.get(n, set())
            for r in comb_deps.get(n, set()):
                out |= producers(r, seen)
            return out

        def consumers(n, seen):
            if n in seen:
                return set()
            seen.add(n)
            out = set(seqr.get(n, set())) | child_in.get(n, set())
            for w in comb_fwd.get(n, set()):
                out |= consumers(w, seen)
            return out

        doms = sorted(set(dnames.values()))
        pdom: dict[str, str | None] = {}
        self._undecided: set[str] = set()
        for p in body.portList:
            if p.kind != SK.Port or p.name in derived:
                continue
            if p.direction == pyslang.ArgumentDirection.InOut:
                ds = (producers(p.name, set()) | consumers(p.name, set())) - {None}
                info = self.tri.get(p.name, {})
                for k in ("o", "oe"):
                    if info.get(k) and not info.get("down"):
                        ds |= producers(info[k], set()) - {None}
                for r in info.get("readers", []):
                    ds |= consumers(r, set()) - {None}
                if info.get("down"):
                    iname, cm, cport = info["down"]
                    key = child_maps.get(iname, {}).get(cm.port_domains.get(cport))
                    if key in dnames:
                        ds.add(dnames[key])
            elif p.direction == pyslang.ArgumentDirection.In:
                ds = consumers(p.name, set()) - {None}
            else:
                ds = producers(p.name, set()) - {None}
                if not ds:
                    ins = {r for r in self._comb_inputs(p.name, comb_deps)}
                    for i in ins:
                        ds |= consumers(i, set()) - {None}
            if len(ds) == 1:
                pdom[p.name] = next(iter(ds))
            elif not ds and len(doms) == 1:
                pdom[p.name] = doms[0]
            elif not ds:
                pdom[p.name] = None
                if doms:   # 조합 모듈은 도메인이 없는 것이 정상
                    self._undecided.add(p.name)
            else:
                pdom[p.name] = sorted(ds)[0]
                self._review(mod, self._loc(p), "port_domain",
                             f"포트 {p.name}을 여러 도메인이 씀: {', '.join(sorted(ds))} ({pdom[p.name]}로 둠. 비동기라면 R2에서 고치세요)")
        return pdom

    def _comb_inputs(self, n, comb_deps, seen=None) -> set[str]:
        seen = seen or set()
        if n in seen:
            return set()
        seen.add(n)
        out = set()
        for r in comb_deps.get(n, set()):
            out.add(r)
            out |= self._comb_inputs(r, comb_deps, seen)
        return out

    # ------------------------------------------------------------------ 파라미터와 포트
    def _expr(self, node, scope, mod: str) -> str:
        """원문 식. 숫자 매크로와 package/$unit 상수는 이름을 남기고 localparam으로 옮기고, 다른 매크로는 풀어 쓴다."""
        text = self.d.text(node)
        return self._fix_text(text, node, scope, mod)

    def _fix_text(self, text: str, node, scope, mod: str) -> str:
        used = re.findall(r"`([A-Za-z_][A-Za-z0-9_$]*)", text)
        if used:
            if all(u in self.d.macros and self.d.macros[u].numeric for u in used):
                for u in used:
                    self._need_macro(u)
                text = re.sub(r"`([A-Za-z_][A-Za-z0-9_$]*)", r"\1", text)
            else:
                text = str(node).strip()
        for pkg, member in PKG_RE.findall(text):
            self._need_const(f"{pkg}::{member}", member, scope)
        text = PKG_RE.sub(r"\2", text)
        for ident in set(IDENT_RE.findall(text)):
            if ident in self._local_names:
                continue
            try:
                sym = scope.lookupName(ident)
            except Exception:
                sym = None
            if sym is not None and sym.kind == SK.Parameter and outside_module(sym):
                self._need_const(ident, ident, scope, sym)
        return " ".join(text.split())

    def _need_macro(self, name: str) -> None:
        self._macros_used.setdefault(name, None)
        m = self.d.macros[name]
        for u in re.findall(r"`([A-Za-z_][A-Za-z0-9_$]*)", m.value):
            if u in self.d.macros and u not in self._macros_used:
                self._need_macro(u)

    def _need_const(self, ref: str, member: str, scope, sym=None) -> None:
        if member in self._consts_used:
            return
        if sym is None:
            try:
                sym = scope.lookupName(ref)
            except Exception:
                sym = None
        val = None
        if sym is not None and sym.kind == SK.Parameter:
            val = const_text(sym.value)
        self._consts_used[member] = val

    def _macro_localparams(self, mod: str) -> list[dict]:
        out = []
        for name in self._macros_used:
            m = self.d.macros[name]
            out.append({"name": name, "type": "int", "default": re.sub(r"`([A-Za-z_][A-Za-z0-9_$]*)", r"\1", m.value),
                        "local": True, "_macro": True})
        for name, val in self._consts_used.items():
            if val is None:
                self._reject(mod, None, "const", f"package/$unit 상수 {name}의 값을 정할 수 없음")
                continue
            out.append({"name": name, "type": "int", "default": val, "local": True, "_macro": True})
        return out

    def _params(self, inst, body, top: bool, mod: str) -> list[dict]:
        self._macros_used: dict[str, None] = {}
        self._consts_used: dict[str, str | None] = {}
        self._local_names = {p.name for p in body.parameters} | {m.name for m in body if m.kind == SK.Parameter}
        used = self._used_names(body)
        self._scan_body_consts(inst, body, mod)
        self._removed_params: dict[str, str] = {}
        out = []
        for m in self._param_syms(body):
            ptype = self._param_type(m, body, mod)
            if top and m.name in self.d.inp.params:
                default = str(self.d.inp.params[m.name])
            elif not m.isLocalParam:
                # 고정한 설정의 값 (하위 모듈은 인스턴스에서 elaborate한 값. 펼친 generate 구조와 맞도록)
                default = const_text(m.value)
            else:
                init = getattr(m.syntax, "initializer", None) if m.syntax is not None else None
                init = init.expr if init is not None else None
                default = self._expr(init, body, mod) if init is not None else const_text(m.value)
            entry = {"name": m.name, "type": ptype, "default": default}
            if m.isLocalParam:
                entry["local"] = True
            if m.name not in used and not m.isLocalParam:
                self._review(mod, self._loc(m), "unused_param",
                             f"파라미터 {m.name}은 고른 구조에서 쓰이지 않음 (generate 조건이나 고르지 않은 갈래에서만 쓰였음). "
                             f"고정 값 {default}로 갈래를 고르고 지움")
                self._removed_params[m.name] = default   # 원본을 빌드할 때는 이 값을 준다 (r4.ref_params)
                continue
            if m.name not in used and m.isLocalParam:
                continue
            out.append(entry)
        return out

    def _scan_body_consts(self, inst, body, mod: str) -> None:
        """모듈 원문 전체(동작 포함)에서 쓰는 숫자 매크로와 package/$unit 상수도 localparam으로 옮긴다
        (Porter가 조각에 `DW`, `K`처럼 이름으로 쓸 수 있게)."""
        text = self.d.module_source(inst.definition)["text"]
        for u in set(re.findall(r"`([A-Za-z_][A-Za-z0-9_$]*)", text)):
            if u in self.d.macros and self.d.macros[u].numeric:
                self._need_macro(u)
        for pkg, member in set(PKG_RE.findall(text)):
            self._need_const(f"{pkg}::{member}", member, body)
        for ident in set(IDENT_RE.findall(text)) - self._local_names:
            try:
                sym = body.lookupName(ident)
            except Exception:
                sym = None
            if sym is not None and sym.kind == SK.Parameter and outside_module(sym):
                self._need_const(ident, ident, body, sym)

    def _param_syms(self, body):
        seen = set()
        for m in body:
            if m.kind == SK.Parameter and m.name not in seen:
                seen.add(m.name)
                yield m

    def _param_type(self, m, body, mod: str) -> str:
        ts = m.declaredType.typeSyntax
        t = self.d.text(ts) if ts is not None else ""
        t = re.sub(r"\b(parameter|localparam)\b", "", t).strip()
        if not t or t in ("integer", "int"):
            return "int"
        if t.startswith("["):
            return "logic " + self._fix_text(t, ts, body, mod)
        return self._fix_text(t, ts, body, mod)

    def _used_names(self, body) -> set[str]:
        """고른 구조(포트, 선언, 블록, 인스턴스)의 원문에 나오는 이름. 파라미터 초기식은 쓰이는 파라미터의 것만 (닫힘)."""
        texts: list[str] = []
        pinit: dict[str, str] = {}

        def walk(scope):
            for m in scope:
                if m.kind == SK.Parameter:
                    init = m.declaredType.initializerSyntax
                    pinit[m.name] = self.d.text(init) if init is not None else ""
                    continue
                if m.kind == SK.GenerateBlock:
                    if not m.isUninstantiated:
                        walk(m)
                    continue
                if m.kind == SK.GenerateBlockArray:
                    for e in m:
                        if e.kind == SK.GenerateBlock and not e.isUninstantiated:
                            walk(e)
                    continue
                if m.kind == SK.Port:
                    isym = m.internalSymbol
                    if isym is not None and isym.declaredType is not None and isym.declaredType.typeSyntax is not None:
                        texts.append(self.d.text(isym.declaredType.typeSyntax))
                    continue
                if m.kind == SK.Instance:
                    try:
                        texts.append(self.d.text(m.syntax.parent.parameters))
                    except AttributeError:
                        pass
                    texts.append(self.d.text(m.syntax))
                    continue
                syn = getattr(m, "syntax", None)
                if syn is not None:
                    if m.kind in (SK.Net, SK.Variable) and syn.parent is not None:
                        texts.append(self.d.text(syn.parent))
                    else:
                        texts.append(self.d.text(syn))
        walk(body)
        used = set()
        for t in texts:
            used |= set(IDENT_RE.findall(t))
        frontier = [n for n in used if n in pinit]
        while frontier:
            n = frontier.pop()
            for x in IDENT_RE.findall(pinit.get(n, "")):
                if x not in used:
                    used.add(x)
                    if x in pinit:
                        frontier.append(x)
        return used

    def _port_type(self, p, body, mod: str) -> str:
        isym = p.internalSymbol
        ts = isym.declaredType.typeSyntax if isym is not None else None
        t = self.d.text(ts) if ts is not None else ""
        t = re.sub(r"\b(wire|reg|logic|var|tri|input|output|inout|bit)\b", "", t).strip()
        if t.startswith("integer"):
            return "logic signed [31:0]"
        if re.match(r"^(signed|unsigned)?\s*(\[.*)?$", t):
            sign = re.match(r"^(signed|unsigned)?", t).group(1)
            dims = t[len(sign or ""):].strip()
            # 원문이 비어 있으면 (non-ANSI 선언이 다른 줄에 있을 때) elaborate한 타입의 폭을 쓴다
            if not dims and p.type.bitWidth > 1 and not t:
                return f"logic [{p.type.bitWidth - 1}:0]"
            dims = self._fix_text(dims, ts, body, mod) if dims else ""
            return " ".join(x for x in ("logic", sign, dims) if x)
        self._reject(mod, self._loc(p), "type_port", f"포트 {p.name}의 타입을 IR로 옮길 수 없음: {t}")
        return "logic"

    def _ports(self, body, pdom, derived, mod: str) -> list[dict]:
        plist = [p for p in body.portList if p.kind == SK.Port and p.name not in derived]
        flat = []
        for p in plist:
            d = "input" if p.direction == pyslang.ArgumentDirection.In else "output"
            t = self._port_type(p, body, mod)
            if p.direction == pyslang.ArgumentDirection.InOut:
                info = self.tri.get(p.name)
                if info is None:
                    continue   # _tri_assigns가 거부함
                if self.top_mod:
                    flat.append({"name": p.name, "dir": "inout", "type": t, "domain": info["domain"] or pdom.get(p.name)})
                    continue
                sp = self.cur.tri_split[p.name]
                dom = info["domain"] or pdom.get(p.name)
                if sp["style"] == "push_pull":
                    flat.append({"name": sp["o"], "dir": "output", "type": t, "domain": dom})
                flat.append({"name": sp["oe"], "dir": "output", "type": info["oe_type"], "domain": dom})
                flat.append({"name": sp["i"], "dir": "input", "type": t, "domain": dom})
                continue
            flat.append({"name": p.name, "dir": d, "type": t, "domain": pdom.get(p.name)})
        # 번들 후보: <base>_[t]valid / <base>_[t]ready
        names_ = {p["name"]: p for p in flat}
        bundles: dict[str, dict] = {}
        for p in flat:
            mm = re.match(r"^(.+?)_t?valid$", p["name"])
            if not mm:
                continue
            base = mm.group(1)
            ready = next((names_[n] for n in (f"{base}_tready", f"{base}_ready") if n in names_), None)
            if not ready or ready["dir"] == p["dir"]:
                continue
            members = [q for q in flat if q["name"].startswith(base + "_")]
            known = {q["domain"] for q in members if q["name"] not in self._undecided}
            if len(known) == 1:   # 쓰이지 않아 도메인을 못 정한 신호는 번들의 도메인을 따른다
                for q in members:
                    if q["name"] in self._undecided:
                        q["domain"] = next(iter(known))
                        self._undecided.discard(q["name"])
            if len({q["domain"] for q in members}) != 1 or any(q["name"] in {s for b in bundles.values() for s in b["names"]} for q in members):
                self._review(mod, None, "bundle", f"번들 후보 {base}의 신호 도메인이 섞여 있어 묶지 않음")
                continue
            bundles[base] = {"names": [q["name"] for q in members], "role": "source" if p["dir"] == "output" else "sink",
                             "domain": p["domain"]}
        for n in sorted(self._undecided):
            self._review(mod, None, "port_domain", f"포트 {n}의 도메인을 정할 수 없음 (쓰이지 않거나 조합 경로뿐. 비동기 포트로 둠)")
        out, placed = [], set()
        for p in flat:
            b = next((k for k, v in bundles.items() if p["name"] in v["names"]), None)
            if b is None:
                out.append(p)
            elif b not in placed:
                placed.add(b)
                v = bundles[b]
                out.append({"bundle": b, "protocol": "valid_ready", "role": v["role"], "domain": v["domain"],
                            "signals": [{k: q[k] for k in ("name", "dir", "type")} for q in flat if q["name"] in v["names"]]})
        return out

    # ------------------------------------------------------------------ 인스턴스와 연결
    def _net(self, inst, port: str) -> str | None:
        """하위 인스턴스 포트에 연결된 부모 넷 이름 (이름 하나일 때만)."""
        try:
            pc = inst.getPortConnection(inst.body.findPort(port))
        except Exception:
            pc = None
        if pc is None or pc.expression is None:
            return None
        e = strip_conv(pc.expression)
        return e.symbol.name if e.kind == EK.NamedValue else None

    def _connections(self, body, children, child_maps, dnames, input_ports, mod: str):
        instances, conns, signals, pending = [], [], [], []
        parent_ports = {p.name: p for p in body.portList if p.kind == SK.Port}
        # 내부 넷: 누가 구동하고 누가 읽는지
        net_drivers: dict[str, list[str]] = {}
        net_readers: dict[str, list[str]] = {}
        for iname, c in children:
            for pc in c.portConnections:
                if pc.expression is None:
                    continue
                e = strip_conv(pc.expression)
                if e.kind != EK.NamedValue:
                    continue
                n = e.symbol.name
                (net_drivers if pc.port.direction == pyslang.ArgumentDirection.Out else net_readers).setdefault(n, []).append(f"{iname}.{pc.port.name}")
        logic_reads: set[str] = set()
        for p in self._procs(body, mod):
            logic_reads |= p.reads
        for info in self.tri.values():   # top의 tristate가 읽는 o, oe (원본의 3상태 assign)
            logic_reads |= {info[k] for k in ("o", "oe") if info.get(k)}
        ids = set()

        def cid(base: str) -> str:
            n, i = re.sub(r"[^A-Za-z0-9_]", "_", base), 2
            k = n
            while k in ids:
                k, i = f"{n}_{i}", i + 1
            ids.add(k)
            return k

        for iname, c in children:
            cm = self.mods.get(c.definition.name)
            if not cm:
                continue
            entry = {"name": iname, "module": c.definition.name}
            params = self._inst_params(c, body, mod)
            if params:
                entry["params"] = params
            dm = {cd: dnames[key] for cd, key in child_maps.get(iname, {}).items() if key in dnames}
            entry["domain_map"] = dm
            instances.append(entry)
            cdoms = cm.ir.get("domains", [])
            child_derived = {d["clock"] for d in cdoms} | {d["reset"]["port"] for d in cdoms if d.get("reset")} | \
                            {x["port"] for d in cdoms if d.get("reset") for x in d["reset"].get("extra", [])}
            cren = cm.renames
            for pc in c.portConnections:
                pname = pc.port.name
                if pname in child_derived or cren.get(pname) in child_derived:
                    continue
                pname = cren.get(pname, pname)   # 하위 모듈에서 바꾼 이름 (SV 예약어)
                if pname in cm.tri_split:
                    conns += self._tri_conns(iname, cm.tri_split[pname], pc, mod, cid)
                    continue
                out_dir = pc.port.direction == pyslang.ArgumentDirection.Out
                e = pc.expression
                if e is None:
                    if not out_dir:
                        pending.append({"instance": iname, "port": pname, "dir": "input", "expr": None,
                                        "note": "연결되지 않은 입력 (원본은 z/x)"})
                        self._review(mod, self._loc(c), "unconnected_input", f"{iname}.{pname} 입력이 연결되지 않음")
                    continue
                se = strip_conv(e)
                conv = se is not None and se.type.bitWidth != pc.port.type.bitWidth
                if se.kind == EK.NamedValue and not conv:
                    n = se.symbol.name
                    if n in parent_ports:
                        frm, to = (f"{iname}.{pname}", f"self.{n}") if out_dir else (f"self.{n}", f"{iname}.{pname}")
                        conns.append({"id": cid(f"c_{iname}_{pname}"), "from": frm, "to": to})
                        continue
                    drivers, readers = net_drivers.get(n, []), net_readers.get(n, [])
                    if out_dir:
                        if len(drivers) == 1 and n not in logic_reads and readers:
                            for r in readers:
                                conns.append({"id": cid(f"c_{iname}_{pname}_{r.split('.')[0]}"), "from": f"{iname}.{pname}", "to": r})
                            continue
                        if not readers and n not in logic_reads:
                            continue  # 아무도 읽지 않는 출력
                        self._signal(signals, n, body, mod)
                        conns.append({"id": cid(f"c_{iname}_{pname}"), "from": f"{iname}.{pname}", "to": f"self.{n}"})
                        continue
                    if len(drivers) == 1 and n not in logic_reads:
                        continue  # 위의 출력 쪽에서 inst→inst로 이음
                    self._signal(signals, n, body, mod)
                    conns.append({"id": cid(f"c_{iname}_{pname}"), "from": f"self.{n}", "to": f"{iname}.{pname}"})
                    continue
                pe = e.left if e.kind == EK.Assignment else e
                pending.append({"instance": iname, "port": pname, "dir": "output" if out_dir else "input",
                                "expr": self.d.text(pe.syntax) if pe.syntax is not None else str(pe),
                                "note": "폭이 다른 연결" if conv else "이름 하나가 아닌 연결 (슬라이스, 연접, 상수, 식)"})
        return instances, conns, signals, pending

    def _signal(self, signals: list[dict], n: str, body, mod: str) -> None:
        if any(s["name"] == n for s in signals):
            return
        sym = body.find(n)
        t = "logic"
        if sym is not None and sym.declaredType is not None and sym.declaredType.typeSyntax is not None:
            raw = self.d.text(sym.declaredType.typeSyntax)
            raw = re.sub(r"\b(wire|reg|logic|var|tri)\b", "", raw).strip()
            t = " ".join(x for x in ("logic", self._fix_text(raw, sym.declaredType.typeSyntax, body, mod)) if x)
        elif sym is not None and sym.type.bitWidth > 1:
            t = f"logic [{sym.type.bitWidth - 1}:0]"
        signals.append({"name": n, "kind": "wire", "type": t})

    def _inst_params(self, c, body, mod: str) -> dict[str, str]:
        out: dict[str, str] = {}
        try:
            pv = c.syntax.parent.parameters
        except AttributeError:
            pv = None
        child_params = [p for p in c.body.parameters if not p.isLocalParam]
        if pv is None:
            return self._defparams(c, out)
        genvars = self._genvars(c)
        for i, pa in enumerate(pv.parameters):
            if pa.kind == pyslang.SyntaxKind.NamedParamAssignment:
                pname = pa.name.valueText
                expr = pa.expr
            else:
                if i >= len(child_params):
                    continue
                pname = child_params[i].name
                expr = pa.expr
            if expr is None:
                continue
            text = self._expr(expr, body, mod)
            if set(IDENT_RE.findall(text)) & genvars:
                sym = next((p for p in c.body.parameters if p.name == pname), None)
                text = const_text(sym.value) if sym is not None else text
            out[pname] = text
        return self._defparams(c, out)

    def _defparams(self, c, out: dict[str, str]) -> dict[str, str]:
        """defparam으로 바뀐 파라미터 (인스턴스 문법에 없음): elaborate한 값으로 대입한다."""
        cm = self.mods.get(c.definition.name)
        defaults = {p["name"]: p["default"] for p in (cm.ir.get("params", []) if cm else [])}
        for p in c.body.parameters:
            if not p.isLocalParam and p.name not in out and (p.isOverridden or const_text(p.value) != defaults.get(p.name)):
                out[p.name] = const_text(p.value)
        return out

    def _genvars(self, c) -> set[str]:
        return set(next((v for (sc, n, _), v in self.genvars.items() if n == c.name and v), frozenset()))

    # ------------------------------------------------------------------ 이름
    def _apply_renames(self, mod: Mod) -> None:
        ir = mod.ir
        ren = {}
        for p in ir["ports"]:
            for s in (p["signals"] if "signals" in p else [p]):
                if s["name"] in SV_KEYWORDS:
                    ren[s["name"]] = s["name"] + "_"
            if "bundle" in p and p["bundle"] in SV_KEYWORDS:
                ren[p["bundle"]] = p["bundle"] + "_"
        for p in ir["params"]:
            if p["name"] in SV_KEYWORDS:
                ren[p["name"]] = p["name"] + "_"
        for i in ir.get("instances", []):
            if i["name"] in SV_KEYWORDS:
                ren[i["name"]] = i["name"] + "_"
        for s in ir.get("signals", []):
            if s["name"] in SV_KEYWORDS:
                ren[s["name"]] = s["name"] + "_"
        for d in ir["domains"]:
            for n in (d["clock"], *([d["reset"]["port"]] if d.get("reset") else [])):
                if n in SV_KEYWORDS:
                    ren[n] = n + "_"
        if not ren:
            return
        mod.renames = ren
        sub = lambda t: IDENT_RE.sub(lambda m: ren.get(m.group(0), m.group(0)), t)
        # 타입 글자의 키워드(logic 등)는 그대로 두고 대괄호 안의 식만 바꾼다
        sub_type = lambda t: re.sub(r"\[[^\]]*\]", lambda m: sub(m.group(0)), t)

        def ident(n: str) -> str:
            return ren.get(n, n)
        for p in ir["ports"]:
            if "bundle" in p:
                p["bundle"] = ident(p["bundle"])
            for x in (p["signals"] if "bundle" in p else [p]):
                x["name"] = ident(x["name"])
                x["type"] = sub_type(x["type"])
        for p in ir["params"]:
            p["name"] = ident(p["name"])
            p["default"] = sub(p["default"])
            p["type"] = sub_type(p["type"])
        for d in ir["domains"]:
            d["clock"] = ident(d["clock"])
            if d.get("reset"):
                d["reset"]["port"] = ident(d["reset"]["port"])
                for x in d["reset"].get("extra", []):
                    x["port"] = ident(x["port"])
        for i in ir.get("instances", []):
            i["name"] = ident(i["name"])
            i["params"] = {k: sub(v) for k, v in i.get("params", {}).items()} if "params" in i else i.get("params")
            if i.get("params") is None:
                i.pop("params", None)
        for sg in ir.get("signals", []):
            sg["name"] = ident(sg["name"])
            sg["type"] = sub_type(sg["type"])
        for c in ir.get("connections", []):
            for k in ("from", "to"):
                a, b = c[k].split(".", 1)
                c[k] = f"{a if a == 'self' else ident(a)}.{ident(b) if a == 'self' else b}"
        for b in ir.get("blocks", []):
            for k in ("pad", "o", "oe", "i"):
                if k in b:
                    b[k] = ident(b[k])
        self._review(mod.name, (mod.source["file"], mod.source["line"]), "rename",
                     "SV 예약어인 이름을 바꿈: " + ", ".join(f"{a} → {b}" for a, b in ren.items()))


# ---------------------------------------------------------------------- 도우미
def scope_name(scope) -> str:
    try:
        return scope.containingInstance.definition.name
    except AttributeError:
        return getattr(scope, "name", "?")


def loc_key(sym) -> tuple[int, int]:
    """심볼의 안정한 키 (pyslang은 순회할 때마다 새 파이썬 객체를 줌)."""
    return (sym.location.buffer.id, sym.location.offset)


def outside_module(sym) -> bool:
    """모듈 밖($unit, package)에서 선언한 심볼인지."""
    try:
        return sym.parentScope.containingInstance is None
    except Exception:
        return False


def sym_name(expr) -> str | None:
    e = strip_conv(expr)
    if e.kind == EK.NamedValue:
        return e.symbol.name
    try:
        s = e.getSymbolReference()
        return s.name if s is not None else None
    except Exception:
        return None


def strip_conv(e):
    """변환을 벗긴다. 출력 포트 연결(넷에 대입하는 꼴)은 왼쪽 넷."""
    if e is not None and e.kind == EK.Assignment:
        e = e.left
    while e is not None and e.kind == EK.Conversion:
        e = e.operand
    return e


def names(node) -> set[str]:
    out: set[str] = set()

    def cb(n):
        if isinstance(n, pyslang.Expression) and n.kind == EK.NamedValue:
            if n.symbol.kind not in (SK.Parameter, SK.EnumValue, SK.Genvar):
                out.add(n.symbol.name)
        return pyslang.VisitAction.Advance
    node.visit(cb)
    return out


def rw(node) -> tuple[set[str], set[str]]:
    """(읽는 이름, 쓰는 이름). 대입 왼쪽의 뿌리는 쓰기, 왼쪽 인덱스 안의 이름과 오른쪽은 읽기."""
    reads: set[str] = set()
    writes: set[str] = set()

    def cb(n):
        if isinstance(n, pyslang.Expression):
            if n.kind == EK.Assignment:
                root = n.left.getSymbolReference()
                if root is not None:
                    writes.add(root.name)
                reads.update(names(n.right))
                reads.update(names(n.left) - ({root.name} if root is not None else set()))
                return pyslang.VisitAction.Skip
            if n.kind == EK.NamedValue and n.symbol.kind not in (SK.Parameter, SK.EnumValue, SK.Genvar):
                reads.add(n.symbol.name)
        return pyslang.VisitAction.Advance
    node.visit(cb)
    return reads, writes


def has_delay(node) -> bool:
    found = []

    def cb(n):
        if isinstance(n, pyslang.Statement) and n.kind == STK.Timed:
            k = n.timing.kind
            if k in (pyslang.TimingControlKind.Delay, pyslang.TimingControlKind.Delay3, pyslang.TimingControlKind.CycleDelay):
                found.append(n)
                return pyslang.VisitAction.Interrupt
        return pyslang.VisitAction.Advance
    if node is not None:
        node.visit(cb)
    return bool(found)


def top_stmts(stmt) -> list:
    """begin/end를 벗긴 맨 위 문장 목록."""
    s = stmt
    while s is not None and s.kind == STK.Block:
        s = s.body
    if s is None:
        return []
    if s.kind == STK.List:
        return [x for x in s.list if x.kind != STK.Empty]
    return [s]


def first_stmt(stmt):
    """begin/end와 문장 목록을 벗기고 첫 문장. 문장이 여럿이면 첫 문장이 if일 때만 그것 (나머지가 있으면 None)."""
    s = stmt
    while s is not None:
        if s.kind == STK.Block:
            s = s.body
        elif s.kind == STK.List:
            items = [x for x in s.list if x.kind != STK.Empty]
            if len(items) != 1:
                return None
            s = items[0]
        else:
            return s if s.kind == STK.Conditional else None
    return None


def cond_signal(stmt) -> tuple[str, str] | None:
    """if 조건이 신호 하나(`x`, `!x`, `~x`, `x == 0/1`, `x != 0/1`)면 (이름, 활성 극성)."""
    if stmt is None or stmt.kind != STK.Conditional or len(stmt.conditions) != 1:
        return None
    e = strip_conv(stmt.conditions[0].expr)
    neg = False
    while e.kind == EK.UnaryOp and e.op in (pyslang.UnaryOperator.LogicalNot, pyslang.UnaryOperator.BitwiseNot):
        neg = not neg
        e = strip_conv(e.operand)
    if e.kind == EK.BinaryOp and e.op in (pyslang.BinaryOperator.Equality, pyslang.BinaryOperator.Inequality,
                                          pyslang.BinaryOperator.CaseEquality, pyslang.BinaryOperator.CaseInequality):
        l, r = strip_conv(e.left), strip_conv(e.right)
        if l.kind != EK.NamedValue:
            l, r = r, l
        if l.kind != EK.NamedValue or r.constant is None:
            return None
        try:
            v = int(str(r.constant).split("'")[-1].lstrip("bdhoBDHO") or "0", 0)
        except ValueError:
            return None
        eq = e.op in (pyslang.BinaryOperator.Equality, pyslang.BinaryOperator.CaseEquality)
        high = (v == 1) == eq
        return (l.symbol.name, "high" if high != neg else "low")
    if e.kind == EK.NamedValue and e.type.bitWidth == 1:
        return (e.symbol.name, "low" if neg else "high")
    return None


def const_only(stmt) -> bool:
    """대입의 오른쪽이 모두 상수(파라미터, 리터럴)인지."""
    ok = [True]

    def cb(n):
        if isinstance(n, pyslang.Expression) and n.kind == EK.Assignment:
            if names(n.right):
                ok[0] = False
                return pyslang.VisitAction.Interrupt
            return pyslang.VisitAction.Skip
        if isinstance(n, pyslang.Statement) and n.kind in (STK.Conditional, STK.Case, STK.ForLoop):
            pass
        return pyslang.VisitAction.Advance
    stmt.visit(cb)
    return ok[0]


def const_text(v) -> str:
    """ConstantValue → SV 리터럴 글자. 정수는 10진수."""
    s = str(v)
    m = re.match(r"^(\d+)'s?([bdho])([0-9a-fA-FxXzZ_]+)$", s)
    if m:
        width, base, digits = m.groups()
        if re.search(r"[xXzZ]", digits):
            return s
        n = int(digits.replace("_", ""), {"b": 2, "d": 10, "h": 16, "o": 8}[base])
        return str(n) if int(width) >= 32 else f"{width}'d{n}"
    return s
