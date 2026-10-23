"""P4: 작업 실행기, 역할 설정, 에이전트 도구 CLI."""
import json
import subprocess
import sys
from pathlib import Path

from pipeline.agents.runner import AgentConfig
from pipeline.agents.task import QuotaExceeded, Task, prepare, run_task
from test_pipeline import LIB, ROOT

FAKE = str(ROOT / "tests" / "fake_agent.py")


def _cfg(mode, **env):
    return AgentConfig("fake", "Test", "fake", [sys.executable, FAKE], "m", env={"FAKE_MODE": mode, **env}, timeout_s=120)


def _task(**kw):
    return Task("author", "author/x/c0", "과제", inputs={"input.txt": "원본"}, tools=False, outputs=["out/result.txt"],
                isolate=False, **kw)  # 가짜 agent는 저장소 안의 파이썬과 스크립트를 쓰므로 격리 없이


def test_task_collects_outputs_and_records(tmp_path):
    r = run_task(_cfg("task_write"), _task(), tmp_path / "w")
    assert r.ok and r.outputs == {"out/result.txt": str(tmp_path / "w" / "out" / "result.txt")} and r.violations == []
    rec = json.loads((tmp_path / "w" / ".task.json").read_text())
    assert rec["role"] == "author" and rec["runs"][0]["returncode"] == 0


def test_task_rejects_writes_outside_scope(tmp_path):
    r = run_task(_cfg("task_escape"), _task(), tmp_path / "w")
    assert not r.ok and r.violations == ["수정: input.txt"]


def test_task_retries_with_check_errors(tmp_path):
    def check(work):
        return [] if (work / "out" / "result.txt").read_text() == "good" else ["결과가 bad임"]

    r = run_task(_cfg("task_retry"), _task(), tmp_path / "w", check=check, retries=1)
    assert r.ok and r.attempts == 2 and r.runs[0]["errors"] == ["결과가 bad임"]
    r = run_task(_cfg("task_retry"), _task(), tmp_path / "w2", check=lambda w: ["항상 실패"], retries=0)
    assert not r.ok and r.attempts == 1 and r.errors == ["항상 실패"]


def test_missing_output_is_an_error(tmp_path):
    r = run_task(_cfg("crash"), _task(), tmp_path / "w")
    assert not r.ok and r.missing == ["out/result.txt"]


def test_roles_warn_on_same_family():
    from pipeline.agents.roles import load

    roles = load()
    assert roles.agents["author"].model == "gemini-3.8-flash-medium" and roles.counts["author"] == 2   # 2026-10-23 agy CLI, 모든 역할 같은 모델
    assert roles.warnings() and "Gemini" in roles.warnings()[0]


# ---------------------------------------------------------------- 도구 CLI (작업 디렉터리에 설치된 사본)
def _installed(tmp_path):
    w = tmp_path / "w"
    raw = LIB["pkt_parser"].raw
    l1 = {k: raw[k] for k in ("module", "role", "params", "domains", "ports")}
    prepare(Task("author", "t", "x", inputs={"in/ir.json": json.dumps(raw), "in/l1.json": json.dumps(l1),
                                              "in/spec.md": (ROOT / "examples/i3c_basic_controller/spec.md").read_text()}), w)
    return w


def _pl(w, *args):
    p = subprocess.run([sys.executable, "tools/pl.py", *args], cwd=w, capture_output=True, text=True)
    return p.returncode, json.loads(p.stdout)


