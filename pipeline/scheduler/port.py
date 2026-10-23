"""P11 실행기: 원본 RTL → 이식 IR(원본과 1:1, 사이클 등가) → SPEC → SPEC 충실도 검사 → golden 묶음.

    R1 구조 추출 → R2 사용자 확정(여기서 멈추고 답을 기다림) → 모듈마다 아래에서 위로 [R3 Porter → R4 원본과 사이클 비교
    (다르면 Porter 고치기, 상한 porter_fix)] → R5 SpecWriter(SPEC 게이트, 2~4절 왕복) → R6 [SPEC만으로 일반 파이프라인 실행 →
    재생성 RTL과 원본의 트랜잭션 비교 → 다르면 차이를 줄여 Arbiter 판정 → spec_gap이면 SpecWriter가 SPEC을 고치고 다시
    (상한 spec_revise), regen_defect면 검토 항목] → 묶음(`package/`)

상태는 `state.json`에 저장하고, 끝난 단계와 모듈은 다시 실행할 때 건너뛴다.

    python3 -m pipeline.scheduler.port start <파일...> --top <모듈> [-f 목록] [-I dir] [-D N=V] [-G N=V] [--lang verilog|sv] --run-dir <dir>
    python3 -m pipeline.scheduler.port answer --run-dir <dir> <항목=값>... [--file answers.json]
    python3 -m pipeline.scheduler.port resume --run-dir <dir> [--roles config/roles.json] [--until r4|r5|r6] [--no-isolate]
    python3 -m pipeline.scheduler.port status --run-dir <dir>
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

from pipeline.agents import arbiter, porter, roles as roles_mod, spec_writer
from pipeline.agents.task import ROOT, QuotaExceeded, run_task
from pipeline.importer import r1 as r1_mod, r2 as r2_mod, r4, r6, spec_tables
from pipeline.importer.sources import Inputs
from pipeline.scheduler.run import STUB_CELLS, Run

STAGES = ("r1", "r2", "r3", "r5", "r6", "package", "done")
RESUMABLE = ("waiting_approval", "paused_quota", "budget_exceeded")   # 재생성 실행을 같은 디렉터리에서 이어 갈 수 있는 상태


class Budget(RuntimeError):
    pass


class PortRun:
    def __init__(self, run_dir: Path, roles: roles_mod.Roles | None = None, cells: list[Path] | None = None,
                 isolate: bool = True, lang: str = "sv", regen_fn=None, r4_kw: dict | None = None, r6_kw: dict | None = None):
        self.regen_fn = regen_fn or self._regen   # (spec, regen_dir) → (상태, 재생성 IR, Verifier 테스트, 에이전트 실행 수)
        self.r4_kw, self.r6_kw = r4_kw or {}, r6_kw or {}   # 시험에서 시드와 사이클을 줄일 때
        self.dir = Path(run_dir).resolve()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.roles, self.cells, self.isolate, self.lang = roles, list(cells or STUB_CELLS), isolate, lang
        p = self.dir / "state.json"
        self.state = json.loads(p.read_text(encoding="utf-8")) if p.exists() else \
            {"stage": "r1", "status": "new", "agent_runs": 0, "modules": {}, "review": [], "events": [], "r6": {"rounds": []}}

    # ------------------------------------------------------------------ 기록
    def save(self) -> None:
        (self.dir / "state.json").write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")

    def event(self, kind: str, msg: str) -> None:
        self.state["events"].append({"t": round(time.time(), 1), "kind": kind, "message": msg})
        self.save()

    def review(self, code: str, msg: str, where: str = "") -> None:
        item = {"code": code, "message": msg, "where": where}
        if item not in self.state["review"]:
            self.state["review"].append(item)
        self.save()

    def r1(self) -> dict:
        return json.loads((self.dir / "r1" / "import.json").read_text(encoding="utf-8"))

    def irs(self, sub: str) -> dict[str, dict]:
        d = self.dir / sub
        return {p.name.removesuffix(".ir.json"): json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("*.ir.json"))}

    def limit(self, k: str, default: int) -> int:
        return (self.roles.limits.get(k, default) if self.roles else default)

    # ------------------------------------------------------------------ 에이전트
    def agent(self, role: str, task, check, name: str):
        if self.roles is None:
            raise RuntimeError("역할 설정이 없음 (--roles)")
        if self.state["agent_runs"] >= self.limit("budget_agent_runs", 10 ** 9):
            raise Budget(f"에이전트 실행 예산 {self.limit('budget_agent_runs', 0)}회를 다 씀")
        cfg = self.roles.agents.get(role) or self.roles.agents.get({"porter": "author", "spec_writer": "verifier"}.get(role, role))
        task.isolate = self.isolate
        work = self.dir / "tasks" / name
        self.state["agent_runs"] += 1
        self.save()
        res = run_task(cfg, task, work, check=check, retries=self.limit("agent_retries", 1), run_root=self.dir / "tasks")
        why = ""
        if not res.ok:   # 실패 원인을 남긴다 (2026-10-20 axis_async_fifo judge_2가 3.6초 만에 실패했는데 원인이 남지 않음)
            last = res.runs[-1] if res.runs else {}
            why = " " + "; ".join([*res.errors[:2], (last.get("stderr_tail") or last.get("stdout_tail") or "").strip()[-300:]]).strip("; ")
        self.event("agent", f"{name}: {'ok' if res.ok else 'fail'} ({res.attempts}회, {res.duration_s}s){why}")
        return res

    # ------------------------------------------------------------------ R1, R2
    def start(self, inp: Inputs) -> dict:
        res = r1_mod.run(inp, self.dir)
        self.state["inputs"] = inp.to_dict()
        if res["rejects"] or not res["ok"]:
            self.state.update(stage="r1", status="rejected")
            for r in res["rejects"]:
                self.review("R1_REJECT", r["message"], f"{r.get('file')}:{r.get('line')}")
            for n, errs in res.get("validate", {}).items():
                for e in errs:
                    self.review("R1_VALIDATE", e, n)
            self.save()
            return self.status()
        irs = self.irs("r1/ir")
        req = r2_mod.make_request(res, irs)
        (self.dir / "r2_request.json").write_text(json.dumps(req, ensure_ascii=False, indent=1), encoding="utf-8")
        self.state.update(stage="r2", status="waiting_user")
        self.save()
        return self.status()

    def answer(self, answers: dict[str, str]) -> dict:
        p = self.dir / "r2_request.json"
        req = json.loads(p.read_text(encoding="utf-8"))
        errs = r2_mod.set_answers(req, answers)
        p.write_text(json.dumps(req, ensure_ascii=False, indent=1), encoding="utf-8")
        left = [i for i in req["items"] if i["answer"] in (None, "") and i["kind"] != "freq"]
        out = {"errors": errs, "unanswered": [i["id"] for i in left]}
        if errs or left:
            return out
        irs, aerrs = r2_mod.apply(req, self.r1(), self.irs("r1/ir"))
        out["errors"] = aerrs
        if aerrs:
            if any(e.startswith("사용자가 멈춤") for e in aerrs):
                self.state.update(status="stopped")
                self.save()
            return out
        d = self.dir / "r2" / "ir"
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        for n, ir in irs.items():
            (d / f"{n}.ir.json").write_text(json.dumps(ir, ensure_ascii=False, indent=1), encoding="utf-8")
        self.state.update(stage="r3", status="ready")
        self.save()
        out["stage"] = "r3"
        return out

    # ------------------------------------------------------------------ R3, R4
    def step_port(self) -> bool:
        r1 = self.r1()
        drafts = self.irs("r2/ir")
        (self.dir / "ported").mkdir(exist_ok=True)
        ref_dir = porter.make_ref_dir(r1, self.dir / "ref")
        for name in r1["order"]:
            st = self.state["modules"].setdefault(name, {"status": "todo", "attempts": 0})
            if st["status"] in ("equal", "reset_only"):
                continue
            ported = self.irs("ported")
            lib = {c: ported[c] for c in _children(drafts[name]) if c in ported}
            missing = [c for c in _children(drafts[name]) if c not in ported]
            if missing:
                st.update(status="blocked", reason=f"하위 모듈이 아직 이식되지 않음: {missing}")
                self.save()
                return False
            if not self._port_module(name, r1, drafts[name], lib, ref_dir, st):
                return False
        return True

    def ref_dir(self, r1: dict) -> Path:
        """원본 시뮬레이션 묶음 (Porter의 `porteq`, SpecWriter의 `refsim`이 읽음)."""
        d = self.dir / "ref"
        return d if (d / "ref.json").exists() else porter.make_ref_dir(r1, d)

    def _port_module(self, name: str, r1: dict, draft: dict, lib: dict, ref_dir: Path, st: dict) -> bool:
        meta = r1["modules"][name]
        notes = {"pending": meta["pending"], "renames": meta["renames"], "macro_params": meta["macro_params"],
                 "reviews": [x for x in r1["reviews"] if x.get("module") == name]}
        src = self.dir / "r1" / "source" / f"{name}.sv"
        headers = sorted((self.dir / "r1" / "source" / "headers").glob("*")) if (self.dir / "r1" / "source" / "headers").exists() else []
        prev, mismatch = None, None
        limit = self.limit("porter_fix", 3)
        while st["attempts"] <= limit:
            k = st["attempts"]
            task = porter.make_task(f"porter/{name}/a{k}", draft, src, headers, notes, lib, ref_dir, self.lang, prev, mismatch)
            res = self.agent("porter", task, porter.check(draft, lib, self.lang), f"porter/{name}/a{k}")
            st["attempts"] += 1
            if not res.ok:
                st.update(status="porter_failed", errors=res.errors[:10])
                self.review("PORTER_FAILED", f"{name}: Porter 게이트를 통과하지 못함: {res.errors[:3]}", name)
                self.save()
                return False
            ir = json.loads(Path(res.outputs[porter.out_rel(name)]).read_text(encoding="utf-8"))
            ported = {**self.irs("ported"), name: ir}
            rr = r4.run_module(name, ported, r1, self.dir / "r4", self.cells, **self.r4_kw)
            st["r4"] = {k2: rr.get(k2) for k2 in ("status", "first", "txn", "mode", "seed", "error")}
            if rr["status"] in ("equal", "reset_only"):
                (self.dir / "ported" / f"{name}.ir.json").write_text(json.dumps(ir, ensure_ascii=False, indent=1), encoding="utf-8")
                st["status"] = rr["status"]
                if rr["status"] == "reset_only":
                    self.review("R4_RESET_ONLY", f"{name}: 리셋 구간과 직후 {r4.RESET_WINDOW}사이클에서만 원본과 다름 "
                                                 f"(첫 차이 {rr.get('first')}). 원본의 리셋 없는 플롭 때문인지 확인", name)
                self.save()
                return True
            prev, mismatch = ir, st["r4"]
            st["status"] = "differ"
            self.save()
        self.review("R4_DIFFER", f"{name}: Porter 고치기 {limit}회 뒤에도 원본과 다름: {st.get('r4')}", name)
        st["status"] = "failed"
        self.save()
        return False

    # ------------------------------------------------------------------ R5
    def _spec_notes(self, r1: dict) -> dict:
        reset_only = {n: s.get("r4") for n, s in self.state["modules"].items() if s.get("status") == "reset_only"}
        consts = {}
        for n, m in r1["modules"].items():
            ir = self.irs("ported").get(n, {})
            for p in ir.get("params", []):
                if p["name"] in m["macro_params"]:
                    consts[p["name"]] = p["default"]
        return {"fixed_params": r1["inputs"]["params"], "defines": r1["inputs"]["defines"], "header_constants": consts,
                "removed_params": [x["message"] for x in r1["reviews"] if x["kind"] == "unused_param"],
                "reviews": r1["reviews"], "r4_reset_only": reset_only}

    def step_spec(self) -> bool:
        if (self.dir / "spec.md").exists() and self.state.get("spec_ok"):
            return True
        r1 = self.r1()
        ported = self.irs("ported")
        top = ported[r1["top"]]
        tables = spec_tables.tables_md(top)
        (self.dir / "spec_tables.md").write_text(tables, encoding="utf-8")
        sources = {f"{n}.sv": self.dir / "r1" / "source" / f"{n}.sv" for n in r1["order"]}
        hd = self.dir / "r1" / "source" / "headers"
        if hd.exists():
            sources["headers"] = hd
        task = spec_writer.make_task("spec_writer/v0", r1["top"], tables, sources, ported, self._spec_notes(r1),
                                     ref_dir=self.ref_dir(r1))
        res = self.agent("spec_writer", task, spec_writer.check(top), "spec_writer/v0")
        if not res.ok:
            self.review("SPEC_WRITER_FAILED", f"SPEC이 게이트를 통과하지 못함: {res.errors[:3]}")
            return False
        shutil.copy(res.outputs[spec_writer.OUT], self.dir / "spec.md")
        shutil.copy(res.outputs[spec_writer.OUT], self.dir / "spec_v0.md")
        self.state["spec_ok"] = True
        self.state["spec_version"] = 0
        self.save()
        return True

    # ------------------------------------------------------------------ R6
    def step_r6(self) -> bool | None:
        """True: 끝남 (같음 또는 regen_defect 판정), False: 실패, None: 재생성 실행이 사람 결정을 기다림."""
        r1 = self.r1()
        ported = self.irs("ported")
        top = ported[r1["top"]]
        rounds = self.state["r6"]["rounds"]
        limit = self.limit("spec_revise", 3)
        while True:
            # 재생성 실행이 사람 결정(기댓값 수정 승인, Repair 충돌)이나 한도 때문에 멈췄으면 같은 실행을 이어 간다
            if rounds and rounds[-1]["regen_status"] in RESUMABLE:
                rounds.pop()
            spec = self.dir / "spec.md"
            last = rounds[-1] if rounds else None
            if last and (last.get("verdict") or {}).get("verdict") == "spec_gap" \
                    and last.get("spec_version") == self.state.get("spec_version", 0):
                # 앞 실행이 spec_gap 판정 뒤 SPEC을 고치기 전에 멈춤 (고치기 상한, SpecWriter 실패): 다시 만들지 않고 고치기부터
                k = last["round"]
                judge = json.loads((self.dir / "r6" / f"judge_{k}.json").read_text(encoding="utf-8"))
                if not self._revise_spec(last, judge, last["verdict"], limit):
                    return False
                continue
            if last and ((last["regen_status"] != "pass" and not last.get("handled"))
                         or (last.get("judge_pending") and not last.get("verdict"))):
                # 앞 실행에서 재생성 뒤 판정 전에 멈춤 (실패, 예산, 한도): 재생성을 다시 돌리지 않고 그 결과로 이어 감
                rnd = last
                k = rnd["round"]
                status = rnd["regen_status"]
                regen = r6.load_regen(self.dir / "r6" / f"regen_{k}") if status == "pass" else {}
                tbs = rnd.get("tbs") or []
            else:
                k = len(rounds)
                regen_dir = self.dir / "r6" / f"regen_{k}"
                status, regen, tbs, n_runs = self.regen_fn(spec, regen_dir)
                self.state["agent_runs"] += n_runs
                rnd = {"round": k, "spec_version": self.state.get("spec_version", 0), "regen_status": status, "tbs": tbs}
                rounds.append(rnd)
                self.save()
            regen_dir = self.dir / "r6" / f"regen_{k}"
            if status in RESUMABLE:
                self.review("R6_REGEN_WAITING", f"SPEC으로 다시 만들기가 멈춤 ({status}). 결정은 "
                                                f"`python3 -m pipeline.scheduler.approve {regen_dir} list`로 보고 내린 뒤 resume")
                return None
            res = {"status": "not_run"}
            if status == "pass":
                res = r6.run(top, regen, r1, self.dir / "r6" / f"cmp_{k}", self.cells, **self.r6_kw)
                rnd.update(compare=res["status"], timing=res.get("timing"), structure=res.get("structure"))
            if tbs and "verifier_on_ref" not in rnd:
                rnd["verifier_on_ref"] = r6.verifier_on_ref(tbs, top, r1, self.dir / "r6" / f"vref_{k}")
            self.save()
            failed_ref = [v for v in rnd.get("verifier_on_ref", []) if not v["ok"]]
            if status != "pass" and not failed_ref:
                # 재생성이 끝나지 않았는데 그 테스트가 원본에서 통과하면 SPEC이 아니라 재생성 쪽 문제
                self.review("R6_REGEN_FAILED", f"SPEC으로 다시 만들기가 끝나지 않음 ({status}). {regen_dir}/report.json 참고")
                return False
            if res["status"] == "equal" and not failed_ref:
                return True
            if res["status"] == "error":
                self.review("R6_ERROR", f"R6 비교 시뮬레이션 실패: {res.get('error', '')[:300]}")
                return False
            rnd["handled"] = True
            # 재생성이 실패했어도, SPEC만 보고 쓴 Verifier 테스트가 원본에서 실패하면 SPEC이 원본과 어긋난 것이므로 판정으로 넘긴다
            judge = r6.judge_input(res) if res["status"] == "differ" else \
                {"verifier_failures_on_original": failed_ref, "regen_status": status,
                 "note": "SPEC만 보고 쓴 Verifier 테스트가 원본 RTL(기준)에서 실패함. 테스트의 기댓값이 SPEC 해석에서 나왔으므로, "
                         "SPEC이 원본 동작을 정하지 않았거나 원본과 다르게 정했는지 본다"}
            (self.dir / "r6" / f"judge_{k}.json").write_text(json.dumps(judge, ensure_ascii=False, indent=1), encoding="utf-8")
            rnd["judge_pending"] = True
            self.save()
            v = self.agent("arbiter", arbiter.make_r6_task(f"r6/judge_{k}", spec, judge), arbiter.check_r6(spec), f"r6/judge_{k}")
            verdict = json.loads(Path(v.workdir, arbiter.OUT).read_text(encoding="utf-8")) if v.ok else None
            rnd["verdict"] = verdict
            self.save()
            if verdict is None:
                self.review("R6_JUDGE_FAILED", "R6 판정을 받지 못함")
                return False
            if verdict["verdict"] == "regen_defect":
                self.review("R6_REGEN_DEFECT", f"SPEC은 원본 동작을 정했는데 재생성 설계가 어김 ({verdict.get('reqs')}): {verdict['reason']}")
                return True
            if not self._revise_spec(rnd, judge, verdict, limit):
                return False

    def _revise_spec(self, rnd: dict, judge: dict, verdict: dict, limit: int) -> bool:
        """한 회차의 spec_gap 판정으로 SPEC을 고친다. 상한에 걸리거나 SpecWriter가 실패하면 False.
        원본에서 실패한 Verifier 테스트의 자극을 SpecWriter에 넘겨, refsim으로 원본에 다시 넣어 보게 한다."""
        k = rnd["round"]
        stims = {Path(f["stim"]).name: Path(f["stim"]) for v in rnd.get("verifier_on_ref", []) for f in v["findings"]
                 if f.get("stim") and Path(f["stim"]).exists()}
        r1 = self.r1()
        ported = self.irs("ported")
        top = ported[r1["top"]]
        if k >= limit:
            self.review("R6_SPEC_GAP", f"SPEC 고치기 {limit}회 뒤에도 차이가 남음: {verdict['reason']}")
            return False
        prev = self.dir / f"spec_v{self.state.get('spec_version', 0)}.md"
        task = spec_writer.make_task(f"spec_writer/v{k + 1}", r1["top"], spec_tables.tables_md(top),
                                     {f"{n}.sv": self.dir / "r1" / "source" / f"{n}.sv" for n in r1["order"]}, ported,
                                     self._spec_notes(r1), prev_spec=prev, r6={**judge, "verdict": verdict},
                                     ref_dir=self.ref_dir(r1), stims=stims)
        res2 = self.agent("spec_writer", task, spec_writer.check(top, prev.read_text(encoding="utf-8")), f"spec_writer/v{k + 1}")
        if not res2.ok:
            self.review("SPEC_WRITER_FAILED", f"R6 SPEC 고치기 실패: {res2.errors[:3]}")
            return False
        self.state["spec_version"] = k + 1
        shutil.copy(res2.outputs[spec_writer.OUT], self.dir / f"spec_v{k + 1}.md")
        shutil.copy(res2.outputs[spec_writer.OUT], self.dir / "spec.md")
        self.save()
        return True

    def _regen(self, spec: Path, regen_dir: Path):
        """SPEC만 입력으로 일반 파이프라인을 돌린다. Architect의 하위 모듈 수는 원본 모듈 수(top 빼고, 적어도 1)를 넘지 않게 한다:
        원본이 모듈 하나면 재생성도 하위 모듈 하나. 실제 실행(axis_cobs_decode, 2026-10-17)에서 작은 블록을 둘로 나눌 때마다
        모듈 사이 계약이 어긋나 재구성이 필요했고, 하나로 둔 회차만 통과했다."""
        run = Run(regen_dir, spec, self.roles, self.cells, isolate=self.isolate, max_modules=max(1, len(self.r1()["order"]) - 1))
        rep = run.execute()
        regen = r6.load_regen(regen_dir) if rep["status"] == "pass" else {}
        return rep["status"], regen, run.state["steps"].get("verifiers") or [], rep.get("agent_runs", 0)

    # ------------------------------------------------------------------ 묶음
    def step_package(self) -> None:
        from pipeline.ir.model import Module
        from pipeline.tb.gen import harness_source

        r1 = self.r1()
        pkg = self.dir / "package"
        if pkg.exists():
            shutil.rmtree(pkg)
        (pkg / "ir").mkdir(parents=True)
        ported = self.irs("ported")
        for n, ir in ported.items():
            (pkg / "ir" / f"{n}.ir.json").write_text(json.dumps(ir, ensure_ascii=False, indent=1), encoding="utf-8")
        if (self.dir / "spec.md").exists():
            shutil.copy(self.dir / "spec.md", pkg / "spec.md")
        (pkg / "harness").mkdir()
        top = ported[r1["top"]]
        (pkg / "harness" / f"{r1['top']}_harness.py").write_text(harness_source(Module(top)), encoding="utf-8")
        golden = {"ir": [f"ir/{n}.ir.json" for n in r1["order"]], "top": r1["top"],
                  "upstream_rtl": r1["units"], "upstream_headers": r1["headers"], "upstream_incdirs": r1["inputs"]["incdirs"],
                  "upstream_defines": r1["inputs"]["defines"], "upstream_wrapped": r1.get("wrapped", []),
                  "upstream_params": {r1["top"]: r1["inputs"]["params"]},
                  "r4": {n: s.get("status") for n, s in self.state["modules"].items()},
                  "r6": [{k: x.get(k) for k in ("round", "spec_version", "regen_status", "compare", "verdict")} for x in self.state["r6"]["rounds"]]}
        (pkg / "golden.json").write_text(json.dumps(golden, ensure_ascii=False, indent=1), encoding="utf-8")

    # ------------------------------------------------------------------ 진행
    def resume(self, until: str | None = None) -> dict:
        try:
            if self.state["stage"] in ("r1", "r2"):
                return self.status()
            if self.state["stage"] == "r3":
                self.state["status"] = "running"
                if not self.step_port():
                    self.state["status"] = "failed"
                    return self.status()
                self.state["stage"] = "r5"
                self.save()
            if until == "r4":
                self.state["status"] = "paused"
                return self.status()
            if self.state["stage"] == "r5":
                if not self.step_spec():
                    self.state["status"] = "failed"
                    return self.status()
                self.state["stage"] = "r6"
                self.save()
            if until == "r5":
                self.state["status"] = "paused"
                return self.status()
            if self.state["stage"] == "r6":
                done = self.step_r6()
                if done is None:
                    self.state["status"] = "waiting_regen"
                    return self.status()
                if not done:
                    self.state["status"] = "failed"
                    self.step_package()
                    return self.status()
                self.state["stage"] = "package"
                self.save()
            if self.state["stage"] == "package":
                self.step_package()
                self.state.update(stage="done", status="pass")
        except QuotaExceeded as e:
            self.state["status"] = "paused_quota"
            self.event("quota", str(e))
        except Budget as e:
            self.state["status"] = "budget_exceeded"
            self.review("BUDGET", str(e))
        self.save()
        return self.status()

    def status(self) -> dict:
        out = {k: self.state.get(k) for k in ("stage", "status", "agent_runs")}
        out["modules"] = {n: {k: s.get(k) for k in ("status", "attempts")} for n, s in self.state["modules"].items()}
        out["review"] = self.state["review"]
        p = self.dir / "r2_request.json"
        if self.state["stage"] == "r2" and p.exists():
            req = json.loads(p.read_text(encoding="utf-8"))
            out["questions"] = [{k: i.get(k) for k in ("id", "question", "options", "answer")} for i in r2_mod.unanswered(req)]
        self.save()
        return out


def _children(ir: dict) -> list[str]:
    return list(dict.fromkeys(i["module"] for i in ir.get("instances", [])))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="P11 원본 RTL에서 IR과 SPEC 만들기")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("start")
    p.add_argument("files", nargs="*", type=Path)
    p.add_argument("--top", required=True)
    p.add_argument("-f", dest="filelists", action="append", type=Path, default=[])
    p.add_argument("-I", dest="incdirs", action="append", type=Path, default=[])
    p.add_argument("-D", dest="defines", action="append", default=[])
    p.add_argument("-G", dest="params", action="append", default=[])
    p.add_argument("--lang", choices=["verilog", "sv"])
    p.add_argument("--run-dir", required=True, type=Path)
    p = sub.add_parser("answer")
    p.add_argument("--run-dir", required=True, type=Path)
    p.add_argument("pairs", nargs="*")
    p.add_argument("--file", type=Path)
    p = sub.add_parser("resume")
    p.add_argument("--run-dir", required=True, type=Path)
    p.add_argument("--roles", default=str(ROOT / "config" / "roles.json"))
    p.add_argument("--until", choices=["r4", "r5", "r6"])
    p.add_argument("--porter-lang", choices=["sv", "py"], default="sv")
    p.add_argument("--no-isolate", action="store_true", help="격리하지 않음 (가짜 agent를 쓰는 시험 전용)")
    p = sub.add_parser("status")
    p.add_argument("--run-dir", required=True, type=Path)
    a = ap.parse_args(argv)
    if a.cmd == "start":
        argv2 = [str(f) for f in a.files] + ["--top", a.top, "-o", str(a.run_dir)]
        for fl in a.filelists:
            argv2 += ["-f", str(fl)]
        for d in a.incdirs:
            argv2 += ["-I", str(d)]
        for d in a.defines:
            argv2 += ["-D", d]
        for g in a.params:
            argv2 += ["-G", g]
        if a.lang:
            argv2 += ["--lang", a.lang]
        inp, _ = r1_mod.parse_args(argv2)
        out = PortRun(a.run_dir).start(inp)
    elif a.cmd == "answer":
        ans = dict(x.split("=", 1) for x in a.pairs)
        if a.file:
            ans.update(json.loads(a.file.read_text(encoding="utf-8")))
        out = PortRun(a.run_dir).answer(ans)
    elif a.cmd == "resume":
        out = PortRun(a.run_dir, roles_mod.load(a.roles), isolate=not a.no_isolate, lang=a.porter_lang).resume(a.until)
    else:
        out = PortRun(a.run_dir).status()
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
