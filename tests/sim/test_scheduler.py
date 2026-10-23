"""P4 스케줄러: 가짜 agent(역할별 정답 파일을 씀)로 SPEC부터 통합까지의 흐름, 재개, 사용량 한도 멈춤을 본다."""
import copy
import json
import shutil
import sys
from pathlib import Path

import pytest

pytest.importorskip("cocotb_tools.runner")
if not shutil.which("verilator"):
    pytest.skip("verilator 없음", allow_module_level=True)

from pipeline.agents.roles import Roles  # noqa: E402
from pipeline.agents.runner import AgentConfig  # noqa: E402
from pipeline.ir.model import frozen_l1  # noqa: E402
from pipeline.scheduler.run import Run  # noqa: E402
from pipeline.spec.l1 import block_top_l1  # noqa: E402
from pipeline.spec.parse import load  # noqa: E402
from test_pipeline import ROOT  # noqa: E402

G = ROOT / "tests" / "golden" / "axis_adapter"
SPEC = G / "block_spec.md"
FAKE = str(ROOT / "tests" / "fake_agent.py")


def _files(tmp_path, author_ir=None):
    """Architect: Block Top(axis_adapter) 아래 하위 모듈 axis_adapter_core 하나. Author: golden IR. Verifier: 손으로 쓴 하니스 TB."""
    top, _ = block_top_l1(load(SPEC))
    golden = json.loads((G / "ir" / "axis_adapter.ir.json").read_text())
    core = copy.deepcopy(golden)
    core["module"] = "axis_adapter_core"
    child_l1 = frozen_l1(core)
    top = {**top, "instances": [{"name": "u_core", "module": "axis_adapter_core",
                                 "params": {p["name"]: p["name"] for p in top["params"]}, "domain_map": {"sys": "sys"}}],
           "connections": [{"id": "c_s", "from": "self.s_axis", "to": "u_core.s_axis"},
                           {"id": "c_m", "from": "u_core.m_axis", "to": "self.m_axis"}]}
    tb = tmp_path / "tb.py"
    tb.write_text((G / "harness" / "tb_axis_adapter_h.py").read_text().replace(
        "class Model:\n", "class Model:\n    def legal(self, s, t):\n        return t['s_axis_tkeep'] in (1, 3, 7, 15)\n\n", 1))
    return {
        "Architect": {"out/ir/axis_adapter.ir.json": json.dumps(top), "out/ir/axis_adapter_core.ir.json": json.dumps(child_l1),
                      "out/assumptions.json": json.dumps({"assumptions": ["동작은 하위 모듈 하나(axis_adapter_core)에 둠"]})},
        "Author": {"out/axis_adapter_core.ir.json": json.dumps(author_ir or core)},
        "Verifier": {"out/test_axis_adapter.py": f"@{tb}"},
    }


def _roles(tmp_path, files, mode="roles", author_mode=None):
    env = {"FAKE_MODE": mode, "FAKE_FILES": json.dumps(files), "FAKE_LOG": str(tmp_path / "calls.log")}
    cfg = AgentConfig("fake", "Test", "fake", [sys.executable, FAKE], "m", env=env, timeout_s=120)
    agents = {r: cfg for r in ("architect", "author", "verifier", "repair", "arbiter")}
    if author_mode:
        agents["author"] = AgentConfig("fake-a", "Other", "fake", [sys.executable, FAKE], "m", env={**env, "FAKE_MODE": author_mode})
    return Roles(agents, {"author": 2, "verifier": 2}, {"repair_per_candidate": 2, "agent_retries": 0, "budget_agent_runs": 50})


def _calls(tmp_path):
    p = tmp_path / "calls.log"
    return p.read_text().split() if p.exists() else []


def test_spec_to_rtl_with_fake_agents(tmp_path):
    run = Run(tmp_path / "run", SPEC, _roles(tmp_path, _files(tmp_path)), isolate=False)
    rep = run.execute()
    assert rep["status"] == "pass", json.dumps(rep, ensure_ascii=False)[:2000]
    assert rep["chosen"] == {"axis_adapter_core": 0}
    assert all(c["status"] == "pass" for c in rep["candidates"].values()) and len(rep["candidates"]) == 2
    assert (tmp_path / "run" / "rtl" / "axis_adapter.sv").exists() and (tmp_path / "run" / "rtl" / "axis_adapter_core.sv").exists()
    codes = {r["code"] for r in rep["review"]}
    assert {"ASSUMPTION", "MODEL_FAMILY"} <= codes   # 가짜 agent는 Author와 Verifier가 같은 계열이라 경고가 남음
    assert not codes & {"VERIFIER_DIFF", "VERIFIER_DIFF_SKIPPED", "CANDIDATE_DIFF"}   # 같은 TB, 같은 IR
    assert sorted(_calls(tmp_path)) == ["Architect", "Author", "Author", "Verifier", "Verifier"]