def test_tools_cli_in_workdir(tmp_path):
    w = _installed(tmp_path)
    assert _pl(w, "validate", "in/ir.json", "--l1", "in/l1.json") == (0, {"ok": True, "errors": [], "warnings": []})
    raw = json.loads((w / "in/ir.json").read_text())
    raw["ports"][1]["reset"] = "1'b1"          # 출력의 리셋값은 Author가 정함 (동결 아님)
    (w / "in/ok.json").write_text(json.dumps(raw))
    assert _pl(w, "validate", "in/ok.json", "--l1", "in/l1.json")[0] == 0
    raw["ports"][1]["type"] = "logic [1:0]"    # 포트 타입은 동결
    (w / "in/bad.json").write_text(json.dumps(raw))
    rc, out = _pl(w, "validate", "in/bad.json", "--l1", "in/l1.json")
    assert rc == 1 and any("[동결] ports" in e for e in out["errors"])
    (w / "in/p.json").write_text(json.dumps([{"op": "set", "target": "ports/pkt_valid", "field": "reset", "value": "1"}]))
    rc, out = _pl(w, "patch", "in/ir.json", "in/p.json")
    assert rc == 1 and out["rejected"]
    out = _pl(w, "spec", "in/spec.md", "TR-07", "FR-99")[1]
    assert out["unknown"] == ["FR-99"] and out["requirements"][0]["text"].startswith("Bus Available")
    assert "blocks/k_cnt" in _pl(w, "fanin", "in/ir.json", "pkt_valid")[1]["nodes"]
    assert "module pkt_parser" in _pl(w, "emit", "in/ir.json")[1]["sv"]
    assert (w / _pl(w, "harness", "in/l1.json", "-o", "out/tb")[1]["harness"]).exists()
    (w / "out/model.py").write_text("class Model:\n    def on_input(self, s, t):\n        return [('pkt_valid', {'pkt_valid': 1})]\n")
    assert _pl(w, "model", "out/model.py", "in/l1.json", "--n", "20")[1] == {"ok": True, "inputs": 20, "skipped": 0, "expected_outputs": {"pkt_valid": 20}}
    (w / "out/bad_model.py").write_text("class Model:\n    def on_input(self, s, t):\n        return 1/0\n")
    assert _pl(w, "model", "out/bad_model.py", "in/l1.json")[1]["ok"] is False
    assert len((w / ".tools.log").read_text().splitlines()) == 10


def test_model_check_without_input_bundles(tmp_path):
    # 단독 포트만 있는 블록(fn_top, od_top): 넣을 트랜잭션이 없어도 실패가 아니고, 테스트가 import하는 harness/를 찾는다
    w = _installed(tmp_path)
    (w / "in/fn.json").write_text((ROOT / "tests/fixtures/v1ext/fn_mod.ir.json").read_text())
    (w / "in/harness").mkdir()
    (w / "in/harness/fn_harness.py").write_text("X = 1\n")
    (w / "out/m.py").write_text("import fn_harness\n\nclass Model:\n    pass\n")
    out = _pl(w, "model", "out/m.py", "in/fn.json")[1]
    assert out["ok"] is True and out["inputs"] == 0


def test_agent_can_call_tools(tmp_path):
    raw = LIB["pkt_parser"].raw
    t = Task("author", "t", "x", inputs={"in/ir.json": json.dumps(raw)}, outputs=["out/result.txt"], isolate=False)
    r = run_task(_cfg("task_tools"), t, tmp_path / "w")
    assert r.ok and json.loads(Path(r.outputs["out/result.txt"]).read_text())["ok"] is True


# ---------------------------------------------------------------- 격리
import shutil  # noqa: E402

import pytest  # noqa: E402

from pipeline.agents.runner import IsolationUnavailable, isolate, sandbox_profile  # noqa: E402


def test_workdir_inside_repo_is_refused():
    t = Task("author", "t", "x", tools=False)
    with pytest.raises(ValueError):
        run_task(_cfg("task_write"), t, ROOT / "build" / "isolation_test")


