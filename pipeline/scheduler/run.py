"""파이프라인 스케줄러. 제어 흐름은 결정론적이고, 에이전트는 작업 단위로 headless 실행한다.

    SPEC 검사 → Block Top L1 → Architect(모듈 트리, 동결) → Verifier 후보(참조 모델, 테스트)와 차분 검사
    → 모듈마다 Author 후보(후보마다 조각 언어 sv/py) → 같은 번호 후보끼리 묶어
       [Validator → 방출 → E1 → (py 모듈) 번역기 검사 → E2(모든 Verifier의 테스트)]
       번역기 검사 실패나 골격(조각 밖) lint 에러: 생성기 버그이므로 Repair 없이 후보를 멈추고 검토 항목에 남김
       실패: 분류 → 원인 후보 모듈(불일치 출력을 구동하는 인스턴스)의 Repair(patch) → Validator부터 다시
             (상한, IR 해시 진동 감지. Repair가 형제 모듈을 지목하면 그 모듈의 Repair로 넘김)
             Repair들이 원인을 서로 넘기면(충돌) Design-lead 결정 대기: 고칠 모듈 지정(그 모듈의 Repair만, 형제 지목 금지)
             또는 Architect 재구성(충돌 경위로 모듈 트리를 다시 정하고 Author부터 다시. Block Top L1과 Verifier 테스트는 그대로)
             lead가 지정한 Repair 뒤 실패한 테스트가 늘면 그 Repair를 되돌리고 lead 결정을 다시 받음
             일부 Verifier에서만 실패 → Arbiter (rtl_wrong이면 Repair, test_wrong이면 사람 승인 대기, 모호하면 검토 항목)
             같은 테스트가 Repair 두 번 뒤에도 같은 이유로 실패하면 상한 전에 실패한 Verifier마다 Arbiter가 판정
             Repair가 L1 변경을 요청하면 실패한 Verifier마다 Arbiter가 먼저 판정 (test_wrong이면 기댓값 수정 승인 대기,
             모호하면 검토 항목, rtl_wrong이면 L1 변경 요청을 검토 항목으로 남기고 후보를 멈춤)
    → 묶음 선택(모든 모듈 통과, Repair 적음, lint 적음)과 Author 후보 간 차분 검사 → 통합(E1, E2) → 최종 보고서

상태는 단계마다 `state.json`에 저장한다. 병렬로 도는 후보와 에이전트 작업은 각자 파일(`modules/`, `tasks/`)에 저장한다. 강제로 멈춘 뒤 같은 명령으로 다시 실행하면 끝난 단계와 끝난 에이전트 작업은
건너뛰고 이어서 실행한다. 에이전트 작업 디렉터리는 저장소 밖에 두고 서로 격리한다.

    python3 -m pipeline.scheduler.run <spec.md> --run-dir <dir> [--roles config/roles.json] [--cells <표준 셀.sv>...]
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pipeline.agents import arbiter, architect, author, repair, roles as roles_mod, verifier
from pipeline.agents.task import RUNS, ROOT, QuotaExceeded, TaskResult, run_task
from pipeline.classifier.classify import classify_findings
from pipeline.emitter.sv import emit
from pipeline.ir.model import Module
from pipeline.patch.apply import apply, ir_hash
from pipeline.spec import check as spec_check
from pipeline.spec.l1 import block_top_l1
from pipeline.spec.parse import load as load_spec
from pipeline.tb.gen import harness_source, pad_top, trace_names
from pipeline.tb.diffsim import compare, describe, episodes, make_plan, truncate, without
from pipeline.tb.report import mismatch_report
from pipeline.validator.checks import errors, validate
from pipeline.validator.structure import feedthrough_paths, feedthrough_rule, frozen_scope, v11_frozen
from pipeline.verify import pyequiv
from pipeline.verify.run import lint, sim

STUB_CELLS = sorted((ROOT / "pipeline" / "emitter" / "stubs").glob("*.sv"))   # 임시 표준 셀 (동기화, 리셋 동기화, 비동기 FIFO)


class BudgetExceeded(RuntimeError):
    pass


class Run:
    def __init__(self, run_dir: Path, spec: Path, roles: roles_mod.Roles, cells: list[Path] | None = None,
                 isolate: bool = True, seeds: list[int] | None = None, seed: dict | None = None, max_modules: int | None = None):
        """seed: 이전 실행에서 이어 가는 재실행(환류). {"parent": 실행 디렉터리, "kind": "external_repair" | "spec_change",
        "failures": {모듈: 분류된 외부 리포트 항목}, "old_spec": 이전 SPEC, "spec_diff": spec_diff.diff 결과}.
        max_modules: Architect의 하위 모듈 수 상한 (P11 R6 재생성). 상태에 남아 이어 가기에서도 같다."""
        self.dir, self.spec_path, self.roles = Path(run_dir).resolve(), Path(spec).resolve(), roles
        self.cells, self.isolate, self.seeds = cells if cells is not None else STUB_CELLS, isolate, seeds or [1]
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "modules").mkdir(exist_ok=True)
        (self.dir / "tasks").mkdir(exist_ok=True)
        self.lock = threading.Lock()
        self.state_path = self.dir / "state.json"
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.event("resume", "저장된 상태에서 이어서 실행")
        else:
            self.state = {"spec": str(self.spec_path), "status": "running", "steps": {}, "review": [], "events": [],
                          "agent_runs": 0}
            if seed:
                self.state["seed"] = seed
            if max_modules:
                self.state["max_modules"] = max_modules

    # -- 기록 --
    @staticmethod
    def _write(path: Path, obj) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)  # 중간에 멈춰도 상태 파일이 깨지지 않게

    def save(self) -> None:
        with self.lock:
            self._write(self.state_path, self.state)

    def event(self, kind: str, msg: str) -> None:
        with self.lock:
            self.state["events"].append({"t": time.strftime("%H:%M:%S"), "kind": kind, "msg": msg})

    def review(self, code: str, msg: str, where: str = "", source: str = "scheduler", pending: str | None = None) -> None:
        item = {"code": code, "severity": "review", "where": where, "message": msg, "source": source,
                **({"pending": pending} if pending else {})}
        with self.lock:
            if item not in self.state["review"]:
                self.state["review"].append(item)

    def roles_changed(self) -> bool:
        """역할 설정(역할별 모델과 계열)을 처음 보면 True. 같은 설정의 경고(MODEL_FAMILY)를 실행마다 올리지 않으려고
        본 설정을 저장소 밖(`roles_seen.json`)에 기록한다 (P8 회고)."""
        seen_path = Path(os.environ.get("CHIPDEPT_ROLES_SEEN") or RUNS.parent / "roles_seen.json")
        fp = hashlib.sha256(json.dumps({r: [a.model, a.family] for r, a in sorted(self.roles.agents.items())},
                                       sort_keys=True).encode()).hexdigest()[:16]
        try:
            seen = json.loads(seen_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            seen = []
        if fp in seen:
            self.event("roles", f"역할 설정 {fp}는 전에 본 것이라 MODEL_FAMILY 검토 항목을 올리지 않음")
            return False
        seen_path.parent.mkdir(parents=True, exist_ok=True)
        seen_path.write_text(json.dumps(seen + [fp]), encoding="utf-8")
        return True

    def close_review(self, p: dict) -> None:
        """사람 결정이 반영되면 그 항목을 기다리던 검토 항목에 결과(결정, 사람, 시각)를 적는다 (P8 회고: "승인 대기"로 남던 문제)."""
        d = p.get("decision") or {}
        res = d.get("result") or d.get("decision")
        tag = f" → {res}{' ' + d['module'] if d.get('module') else ''} ({d.get('by') or '-'}, {d.get('at') or p.get('applied')})"
        with self.lock:
            for r in self.state["review"]:
                if r.get("pending") == p["id"] and "decided" not in r:
                    r["message"] = r["message"].replace(" (사람 승인 대기)", "").replace(f" {p['id']} 대기", f" {p['id']}") + tag
                    r["decided"] = {"decision": res, "by": d.get("by"), "at": d.get("at") or p.get("applied")}

    def mod_state(self, key: str) -> dict:
        p = self.dir / "modules" / f"{key.replace('/', '__')}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else \
            {"status": "author", "repairs": 0, "hashes": [], "history": []}

    def save_mod(self, key: str, st: dict) -> None:
        self._write(self.dir / "modules" / f"{key.replace('/', '__')}.json", st)

    def modules(self) -> dict[str, dict]:
        return {p.stem.replace("__", "/"): json.loads(p.read_text(encoding="utf-8"))
                for p in sorted((self.dir / "modules").glob("*.json"))}

    def arch_tag(self, sep: str = "/") -> str:
        """Architect 재구성 뒤의 에이전트 작업과 검증 디렉터리 이름 표지 (이전 트리의 끝난 작업과 섞이지 않게)."""
        rev = self.state.get("arch_rev", 0)
        return f"{sep}a{rev}" if rev else ""

    def done(self, step: str):
        return self.state["steps"].get(step)

    def finish_step(self, step: str, result) -> None:
        self.state["steps"][step] = result
        self.save()

    # -- 에이전트 작업 (끝난 작업은 다시 실행하지 않음) --
    def work_dir(self, task_name: str) -> Path:
        """에이전트 작업 디렉터리. 격리하면 저장소 밖(RUNS)에 두고, 실행 디렉터리의 전체 경로로 구분한다. 이름만 쓰면 다른
        실행의 같은 이름 디렉터리(P11 R6의 r6/regen_0 등)와 겹쳐, 동시에 돌면 서로의 작업 디렉터리를 덮는다
        (2026-10-19 golden 3블록 동시 실행에서 발생)."""
        if not self.isolate:
            return self.dir / "work" / task_name.replace("/", "__")
        key = hashlib.sha256(str(self.dir.resolve()).encode()).hexdigest()[:8]
        return RUNS / f"{self.dir.name}-{key}" / task_name.replace("/", "__")

    def agent(self, role: str, task, check) -> TaskResult:
        rec = self.dir / "tasks" / f"{task.name.replace('/', '__')}.json"
        if rec.exists():
            return TaskResult(**json.loads(rec.read_text(encoding="utf-8")))
        with self.lock:
            if self.state["agent_runs"] >= self.roles.limits.get("budget_agent_runs", 10**9):
                raise BudgetExceeded(f"에이전트 실행 예산 {self.roles.limits['budget_agent_runs']}회를 다 씀")
        task.isolate = self.isolate
        work = self.work_dir(task.name)
        res = run_task(self.roles.agents[role], task, work, check=check, retries=self.roles.limits.get("agent_retries", 1),
                       run_root=RUNS if self.isolate else None)
        with self.lock:
            self.state["agent_runs"] += res.attempts
        self._write(rec, res.to_dict())
        self.event("agent", f"{task.name}: {'통과' if res.ok else '실패'} ({res.duration_s}s, {res.attempts}회)")
        self.save()
        return res

    # -- 사람 승인 --
    def add_pending(self, item: dict) -> str:
        """사람 결정을 기다리는 항목을 등록하고 id를 돌려준다. 결정은 `decisions.json`에 따로 쌓인다 (approve CLI)."""
        with self.lock:
            pend = self.state.setdefault("pending", [])
            pid = f"E{len(pend) + 1}"
            pend.append({"id": pid, **item, "created": time.strftime("%Y-%m-%d %H:%M:%S")})
        self.save()
        return pid

    def join_pending(self, item: dict, key: str) -> str | None:
        """같은 Verifier의 같은 테스트가 같은 이유로 다른 후보에서도 실패했으면, 아직 결정하지 않은 그 항목에 이 후보를 붙인다
        (P8 회고: 같은 결정을 후보마다 두 번 받음). 붙였으면 그 항목의 id."""
        decided = {d["id"] for d in load_decisions(self.dir)}
        with self.lock:
            for p in self.state.get("pending", []):
                if p["kind"] == item["kind"] and p.get("group_key") == key and p["id"] not in decided and not p.get("applied"):
                    if p["c"] != item["c"] and all(x["c"] != item["c"] for x in p.get("also", [])):
                        p.setdefault("also", []).append({k: item.get(k) for k in ("c", "mods", "arbiter_dir", "first_failure")})
                    return p["id"]
        return None

    def apply_decisions(self, tree: dict, tbs: list[str]) -> list[str]:
        """사람 결정을 반영한다. 승인: Verifier가 수정안대로 테스트를 고치고, 그 Verifier를 기다리던 후보를 모두 다시 돌린다.
        거절: 그 후보를 다시 돌리되 그 Verifier의 실패는 Arbiter 없이 Repair로 보낸다."""
        decisions = {d["id"]: d for d in load_decisions(self.dir)}
        todo = [p for p in self.state.get("pending", []) if p["id"] in decisions and not p.get("applied")
                and p["kind"] == "expectation_change"]
        if not todo:
            return tbs
        for p in todo:
            p["decision"] = decisions[p["id"]]
        top = tree["top"]
        l1 = {k: top[k] for k in ("module", "role", "params", "domains", "ports") if k in top}
        approved: dict[int, list[dict]] = {}
        for p in todo:
            if p["decision"]["decision"] == "approved":
                approved.setdefault(p["verifier"], []).append(p)
        for k, items in sorted(approved.items()):
            rev = self.state.get("tb_rev", 0) + 1
            before = Path(tbs[k]).read_text(encoding="utf-8")
            res = self.agent("verifier", verifier.make_revise_task(
                f"verifier/v{k}/rev{rev}", l1, self.spec_path, Path(tbs[k]),
                [{x: p.get(x) for x in ("id", "reqs", "reason", "proposal", "first_failure")} | {"note": p["decision"].get("note")}
                 for p in items]), verifier.check(l1, before))
            if not res.ok:
                self.review("VERIFIER_REVISE_FAILED", f"Verifier {k}가 승인된 수정안({[p['id'] for p in items]})을 반영하지 못함: "
                                                      f"{res.errors[:3]}", "verifier")
                for p in items:
                    p["decision"] = {**p["decision"], "result": "revise_failed"}
                continue
            new = res.outputs[verifier.out_rel(l1["module"])]
            self.state.setdefault("tb_history", []).append({"verifier": k, "from": tbs[k], "to": new, "items": [p["id"] for p in items]})
            self.event("tb_revise", f"Verifier {k} 테스트 수정 ({[p['id'] for p in items]})")
            tbs[k] = new
            self.state["tb_rev"] = rev
            self.state["steps"]["verifiers"] = tbs
            # 같은 Verifier를 기다리던 다른 항목은 테스트가 바뀌었으므로 다시 돌려 본다
            for p in self.state.get("pending", []):
                if p.get("verifier") == k and not p.get("applied") and p not in todo:
                    p["decision"] = {"decision": "superseded", "by": f"Verifier {k} 수정 {[x['id'] for x in items]}"}
                    todo.append(p)
        if approved:
            self.verifier_diff_review(tbs, l1, "수정 후 ")
            self.refund_repairs([p["id"] for ps in approved.values() for p in ps])
        for p in todo:
            if p["decision"].get("result") == "revise_failed":
                continue
            for m, c in [(m, p["c"]) for m in p["mods"]] + [(m, x["c"]) for x in p.get("also", []) for m in x["mods"]]:
                key = f"{m}/c{c}"
                st = self.mod_state(key)
                ids = st.get("pending_ids") or [st.get("pending")]
                if st["status"] == "waiting_approval" and p["id"] in ids:
                    if p["decision"]["decision"] == "rejected":
                        st.setdefault("skip_arbiter", []).append(p["verifier"])
                    decided = {q["id"] for q in self.state.get("pending", []) if q.get("decision")}
                    if all(i in decided for i in ids):   # 기다리던 항목이 모두 결정되면 다시 돌린다
                        st["status"] = "running"
                        st.pop("pending_ids", None)
                    self.save_mod(key, st)
            p["applied"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self.close_review(p)
            self.event("decision", f"{p['id']} {p['decision']['decision']} ({p['decision'].get('by')})")
        self.save()
        return tbs

    def refund_repairs(self, ids: list[str]) -> None:
        """승인된 기댓값 수정으로 테스트가 바뀌면, 마지막 테스트 수정 뒤 쓴 Repair 횟수를 모든 후보에 돌려준다. 그 횟수는 틀린 테스트에
        맞추느라 쓴 것일 수 있다 (P8a async_fifo: SV 후보가 테스트가 고쳐지기 전에 상한을 다 써서 다시 해볼 기회가 없었음).
        Repair 상한으로 멈춘 후보는 다시 돌린다. 돌려주는 일은 사람 승인마다 한 번이라 무한히 돌지 않는다."""
        revive = set()
        for f in sorted((self.dir / "modules").glob("*.json")):
            key = f.stem.replace("__", "/")
            st = self.mod_state(key)
            back = st.get("repairs_since_tb", 0)
            if back:
                st["repairs"] = max(0, st["repairs"] - back)
                st.setdefault("refunds", []).append({"items": ids, "repairs": back})
            st["repairs_since_tb"] = 0
            if st["status"] == "failed" and "Repair 상한" in (st.get("reason") or ""):
                revive.add(key.rsplit("/c", 1)[1])
            self.save_mod(key, st)
        for f in sorted((self.dir / "modules").glob("*.json")):
            key = f.stem.replace("__", "/")
            st = self.mod_state(key)
            if key.rsplit("/c", 1)[1] in revive and st["status"] == "failed" and "Repair 상한" in (st.get("reason") or ""):
                st["status"] = "running"
                st.pop("reason", None)
                self.save_mod(key, st)
        if revive:
            self.event("refund", f"테스트 수정 {ids}: Repair 횟수를 돌려주고 상한으로 멈춘 후보 {sorted(revive)}를 다시 돌림")

    def apply_lead_decisions(self, tree: dict) -> dict:
        """Repair 충돌(repair_conflict)에 대한 Design-lead 결정을 반영하고 (바뀌었을 수 있는) 모듈 트리를 돌려준다.
        repair: 그 후보 묶음을 다시 돌리되 Repair는 지정한 모듈에서만 한다 (형제 지목 금지).
        architect: Architect가 충돌 경위를 보고 모듈 트리를 다시 정한다. 성공하면 모든 후보를 Author부터 다시 돌리고
        (이전 후보 상태는 modules_a<판>/에 보관), 아직 결정하지 않은 다른 충돌 항목은 대체됨으로 닫는다."""
        decisions = {d["id"]: d for d in load_decisions(self.dir)}
        todo = [p for p in self.state.get("pending", []) if p["kind"] == "repair_conflict" and p["id"] in decisions
                and not p.get("applied")]
        if not todo:
            return tree
        for p in todo:
            p["decision"] = decisions[p["id"]]
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        arch = [p for p in todo if p["decision"]["decision"] == "architect"]
        new = self.revise_architecture(tree, arch) if arch else None
        if new is not None:
            for p in self.state.get("pending", []):   # 트리가 바뀌어 남은 충돌 항목(결정했든 안 했든)은 의미가 없음
                if p["kind"] == "repair_conflict" and not p.get("applied") and p not in arch:
                    p["decision"] = {**(p.get("decision") or {"decision": "superseded"}), "result": "superseded",
                                     "superseded_by": f"Architect 재구성 {[x['id'] for x in arch]}"}
                    p["applied"] = now
                    self.close_review(p)
            tree = new
        else:
            for p in arch:
                p["decision"] = {**p["decision"], "result": "revise_failed"}
                for m in p["mods"]:
                    st = self.mod_state(f"{m}/c{p['c']}")
                    if st["status"] == "waiting_approval" and st.get("pending") == p["id"]:
                        st.update(status="failed", reason="Architect 재구성 실패")
                        self.save_mod(f"{m}/c{p['c']}", st)
            for p in todo:
                d = p["decision"]
                if d["decision"] != "repair":
                    continue
                for m in p["mods"]:
                    key = f"{m}/c{p['c']}"
                    st = self.mod_state(key)
                    if st["status"] == "waiting_approval" and st.get("pending") == p["id"]:
                        st["status"] = "running"
                        st["lead_repair"] = {"id": p["id"], "module": d["module"], "note": d.get("note"), "by": d.get("by"),
                                             "chain": p["chain"], "claims": [{k: x[k] for k in ("module", "suspects", "reason")}
                                                                             for x in p["claims"]]}
                        self.save_mod(key, st)
        for p in todo:
            p["applied"] = now
            self.close_review(p)
            d = p["decision"]
            self.event("decision", f"{p['id']} {d['decision']}{' ' + d['module'] if d.get('module') else ''} ({d.get('by')})")
        self.save()
        return tree

    def revise_architecture(self, tree: dict, items: list[dict]) -> dict | None:
        """lead의 근본 원인 수정 요청으로 Architect가 모듈 트리를 다시 정한다. Block Top L1은 그대로(게이트)라 Verifier 테스트를 그대로 쓴다."""
        rev = self.state.get("arch_rev", 0) + 1
        top_l1, _ = block_top_l1(load_spec(self.spec_path))
        conflicts = [{k: p.get(k) for k in ("id", "group", "c", "reqs", "first_failure", "spec", "chain")}
                     | {"claims": [{k: x[k] for k in ("module", "suspects", "reason")} for x in p["claims"]],
                        "lead_note": p["decision"].get("note"), "by": p["decision"].get("by")} for p in items]
        mx = self.state.get("max_modules")
        res = self.agent("architect", architect.make_revise_task(f"architect/rev{rev}", top_l1, self.spec_path, tree, conflicts, mx),
                         architect.check(top_l1, mx))
        if not res.ok:
            self.review("ARCHITECT_REVISE_FAILED", f"Architect가 모듈 트리를 다시 정하지 못함 ({[p['id'] for p in items]}): "
                                                   f"{res.errors[:3]}", str(res.workdir), "architect")
            return None
        top_ir, children, assumptions = architect.read_tree(Path(res.workdir), top_l1["module"])
        for a in assumptions:
            if architect.needs_review(a):
                self.review("ASSUMPTION", f"[재구성 {rev}] {_assumption(a)}", "architect", "architect")
        lib = {n: Module(c) for n, c in children.items()} | {top_ir["module"]: Module(top_ir)}
        new = {"top": top_ir, "children": children, "frozen": frozen_scope(lib), "assumptions": assumptions,
               "contracts": architect.read_contracts(Path(res.workdir))}
        # 이전 트리의 후보 상태와 L1은 보관하고 새 트리로 Author부터 다시 돌린다
        (self.dir / "modules").rename(self.dir / f"modules_a{rev - 1}")
        (self.dir / "modules").mkdir()
        old_l1 = self.dir / f"ir_a{rev - 1}"
        old_l1.mkdir(exist_ok=True)
        for f in (self.dir / "ir").glob("*.l1.json"):
            f.rename(old_l1 / f.name)
        for n, ir in [(top_ir["module"], top_ir), *children.items()]:
            (self.dir / "ir" / f"{n}.l1.json").write_text(json.dumps(ir, ensure_ascii=False, indent=1), encoding="utf-8")
        self.state.setdefault("arch_history", []).append({"rev": rev, "items": [p["id"] for p in items], "previous": tree,
                                                           "modules": sorted(children)})
        self.state["arch_rev"] = rev
        self.state["steps"]["architect"] = new
        self.state.pop("chosen", None)
        self.review("ARCHITECTURE_REVISED", f"Design-lead 요청({[p['id'] for p in items]})으로 모듈 트리를 다시 정함 (재구성 {rev}): "
                                            f"{sorted(tree['children'])} → {sorted(children)}. 모든 후보를 Author부터 다시 돌림",
                    str(res.workdir), "architect")
        self.save()
        return new

    # -- 단계 --
    def step_spec(self) -> bool:
        if (s := self.done("spec")) is not None:
            return s["gate"] == "pass"
        res = spec_check.run(load_spec(self.spec_path))
        for i in res["review"]:
            self.review(i["code"], i["message"], i["where"], i["source"])
        self.finish_step("spec", {"gate": res["gate"], "errors": [i for i in res["issues"] if i["severity"] == "error"]})
        return res["gate"] == "pass"

    def parent_state(self) -> dict | None:
        seed = self.state.get("seed")
        return json.loads((Path(seed["parent"]) / "state.json").read_text(encoding="utf-8")) if seed else None

    def step_architect(self) -> dict | None:
        if (s := self.done("architect")) is not None:
            return s
        if (ps := self.parent_state()) is not None:
            # 재실행: 이전 실행의 모듈 트리와 동결을 그대로 쓴다 (L1이 바뀌는 SPEC 개정은 처음부터 다시 실행)
            tree = ps["steps"]["architect"]
            (self.dir / "ir").mkdir(exist_ok=True)
            for n, ir in [(tree["top"]["module"], tree["top"]), *tree["children"].items()]:
                (self.dir / "ir" / f"{n}.l1.json").write_text(json.dumps(ir, ensure_ascii=False, indent=1), encoding="utf-8")
            self.finish_step("architect", tree)
            return tree
        top_l1, _ = block_top_l1(load_spec(self.spec_path))
        mx = self.state.get("max_modules")
        res = self.agent("architect", architect.make_task("architect", top_l1, self.spec_path, mx), architect.check(top_l1, mx))
        if not res.ok:
            self.finish_step("architect", None)
            return None
        top_ir, children, assumptions = architect.read_tree(Path(res.workdir), top_l1["module"])
        for a in assumptions:   # 모듈 안의 배분만 정한 가정은 Architect 기록(tree)에만 남긴다
            if architect.needs_review(a):
                self.review("ASSUMPTION", _assumption(a), "architect", "architect")
        lib = {n: Module(c) for n, c in children.items()} | {top_ir["module"]: Module(top_ir)}
        tree = {"top": top_ir, "children": children, "frozen": frozen_scope(lib), "assumptions": assumptions,
                "contracts": architect.read_contracts(Path(res.workdir))}
        (self.dir / "ir").mkdir(exist_ok=True)
        for n, ir in [(top_ir["module"], top_ir), *children.items()]:
            (self.dir / "ir" / f"{n}.l1.json").write_text(json.dumps(ir, ensure_ascii=False, indent=1), encoding="utf-8")
        self.finish_step("architect", tree)
        return tree

    def step_verifiers(self, top_ir: dict) -> list[str]:
        """Verifier 후보들의 테스트 파일. Block Top의 L1만 준다 (L2, L3, RTL은 없음)."""
        if (s := self.done("verifiers")) is not None:
            return s
        l1 = {k: top_ir[k] for k in ("module", "role", "params", "domains", "ports") if k in top_ir}
        if (ps := self.parent_state()) is not None:
            tbs = list(ps["steps"]["verifiers"])
            seed = self.state["seed"]
            if seed["kind"] == "spec_change":
                for k in range(len(tbs)):
                    before = Path(tbs[k]).read_text(encoding="utf-8")
                    res = self.agent("verifier", verifier.make_spec_update_task(
                        f"verifier/v{k}/spec_update", l1, self.spec_path, seed["old_spec"], Path(tbs[k]), seed["spec_diff"]),
                        verifier.check_spec_update(l1, before))
                    if not res.ok:
                        self.review("VERIFIER_SPEC_UPDATE_FAILED", f"Verifier {k}가 개정된 SPEC에 테스트를 맞추지 못함: {res.errors[:3]}",
                                    "verifier")
                        continue
                    if (why := verifier.no_change(Path(res.workdir))) is not None:
                        self.event("spec_update", f"Verifier {k}: 고칠 것 없음 ({why[:200]})")
                        continue
                    new = res.outputs[verifier.out_rel(l1["module"])]
                    self.state.setdefault("tb_history", []).append({"verifier": k, "from": tbs[k], "to": new, "spec_update": True})
                    tbs[k] = new
                self.verifier_diff_review(tbs, l1, "SPEC 개정 후 ")
            self.seed_modules(ps)
            self.finish_step("verifiers", tbs)
            return tbs
        names = [f"verifier/v{k}" for k in range(self.roles.counts.get("verifier", 1))]
        with ThreadPoolExecutor(len(names)) as ex:
            results = list(ex.map(lambda n: self.agent("verifier", verifier.make_task(n, l1, self.spec_path),
                                                       verifier.check(l1)), names))
        tbs = [r.outputs[verifier.out_rel(l1["module"])] for r in results if r.ok]
        if (ws := self.roles.warnings()) and self.roles_changed():
            for w in ws:
                self.review("MODEL_FAMILY", w, "roles")
        if len(tbs) >= 2:
            self.verifier_diff_review(tbs, l1)
        self.finish_step("verifiers", tbs)
        return tbs

    def seed_modules(self, ps: dict) -> None:
        """이전 실행에서 고른 모듈 IR을 후보 하나(c0)로 들여온다. 다시 쓰지 않고 검증부터 한다."""
        tree = ps["steps"]["architect"]
        chosen = ps.get("chosen") or {}
        parent = Path(self.state["seed"]["parent"])
        for m, ch in tree["children"].items():
            if ch.get("role") != "module" or m not in chosen:
                continue
            src = json.loads((parent / "modules" / f"{m}__c{chosen[m]}.json").read_text(encoding="utf-8"))
            self.save_mod(f"{m}/c0", {"status": "running", "repairs": 0, "hashes": [ir_hash(src["ir"])], "history": [],
                                      "ir": src["ir"], "from_parent": f"{m}/c{chosen[m]}"})
        self.state["author_count"] = 1

    def step_external_repair(self, tree: dict) -> None:
        """외부 검사 리포트에서 조각 내부로 분류된 항목을 그 모듈의 Repair로 고친다. 이어서 일반 검증 루프를 탄다."""
        seed = self.state.get("seed") or {}
        if seed.get("kind") != "external_repair" or self.done("external_repair") is not None:
            return
        done = {}
        for m, items in sorted(seed.get("failures", {}).items()):
            key = f"{m}/c0"
            st = self.mod_state(key)
            if "ir" not in st:
                self.review("EXTERNAL_UNKNOWN_MODULE", f"외부 리포트의 모듈 {m}이 이번 실행에 없음", m)
                continue
            failure = {"classified": items, "report": None,
                       "source": "외부 검사 리포트. 파이프라인 안의 테스트는 통과했지만 외부 툴이 이 줄을 지적함"}
            res = self.agent("repair", repair.make_task(f"repair/{m}/c0/external", st["ir"], self.spec_path, failure, []),
                             repair.check(st["ir"]))
            st["repairs"] += 1
            if not res.ok:
                st.update(status="failed", reason=f"외부 리포트 Repair 실패: {res.errors[:3]}")
                self.save_mod(key, st)
                done[m] = "failed"
                continue
            patches = json.loads(Path(res.outputs[repair.OUT]).read_text())
            if (reason := repair.interface_change(patches)) is not None:
                self.review("INTERFACE_CHANGE", f"{m}: 외부 리포트를 고치려면 L1 변경이 필요함: {reason}", res.workdir, "repair")
                st.update(status="interface_change")
                self.save_mod(key, st)
                done[m] = "interface_change"
                continue
            new = apply(st["ir"], patches)
            st["hashes"].append(ir_hash(new.ir))
            st["history"] += patches
            st["ir"] = new.ir
            self.save_mod(key, st)
            done[m] = "patched"
        self.finish_step("external_repair", done)

    def verifier_diff_review(self, tbs: list[str], l1: dict, when: str = "") -> None:
        hdir = self.dir / "harness"
        hdir.mkdir(exist_ok=True)
        (hdir / f"{l1['module']}_harness.py").write_text(harness_source(Module(l1)), encoding="utf-8")
        try:
            diff = verifier_diff(tbs, l1, harness_dir=hdir)
        except ImportError as e:
            diff = None
            self.review("VERIFIER_DIFF_SKIPPED", f"{when}참조 모델을 불러올 수 없어 Verifier 간 차분 검사를 못 함: {e}", "verifier")
        if diff:
            self.review("VERIFIER_DIFF", f"{when}참조 모델끼리 같은 입력에 다른 출력을 냄: {diff}. SPEC 모호함 또는 한쪽 모델 오류",
                        "verifier")

    # -- 검증 --
    def verify(self, lib: dict[str, dict], top: str, tbs: list[str], tag: str, py_check: bool = True) -> dict:
        """방출 → E1 → (py 모듈) 번역기 검사 → E2(Verifier 테스트마다). 결과와 실패 리포트를 돌려준다.
        py_check=False면 번역기 검사를 건너뛴다 (같은 IR을 이미 검사한 보강 테스트 실행)."""
        out = self.dir / "verify" / tag
        rtl = out / "rtl"
        rtl.mkdir(parents=True, exist_ok=True)
        mods = {n: Module(r) for n, r in lib.items()}
        files, maps = list(self.cells), {}
        for n, m in mods.items():
            e = emit(m, mods)
            (rtl / f"{n}.sv").write_text(e.text, encoding="utf-8")
            files.append(rtl / f"{n}.sv")
            maps[n] = e.sourcemap
        roles = {n: m.role for n, m in mods.items()}
        e1 = lint(files, top)
        e1c = classify_findings(e1.findings, maps, roles)
        # 조각의 lint 오류는 Repair가 먼저 고친다. 경고도 Verilator 빌드를 멈추므로 E2와 번역기 검사를 돌리면
        # 빌드 실패가 TB 실행 오류나 생성기 버그로 잘못 분류된다 (RTLLM multi_16bit WIDTHTRUNC)
        frag_lint = any(c.type == "lint_fragment" for c in e1c)
        # 번역기 검사: Python 조각 모듈의 설계 모델과 방출한 RTL이 같은 무작위 자극에서 같은 출력을 내는지 (SPEC 해석은 보지 않음)
        py_equiv = [{"module": n, **pyequiv.check(n, mods, files, out / f"pyequiv_{n}")}
                    for n, m in mods.items() if m.lang == "py" and py_check and not frag_lint]
        top_m = mods[top]
        hdir, tdir = out / "harness", out / "verify_only"
        hdir.mkdir(exist_ok=True)
        tdir.mkdir(exist_ok=True)
        (hdir / f"{top}_harness.py").write_text(harness_source(top_m), encoding="utf-8")
        (tdir / f"{top}_trace.json").write_text(json.dumps(trace_names(top_m, mods)), encoding="utf-8")
        e2 = []
        pads, sim_top = pad_top(top_m.raw, out / "pad")   # inout Block Top은 패드 래퍼를 top으로
        for k, tb in enumerate([] if frag_lint else tbs):
            r = sim([*files, *pads], sim_top, Path(tb).stem, Path(tb).parent, out / f"sim_v{k}", seeds=self.seeds, harness_dir=hdir,
                    trace=tdir / f"{top}_trace.json")
            res_dir = out / f"sim_v{k}" / "tb_out" / str(self.seeds[0])
            e2.append({"verifier": k, "ok": r.ok, "findings": [{"test": f.test, "rule": f.rule, "message": f.message[:300]}
                                                               for f in r.findings], "out": str(res_dir)})
        return {"e1": [{"type": c.type, "node": c.node, "module": c.module, "message": c.message[:200],
                        "severity": c.source.get("severity")} for c in e1c],
                "e2": e2, "py_equiv": py_equiv,
                "ok": all(x["ok"] for x in e2 + py_equiv) and not any(c.type == "lint_fragment" for c in e1c)}

    @staticmethod
    def result_name(out: Path, tests: list[str]) -> str | None:
        """실패한 cocotb 테스트의 결과 파일 이름. 결과 파일은 하니스의 name을 따르므로 함수 이름과 다를 수 있다.
        함수 이름이나 `test_`를 뗀 이름이 없으면 실패가 기록된 첫 결과 파일."""
        for t in tests:
            for n in (t, t.removeprefix("test_")):
                if (out / f"{n}.result.json").exists():
                    return n
        for res in sorted(out.glob("*.result.json")):
            r = json.loads(res.read_text())
            if r.get("scoreboard") or r.get("protocol") or r.get("properties"):
                return res.name.removesuffix(".result.json")
        return None

    def failure_report(self, lib: dict, top: str, v: dict) -> dict:
        out = Path(v["out"])
        test = self.result_name(out, [f["test"] for f in v["findings"] if f["test"]])
        if test is None:
            # 하니스 결과가 없는 실패 (테스트 안의 assert 등): 트레이스는 없지만 실패한 테스트와 메시지는 넘긴다
            # (없으면 Repair와 lead가 첫 불일치를 모른 채 판단함. P8a async_fifo E1)
            named = [f for f in v["findings"] if f["test"]]
            report = {"first_failure": {"check": "assertion", "test": named[0]["test"], "message": named[0]["message"]},
                      "note": "하니스 결과 파일이 없는 실패 (테스트 안의 assert). 트레이스 없음"} if named else None
            return {"classified": [{"type": _sim_type(f["rule"]), **f} for f in v["findings"]], "report": report}
        mods = {n: Module(r) for n, r in lib.items()}
        rep = mismatch_report(mods[top], json.loads((out / f"{test}.result.json").read_text()),
                              json.loads((out / f"{test}.trace.json").read_text()),
                              self.spec_path.read_text(encoding="utf-8"), mods)
        sva = [f for f in v["findings"] if f["rule"].startswith("SVA_")]
        if rep is None and sva:   # 하니스 검사는 통과했지만 IR의 SVA가 실패함: 실패한 속성 노드와 식을 넘긴다
            node, rest = sva[0]["message"].split(" ", 1)
            mod = rest.removeprefix("(").split(",", 1)[0]
            expr = next((a["expr"] for a in lib.get(mod, {}).get("assertions", []) if f'assertions/{a["id"]}' == node), None)
            rep = {"test": sva[0]["test"], "first_failure": {"check": "sva", "test": sva[0]["test"], "module": mod, "node": node,
                                                              "expr": expr, "message": sva[0]["message"]}}
        return {"classified": [{"type": _sim_type(f["rule"]),
                                "test": f["test"], "message": f["message"]} for f in v["findings"]], "report": rep}

    def author_step(self, mod: str, c: int, tree: dict) -> dict:
        """후보 하나의 Author. 상태는 modules[mod][c]에 저장."""
        key = f"{mod}/c{c}"
        st = self.mod_state(key)
        if "ir" in st or st["status"] != "author":
            return st
        l1 = tree["children"][mod]
        lang = self.roles.lang_of(c)
        st["lang"] = lang
        contracts = architect.contracts_for(tree.get("contracts", []), tree["top"], mod)
        res = self.agent("author", author.make_task(f"author/{mod}/c{c}{self.arch_tag()}", l1, self.spec_path, lang=lang,
                                                    contracts=contracts),
                         author.check(l1, lang=lang))
        if not res.ok:
            st.update(status="failed", reason=f"Author 실패: {res.errors[:3]}")
        else:
            st["ir"] = json.loads(Path(res.outputs[author.out_rel(mod)]).read_text(encoding="utf-8"))
        self.save_mod(key, st)
        return st

    @staticmethod
    def culprit(tree: dict, mods: list[str], failure: dict, e1: list[dict]) -> str:
        """실패의 원인 후보 모듈: 불일치 출력을 구동하는 인스턴스의 모듈, 없으면 lint 조각 오류가 난 모듈."""
        inst = {i["name"]: i["module"] for i in tree["top"].get("instances", [])}
        for node in ((failure.get("report") or {}).get("fanin") or {}).get("nodes", []):
            if node.startswith("instances/") and inst.get(node.split("/", 1)[1]) in mods:
                return inst[node.split("/", 1)[1]]
        for x in e1:
            if x.get("type") == "lint_fragment" and x.get("module") in mods:
                return x["module"]
        return mods[0]

    def arbitrate(self, lib: dict, top: str, tbs: list[str], gname: str, mods: list[str], c: int, sts: dict,
                  v: dict, tag: str, claim: dict | None = None, repeated: dict | None = None) -> str:
        """Verifier v의 실패를 Arbiter가 판정한다. test_wrong이면 기댓값 수정 승인 대기, spec_ambiguous면 검토 항목과 대기.
        claim은 Repair의 L1 변경 요청 (RTL로는 기댓값을 맞출 수 없다는 주장), repeated는 Repair를 거듭해도 같은 테스트가 실패한 경위."""
        rep = self.failure_report(lib, top, v)
        mismatch = rep["report"] or rep
        if claim:
            mismatch = {**mismatch, "repair_claim": claim}
        if repeated:
            mismatch = {**mismatch, "repeated_failure": repeated}
        rev = self.state.get("tb_rev", 0)   # 테스트가 수정되면 같은 Repair 횟수에서도 다시 판정한다
        name = f"arbiter/{gname}/c{c}/{tag}" + (f"/v{v['verifier']}" if claim or repeated else "") + (f"/t{rev}" if rev else "") + self.arch_tag()
        ares = self.agent("arbiter", arbiter.make_task(name, self.spec_path, mismatch, Path(tbs[v["verifier"]])),
                          arbiter.check(self.spec_path))
        vj = json.loads(Path(ares.outputs[arbiter.OUT]).read_text()) if ares.ok else {"verdict": "spec_ambiguous"}
        verdict = vj["verdict"]
        keys = {m: f"{m}/c{c}" for m in mods}

        def save_all(**upd):
            for m in mods:
                sts[m].update(**upd)
                self.save_mod(keys[m], sts[m])
        for st in sts.values():
            st.setdefault("arbiter", []).append(verdict)
        if verdict == "test_wrong":
            ff = (rep["report"] or {}).get("first_failure") or {}
            # 같은 실패: 같은 Verifier, 같은 테스트, 같은 검사 (사이클과 값은 후보마다 다를 수 있음)
            key = json.dumps([v["verifier"], (rep["report"] or {}).get("test") or ff.get("test"), ff.get("check"),
                              ff.get("property") or ff.get("stream") or ff.get("bundle") or ff.get("message"),
                              ff.get("kind") or ff.get("rule")], ensure_ascii=False)
            item = {"kind": "expectation_change", "verifier": v["verifier"], "group": gname, "mods": mods,
                    "c": c, "reqs": vj.get("reqs"), "reason": vj.get("reason"), "proposal": vj.get("proposal"),
                    "first_failure": (rep["report"] or {}).get("first_failure"), "group_key": key,
                    "arbiter_dir": str(ares.workdir), **({"repair_claim": claim} if claim else {}),
                    **({"repeated_failure": repeated} if repeated else {})}
            joined = self.join_pending(item, key)
            if joined:
                ids = sts[mods[0]].get("pending_ids", []) if sts[mods[0]]["status"] == "waiting_approval" else []
                self.review("EXPECTATION_CHANGE", f"{gname} 후보 {c}: Verifier {v['verifier']}의 같은 실패라 기댓값 수정 제안 "
                            f"{joined}에 묶음 (사람 승인 대기)", str(ares.workdir), "arbiter", pending=joined)
                save_all(status="waiting_approval", pending=joined, pending_ids=ids + [joined])
                return verdict
            pid = self.add_pending(item)
            self.review("EXPECTATION_CHANGE", f"{gname} 후보 {c}: Verifier {v['verifier']}의 기댓값 수정 제안 {pid} (사람 승인 대기)"
                        + (" (Repair의 L1 변경 요청을 Arbiter가 테스트 쪽 문제로 판정)" if claim else "")
                        + (f" (Repair {repeated['repairs']}번 뒤에도 같은 테스트가 실패해 Arbiter가 판정)" if repeated else ""),
                        str(ares.workdir), "arbiter", pending=pid)
            # L1 변경 요청은 실패한 Verifier마다 판정하므로 한 후보가 항목 여럿을 기다릴 수 있다
            ids = sts[mods[0]].get("pending_ids", []) if sts[mods[0]]["status"] == "waiting_approval" else []
            save_all(status="waiting_approval", pending=pid, pending_ids=ids + [pid])
        elif verdict == "spec_ambiguous":
            self.review("SPEC_AMBIGUOUS", f"{gname} 후보 {c}: Verifier 사이 해석이 갈림" if not claim else
                        f"{gname} 후보 {c}: Repair의 L1 변경 요청에 대해 SPEC이 모호함", str(ares.workdir), "arbiter")
            save_all(status="waiting_approval")
        return verdict

    def candidate_loop(self, c: int, mods: list[str], tree: dict, tbs: list[str]) -> dict[str, dict]:
        """c번 후보 묶음: 모듈마다 c번 Author 후보를 모아 Block Top에서 함께 검증한다 (하위 모듈이 하나면 그 후보 하나).
        실패면 Arbiter/Repair. Repair는 원인 후보 모듈 하나에 넘기고, 그 Repair가 형제 모듈을 지목하면 그 모듈로 넘긴다."""
        keys = {m: f"{m}/c{c}" for m in mods}
        sts = {m: self.mod_state(k) for m, k in keys.items()}

        def save_all(**upd):
            for m in mods:
                sts[m].update(**upd)
                self.save_mod(keys[m], sts[m])

        if all(st["status"] in ("pass", "failed", "waiting_approval", "interface_change") for st in sts.values()):
            return sts
        if bad := [m for m in mods if "ir" not in sts[m]]:
            for m in mods:
                if m not in bad:
                    sts[m].update(status="failed", reason=f"같은 묶음의 Author 실패: {bad}")
                self.save_mod(keys[m], sts[m])
            return sts
        top_ir, children = tree["top"], tree["children"]
        gname = mods[0] if len(mods) == 1 else top_ir["module"]
        limit = self.roles.limits.get("repair_per_candidate", 5)
        while True:
            total = sum(_seq(st) for st in sts.values())   # 작업 이름과 검증 태그용 (되돌린 횟수와 무관하게 늘기만 함)
            lib = {top_ir["module"]: top_ir, **{n: (sts[n]["ir"] if n in sts else ch) for n, ch in children.items()}}
            v11 = v11_frozen({n: Module(r) for n, r in lib.items()}, tree["frozen"])
            if v11:
                save_all(status="failed", reason=f"동결 위반: {[str(i) for i in v11]}")
                break
            vtag = f"{gname}_c{c}_r{total}{self.arch_tag('_')}"
            vres = self.verify(lib, top_ir["module"], tbs, vtag)
            last = {"ok": vres["ok"], "e1": len(vres["e1"]), "e2": [{k: x[k] for k in ("verifier", "ok")} for x in vres["e2"]]}
            for st in sts.values():
                st["last_verify"] = last
            if bad := [x for x in vres.get("py_equiv", []) if not x["ok"]]:
                # 설계 모델과 RTL이 다름: 번역기나 실행기의 버그다. Repair로 고칠 일이 아니므로 사람에게 넘긴다
                for x in bad:
                    self.review("PY_EQUIV", f'{x["module"]} 후보 {c}: 번역기 검사 실패 (생성기 버그): '
                                f'{json.dumps(x.get("mismatch") or x.get("error"), ensure_ascii=False)[:300]}',
                                str(self.dir / "verify" / vtag / f'pyequiv_{x["module"]}'))
                save_all(status="failed", reason=f'번역기 검사 실패: {[x["module"] for x in bad]}')
                break
            # 골격에서 구동이 없거나 둘인 신호는 경고여도 생성기 버그다 (P8a async_fifo: 인스턴스 출력의 넷 이름이 갈려 m_pause_ack 무구동)
            if skel := [x for x in vres["e1"] if x["type"] == "lint_skeleton" and (
                    x.get("severity") == "error" or any(f" {r}]" in x["message"] for r in ("UNDRIVEN", "MULTIDRIVEN")))]:
                # 방출기가 만든 골격의 lint 에러: 생성기 버그다. Repair나 Verifier로 고칠 일이 아니므로 사람에게 넘긴다
                self.review("GENERATOR_BUG", f"{gname} 후보 {c}: 골격 lint 에러 (생성기 버그): "
                            f'{"; ".join(x["message"] for x in skel[:3])[:400]}', str(self.dir / "verify" / vtag / "rtl"))
                save_all(status="failed", reason=f'생성기 버그 (골격 lint 에러 {len(skel)}건)')
                break
            if vres["ok"]:
                # SPEC 9절이 입력→출력 조합 경로를 막으면 통과한 후보의 경로를 검토 항목으로 낸다 (P8 회고: pi.lead가 RTL을 읽어 찾음)
                rule = feedthrough_rule(self.spec_path.read_text(encoding="utf-8"))
                if rule and (paths := feedthrough_paths(Module(top_ir), {n: Module(r) for n, r in lib.items()}, rule)):
                    self.review("COMB_PATH", f"{gname} 후보 {c}: SPEC 9절이 입력→출력 조합 경로를 막는데 경로가 있음 "
                                f"({len(paths)}개): {', '.join(f'{i}→{o}' for i, o in paths[:8])}",
                                str(self.dir / "verify" / vtag / "rtl"))
                save_all(status="pass", lint=len(vres["e1"]), outputs=[x["out"] for x in vres["e2"]],
                         comb_paths=len(paths) if rule else None)
                break
            failed = [x for x in vres["e2"] if not x["ok"]]
            nfail = sum(len({f["test"] for f in x["findings"] if f["test"]}) or 1 for x in failed)
            # 실패한 테스트가 가장 적었던 리비전: 끝까지 통과하지 못하면 사람이 볼 후보로 남긴다 (Repair가 나중 리비전을
            # 망가뜨리는 경우. RTLLM freq_divbyeven은 r0가 외부 정답 TB를 통과했고 r3는 실패)
            best = sts[mods[0]].get("best")
            if vres["e2"] and (best is None or nfail < best["fail"]):
                save_all(best={"fail": nfail, "verify": vtag})
            # lead가 지정한 Repair 뒤 실패한 테스트가 늘면 그 Repair를 되돌리고 lead에게 다시 묻는다 (P8a async_fifo)
            undo = next(((m, sts[m]["undo"]) for m in mods if sts[m].get("undo")), None)
            for m in mods:
                sts[m].pop("undo", None)
            if undo and nfail > undo[1]["fail_count"]:
                m, u = undo
                sts[m]["ir"], sts[m]["history"] = u["ir"], sts[m]["history"][:u["hist_len"]]
                for x in mods:
                    sts[x].pop("lead_repair", None)
                rp = (self.failure_report(lib, top_ir["module"], failed[0]).get("report") or {}) if failed else {}
                msg = f"lead가 지정한 {m}의 Repair 뒤 실패한 테스트가 {u['fail_count']}개 → {nfail}개로 늘어 그 Repair를 되돌림"
                pid = self.add_pending({"kind": "repair_conflict", "group": gname, "mods": mods, "c": c, "chain": [m],
                                        "remaining": {x: max(0, limit - sts[x]["repairs"]) for x in mods},
                                        "claims": [{"module": m, "suspects": None, "reason": msg, "workdir": u["workdir"]}],
                                        "reqs": [r] if (r := (rp.get("first_failure") or {}).get("req")) else [],
                                        "first_failure": rp.get("first_failure"), "spec": rp.get("spec"), "verify": vtag,
                                        "regressed": True})
                self.review("LEAD_REPAIR_REGRESSED", f"{gname} 후보 {c}: {msg}. Design-lead 결정 {pid} 대기 "
                            "(다른 모듈 지정 또는 Architect 재구성)", u["workdir"], "repair", pending=pid)
                save_all(status="waiting_approval", pending=pid)
                break
            skip = sts[mods[0]].get("skip_arbiter", [])   # 사람이 기댓값 수정을 거절한 Verifier: 테스트가 맞으므로 Repair로
            single = bool(failed) and len(failed) < len(vres["e2"]) and failed[0]["verifier"] not in skip
            if single:
                # 한 Verifier에서만 실패: 그 Verifier의 오류일 수 있으므로 Arbiter가 판정한다
                verdict = self.arbitrate(lib, top_ir["module"], tbs, gname, mods, c, sts, failed[0], f"r{total}")
                if verdict in ("test_wrong", "spec_ambiguous"):
                    break
            # 같은 테스트가 Repair를 거듭해도 같은 이유로 실패: 테스트 쪽 문제일 수 있으므로 상한 전에 Arbiter가 판정한다 (P8 회고)
            sig = json.dumps(sorted([x["verifier"], sorted({f["test"] for f in x["findings"] if f["test"]})] for x in failed))
            first = sts[mods[0]]
            n = first.get("fail_sig_n", 0) + 1 if failed and first.get("fail_sig") == sig else 1
            save_all(fail_sig=sig, fail_sig_n=n)
            after = self.roles.limits.get("arbiter_after_repeats", 2)
            if failed and not single and n > after and sig not in first.get("repeat_arbitrated", []):
                save_all(repeat_arbitrated=first.get("repeat_arbitrated", []) + [sig])
                repeated = {"repairs": n - 1, "modules": mods,
                            "note": f"Repair {n - 1}번 뒤에도 같은 테스트가 같은 이유로 실패함"}
                verdicts = [self.arbitrate(lib, top_ir["module"], tbs, gname, mods, c, sts, v, f"r{total}/repeat", repeated=repeated)
                            for v in failed if v["verifier"] not in skip]
                if any(x in ("test_wrong", "spec_ambiguous") for x in verdicts):
                    break
            fail_v = failed[0] if failed else {"findings": [], "out": ""}
            failure = self.failure_report(lib, top_ir["module"], fail_v) if failed else {"classified": vres["e1"], "report": None}
            if any(x.get("type") == "tb_error" for x in failure["classified"]) and not any(
                    x.get("type") in ("sim_mismatch", "sim_hang") for x in failure["classified"]):
                self.review("TB_ERROR", f"{gname} 후보 {c}: 테스트 실행 오류 (Verifier가 고쳐야 함)", fail_v.get("out", ""))
                save_all(status="failed", reason="TB 실행 오류")
                break
            pin = sts[mods[0]].get("lead_repair")   # lead가 고칠 모듈을 지정함: 그 모듈의 Repair만, 형제 지목 금지
            mod, tried, claims, stop = pin["module"] if pin else self.culprit(tree, mods, failure, vres["e1"]), [], [], None
            while stop is None:
                st = sts[mod]
                tried.append(mod)
                if st["repairs"] >= limit:
                    stop = dict(status="failed", reason=f"{mod}: Repair 상한 {limit}회")
                    break
                block = None if len(mods) == 1 else {
                    "top": top_ir["module"], "instances": top_ir.get("instances", []), "connections": top_ir.get("connections", []),
                    "siblings": {n: children[n] for n in mods if n != mod},
                    "contracts": architect.contracts_for(tree.get("contracts", []), top_ir, mod)}
                n = _seq(st)
                rres = self.agent("repair", repair.make_task(f"repair/{mod}/c{c}/r{n}{self.arch_tag()}", st["ir"],
                                                             self.spec_path, failure, st["history"], block, lead=pin),
                                  repair.check(st["ir"], siblings=set(block["siblings"]) if block else None, pinned=bool(pin)))
                st["repairs"] += 1
                st["repair_seq"] = n + 1
                st["repairs_since_tb"] = st.get("repairs_since_tb", 0) + 1
                self.save_mod(keys[mod], st)
                if not rres.ok:
                    stop = dict(status="failed", reason=f"{mod}: Repair 실패: {rres.errors[:3]}")
                    break
                patches = json.loads(Path(rres.outputs[repair.OUT]).read_text())
                if (reason := repair.interface_change(patches)) is not None:
                    # L1 변경 요청은 테스트 기댓값 탓일 수 있다: 실패한 Verifier마다 Arbiter가 먼저 판정한다 (P8 회고 1)
                    claim = {"module": mod, "reason": reason}
                    verdicts = [self.arbitrate(lib, top_ir["module"], tbs, gname, mods, c, sts, v, f"r{total}/ic", claim)
                                for v in failed if v["verifier"] not in skip]
                    if any(x in ("test_wrong", "spec_ambiguous") for x in verdicts):
                        stop = {}   # arbitrate가 승인 대기로 바꿔 둠
                        break
                    self.review("INTERFACE_CHANGE", f"{mod} 후보 {c}: Repair가 L1 변경을 요청함: {reason}", rres.workdir, "repair")
                    stop = dict(status="interface_change")
                    break
                if (sus := repair.suspect(patches)) is not None:
                    # 자식 모듈 원인 의심: 지목된 형제 모듈의 Repair로 넘긴다. 이번 실패에서 이미 거친 모듈이면 충돌이므로 lead가 정한다
                    self.event("suspect", f"{mod} → {sus[0]}: {sus[1][:200]}")
                    claims.append({"module": mod, "suspects": sus[0], "reason": sus[1], "workdir": str(rres.workdir)})
                    if sus[0] in tried:
                        # 형제 Repair들이 서로 넘김: 모듈 쪽이 아니라 테스트 쪽 문제일 수 있으므로 lead에게 올리기 전에 Arbiter가 한 번 판정
                        # (P8a async_fifo: Verifier의 set_ready 오해로 생긴 실패가 Repair 충돌로 올라가 lead가 테스트를 고를 수 없었음)
                        if failed and sig not in sts[mods[0]].get("repeat_arbitrated", []):
                            save_all(repeat_arbitrated=sts[mods[0]].get("repeat_arbitrated", []) + [sig])
                            conflict = {"repairs": total, "modules": tried + [sus[0]],
                                        "note": f"형제 모듈의 Repair들이 서로 원인을 넘김 ({' → '.join(tried + [sus[0]])}). 각 모듈은 자기 쪽이 SPEC대로라고 함"}
                            verdicts = [self.arbitrate(lib, top_ir["module"], tbs, gname, mods, c, sts, v, f"r{total}/conflict",
                                                       repeated=conflict) for v in failed if v["verifier"] not in skip]
                            if any(x in ("test_wrong", "spec_ambiguous") for x in verdicts):
                                stop = {}
                                break
                        rp = failure.get("report") or {}
                        pid = self.add_pending({"kind": "repair_conflict", "group": gname, "mods": mods, "c": c,
                                                "remaining": {x: max(0, limit - sts[x]["repairs"]) for x in mods},
                                                "chain": tried + [sus[0]], "claims": claims,
                                                "reqs": [r] if (r := (rp.get("first_failure") or {}).get("req")) else [],
                                                "first_failure": rp.get("first_failure"), "spec": rp.get("spec"), "verify": vtag})
                        self.review("REPAIR_CONFLICT", f"{gname} 후보 {c}: Repair들이 서로 원인을 넘김 ({' → '.join(tried + [sus[0]])}). "
                                    f"Design-lead 결정 {pid} 대기 (고칠 모듈 지정 또는 Architect 재구성)", rres.workdir, "repair",
                                    pending=pid)
                        stop = dict(status="waiting_approval", pending=pid)
                        break
                    mod = sus[0]
                    continue
                new = apply(st["ir"], patches)
                h = ir_hash(new.ir)
                if h in st["hashes"]:
                    self.review("OSCILLATION", f"{mod} 후보 {c}: 같은 IR로 돌아옴 (진동). 사람에게 넘김", rres.workdir)
                    stop = dict(status="failed", reason=f"{mod}: 진동")
                    break
                if pin:   # 다음 검증에서 실패가 늘면 되돌린다
                    st["undo"] = {"ir": st["ir"], "hist_len": len(st["history"]), "fail_count": nfail, "workdir": str(rres.workdir)}
                st["hashes"].append(h)
                st["history"] += patches
                st["ir"] = new.ir
                self.save_mod(keys[mod], st)
                break
            if stop is not None:
                if stop:
                    save_all(**stop)
                break
        return sts

    def step_modules(self, tree: dict, tbs: list[str]) -> dict[str, dict]:
        """모듈마다 Author 후보를 병렬로 돌리고, 같은 번호 후보끼리 묶어 검증한 뒤 묶음을 고른다.
        자식부터(여기서는 Block Top의 자식 한 단계)."""
        mods = [n for n, c in tree["children"].items() if c.get("role") == "module"]
        n = self.state.get("author_count") or self.roles.counts.get("author", 1)
        jobs = [(m, c) for m in mods for c in range(n)]
        with ThreadPoolExecutor(max(1, len(jobs))) as ex:
            list(ex.map(lambda mc: self.author_step(mc[0], mc[1], tree), jobs))
        with ThreadPoolExecutor(max(1, n)) as ex:
            list(ex.map(lambda c: self.candidate_loop(c, mods, tree, tbs), range(n)))
        allst = self.modules()
        with self.lock:   # 이전 실행(승인 대기 등)에서 남은 결과는 이번 판정으로 바꾼다
            self.state["review"] = [r for r in self.state["review"] if r["code"] not in ("MODULE_FAILED", "BEST_REVISION")]
        # 묶음 c는 모든 모듈의 c번 후보가 함께 통과해야 쓸 수 있다
        ok = sorted([(sum(allst[f"{m}/c{c}"]["repairs"] for m in mods), sum(allst[f"{m}/c{c}"].get("lint", 0) for m in mods), c)
                     for c in range(n) if all(allst[f"{m}/c{c}"]["status"] == "pass" for m in mods)])
        chosen = {}
        if not ok:
            for m in mods:
                self.review("MODULE_FAILED", f"{m}: 통과한 후보가 없음 "
                                             f"({[allst[f'{m}/c{c}'].get('reason') or allst[f'{m}/c{c}']['status'] for c in range(n)]})", m)
            for c in range(n):
                if (b := allst[f"{mods[0]}/c{c}"].get("best")) and allst[f"{mods[0]}/c{c}"]["status"] != "waiting_approval":
                    self.review("BEST_REVISION", f"{', '.join(mods)} 후보 {c}: 실패한 테스트가 가장 적었던 리비전 {b['verify']} "
                                                 f"({b['fail']}개)", str(self.dir / "verify" / b["verify"] / "rtl"))
        else:
            chosen = {m: ok[0][2] for m in mods}
            passed = [c for _, _, c in ok]
            if len(passed) >= 2:
                diff = candidate_diff([allst[f"{mods[0]}/c{c}"]["outputs"] for c in passed])
                if diff:
                    self.review("CANDIDATE_DIFF", f"{', '.join(mods)}: 통과한 후보들이 같은 테스트에서 다른 출력을 냄 ({diff}). "
                                                  "테스트에 구멍이 있음. Verifier에게 돌려 보강", ", ".join(mods))
                elif self.diff_and_augment(tree, mods, passed, tbs):
                    return self.step_modules(tree, tbs)   # 보강된 테스트로 모든 묶음을 다시 검증
        self.state["chosen"] = chosen
        self.save()
        return chosen

    def group_lib(self, tree: dict, mods: list[str], c: int) -> dict[str, dict]:
        allst = self.modules()
        return {tree["top"]["module"]: tree["top"],
                **{n: (allst[f"{n}/c{c}"]["ir"] if n in mods else ch) for n, ch in tree["children"].items()}}

    def diff_and_augment(self, tree: dict, mods: list[str], passed: list[int], tbs: list[str]) -> bool:
        """후보 간 차분 시뮬레이션. 차이가 나면 Verifier가 그 상황의 테스트를 더하고, 통과했던 묶음을 다시 돌린다.
        다시 돌려야 하면 True. 보강 횟수는 limits.candidate_diff_rounds(기본 2)까지."""
        rnd = self.state.get("diff_rounds", 0)
        limit = self.roles.limits.get("candidate_diff_rounds", 2)
        top = tree["top"]
        hdir = self.dir / "harness"   # 참조 모델(legal로 입력을 거름)이 하니스를 import한다
        hdir.mkdir(exist_ok=True)
        (hdir / f"{top['module']}_harness.py").write_text(harness_source(Module(top)), encoding="utf-8")
        recs: dict[tuple, dict | None] = {}
        found = None
        for mode, seed in DIFF_RUNS:
            for c in passed:
                files, _ = emit_lib(self.group_lib(tree, mods, c), self.dir / "diffsim" / f"r{rnd}" / f"c{c}" / "rtl", self.cells)
                recs[(c, mode, seed)] = run_diffsim(files, top, mode, seed, self.dir / "diffsim" / f"r{rnd}" / f"c{c}" / f"{mode}{seed}",
                                                    model=tbs[0], harness_dir=hdir)
            base = recs[(passed[0], mode, seed)]
            for c in passed[1:]:
                other = recs[(c, mode, seed)]
                if base is None or other is None:
                    found = {"mode": mode, "seed": seed, "a": passed[0], "b": c,
                             "difference": {"kind": "sim_failed", "a_ok": base is not None, "b_ok": other is not None}}
                elif (d := compare(base, other)) is not None:
                    found = {"mode": mode, "seed": seed, "a": passed[0], "b": c, "difference": d,
                             "events": diff_window(base, d), "inputs_accepted": {"A": base["inputs"], "B": other["inputs"]}}
                if found:
                    break
            if found:
                break
        self.finish_step(f"candidate_diffsim_r{rnd}", {"groups": passed, "difference": found})
        if found is None:
            self.event("diffsim", f"후보 간 차분 시뮬레이션: 차이 없음 (묶음 {passed})")
            return False
        msg = f"{', '.join(mods)}: 묶음 {found['a']}, {found['b']}가 같은 자극({found['mode']}, seed {found['seed']})에 다른 출력을 냄"
        if rnd >= limit:
            self.review("CANDIDATE_DIFF", f"{msg}. 보강 상한 {limit}회", ", ".join(mods))
            return False
        k = rnd % len(tbs)
        l1 = {x: top[x] for x in ("module", "role", "params", "domains", "ports") if x in top}
        before = Path(tbs[k]).read_text(encoding="utf-8")
        diff_in = {x: found[x] for x in ("mode", "seed", "difference", "events", "inputs_accepted") if x in found}
        if found["difference"].get("kind") != "sim_failed":
            # 차이를 유지하는 가장 작은 자극을 찾아 Verifier에게 짧은 시나리오로 준다
            mdir = self.dir / "diffsim" / f"r{rnd}" / "min"
            files = {c: emit_lib(self.group_lib(tree, mods, c), mdir / f"c{c}" / "rtl", self.cells)[0] for c in (found["a"], found["b"])}
            mini = minimize_diff(files, top, found["mode"], found["seed"], mdir, tbs[0], hdir)
            if mini:
                h = diff_cfg(top, found["mode"], found["seed"])["harness"]
                ins = ", ".join(b["name"] for b in h["bundles"] if not b["dut_drives"])
                diff_in = {"mode": found["mode"], "difference": mini["difference"], "inputs_accepted": mini["inputs_accepted"],
                           "scenario": {"total_cycles": mini["total_cycles"],
                                        "background": f"처음 {INIT_RESET_CYCLES}사이클 리셋 뒤, 입력 번들({ins})에 무작위 트랜잭션을 "
                                                      "사이클의 약 70%에 보냄(SPEC이 허용하는 값만). 아래 사건 외에는 출력 ready=1, "
                                                      "단독 입력=0, 리셋 없음",
                                        "events": describe(mini["plan"], h)}}
                self.finish_step(f"candidate_diffsim_r{rnd}_min", {k2: mini[k2] for k2 in ("difference", "episodes", "total_cycles", "runs")}
                                 | {"events": describe(mini["plan"], h)})
                self.event("diffsim_min", f"자극 축소: 사건 {mini['episodes']}개, {mini['total_cycles']}사이클 ({mini['runs']}회 실행)")
        attempts = self.roles.limits.get("augment_attempts", 2)
        feedback, new = None, None
        for a in range(attempts):
            res = self.agent("verifier", verifier.make_augment_task(
                f"verifier/v{k}/diff{rnd}" + (f"a{a}" if a else ""), l1, self.spec_path, Path(tbs[k]),
                diff_in | ({"previous_attempt": feedback} if feedback else {})), verifier.check_augment(l1, before))
            if not res.ok:
                self.state["diff_rounds"] = rnd + 1
                self.review("CANDIDATE_DIFF", f"{msg}. Verifier {k}가 테스트를 보강하지 못함: {res.errors[:3]}", ", ".join(mods))
                self.save()
                return False
            if (reason := verifier.spec_undefined(Path(res.workdir))) is not None:
                self.state["diff_rounds"] = rnd + 1
                self.review("CANDIDATE_DIFF_UNDEFINED", f"{msg}. Verifier {k}: SPEC이 정하지 않은 동작 ({reason[:300]})",
                            ", ".join(mods), "verifier")
                self.save()
                return False
            cand = res.outputs[verifier.out_rel(l1["module"])]
            # 보강된 테스트는 후보를 갈라야 한다. 모두 실패하면 테스트 결함일 가능성이 크고, 모두 통과하면 상황을 재현하지 못한 것
            runs = {c: self.verify(self.group_lib(tree, mods, c), top["module"], [cand], f"augment_r{rnd}a{a}_c{c}",
                                    py_check=False)["e2"][0]
                    for c in passed}
            ok = [c for c in passed if runs[c]["ok"]]
            self.event("augment_check", f"보강 테스트 시도 {a}: 통과 묶음 {ok} / {passed}")
            if 0 < len(ok) < len(passed):
                new = cand
                break
            feedback = {"result": "통과했던 후보가 모두 실패함. 테스트 자체의 결함(기댓값 관리, 시나리오 구성)을 먼저 의심할 것"
                        if not ok else "통과했던 후보가 모두 통과함. 차이가 난 상황을 재현하지 못함",
                        "failures": {f"후보 {i}": [f["message"][:400] for f in runs[c]["findings"][:2]] for i, c in enumerate(passed)}}
            if not ok and a == attempts - 1:
                new = cand   # 끝까지 모두 실패: 후보가 모두 틀렸을 수도 있으므로 채택하고 Arbiter와 사람이 판단
        self.state["diff_rounds"] = rnd + 1
        if new is None:
            self.review("CANDIDATE_DIFF", f"{msg}. Verifier {k}의 보강 테스트가 후보를 가르지 못함", ", ".join(mods))
            self.save()
            return False
        self.state.setdefault("tb_history", []).append({"verifier": k, "from": tbs[k], "to": new, "diff_round": rnd})
        tbs[k] = new
        self.state["tb_rev"] = self.state.get("tb_rev", 0) + 1
        self.state["steps"]["verifiers"] = tbs
        self.event("tb_augment", f"{msg} → Verifier {k} 테스트 보강")
        for c in passed:
            for m in mods:
                key = f"{m}/c{c}"
                st = self.mod_state(key)
                st["status"] = "running"
                self.save_mod(key, st)
        self.save()
        return True

    def step_integrate(self, tree: dict, chosen: dict, tbs: list[str]) -> dict:
        lib = {tree["top"]["module"]: tree["top"]}
        for n, ch in tree["children"].items():
            lib[n] = self.modules()[f"{n}/c{chosen[n]}"]["ir"] if n in chosen else ch
        final = self.verify(lib, tree["top"]["module"], tbs, "integration")
        rtl = self.dir / "rtl"
        rtl.mkdir(exist_ok=True)
        mods = {n: Module(r) for n, r in lib.items()}
        for n, m in mods.items():
            e = emit(m, mods)
            (rtl / f"{n}.sv").write_text(e.text, encoding="utf-8")
            # 외부 리포트를 IR 노드에 대응시킬 때 쓴다
            (rtl / f"{n}.srcmap.json").write_text(json.dumps(e.sourcemap, ensure_ascii=False), encoding="utf-8")
            (self.dir / "ir" / f"{n}.ir.json").write_text(json.dumps(lib[n], ensure_ascii=False, indent=1), encoding="utf-8")
            if m.role in ("top", "block_top"):   # SDC 골격 (PI의 STA, SDC 검사 입력)
                from pipeline.emitter.sdc import emit_sdc
                (rtl / f"{n}.sdc").write_text(emit_sdc(m, mods), encoding="utf-8")
        for cell in self.cells:   # 표준 셀(동기화 셀 등)도 RTL과 함께 둔다
            if Path(cell).suffix == ".sv" and Path(cell).exists():
                (rtl / Path(cell).name).write_text(Path(cell).read_text(encoding="utf-8"), encoding="utf-8")
        self.finish_step("integration", {"ok": final["ok"], "e1": final["e1"], "e2": [{k: x[k] for k in ("verifier", "ok")} for x in final["e2"]],
                                         "py_equiv": final["py_equiv"]})
        return final

    def execute(self) -> dict:
        try:
            if not self.step_spec():
                self.state["status"] = "spec_rejected"
                return self.report()
            tree = self.step_architect()
            if tree is None:
                self.state["status"] = "failed"
                self.review("ARCHITECT_FAILED", "Architect가 모듈 트리를 만들지 못함")
                return self.report()
            tbs = self.step_verifiers(tree["top"])
            if tbs:
                tbs = self.apply_decisions(tree, tbs)
                tree = self.apply_lead_decisions(tree)
            if not tbs:
                self.state["status"] = "failed"
                self.review("VERIFIER_FAILED", "쓸 수 있는 Verifier 테스트가 없음")
                return self.report()
            self.step_external_repair(tree)
            chosen = self.step_modules(tree, tbs)
            mods = [n for n, c in tree["children"].items() if c.get("role") == "module"]
            if len(chosen) < len(mods):
                waiting = any(st["status"] == "waiting_approval" for st in self.modules().values())
                self.state["status"] = "waiting_approval" if waiting else "failed"
                return self.report()
            final = self.step_integrate(tree, chosen, tbs)
            self.state["status"] = "pass" if final["ok"] else "failed"
            if (self.state.get("seed") or {}).get("kind") == "external_repair" and final["ok"]:
                self.review("EXTERNAL_RECHECK", "외부 리포트 항목을 고쳤고 파이프라인 검증은 통과함. 외부 툴로 다시 확인해야 함", "integration")
        except BudgetExceeded as e:
            self.state["status"] = "budget_exceeded"
            self.review("BUDGET", str(e))
        except QuotaExceeded as e:
            # 끝난 단계와 작업은 저장되어 있다. 한도가 풀린 뒤 같은 명령으로 이어서 실행한다.
            self.state["status"] = "paused_quota"
            self.event("quota", str(e))
        return self.report()

    def report(self) -> dict:
        st = self.state
        rep = {"status": st["status"], "spec": st["spec"], "agent_runs": st["agent_runs"], "chosen": st.get("chosen"),
               "candidates": {k: {x: v.get(x) for x in ("status", "repairs", "lint", "reason", "arbiter")}
                              for k, v in self.modules().items()},
               "integration": st["steps"].get("integration"), "pending": st.get("pending", []),
               "review": st["review"], "events": st["events"]}
        self.save()
        (self.dir / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        return rep


INIT_RESET_CYCLES = 10
DIFF_RUNS = [("steady", 1), ("steady", 2), ("reset", 3), ("reset", 4)]


def emit_lib(lib: dict[str, dict], rtl: Path, cells: list[Path]) -> tuple[list[Path], dict]:
    """IR 묶음을 SV로 방출한다 → (파일 목록, 모듈별 소스맵)."""
    rtl.mkdir(parents=True, exist_ok=True)
    mods = {n: Module(r) for n, r in lib.items()}
    files, maps = list(cells), {}
    for n, m in mods.items():
        e = emit(m, mods)
        (rtl / f"{n}.sv").write_text(e.text, encoding="utf-8")
        files.append(rtl / f"{n}.sv")
        maps[n] = e.sourcemap
    return files, maps


def diff_cfg(top_ir: dict, mode: str, seed: int, model: str | None = None, harness_dir: Path | None = None,
             cycles: int = 3000, drain: int = 600) -> dict:
    from pipeline.tb.gen import harness_config, signal_widths

    return {"harness": harness_config(Module(top_ir)), "widths": signal_widths(top_ir), "mode": mode, "seed": seed,
            "cycles": cycles, "drain": drain, "model": model, "harness_dir": str(harness_dir) if harness_dir else None}


def run_diffsim(files: list[Path], top_ir: dict, mode: str, seed: int, build_dir: Path, model: str | None = None,
                harness_dir: Path | None = None, plan: dict | None = None, rebuild: bool = True) -> dict | None:
    """후보 하나에 차분 자극을 넣고 출력 기록을 돌려준다. 시뮬레이션이 끝나지 않거나 실패하면 None.
    plan을 주면 그 자극 계획을 쓴다 (축소할 때). rebuild=False면 이미 만든 시뮬레이터를 다시 쓴다."""
    build_dir = Path(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)
    cfg = diff_cfg(top_ir, mode, seed, model, harness_dir) | ({"plan": plan} if plan else {})
    (build_dir / "diff_cfg.json").write_text(json.dumps(cfg), encoding="utf-8")
    out = build_dir / "diff_out.json"
    out.unlink(missing_ok=True)
    pads, sim_top = pad_top(top_ir, build_dir / "pad")   # inout Block Top은 패드 래퍼를 top으로
    sim([*files, *pads], sim_top, "diffsim", ROOT / "pipeline" / "tb", build_dir,
        env={"DIFF_CFG": str((build_dir / "diff_cfg.json").resolve()), "DIFF_OUT": str(out.resolve())},
        harness_dir=harness_dir, rebuild=rebuild, timeout_s=300)
    return json.loads(out.read_text()) if out.exists() else None


def minimize_diff(files: dict[int, list[Path]], top_ir: dict, mode: str, seed: int, work: Path, model: str | None,
                  harness_dir: Path | None, budget: int = 150, sim_fn=None, plan_opts: dict | None = None) -> dict | None:
    """두 후보(files의 두 키)의 출력 차이를 유지하는 가장 작은 자극을 찾는다 (시뮬레이션만, 에이전트 없음).
    1) 차이 지점 뒤를 잘라 내고 2) 리셋, backpressure, 단독 입력 사건을 delta debugging으로 지운다.
    sim_fn(후보 키, 계획, rebuild) → 기록: 후보마다 빌드 방식이 다를 때 (P11 R6의 원본 RTL은 -G, include가 필요).
    plan_opts는 자극 계획 설정에 더한다 (R6의 `joint_reset`)."""
    ca, cb = sorted(files)
    cfg = diff_cfg(top_ir, mode, seed, model, harness_dir) | (plan_opts or {})
    plan = make_plan(cfg)
    built: set[int] = set()
    runs = 0
    last: dict = {}

    def differs(p: dict) -> dict | None:
        nonlocal runs
        runs += 1
        recs = []
        for c in (ca, cb):
            if sim_fn is not None:
                recs.append(sim_fn(c, p, c not in built))
            else:
                recs.append(run_diffsim(files[c], top_ir, mode, seed, work / f"c{c}", model, harness_dir, plan=p,
                                        rebuild=c not in built))
            built.add(c)
        if None in recs:
            d = {"kind": "sim_failed", "a_ok": recs[0] is not None, "b_ok": recs[1] is not None}
        else:
            d = compare(recs[0], recs[1])
        if d:
            last.update(diff=d, recs=recs)
        return d

    d = differs(plan)
    if not d:
        return None
    anchor = next((t[1] for t in (d.get("a"), d.get("b")) if t), None) or \
        max([t[1] for t in d.get("a_prev", []) + d.get("b_prev", [])], default=plan["total"])
    for margin in (50, 200, 800):
        tp = truncate(plan, anchor + margin)
        if differs(tp):
            plan = tp
            break
    eps, n = episodes(plan), 2
    while eps and runs < budget:
        n = min(n, len(eps))
        size = -(-len(eps) // n)
        for i in range(0, len(eps), size):
            chunk = eps[i:i + size]
            p2 = without(plan, chunk)
            if differs(p2):
                plan, eps = p2, eps[:i] + eps[i + size:]
                n = max(n - 1, 2)
                break
            if runs >= budget:
                break
        else:
            if n >= len(eps):
                break
            n = min(n * 2, len(eps))
    final = differs(plan) or last.get("diff")
    return {"plan": plan, "difference": final, "episodes": len(episodes(plan)), "total_cycles": plan["total"], "runs": runs,
            "inputs_accepted": {"A": last["recs"][0]["inputs"] if last.get("recs") and last["recs"][0] else None,
                                "B": last["recs"][1]["inputs"] if last.get("recs") and last["recs"][1] else None}}


def diff_window(rec: dict, diff: dict, limit: int = 300) -> list:
    """차이가 난 곳까지의 자극 사건(리셋, ready, 단독 입력 변화)."""
    ev = rec["events"]
    if rec.get("probe_at") is not None:
        last = [max([c for c in r if c < rec["probe_at"]], default=0) for r in rec["resets"].values()]
        start = min(last) - 100
        ev = [e for e in ev if e[1] >= start and e[1] <= rec["probe_at"]]
    elif diff.get("kind") == "data":
        ev = [e for e in ev if diff["a"][1] - 300 <= e[1] <= diff["a"][1]]
    return ev[-limit:]


def load_decisions(run_dir: Path) -> list[dict]:
    p = Path(run_dir) / "decisions.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else []


def _seq(st: dict) -> int:
    """Repair 작업 번호. 되돌릴 수 있는 횟수(repairs)와 달리 늘기만 해서 작업 이름(캐시 키)이 겹치지 않는다."""
    return st.get("repair_seq", st["repairs"])


def _assumption(a: dict) -> str:
    return (f"[{', '.join(a['affects'])}] " if a.get("affects") else "") + a["text"]


def _sim_type(rule: str) -> str:
    """sim 실패 분류. 시간 초과는 RTL이 멈춘 것으로 보고 Repair에 넘긴다. IR의 SVA assert 실패는 RTL 쪽(Repair),
    assume 실패는 테스트 입력이 설계 가정을 어긴 것(Verifier)."""
    if rule in ("AssertionError", "T1_INIT", "SVA_ASSERT"):
        return "sim_mismatch"
    return "sim_hang" if rule == "SIM_TIMEOUT" else "tb_error"


def _load_model(tb: str, harness_dir: Path | None = None):
    import importlib.util

    for p in (harness_dir, Path(tb).parent):
        if p and str(p) not in sys.path:
            sys.path.insert(0, str(p))
    spec = importlib.util.spec_from_file_location(f"vm_{abs(hash(tb))}", tb)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Model()


def verifier_diff(tbs: list[str], l1: dict, n: int = 300, seed: int = 7, harness_dir: Path | None = None) -> str | None:
    """참조 모델들에 같은 무작위 입력을 넣어 기대 출력을 비교한다 (Verifier 간 차분 검사)."""
    from pipeline.tb.gen import harness_config, signal_widths

    cfg = harness_config(Module(l1))
    widths = signal_widths(l1)
    ins = [b for b in cfg["bundles"] if not b["dut_drives"]]
    if not ins:
        return None
    try:
        models = [_load_model(t, harness_dir) for t in tbs]
    except Exception as e:  # 모델을 읽을 수 없으면 차분 검사를 할 수 없음
        raise ImportError(str(e)) from e
    rng = random.Random(seed)
    for i in range(n):
        b = rng.choice(ins)
        txn = {d: rng.getrandbits(widths.get(d, 8)) for d in b["data"]}
        outs = []
        for m in models:
            if hasattr(m, "legal") and not m.legal(b["name"], dict(txn)):
                outs.append(None)
                continue
            try:
                outs.append([(o[0], o[1]) for o in (m.on_input(b["name"], dict(txn)) or [])])
            except Exception as e:
                outs.append(f"예외 {type(e).__name__}")
        real = [o for o in outs if o is not None]
        if len({json.dumps(o, sort_keys=True, default=str) for o in real}) > 1:
            return f"입력 {i}번째 {b['name']} {txn}: {real}"
    return None


def candidate_diff(outputs: list[list[str]]) -> str | None:
    """후보마다 같은 Verifier 테스트에서 낸 실제 출력 트랜잭션을 비교한다."""
    base = outputs[0]
    for other in outputs[1:]:
        for a_dir, b_dir in zip(base, other):
            for res in sorted(Path(a_dir).glob("*.result.json")):
                b = Path(b_dir) / res.name
                if not b.exists():
                    continue
                oa, ob = json.loads(res.read_text()).get("outputs"), json.loads(b.read_text()).get("outputs")
                if oa != ob:
                    return f"{res.name.removesuffix('.result.json')}"
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="파이프라인 실행 (SPEC → RTL)")
    ap.add_argument("spec")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--roles", default=str(ROOT / "config" / "roles.json"))
    ap.add_argument("--cells", nargs="*")
    ap.add_argument("--seed", type=Path, help="재실행 정보(JSON). 처음 실행할 때만 읽는다")
    ap.add_argument("--no-isolate", action="store_true", help="격리하지 않음 (가짜 agent를 쓰는 시험 전용)")
    args = ap.parse_args(argv)
    seed = json.loads(args.seed.read_text(encoding="utf-8")) if args.seed else None
    run = Run(Path(args.run_dir), Path(args.spec), roles_mod.load(args.roles), [Path(c) for c in args.cells] if args.cells else None,
              isolate=not args.no_isolate, seed=seed)
    rep = run.execute()
    print(json.dumps({k: rep[k] for k in ("status", "agent_runs", "chosen", "candidates")}, ensure_ascii=False, indent=1))
    return 0 if rep["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