def test_resume_after_interruption(tmp_path, monkeypatch):
    files = _files(tmp_path)
    run = Run(tmp_path / "run", SPEC, _roles(tmp_path, files), isolate=False)
    monkeypatch.setattr(Run, "step_modules", lambda self, *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        run.execute()
    assert sorted(_calls(tmp_path)) == ["Architect", "Verifier", "Verifier"]
    monkeypatch.undo()
    rep = Run(tmp_path / "run", SPEC, _roles(tmp_path, files), isolate=False).execute()   # 새 프로세스처럼 상태 파일에서 시작
    assert rep["status"] == "pass"
    assert sorted(_calls(tmp_path)) == ["Architect", "Author", "Author", "Verifier", "Verifier"]   # 끝난 작업은 다시 부르지 않음
    assert any(e["kind"] == "resume" for e in rep["events"])


def test_quota_pauses_and_frozen_change_is_rejected(tmp_path):
    rep = Run(tmp_path / "q", SPEC, _roles(tmp_path, _files(tmp_path), author_mode="quota"), isolate=False).execute()
    assert rep["status"] == "paused_quota"
    core = json.loads(_files(tmp_path)["Author"]["out/axis_adapter_core.ir.json"])
    core["ports"][0]["signals"][0]["type"] = "logic [15:0]"   # 동결된 L1을 바꿈
    rep = Run(tmp_path / "f", SPEC, _roles(tmp_path, _files(tmp_path, core)), isolate=False).execute()
    assert rep["status"] == "failed"
    assert all("Author 실패" in (c["reason"] or "") for c in rep["candidates"].values())


def _out_module() -> dict:
    """M쪽 폭의 valid/ready를 그대로 넘기는 모듈 (하위 모듈이 둘인 트리 시험용)."""
    names = ["tdata", "tkeep", "tvalid", "tready", "tlast", "tid", "tdest", "tuser"]
    types = {"tdata": "logic [M_DATA_WIDTH-1:0]", "tkeep": "logic [M_KEEP_WIDTH-1:0]", "tid": "logic [ID_WIDTH-1:0]",
             "tdest": "logic [DEST_WIDTH-1:0]", "tuser": "logic [USER_WIDTH-1:0]"}

    def sig(side, n, d):
        s = {"name": f"{side}_axis_{n}", "dir": d, "type": types.get(n, "logic")}
        return {**s, "kind": "wire"} if d == "output" else s
    ports = [{"bundle": "s_axis", "protocol": "valid_ready", "role": "sink", "domain": "sys",
              "signals": [sig("s", n, "output" if n == "tready" else "input") for n in names]},
             {"bundle": "m_axis", "protocol": "valid_ready", "role": "source", "domain": "sys",
              "signals": [sig("m", n, "input" if n == "tready" else "output") for n in names]}]
    outs = [n for n in names if n != "tready"]
    body = "\n".join([f"m_axis_{n} = s_axis_{n};" for n in outs] + ["s_axis_tready = m_axis_tready;"])
    return {"module": "axis_adapter_out", "role": "module",
            "params": [{"name": p, "type": "int", "default": d} for p, d in
                       (("M_DATA_WIDTH", "8"), ("M_KEEP_WIDTH", "1"), ("ID_WIDTH", "8"), ("DEST_WIDTH", "8"), ("USER_WIDTH", "1"))],
            "domains": [{"name": "sys", "clock": "clk", "edge": "pos", "reset": {"port": "rst", "active": "high", "async": False}}],
            "ports": ports, "types": [], "signals": [],
            "blocks": [{"id": "k_pass", "kind": "comb", "reads": [f"s_axis_{n}" for n in outs] + ["m_axis_tready"],
                        "writes": [f"m_axis_{n}" for n in outs] + ["s_axis_tready"], "body": body}]}


def _files2(tmp_path, core_ir=None, repair=None):
    """하위 모듈 둘: axis_adapter_core(golden) → axis_adapter_out(그대로 넘김). 출력을 구동하는 것은 out."""
    f = _files(tmp_path)
    top = json.loads(f["Architect"]["out/ir/axis_adapter.ir.json"])
    core = json.loads(f["Author"]["out/axis_adapter_core.ir.json"])
    out = _out_module()
    top["instances"].append({"name": "u_out", "module": "axis_adapter_out",
                             "params": {p: p for p in ("M_DATA_WIDTH", "M_KEEP_WIDTH", "ID_WIDTH", "DEST_WIDTH", "USER_WIDTH")},
                             "domain_map": {"sys": "sys"}})
    top["connections"] = [{"id": "c_s", "from": "self.s_axis", "to": "u_core.s_axis"},
                          {"id": "c_mid", "from": "u_core.m_axis", "to": "u_out.s_axis"},
                          {"id": "c_m", "from": "u_out.m_axis", "to": "self.m_axis"}]
    f["Architect"]["out/ir/axis_adapter.ir.json"] = json.dumps(top)
    f["Architect"]["out/ir/axis_adapter_out.ir.json"] = json.dumps(frozen_l1(out))
    f["Architect"]["out/contracts.json"] = json.dumps({"contracts": [
        {"from": "u_core.m_axis", "to": "u_out.s_axis", "carries": "변환한 출력 비트", "timing": "out은 조합으로 그대로 넘김"}]})
    f["Author"] = {"out/axis_adapter_core.ir.json": json.dumps(core_ir or core), "out/axis_adapter_out.ir.json": json.dumps(out)}
    if repair is not None:
        f["Repair"] = {"out/patches.json": json.dumps(repair)}
    return f


def test_two_modules_verified_together(tmp_path):
    rep = Run(tmp_path / "run", SPEC, _roles(tmp_path, _files2(tmp_path)), isolate=False).execute()
    assert rep["status"] == "pass", json.dumps(rep, ensure_ascii=False)[:3000]
    assert rep["chosen"] == {"axis_adapter_core": 0, "axis_adapter_out": 0}
    assert len(rep["candidates"]) == 4 and all(c["status"] == "pass" for c in rep["candidates"].values())
    assert sorted(_calls(tmp_path)) == ["Architect"] + ["Author"] * 4 + ["Verifier"] * 2
    # Architect의 연결 계약이 양쪽 모듈의 Author에게 감
    for m in ("axis_adapter_core", "axis_adapter_out"):
        c = json.loads((tmp_path / "run" / "work" / f"author__{m}__c0" / "contracts.json").read_text())["contracts"]
        assert [(x["from"], x["to"]) for x in c] == [("u_core.m_axis", "u_out.s_axis")]


def test_repair_goes_to_driver_then_suspected_sibling(tmp_path):
    sys.path.insert(0, str(ROOT / "tests" / "golden"))
    from bugs import BUGS

    core = json.loads(_files(tmp_path)["Author"]["out/axis_adapter_core.ir.json"])
    BUGS["adapter_shift"][1](core)
    # Repair는 항상 core를 지목한다: 출력 구동 모듈(out)의 Repair → core의 Repair. core가 자신을 지목하면 게이트가 거부
    files = _files2(tmp_path, core, [{"op": "suspect_module", "module": "axis_adapter_core", "reason": "데이터가 core에서 틀림"}])
    rep = Run(tmp_path / "run", SPEC, _roles(tmp_path, files), isolate=False).execute()
    assert rep["status"] == "failed"
    assert all("형제 모듈이 아님: axis_adapter_core" in c["reason"] for c in rep["candidates"].values())
    sus = [e["msg"] for e in rep["events"] if e["kind"] == "suspect"]
    assert sum(m.startswith("axis_adapter_out → axis_adapter_core") for m in sus) == 2   # 묶음 둘 다 out에서 시작
    agents = [e["msg"].split(":")[0] for e in rep["events"] if e["kind"] == "agent" and e["msg"].startswith("repair/")]
    assert sorted(agents) == ["repair/axis_adapter_core/c0/r0", "repair/axis_adapter_core/c1/r0",
                              "repair/axis_adapter_out/c0/r0", "repair/axis_adapter_out/c1/r0"]
    # 불일치 리포트의 팬인은 인스턴스 입력을 거쳐 형제 모듈로 이어지고, 인스턴스 포트 값이 트레이스에 있다
    fail = json.loads((tmp_path / "run" / "work" / "repair__axis_adapter_out__c0__r0" / "failure.json").read_text())
    assert fail["report"]["fanin"]["nodes"] == ["instances/u_out", "instances/u_core"]
    assert "u_out.s_axis_tdata" in fail["report"]["fanin"]["values"][-1]


def _files_wrong_v1(tmp_path):
    """Verifier 1의 테스트에만 틀린 속성이 있음. Arbiter는 test_wrong, 승인된 수정 작업은 올바른 테스트를 씀."""
    f = _files(tmp_path)
    good = Path(f["Verifier"]["out/test_axis_adapter.py"][1:])
    bad = tmp_path / "tb_bad.py"
    bad.write_text(good.read_text().replace(
        "    await h.start()\n",
        "    h.add_property('bogus', 'sys', lambda s, c: 'always fails' if c == 30 else None, 'ADP-01')\n    await h.start()\n", 1))
    verdict = {"verdict": "test_wrong", "reqs": ["ADP-01"], "reason": "SPEC에 없는 검사", "proposal": "bogus 속성을 지운다"}
    return {"Architect": f["Architect"], "Author": f["Author"],
            "Verifier|기댓값 수정": {"out/test_axis_adapter.py": f"@{good}"},
            "Verifier|verifier__v1": {"out/test_axis_adapter.py": f"@{bad}"},
            "Verifier": {"out/test_axis_adapter.py": f"@{good}"},
            "Arbiter": {"out/verdict.json": json.dumps(verdict)}}


def test_approved_expectation_change_revises_test_and_resumes(tmp_path):
    from pipeline.scheduler import approve

    files = _files_wrong_v1(tmp_path)
    rep = Run(tmp_path / "run", SPEC, _roles(tmp_path, files), isolate=False).execute()
    assert rep["status"] == "waiting_approval"
    items = approve.pending(tmp_path / "run")
    # 두 후보가 같은 Verifier의 같은 테스트에서 같은 이유로 실패: 항목 하나로 묶인다 (P8 회고)
    assert [(p["verifier"], sorted([p["c"]] + [x["c"] for x in p.get("also", [])])) for p in items] == [(1, [0, 1])]
    first = items[0]["id"]
    with pytest.raises(ValueError):
        approve.decide(tmp_path / "run", [first], "approved", by="")   # 결정한 사람이 필요
    approve.decide(tmp_path / "run", [first], "approved", by="design-lead", note="SPEC에 없는 검사")
    with pytest.raises(ValueError):
        approve.decide(tmp_path / "run", [first], "rejected", by="x")   # 두 번 결정할 수 없음
    rep = Run(tmp_path / "run", SPEC, _roles(tmp_path, files), isolate=False).execute()
    assert rep["status"] == "pass", json.dumps(rep, ensure_ascii=False)[:3000]
    calls = _calls(tmp_path)
    assert calls.count("Verifier|기댓값_수정") == 1 and calls.count("Arbiter") == 2   # 수정 한 번, 다시 판정하지 않음
    decisions = {p["id"]: p["decision"]["decision"] for p in rep["pending"]}
    assert list(decisions.values()) == ["approved"]   # 한 번 결정으로 두 후보가 함께 풀림
    exp = [r for r in rep["review"] if r["code"] == "EXPECTATION_CHANGE"]
    assert all(r["decided"]["by"] == "design-lead" and "승인 대기" not in r["message"] for r in exp)   # 결정 결과로 바뀜
    st = json.loads((tmp_path / "run" / "state.json").read_text())
    assert len(st["tb_history"]) == 1 and st["tb_history"][0]["verifier"] == 1
    d = json.loads((tmp_path / "run" / "decisions.json").read_text())[0]
    assert d["by"] == "design-lead" and d["reqs"] == ["ADP-01"] and d["spec_sha256"] and d["run_id"] == "run"
    assert approve.pending(tmp_path / "run") == []


def test_rejected_expectation_change_goes_to_repair(tmp_path):
    from pipeline.scheduler import approve

    files = _files_wrong_v1(tmp_path)
    Run(tmp_path / "run", SPEC, _roles(tmp_path, files), isolate=False).execute()
    approve.decide(tmp_path / "run", [p["id"] for p in approve.pending(tmp_path / "run")], "rejected", by="design-lead")
    rep = Run(tmp_path / "run", SPEC, _roles(tmp_path, files), isolate=False).execute()
    calls = _calls(tmp_path)
    assert calls.count("Arbiter") == 2 and "Verifier|기댓값_수정" not in calls   # 다시 판정하지 않고, 테스트도 고치지 않음
    # 가짜 Repair는 patch를 쓰지 않으므로 게이트에서 실패한다: Repair로 넘어갔다는 증거
    assert rep["status"] == "failed"
    assert all("Repair 실패" in (c["reason"] or "") for c in rep["candidates"].values())


def test_candidate_diffsim_finds_test_gap_and_verifier_closes_it(tmp_path):
    """두 후보가 약한 테스트를 모두 통과하지만 동작이 다름 → 차분 시뮬레이션이 찾고, Verifier가 테스트를 보강하면 틀린 후보가 떨어진다."""
    sys.path.insert(0, str(ROOT / "tests" / "golden"))
    from bugs import BUGS

    f = _files(tmp_path)
    good_ir = json.loads(f["Author"]["out/axis_adapter_core.ir.json"])
    bad_ir = copy.deepcopy(good_ir)
    BUGS["adapter_shift"][1](bad_ir)
    good_tb = Path(f["Verifier"]["out/test_axis_adapter.py"][1:])
    weak = tmp_path / "tb_weak.py"   # 기대 출력을 내지 않는 테스트 (프로토콜만 봄)
    weak.write_text(good_tb.read_text().replace("    def on_input(self, stream, t):\n",
                                                "    def on_input(self, stream, t):\n        return []\n", 1)
                    .replace("class Model:\n", "class Model:\n    streams = set()\n", 1))
    verdict = {"verdict": "rtl_wrong", "reqs": ["ADP-01"], "reason": "바이트 순서가 SPEC과 다름", "proposal": ""}
    files = {"Architect": f["Architect"],
             "Author|author__axis_adapter_core__c1": {"out/axis_adapter_core.ir.json": json.dumps(bad_ir)},
             "Author": {"out/axis_adapter_core.ir.json": json.dumps(good_ir)},
             "Verifier|후보 간 차분": {"out/test_axis_adapter.py": f"@{good_tb}"},
             "Verifier": {"out/test_axis_adapter.py": f"@{weak}"},
             "Arbiter": {"out/verdict.json": json.dumps(verdict)}}
    rep = Run(tmp_path / "run", SPEC, _roles(tmp_path, files), isolate=False).execute()
    assert rep["status"] == "pass", json.dumps(rep, ensure_ascii=False)[:3000]
    assert rep["chosen"] == {"axis_adapter_core": 0}
    assert rep["candidates"]["axis_adapter_core/c1"]["status"] == "failed"   # 보강된 테스트 → Arbiter rtl_wrong → Repair(가짜, 실패)
    calls = _calls(tmp_path)
    assert calls.count("Verifier|후보_간_차분") == 1 and calls.count("Arbiter") == 1
    st = json.loads((tmp_path / "run" / "state.json").read_text())
    d = st["steps"]["candidate_diffsim_r0"]["difference"]
    assert d["mode"] == "steady" and d["difference"]["kind"] == "data"
    mini = st["steps"]["candidate_diffsim_r0_min"]   # 데이터 오류는 backpressure와 무관하므로 사건이 모두 지워진다
    assert mini["episodes"] == 0 and mini["difference"]["kind"] == "data" and mini["total_cycles"] < 3600
    assert st["tb_history"][-1]["diff_round"] == 0


def test_augmented_test_that_cannot_split_candidates_is_not_adopted(tmp_path):
    """보강된 테스트가 후보를 가르지 못하면(모두 통과) 다시 시도하고, 끝내 못 가르면 채택하지 않고 검토 항목으로 남긴다."""
    sys.path.insert(0, str(ROOT / "tests" / "golden"))
    from bugs import BUGS

    f = _files(tmp_path)
    good_ir = json.loads(f["Author"]["out/axis_adapter_core.ir.json"])
    bad_ir = copy.deepcopy(good_ir)
    BUGS["adapter_shift"][1](bad_ir)
    good_tb = Path(f["Verifier"]["out/test_axis_adapter.py"][1:])
    weak = tmp_path / "tb_weak.py"
    weak.write_text(good_tb.read_text().replace("    def on_input(self, stream, t):\n",
                                                "    def on_input(self, stream, t):\n        return []\n", 1)
                    .replace("class Model:\n", "class Model:\n    streams = set()\n", 1))
    weak2 = tmp_path / "tb_weak2.py"   # 바뀌었지만 여전히 출력을 보지 않음
    weak2.write_text(weak.read_text() + "\n# 보강 시도\n")
    files = {"Architect": f["Architect"],
             "Author|author__axis_adapter_core__c1": {"out/axis_adapter_core.ir.json": json.dumps(bad_ir)},
             "Author": {"out/axis_adapter_core.ir.json": json.dumps(good_ir)},
             "Verifier|후보 간 차분": {"out/test_axis_adapter.py": f"@{weak2}"},
             "Verifier": {"out/test_axis_adapter.py": f"@{weak}"}}
    rep = Run(tmp_path / "run", SPEC, _roles(tmp_path, files), isolate=False).execute()
    assert rep["status"] == "pass"
    assert _calls(tmp_path).count("Verifier|후보_간_차분") == 2   # 두 번 시도
    assert any(r["code"] == "CANDIDATE_DIFF" and "가르지 못함" in r["message"] for r in rep["review"])
    st = json.loads((tmp_path / "run" / "state.json").read_text())
    assert "tb_history" not in st   # 채택하지 않음


def test_python_fragment_candidate_with_translator_check(tmp_path):
    """후보 1은 Python 조각(같은 동작을 Python으로 쓴 IR). 번역기 검사를 거쳐 SV 후보와 함께 통과하고, 후보 간 차분도 없다."""
    f = _files(tmp_path)
    py = json.loads((ROOT / "tests" / "fixtures" / "py" / "axis_adapter.ir.json").read_text())
    py["module"] = "axis_adapter_core"
    files = {"Architect": f["Architect"],
             "Author|author__axis_adapter_core__c1": {"out/axis_adapter_core.ir.json": json.dumps(py)},
             "Author": f["Author"], "Verifier": f["Verifier"]}
    roles = _roles(tmp_path, files)
    roles.author_langs = ("sv", "py")
    rep = Run(tmp_path / "run", SPEC, roles, isolate=False).execute()
    assert rep["status"] == "pass", json.dumps(rep, ensure_ascii=False)[:3000]
    assert all(c["status"] == "pass" for c in rep["candidates"].values())
    c1 = json.loads((tmp_path / "run" / "modules" / "axis_adapter_core__c1.json").read_text())
    assert c1["lang"] == "py" and c1["ir"]["lang"] == "py"
    eq = list((tmp_path / "run" / "verify").glob("*_c1_*/pyequiv_axis_adapter_core/pyequiv_result.json"))
    assert eq and json.loads(eq[0].read_text())["ok"]
    assert not list((tmp_path / "run" / "verify").glob("*_c0_*/pyequiv_*"))   # SV 후보는 번역기 검사가 없음
    assert not any(r["code"] in ("CANDIDATE_DIFF", "PY_EQUIV") for r in rep["review"])


def _files_conflict(tmp_path):
    """하위 모듈 둘(core → out)에서 core에 버그. out의 Repair는 core를, core의 Repair는 out을 지목해 충돌한다.
    lead가 core를 지정하면 그 Repair가 고치고, Architect 재구성을 요청하면 하위 모듈 하나(core)짜리 트리로 다시 정한다."""
    sys.path.insert(0, str(ROOT / "tests" / "golden"))
    from bugs import BUGS

    good = json.loads(_files(tmp_path)["Author"]["out/axis_adapter_core.ir.json"])
    core = copy.deepcopy(good)
    BUGS["adapter_shift"][1](core)
    f = _files2(tmp_path, core)
    fix = [{"op": "set", "target": "blocks/k_data", "field": "body",
            "value": next(b for b in good["blocks"] if b["id"] == "k_data")["body"]}]
    single = _files(tmp_path)
    return {"Architect|재구성 요청": single["Architect"], "Architect": f["Architect"],
            "Author|__a1": single["Author"], "Author": f["Author"], "Verifier": f["Verifier"],
            "Repair|lead 지정": {"out/patches.json": json.dumps(fix)},
            "Repair|repair__axis_adapter_out": {"out/patches.json": json.dumps(
                [{"op": "suspect_module", "module": "axis_adapter_core", "reason": "데이터가 core에서 틀림"}])},
            "Repair|repair__axis_adapter_core": {"out/patches.json": json.dumps(
                [{"op": "suspect_module", "module": "axis_adapter_out", "reason": "core 출력은 맞고 out이 틀림"}])},
            # 충돌을 lead에게 올리기 전 Arbiter 판정: 테스트는 맞음
            "Arbiter": {"out/verdict.json": json.dumps({"verdict": "rtl_wrong", "reqs": ["ADP-01"], "reason": "SPEC대로 기댓값이 맞음"})}}


def _conflicts(tmp_path, files):
    from pipeline.scheduler import approve

    rep = Run(tmp_path / "run", SPEC, _roles(tmp_path, files), isolate=False).execute()
    assert rep["status"] == "waiting_approval", json.dumps(rep, ensure_ascii=False)[:3000]
    items = {p["c"]: p for p in approve.pending(tmp_path / "run")}
    assert sorted(items) == [0, 1] and all(p["kind"] == "repair_conflict" for p in items.values())
    p = items[0]
    assert p["chain"] == ["axis_adapter_out", "axis_adapter_core", "axis_adapter_out"]
    assert [(x["module"], x["suspects"]) for x in p["claims"]] == [("axis_adapter_out", "axis_adapter_core"),
                                                                    ("axis_adapter_core", "axis_adapter_out")]
    assert "REPAIR_CONFLICT" in {r["code"] for r in rep["review"]}
    return items


def test_repair_conflict_lead_pins_module(tmp_path):
    from pipeline.scheduler import approve

    files = _files_conflict(tmp_path)
    items = _conflicts(tmp_path, files)
    run = tmp_path / "run"
    with pytest.raises(ValueError, match="approved"):
        approve.decide(run, [items[0]["id"]], "approved", by="lead")   # 충돌은 승인/거절이 아님
    with pytest.raises(ValueError, match="고칠 모듈"):
        approve.decide(run, [items[0]["id"]], "repair", by="lead", module="nope")
    for p in items.values():
        approve.decide(run, [p["id"]], "repair", by="lead", note="shift 양이 core에서 틀림", module="axis_adapter_core")
    rep = Run(run, SPEC, _roles(tmp_path, files), isolate=False).execute()
    assert rep["status"] == "pass", json.dumps(rep, ensure_ascii=False)[:3000]
    calls = _calls(tmp_path)
    assert calls.count("Repair|lead_지정") == 2 and calls.count("Architect") == 1   # 지정한 모듈의 Repair만, 재구성 없음
    lead = json.loads((run / "work" / "repair__axis_adapter_core__c0__r1" / "lead.json").read_text())
    assert lead["note"] == "shift 양이 core에서 틀림" and lead["module"] == "axis_adapter_core"
    assert sorted(p["decision"]["module"] for p in rep["pending"]) == ["axis_adapter_core"] * 2


def test_repair_conflict_lead_requests_architect_revision(tmp_path):
    from pipeline.scheduler import approve

    files = _files_conflict(tmp_path)
    items = _conflicts(tmp_path, files)
    run = tmp_path / "run"
    approve.decide(run, [items[0]["id"]], "architect", by="lead", note="출력 시점과 ready를 한 모듈이 정해야 함")
    # 후보 1의 충돌은 결정하지 않았지만 트리가 바뀌므로 대체됨으로 닫힌다
    rep = Run(run, SPEC, _roles(tmp_path, files), isolate=False).execute()
    assert rep["status"] == "pass", json.dumps(rep, ensure_ascii=False)[:3000]
    assert rep["chosen"] == {"axis_adapter_core": 0}
    calls = _calls(tmp_path)
    assert calls.count("Architect|재구성_요청") == 1 and calls.count("Author|__a1") == 2
    conflict = json.loads((run / "work" / "architect__rev1" / "conflict.json").read_text())
    assert conflict[0]["lead_note"] == "출력 시점과 ready를 한 모듈이 정해야 함" and conflict[0]["chain"][0] == "axis_adapter_out"
    assert (run / "work" / "architect__rev1" / "current" / "axis_adapter_out.ir.json").exists()
    st = json.loads((run / "state.json").read_text())
    assert st["arch_rev"] == 1 and sorted(st["steps"]["architect"]["children"]) == ["axis_adapter_core"]
    assert sorted(p.name for p in (run / "modules_a0").glob("*.json"))[0] == "axis_adapter_core__c0.json"   # 이전 후보 보관
    assert sorted(p.name for p in (run / "modules").glob("*.json")) == ["axis_adapter_core__c0.json", "axis_adapter_core__c1.json"]
    res = {p["c"]: p["decision"].get("result", p["decision"]["decision"]) for p in rep["pending"]}
    assert res == {0: "architect", 1: "superseded"} and approve.pending(run) == []
    assert "ARCHITECTURE_REVISED" in {r["code"] for r in rep["review"]}


def _files_interface_change(tmp_path, verdict):
    """두 Verifier 모두 틀린 속성으로 실패하고, Repair는 L1 변경을 요청함. Arbiter는 실패한 Verifier마다 판정."""
    f = _files_wrong_v1(tmp_path)
    good = f["Verifier"]
    bad = f.pop("Verifier|verifier__v1")
    v = {"verdict": verdict, "reqs": ["ADP-01"], "reason": "SPEC보다 엄격한 기댓값" if verdict == "test_wrong" else "SPEC대로임",
         "proposal": "bogus 속성을 지운다"}
    return {"Architect": f["Architect"], "Author": f["Author"], "Verifier|기댓값 수정": good, "Verifier": bad,
            "Repair": {"out/patches.json": json.dumps([{"op": "request_interface_change", "reason": "리셋 포트를 읽어야 함"}])},
            "Arbiter|__ic__": {"out/verdict.json": json.dumps(v)}}


def test_interface_change_request_goes_to_arbiter_first(tmp_path):
    from pipeline.scheduler import approve

    files = _files_interface_change(tmp_path, "test_wrong")
    run = tmp_path / "run"
    rep = Run(run, SPEC, _roles(tmp_path, files), isolate=False).execute()
    assert rep["status"] == "waiting_approval", json.dumps(rep, ensure_ascii=False)[:3000]
    items = approve.pending(run)
    # Verifier마다 항목 하나, 두 후보가 같은 이유로 실패해 묶임
    assert sorted((p["verifier"], sorted([p["c"]] + [x["c"] for x in p.get("also", [])])) for p in items) == [(0, [0, 1]), (1, [0, 1])]
    assert all(p["repair_claim"]["reason"] == "리셋 포트를 읽어야 함" for p in items)
    mm = json.loads((run / "work" / "arbiter__axis_adapter_core__c0__r0__ic__v0" / "mismatch.json").read_text())
    assert mm["repair_claim"]["module"] == "axis_adapter_core"
    assert "INTERFACE_CHANGE" not in {r["code"] for r in rep["review"]}
    # 후보가 기다리는 항목 중 일부만 결정하면 아직 다시 돌리지 않는다
    approve.decide(run, [p["id"] for p in items if p["verifier"] == 0], "approved", by="design-lead")
    rep = Run(run, SPEC, _roles(tmp_path, files), isolate=False).execute()
    assert rep["status"] == "waiting_approval"
    assert _calls(tmp_path).count("Verifier|기댓값_수정") == 1
    approve.decide(run, [p["id"] for p in approve.pending(run)], "approved", by="design-lead")
    rep = Run(run, SPEC, _roles(tmp_path, files), isolate=False).execute()
    assert rep["status"] == "pass", json.dumps(rep, ensure_ascii=False)[:3000]
    assert _calls(tmp_path).count("Verifier|기댓값_수정") == 2


def test_interface_change_stands_when_arbiter_says_rtl_wrong(tmp_path):
    files = _files_interface_change(tmp_path, "rtl_wrong")
    rep = Run(tmp_path / "run", SPEC, _roles(tmp_path, files), isolate=False).execute()
    assert rep["status"] == "failed"
    assert all(c["status"] == "interface_change" for c in rep["candidates"].values())
    assert "INTERFACE_CHANGE" in {r["code"] for r in rep["review"]} and not rep["pending"]
    assert _calls(tmp_path).count("Arbiter|__ic__") == 4   # 후보 둘 × 실패한 Verifier 둘


def test_skeleton_lint_error_stops_as_generator_bug(tmp_path, monkeypatch):
    """방출한 골격(조각 밖)의 lint 에러는 생성기 버그: Repair나 TB 오류로 가지 않고 후보를 멈춘다."""
    from pipeline.scheduler import run as run_mod

    real = run_mod.emit

    def broken(m, lib=None):
        e = real(m, lib)
        if m.role == "block_top":
            e.text = e.text.replace("endmodule", "  logic [NOPE-1:0] bad_wire;\nendmodule")
        return e
    monkeypatch.setattr(run_mod, "emit", broken)
    rep = Run(tmp_path / "run", SPEC, _roles(tmp_path, _files(tmp_path)), isolate=False).execute()
    assert rep["status"] == "failed"
    assert all("생성기 버그" in (c["reason"] or "") for c in rep["candidates"].values())
    codes = {r["code"] for r in rep["review"]}
    assert "GENERATOR_BUG" in codes and "TB_ERROR" not in codes
    assert "Repair" not in _calls(tmp_path)


def _files_repeat(tmp_path, verdict):
    """두 Verifier 모두 같은 틀린 속성으로 실패하고, Repair는 매번 다른 (효과 없는) patch를 냄. 두 번 뒤 Arbiter가 판정."""
    f = _files_wrong_v1(tmp_path)
    good = f["Verifier"]
    bad = f.pop("Verifier|verifier__v1")
    body = json.loads(f["Author"]["out/axis_adapter_core.ir.json"])
    k = next(b for b in body["blocks"] if b["id"] == "k_ports")["body"]
    v = {"verdict": verdict, "reqs": ["ADP-01"], "reason": "SPEC에 없는 검사", "proposal": "bogus 속성을 지운다"}
    files = {"Architect": f["Architect"], "Author": f["Author"], "Verifier|기댓값 수정": good, "Verifier": bad,
             "Arbiter|__repeat__": {"out/verdict.json": json.dumps(v)}}
    for n in range(5):   # Repair r0, r1, ...: 주석만 다른 body (IR 해시가 매번 다름)
        files[f"Repair|__r{n}"] = {"out/patches.json": json.dumps(
            [{"op": "set", "target": "blocks/k_ports", "field": "body", "value": f"// repair {n}\n{k}"}])}
    return files


def test_repeated_failure_goes_to_arbiter_before_limit(tmp_path):
    from pipeline.scheduler import approve

    files = _files_repeat(tmp_path, "test_wrong")
    run = tmp_path / "run"
    rep = Run(run, SPEC, _roles(tmp_path, files), isolate=False).execute()
    assert rep["status"] == "waiting_approval", json.dumps(rep, ensure_ascii=False)[:3000]
    calls = _calls(tmp_path)
    assert calls.count("Arbiter|__repeat__") == 4   # 후보 둘 × 실패한 Verifier 둘, 한 번씩
    assert all(c["repairs"] == 2 for c in rep["candidates"].values())   # Repair 두 번 뒤, 상한(2) 전에
    items = approve.pending(run)
    assert all(p["repeated_failure"]["repairs"] == 2 for p in items)
    approve.decide(run, [p["id"] for p in items], "approved", by="design-lead")
    rep = Run(run, SPEC, _roles(tmp_path, files), isolate=False).execute()
    assert rep["status"] == "pass", json.dumps(rep, ensure_ascii=False)[:3000]


def test_repeated_failure_rtl_wrong_keeps_repairing(tmp_path):
    files = _files_repeat(tmp_path, "rtl_wrong")
    rep = Run(tmp_path / "run", SPEC, _roles(tmp_path, files), isolate=False).execute()
    assert rep["status"] == "failed"
    assert all("Repair 상한" in (c["reason"] or "") for c in rep["candidates"].values())
    assert _calls(tmp_path).count("Arbiter|__repeat__") == 4   # 같은 실패는 한 번만 판정


def test_lead_pinned_repair_that_regresses_is_rolled_back(tmp_path):
    """lead가 지정한 모듈의 Repair 뒤 실패한 테스트가 늘면 그 Repair를 되돌리고 lead에게 다시 묻는다.
    테스트벤치에 처음부터 통과하는 테스트(리셋 뒤 조용함)를 더하고, 지정 Repair가 그 테스트까지 깨뜨리게 한다."""
    from pipeline.scheduler import approve

    files = _files_conflict(tmp_path)
    tb = Path(files["Verifier"]["out/test_axis_adapter.py"][1:])
    quiet = tmp_path / "tb_quiet.py"
    quiet.write_text(tb.read_text() + '''

@cocotb.test()
async def quiet_after_reset(dut):
    h = make(dut, Model(), name="quiet_after_reset")
    await h.start()
    await h.drain(extra=20)
    h.finish()
''')
    files["Verifier"] = {"out/test_axis_adapter.py": f"@{quiet}"}
    files["Repair|lead 지정"] = {"out/patches.json": json.dumps([
        {"op": "set", "target": "blocks/k_ctl", "field": "body", "value": "m_axis_tvalid <= 1'b1;\ns_axis_tvalid_reg <= 1'b0;"},
        {"op": "set", "target": "blocks/k_ctl", "field": "reads", "value": []}])}
    items = _conflicts(tmp_path, files)
    run = tmp_path / "run"
    before = json.loads((run / "modules" / "axis_adapter_core__c0.json").read_text())["ir"]
    for p in items.values():
        approve.decide(run, [p["id"]], "repair", by="lead", module="axis_adapter_core")
    rep = Run(run, SPEC, _roles(tmp_path, files), isolate=False).execute()
    assert rep["status"] == "waiting_approval", json.dumps(rep, ensure_ascii=False)[:3000]
    again = [p for p in approve.pending(run) if p.get("regressed")]
    assert sorted(p["c"] for p in again) == [0, 1] and all(p["chain"] == ["axis_adapter_core"] for p in again)
    assert "LEAD_REPAIR_REGRESSED" in {r["code"] for r in rep["review"]}
    st = json.loads((run / "modules" / "axis_adapter_core__c0.json").read_text())
    assert st["ir"] == before and "lead_repair" not in st   # 되돌리고 지정도 풀림


def test_failure_without_harness_result_still_reports_test_and_message(tmp_path):
    """테스트 안의 assert로 실패해 하니스 결과 파일이 없어도, Repair와 lead가 볼 첫 실패(테스트와 메시지)는 남긴다."""
    run = Run.__new__(Run)
    (tmp_path / "out").mkdir()
    v = {"out": str(tmp_path / "out"), "findings": [{"test": "sc02_reset", "rule": "AssertionError", "message": "tlast too early"}]}
    rep = run.failure_report({}, "t", v)
    assert rep["report"]["first_failure"] == {"check": "assertion", "test": "sc02_reset", "message": "tlast too early"}
    assert rep["classified"][0]["type"] == "sim_mismatch"


def test_review_noise_assumptions_and_model_family(tmp_path):
    """가정은 L1, 타이밍, 리셋, SPEC의 빈 곳을 건드리는 것만 검토 항목. MODEL_FAMILY는 처음 보는 역할 설정에서만."""
    files = _files(tmp_path)
    files["Architect"]["out/assumptions.json"] = json.dumps({"assumptions": [
        {"text": "동작은 하위 모듈 하나에 둠", "affects": []},
        {"text": "출력은 입력 다음 클럭", "affects": ["timing"]},
        "표시 없는 가정"]})
    rep = Run(tmp_path / "a", SPEC, _roles(tmp_path, files), isolate=False).execute()
    msgs = sorted(r["message"] for r in rep["review"] if r["code"] == "ASSUMPTION")
    assert msgs == ["[timing] 출력은 입력 다음 클럭", "표시 없는 가정"]
    assert "MODEL_FAMILY" in {r["code"] for r in rep["review"]}
    rep = Run(tmp_path / "b", SPEC, _roles(tmp_path, files), isolate=False).execute()   # 같은 역할 설정으로 새 실행
    assert "MODEL_FAMILY" not in {r["code"] for r in rep["review"]}


def test_comb_path_constraint_is_reviewed(tmp_path, monkeypatch):
    """SPEC 9절이 입력→출력 조합 경로를 막으면 통과한 후보의 경로를 검토 항목으로 낸다 (실패로 만들지는 않음)."""
    from pipeline.scheduler import run as run_mod

    spec = tmp_path / "spec.md"
    spec.write_text(SPEC.read_text().replace("### 금지 사항\n", "### 금지 사항\n- 입출력 사이의 조합 통과 경로(feedthrough)\n", 1))
    seen = []

    def fake(m, lib, rule):
        seen.append(rule)
        return [("s_axis_tdata", "m_axis_tdata")]
    monkeypatch.setattr(run_mod, "feedthrough_paths", fake)
    rep = Run(tmp_path / "run", spec, _roles(tmp_path, _files(tmp_path)), isolate=False).execute()
    assert rep["status"] == "pass" and seen and seen[0] == [("", "")]
    comb = [r for r in rep["review"] if r["code"] == "COMB_PATH"]
    assert comb and "s_axis_tdata→m_axis_tdata" in comb[0]["message"]


def test_repair_conflict_goes_to_arbiter_first(tmp_path):
    """Repair들이 서로 넘기면 lead에게 올리기 전에 Arbiter가 판정한다. 테스트 쪽 문제면 기댓값 수정 승인으로 간다."""
    from pipeline.scheduler import approve

    files = _files_conflict(tmp_path)
    files["Arbiter"] = {"out/verdict.json": json.dumps({"verdict": "test_wrong", "reqs": ["ADP-01"], "reason": "테스트가 ready를 잘못 씀",
                                                        "proposal": "ready 패턴을 고친다"})}
    rep = Run(tmp_path / "run", SPEC, _roles(tmp_path, files), isolate=False).execute()
    assert rep["status"] == "waiting_approval"
    items = approve.pending(tmp_path / "run")
    # 두 Verifier가 모두 실패: Verifier마다 항목 하나, 두 후보가 묶임
    assert sorted((p["kind"], p["verifier"], sorted([p["c"]] + [x["c"] for x in p.get("also", [])])) for p in items) == \
        [("expectation_change", 0, [0, 1]), ("expectation_change", 1, [0, 1])]
    assert "REPAIR_CONFLICT" not in {r["code"] for r in rep["review"]}
    assert all("서로 원인을 넘김" in p["repeated_failure"]["note"] for p in approve.pending(tmp_path / "run"))


def test_approved_test_fix_refunds_repairs_and_revives_capped_candidate(tmp_path):
    """후보 0은 틀린 테스트에 맞추느라 Repair 상한을 다 써서 멈추고, 후보 1은 기댓값 수정 승인을 기다린다.
    승인으로 테스트가 바뀌면 Repair 횟수를 돌려받고 후보 0도 다시 돌아 통과한다 (P8a async_fifo SV 후보)."""
    from pipeline.scheduler import approve

    f = _files_wrong_v1(tmp_path)
    body = json.loads(f["Author"]["out/axis_adapter_core.ir.json"])
    k = next(b for b in body["blocks"] if b["id"] == "k_ports")["body"]
    tw = {"verdict": "test_wrong", "reqs": ["ADP-01"], "reason": "SPEC에 없는 검사", "proposal": "bogus 속성을 지운다"}
    files = {"Architect": f["Architect"], "Author": f["Author"], "Verifier|기댓값 수정": f["Verifier|기댓값 수정"],
             "Verifier|verifier__v1": f["Verifier|verifier__v1"], "Verifier": f["Verifier"],
             "Arbiter|__c0__": {"out/verdict.json": json.dumps({**tw, "verdict": "rtl_wrong", "proposal": ""})},
             "Arbiter|__c1__": {"out/verdict.json": json.dumps(tw)}}
    for n in range(6):
        files[f"Repair|__r{n}"] = {"out/patches.json": json.dumps(
            [{"op": "set", "target": "blocks/k_ports", "field": "body", "value": f"// repair {n}\n{k}"}])}
    run = tmp_path / "run"
    rep = Run(run, SPEC, _roles(tmp_path, files), isolate=False).execute()
    c0 = json.loads((run / "modules" / "axis_adapter_core__c0.json").read_text())
    assert c0["status"] == "failed" and "Repair 상한" in c0["reason"] and c0["repairs"] == 2, json.dumps(rep, ensure_ascii=False)[:2000]
    approve.decide(run, [p["id"] for p in approve.pending(run)], "approved", by="design-lead")
    rep = Run(run, SPEC, _roles(tmp_path, files), isolate=False).execute()
    assert rep["status"] == "pass", json.dumps(rep, ensure_ascii=False)[:3000]
    assert all(c["status"] == "pass" for c in rep["candidates"].values())
    c0 = json.loads((run / "modules" / "axis_adapter_core__c0.json").read_text())
    assert c0["repairs"] == 0 and c0["refunds"][0]["repairs"] == 2 and c0["repair_seq"] == 2   # 작업 번호는 그대로 늘어난 채


def test_lead_cannot_pin_module_without_repairs_left(tmp_path):
    """Repair 충돌 항목은 모듈별 남은 Repair 횟수를 보이고, 다 쓴 모듈은 고칠 모듈로 지정할 수 없다."""
    from pipeline.scheduler import approve

    run = tmp_path / "run"
    run.mkdir()
    item = {"id": "E1", "kind": "repair_conflict", "group": "g", "mods": ["a", "b"], "c": 0, "chain": ["a", "b", "a"],
            "claims": [{"module": "a", "suspects": "b", "reason": "x"}], "remaining": {"a": 0, "b": 2}}
    (run / "state.json").write_text(json.dumps({"status": "waiting_approval", "spec": str(SPEC), "pending": [item], "review": []}))
    assert "남은 Repair a 0, b 2" in approve._summary(item)
    with pytest.raises(ValueError, match="상한을 다 써서"):
        approve.decide(run, ["E1"], "repair", by="lead", module="a")
    assert approve.decide(run, ["E1"], "repair", by="lead", module="b")[0]["module"] == "b"


def test_comb_block_top_with_fake_agents(tmp_path):
    """클럭이 없는 조합 Block Top: SPEC 2절의 "없음" 행 → domains 없는 L1 → 하니스는 가상 클럭 래퍼로 검증."""
    C = ROOT / "tests" / "fixtures" / "comb_top"
    files = {
        "Architect": {"out/ir/add8.ir.json": f"@{C / 'add8.ir.json'}", "out/ir/add8_core.ir.json": f"@{C / 'add8_core.ir.json'}",
                      "out/assumptions.json": json.dumps({"assumptions": ["동작은 하위 모듈 하나(add8_core)에 둠"]})},
        "Author": {"out/add8_core.ir.json": f"@{C / 'add8_core.ir.json'}"},
        "Verifier": {"out/test_add8.py": f"@{C / 'tb_add8.py'}"},
    }
    run = Run(tmp_path / "run", C / "add8.spec.md", _roles(tmp_path, files), isolate=False)
    rep = run.execute()
    assert rep["status"] == "pass", json.dumps(rep, ensure_ascii=False)[:2000]
    assert block_top_l1(load(C / "add8.spec.md"))[0]["domains"] == []
    rtl = (tmp_path / "run" / "rtl" / "add8.sv").read_text()
    assert "tb_comb_clk" not in rtl and "always_ff" not in (tmp_path / "run" / "rtl" / "add8_core.sv").read_text()


def test_comb_block_top_wrong_design_fails(tmp_path):
    C = ROOT / "tests" / "fixtures" / "comb_top"
    bad = json.loads((C / "add8_core.ir.json").read_text())
    bad["blocks"][0]["body"] = bad["blocks"][0]["body"].replace("a + b + {7'b0, cin}", "a + b")   # cin을 빠뜨림
    files = {
        "Architect": {"out/ir/add8.ir.json": f"@{C / 'add8.ir.json'}", "out/ir/add8_core.ir.json": f"@{C / 'add8_core.ir.json'}",
                      "out/assumptions.json": json.dumps({"assumptions": ["동작은 하위 모듈 하나(add8_core)에 둠"]})},
        "Author": {"out/add8_core.ir.json": json.dumps(bad)},
        "Verifier": {"out/test_add8.py": f"@{C / 'tb_add8.py'}"},
    }
    rep = Run(tmp_path / "run", C / "add8.spec.md", _roles(tmp_path, files), isolate=False).execute()
    assert rep["status"] != "pass"
    props = [x for f in (tmp_path / "run" / "verify").rglob("*.result.json") for x in json.loads(f.read_text())["properties"]]
    assert props and {x["property"] for x in props} == {"sum_is_a_plus_b"}   # 가상 클럭 표본의 속성(T4)이 잡음


def test_comb_block_top_python_fragment_candidate(tmp_path):
    """조합 Block Top의 Python 조각 후보: 번역기 검사(도메인 없는 모듈)를 거쳐 SV 후보와 함께 통과한다."""
    C = ROOT / "tests" / "fixtures" / "comb_top"
    files = {
        "Architect": {"out/ir/add8.ir.json": f"@{C / 'add8.ir.json'}", "out/ir/add8_core.ir.json": f"@{C / 'add8_core.ir.json'}",
                      "out/assumptions.json": json.dumps({"assumptions": ["동작은 하위 모듈 하나(add8_core)에 둠"]})},
        "Author|author__add8_core__c1": {"out/add8_core.ir.json": f"@{C / 'add8_core.py.ir.json'}"},
        "Author": {"out/add8_core.ir.json": f"@{C / 'add8_core.ir.json'}"},
        "Verifier": {"out/test_add8.py": f"@{C / 'tb_add8.py'}"},
    }
    roles = _roles(tmp_path, files)
    roles.author_langs = ("sv", "py")
    rep = Run(tmp_path / "run", C / "add8.spec.md", roles, isolate=False).execute()
    assert rep["status"] == "pass", json.dumps(rep, ensure_ascii=False)[:3000]
    assert all(c["status"] == "pass" for c in rep["candidates"].values())
    eq = list((tmp_path / "run" / "verify").glob("*_c1_*/pyequiv_add8_core/pyequiv_result.json"))
    assert eq and json.loads(eq[0].read_text())["ok"]
    assert not any(r["code"] in ("CANDIDATE_DIFF", "PY_EQUIV") for r in rep["review"])