@pytest.mark.skipif(not shutil.which("sandbox-exec"), reason="macOS sandbox-exec 없음")
def test_sandbox_blocks_repo_and_sibling_candidates(tmp_path):
    run = tmp_path / "run"
    (run / "author_c1").mkdir(parents=True)
    (run / "author_c1" / "answer.ir.json").write_text("정답")
    me = run / "author_c0"
    me.mkdir()
    cmd = ["/bin/sh", "-c", f"cat {ROOT}/README.md; cat {run}/author_c1/answer.ir.json; echo ok > {me}/w.txt && echo own-ok"]
    p = subprocess.run(isolate(cmd, [ROOT, run], [me]), capture_output=True, text=True)
    assert "own-ok" in p.stdout and "정답" not in p.stdout
    assert "README.md: Operation not permitted" in p.stderr and "answer.ir.json: Operation not permitted" in p.stderr


def test_profile_and_missing_isolation(monkeypatch):
    prof = sandbox_profile([Path("/a")], [Path("/a/b")])
    assert prof.index('(deny file-read* file-write* (subpath "/a"))') < prof.index('(allow file-read* file-write* (subpath "/a/b"))')
    monkeypatch.setattr("pipeline.agents.runner.sys.platform", "linux")
    with pytest.raises(IsolationUnavailable):
        isolate(["true"], [], [])


# ---------------------------------------------------------------- 차분 검사
def test_verifier_and_candidate_diff(tmp_path):
    from pipeline.scheduler.run import candidate_diff, verifier_diff

    raw = LIB["pkt_parser"].raw
    l1 = {k: raw[k] for k in ("module", "role", "params", "domains", "ports")}
    a, b = tmp_path / "va.py", tmp_path / "vb.py"
    a.write_text("class Model:\n    def on_input(self, s, t):\n        return [('pkt_valid', {'pkt_valid': 1})] if t['s_data'] == 0xA5 else []\n")
    b.write_text("class Model:\n    def on_input(self, s, t):\n        return [('pkt_valid', {'pkt_valid': 1})] if t['s_data'] == 0x5A else []\n")
    assert verifier_diff([str(a), str(a)], l1) is None
    assert "s_data" in verifier_diff([str(a), str(b)], l1)
    for name, outs in (("c0", {"m": [{"x": 1}]}), ("c1", {"m": [{"x": 1}]}), ("c2", {"m": [{"x": 2}]})):
        (tmp_path / name).mkdir()
        (tmp_path / name / "t1.result.json").write_text(json.dumps({"outputs": outs}))
    assert candidate_diff([[str(tmp_path / "c0")], [str(tmp_path / "c1")]]) is None
    assert candidate_diff([[str(tmp_path / "c0")], [str(tmp_path / "c2")]]) == "t1"


def test_claude_code_session_limit_is_quota(tmp_path):
    """Claude Code의 세션 한도 안내는 Repair 실패가 아니라 사용량 한도로 멈춰야 한다 (P8a async_fifo)."""
    with pytest.raises(QuotaExceeded):
        run_task(_cfg("quota_cc"), _task(), tmp_path / "w", retries=1)


def test_architect_rejects_packed_word_between_modules():
    """하위 모듈끼리 여러 필드를 묶은 워드를 넘기면 거부한다. 자식 파라미터 하나에 합을 바인딩해 숨겨도 잡는다 (P8a async_fifo)."""
    from pipeline.agents.architect import packed_ports

    def mod(name, port, typ, d, params=()):
        return {"module": name, "role": "module", "params": [{"name": p, "type": "int", "default": "8"} for p in params],
                "ports": [{"name": port, "dir": d, "type": typ, "domain": "sys"}]}
    top = {"module": "t", "params": [{"name": "DW", "type": "int", "default": "8"}, {"name": "IW", "type": "int", "default": "4"}],
           "instances": [{"name": "u_w", "module": "w", "params": {"DW": "DW", "IW": "IW"}},
                         {"name": "u_r", "module": "r", "params": {"WW": "DW+IW+1"}}],
           "connections": [{"id": "c", "from": "u_w.word", "to": "u_r.word"}]}
    kids = {"w": mod("w", "word", "logic [DW+IW:0]", "output", ("DW", "IW")), "r": mod("r", "word", "logic [WW-1:0]", "input", ("WW",))}
    errs = packed_ports(top, kids)
    assert len(errs) == 2 and all("[묶음 워드]" in e for e in errs)
    kids["w"]["ports"][0]["type"] = kids["r"]["ports"][0]["type"] = "logic [DW-1:0]"   # 필드 하나
    top["instances"][1]["params"] = {"WW": "DW"}
    kids["r"]["ports"][0]["type"] = "logic [WW-1:0]"
    assert packed_ports(top, kids) == []


def test_architect_contracts_and_module_limit(tmp_path):
    """하위 모듈끼리 잇는 연결마다 계약(carries, timing)이 있어야 하고, 계약은 그 연결의 양쪽 모듈에게 간다.
    max_modules(P11 R6 재생성)를 넘으면 거부한다."""
    from pipeline.agents import architect

    top = {"instances": [{"name": "u_a", "module": "a"}, {"name": "u_b", "module": "b"}, {"name": "u_c", "module": "c"}],
           "connections": [{"id": "c1", "from": "self.s", "to": "u_a.s"}, {"id": "c2", "from": "u_a.m", "to": "u_b.s"},
                           {"id": "c3", "from": "u_b.m", "to": "self.m"}]}
    assert architect.sibling_links(top) == [("u_a.m", "u_b.s")]
    errs = architect.contract_errors(top, [])
    assert len(errs) == 1 and "u_a.m → u_b.s" in errs[0]
    ok = [{"from": "u_a.m", "to": "u_b.s", "carries": "디코드한 바이트", "timing": "a는 입력 다음 사이클, ready는 조합 아님"}]
    assert architect.contract_errors(top, ok) == []
    assert "timing" in architect.contract_errors(top, [{**ok[0], "timing": " "}])[0]
    assert architect.contracts_for(ok, top, "b") == ok and architect.contracts_for(ok, top, "c") == []

    work = tmp_path / "w"
    (work / "out" / "ir").mkdir(parents=True)
    rx = json.loads((ROOT / "tests" / "fixtures" / "pkt_rx_top.ir.json").read_text())
    parser = json.loads((ROOT / "tests" / "fixtures" / "pkt_parser.ir.json").read_text())
    l1 = {k: rx[k] for k in ("module", "role", "params", "domains", "ports")}
    (work / "out" / "ir" / "pkt_rx_top.ir.json").write_text(json.dumps(rx))
    (work / "out" / "ir" / "pkt_parser.ir.json").write_text(json.dumps(parser))
    (work / "out" / "ir" / "pkt_extra.ir.json").write_text(json.dumps({**parser, "module": "pkt_extra"}))
    (work / "out" / "assumptions.json").write_text(json.dumps({"assumptions": [{"text": "x", "affects": []}]}))
    assert not any("하위 모듈(role module)" in e for e in architect.check(l1)(work))
    assert any("하위 모듈(role module)이 2개" in e for e in architect.check(l1, max_modules=1)(work))
    assert "1개 이하" in architect.make_task("architect", l1, ROOT / "IMPLEMENTATION.md", max_modules=1).text
    assert "개 이하" not in architect.make_task("architect", l1, ROOT / "IMPLEMENTATION.md").text


def test_author_gets_contracts():
    from pipeline.agents import author

    l1 = json.loads((ROOT / "tests" / "fixtures" / "pkt_parser.ir.json").read_text())
    c = [{"from": "u_a.m", "to": "u_parser.s", "carries": "x", "timing": "y"}]
    t = author.make_task("author/pkt_parser/c0", l1, ROOT / "IMPLEMENTATION.md", contracts=c)
    assert json.loads(t.inputs["contracts.json"])["contracts"] == c and "contracts.json" in t.text
    assert "contracts.json" not in author.make_task("author/pkt_parser/c0", l1, ROOT / "IMPLEMENTATION.md").inputs
