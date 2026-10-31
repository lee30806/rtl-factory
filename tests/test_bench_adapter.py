"""BENCHMARK_PLAN B0·B1 단위 및 통합 시험:
1. 기존 axis_adapter 원본 헤더 + 설명 → to_spec.py L1 추출 및 10개 섹션 spec.md 생성·검증 (정답 RTL 격리 확인)
2. 무인 승인 루프(auto_decide): 기댓값 수정 승인(expectation_change) 및 Repair 충돌(첫 회 architect 재구성, 이후 repair 모듈 지정)
3. RTLLM 2.0 pulse_detect 파일럿 예제: 헤더 추출 → SPEC 생성 → 파이프라인(c0 SV, c1 Python) → iverilog 외부 TB 채점 및 Yosys PPA 집계
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.bench.adapters.rtllm import load_rtllm_dir, materialize_builtin_pilot  # noqa: E402
from experiments.bench.common.metrics import write_summary  # noqa: E402
from experiments.bench.common.runner import auto_decide, evaluate_ppa, evaluate_rtl, run_bench_item  # noqa: E402
from experiments.bench.common.schema import BenchItem, EvalSpec  # noqa: E402
from experiments.bench.common.to_spec import extract_header_l1, generate_spec, make_bench_spec_task  # noqa: E402
from pipeline.agents.roles import Roles  # noqa: E402
from pipeline.agents.runner import AgentConfig  # noqa: E402
from pipeline.importer.spec_tables import comparable, roundtrip_diff  # noqa: E402
from pipeline.spec.l1 import block_top_l1  # noqa: E402
from pipeline.spec.parse import load as load_spec  # noqa: E402

G = ROOT / "tests" / "golden" / "axis_adapter"
FAKE = str(ROOT / "tests" / "fake_agent.py")


def _fake_roles(tmp_path: Path, files: dict[str, dict[str, str]]) -> Roles:
    env = {"FAKE_MODE": "roles", "FAKE_FILES": json.dumps(files), "FAKE_LOG": str(tmp_path / "calls.log")}
    cfg = AgentConfig("fake", "Test", "fake", [sys.executable, FAKE], "m", env=env, timeout_s=120)
    agents = {r: cfg for r in ("architect", "author", "verifier", "repair", "arbiter", "spec_writer")}
    roles = Roles(agents, {"author": 2, "verifier": 2}, {"repair_per_candidate": 2, "agent_retries": 0, "budget_agent_runs": 50})
    roles.author_langs = ("sv", "py")
    return roles


def test_b0_axis_adapter_header_to_spec_and_isolation(tmp_path: Path):
    """B0 완료 기준 2: axis_adapter의 원본 헤더와 설명만 넣었을 때 to_spec.py가 유효한 10개 섹션 spec.md를 만들고 spec.check를 통과함.
    또한 reference_rtl이 있더라도 SpecWriter 작업 디렉터리에는 노출되지 않음을 확인한다."""
    golden_cfg = json.loads((G / "golden.json").read_text(encoding="utf-8"))
    params = {k: str(v) for k, v in golden_cfg["upstream_params"]["axis_adapter"].items()}
    upstream_v = G / "upstream" / "rtl" / "axis_adapter.v"
    prompt_text = (G / "spec.md").read_text(encoding="utf-8") + "\nClock: posedge clk, synchronous active-high reset rst."

    item = BenchItem(
        benchmark="rtllm",
        id="axis_adapter",
        category="sequential",
        top="axis_adapter",
        prompt_text=prompt_text,
        header_sv=str(upstream_v),
        params=params,
        reference_rtl=[str(upstream_v)],
    )

    top_l1, tables, notes = extract_header_l1(item, tmp_path / "l1")
    golden_spec_l1, _ = block_top_l1(load_spec(G / "block_spec.md"))
    assert roundtrip_diff(top_l1, golden_spec_l1) == []
    assert comparable(top_l1) == comparable(golden_spec_l1)

    # 정답 RTL 격리 확인: Task inputs에 reference_rtl이나 구현 본문이 들어가지 않음
    hdr_text = (tmp_path / "l1" / "header.sv").read_text(encoding="utf-8")
    assert "always" not in hdr_text
    task = make_bench_spec_task(item, tables, hdr_text, notes)
    assert all("upstream" not in k and "source/" not in k for k in task.inputs)

    files = {"SpecWriter": {"out/spec.md": f"@{G / 'block_spec.md'}"}}
    spec_path, errs, res = generate_spec(item, tmp_path / "spec_out", _fake_roles(tmp_path, files), isolate=False)
    assert res.ok and not errs and spec_path is not None and spec_path.exists()


def test_b0_auto_decide_expectation_and_conflict(tmp_path: Path):
    """B0 완료 기준 1 (무인 승인 정책):
    - expectation_change: max_auto_approves까지 자동 승인(approved), 초과 시 자동 거절(rejected)
    - repair_conflict: 첫 충돌은 architect 재구성, 재구성 상한 도달 후에는 남은 Repair 예산이 가장 많은 모듈로 repair 지정"""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    spec_file = G / "block_spec.md"

    # 1) expectation_change 2회 (max_auto_approves=1로 설정하여 첫 회 approved, 둘째 회 rejected 확인)
    state = {
        "spec": str(spec_file),
        "status": "waiting_approval",
        "pending": [
            {"id": "E1", "kind": "expectation_change", "c": 0, "mods": ["m_core"], "verifier": 0, "reqs": ["ADP-01"]},
            {"id": "E2", "kind": "expectation_change", "c": 0, "mods": ["m_core"], "verifier": 1, "reqs": ["ADP-02"]},
        ],
    }
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    decs = auto_decide(run_dir, max_auto_approves=1, max_arch_revisions=1)
    assert [(d["id"], d["decision"], d["by"]) for d in decs] == [
        ("E1", "approved", "bench_auto"),
        ("E2", "rejected", "bench_auto"),
    ]

    # 2) repair_conflict 첫 번째 발생 → architect 재구성
    state["pending"].append(
        {
            "id": "E3",
            "kind": "repair_conflict",
            "group": "m_a+m_b",
            "c": 0,
            "mods": ["m_a", "m_b"],
            "remaining": {"m_a": 1, "m_b": 2},
            "claims": [],
        }
    )
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    decs2 = auto_decide(run_dir, max_auto_approves=1, max_arch_revisions=1)
    assert [(d["id"], d["decision"]) for d in decs2] == [("E3", "architect")]

    # 3) repair_conflict 두 번째 발생 (architect 상한 도달) → remaining이 큰 m_b(2 > 1)로 repair 지정
    state["pending"].append(
        {
            "id": "E4",
            "kind": "repair_conflict",
            "group": "m_a+m_b",
            "c": 0,
            "mods": ["m_a", "m_b"],
            "remaining": {"m_a": 1, "m_b": 2},
            "claims": [],
        }
    )
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    decs3 = auto_decide(run_dir, max_auto_approves=1, max_arch_revisions=1)
    assert [(d["id"], d["decision"], d.get("module")) for d in decs3] == [("E4", "repair", "m_b")]


PULSE_SPEC_MD = """\
# pulse_detect SPEC

## 1. 목적과 범위

`pulse_detect`는 단일 비트 직렬 입력 `data_in`에서 1클럭 폭의 양의 펄스(`0 -> 1 -> 0`)를 검출하여 `data_out`으로 알리는 동기식 검출기입니다.
범위 밖 항목은 다중 비트 버스 입력 및 가변 폭 펄스 검출입니다.

## 2. 클럭, 리셋, 전력

| 도메인 | 클럭 포트 | 주파수 | 리셋 포트 | 리셋 극성 | 리셋 동기 | 도메인 간 관계 | 전력 도메인 | isolation | retention |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sys | clk | 100 MHz | rst_n | low | 비동기 | 단일 도메인 | 없음 | 없음 | 없음 |

## 3. 인터페이스

| 포트 또는 번들 | 방향 | 타입 | 프로토콜 태그 | 도메인 |
| --- | --- | --- | --- | --- |
| clk | input | logic | – | sys |
| rst_n | input | logic | – | sys |
| data_in | input | logic | – | sys |
| data_out | output | logic | – | sys |

## 4. 파라미터

| 이름 | 타입 | 기본값 | 허용 범위 |
| --- | --- | --- | --- |

## 5. 기능 요구사항

- PD-01 (필수) 매 `clk` 상승 에지에서 입력 `data_in`을 샘플링하며, 직전 두 클럭과 현재 클럭의 `data_in` 순서가 `0 -> 1 -> 0`일 때 해당 세 번째 클럭 에지 직후 `data_out`을 1로 구동한다.
- PD-02 (필수) `data_in`이 2클럭 이상 연속 1(`0 -> 1 -> 1 -> 0`)이거나 `0 -> 1 -> 0` 조건을 만족하지 않는 모든 사이클에는 `data_out`을 0으로 구동한다.
- PD-03 (필수) `0 -> 1 -> 0 -> 1 -> 0`처럼 중첩된 입력 시퀀스에서는 두 번째 `0`과 세 번째 `0` 사이클에서 각각 `data_out`을 1로 구동한다.

## 6. 타이밍과 성능

- PD-T01 (필수) `data_in`의 세 번째 값(`0`)이 샘플링된 `clk` 상승 에지부터 1클럭 동안 `data_out`이 1로 유지된다.

## 7. 예외와 코너 케이스

- PD-E01 (필수) 비동기 저활성 리셋 `rst_n`이 0이면 즉시 내부 히스토리를 초기화하고 `data_out`을 0으로 구동한다.

## 8. 예시 시나리오

### SC-01 정상: 단일 펄스 및 중첩 펄스 검출

| 입력 (data_in 시퀀스) | 기대 출력 (data_out 시퀀스) |
| --- | --- |
| 0, 1, 0, 1, 0 | 0, 0, 1, 0, 1 |

### SC-02 코너: 2사이클 연속 1 무시

| 입력 (data_in 시퀀스) | 기대 출력 (data_out 시퀀스) |
| --- | --- |
| 0, 1, 1, 0 | 0, 0, 0, 0 |

## 9. 구현 제약

### 필수 구조
- 단일 클럭(`clk`) 및 비동기 저활성 리셋(`rst_n`) 도메인으로 구현한다.

### 금지 사항
- 외부 메모리나 조합 루프를 사용하지 않는다.

## 10. 열린 질문

- 없음
"""


def _pulse_detect_fake_files(tmp_path: Path) -> dict[str, dict[str, str]]:
    top_ir = {
        "module": "pulse_detect",
        "role": "block_top",
        "params": [],
        "domains": [{"name": "sys", "clock": "clk", "edge": "pos", "freq_mhz": 100.0, "reset": {"port": "rst_n", "active": "low", "async": True}}],
        "ports": [
            {"name": "data_in", "dir": "input", "type": "logic", "domain": "sys"},
            {"name": "data_out", "dir": "output", "type": "logic", "domain": "sys", "kind": "wire"},
        ],
        "instances": [{"name": "u_core", "module": "pulse_detect_core", "params": {}, "domain_map": {"sys": "sys"}}],
        "connections": [
            {"id": "c_in", "from": "self.data_in", "to": "u_core.data_in"},
            {"id": "c_out", "from": "u_core.data_out", "to": "self.data_out"},
        ],
    }
    child_l1 = {
        "module": "pulse_detect_core",
        "role": "module",
        "params": [],
        "domains": [{"name": "sys", "clock": "clk", "edge": "pos", "freq_mhz": 100.0, "reset": {"port": "rst_n", "active": "low", "async": True}}],
        "ports": [
            {"name": "data_in", "dir": "input", "type": "logic", "domain": "sys"},
            {"name": "data_out", "dir": "output", "type": "logic", "domain": "sys", "kind": "reg", "reset": "1'b0"},
        ],
    }
    core_sv = {
        **child_l1,
        "types": [{"name": "state_e", "kind": "enum", "members": ["S_IDLE", "S_ZERO", "S_ONE"]}],
        "signals": [],
        "blocks": [
            {
                "id": "k_fsm",
                "kind": "fsm",
                "domain": "sys",
                "state_type": "state_e",
                "state_name": "state",
                "reset_state": "S_IDLE",
                "reads": ["data_in"],
                "transitions": [
                    {"id": "tr_i0", "from": "S_IDLE", "to": "S_ZERO", "cond": "!data_in"},
                    {"id": "tr_z1", "from": "S_ZERO", "to": "S_ONE", "cond": "data_in"},
                    {"id": "tr_z0", "from": "S_ZERO", "to": "S_ZERO", "cond": "!data_in"},
                    {"id": "tr_o0", "from": "S_ONE", "to": "S_ZERO", "cond": "!data_in"},
                    {"id": "tr_o1", "from": "S_ONE", "to": "S_IDLE", "cond": "data_in"},
                ],
            },
            {
                "id": "k_out",
                "kind": "seq",
                "domain": "sys",
                "reads": ["state_q", "data_in"],
                "writes": ["data_out"],
                "body": "data_out <= (state_q == S_ONE) && !data_in;",
            },
        ],
    }
    core_py = {
        **core_sv,
        "lang": "py",
        "blocks": [
            {
                **core_sv["blocks"][0],
                "transitions": [
                    {"id": "tr_i0", "from": "S_IDLE", "to": "S_ZERO", "cond": "not data_in"},
                    {"id": "tr_z1", "from": "S_ZERO", "to": "S_ONE", "cond": "data_in"},
                    {"id": "tr_z0", "from": "S_ZERO", "to": "S_ZERO", "cond": "not data_in"},
                    {"id": "tr_o0", "from": "S_ONE", "to": "S_ZERO", "cond": "not data_in"},
                    {"id": "tr_o1", "from": "S_ONE", "to": "S_IDLE", "cond": "data_in"},
                ],
            },
            {
                "id": "k_out",
                "kind": "seq",
                "domain": "sys",
                "reads": ["state_q", "data_in"],
                "writes": ["data_out"],
                "body": "data_out = 1 if (state_q == S_ONE and not data_in) else 0",
            },
        ],
    }
    tb_code = """\
import cocotb
from cocotb.triggers import FallingEdge
from pulse_detect_harness import make


class Model:
    streams = set()

    def on_input(self, stream, t):
        return []


def attach_checker(h):
    hist = []

    def check_pulse(s, c):
        if s.get("rst_n") == 0:
            hist.clear()
            return None if s["data_out"] == 0 else "reset: data_out != 0"
        exp = 1 if len(hist) >= 2 and hist[-2:] == [0, 1] and s["data_in"] == 0 else 0
        # 레지스터 출력이므로 직전 클럭 에지에서 샘플링된 시퀀스 결과는 다음 사이클에 보임
        hist.append(s["data_in"])
        if len(hist) >= 4:
            exp_reg = 1 if hist[-4:-1] == [0, 1, 0] else 0
            if s["data_out"] != exp_reg:
                return f"cycle {c}: got {s['data_out']}, exp {exp_reg}"
        return None

    h.add_property("pulse_check", "sys", check_pulse, ["PD-01", "PD-02", "PD-03", "PD-T01", "PD-E01"])


@cocotb.test()
async def test_directed_pulse(dut):
    h = make(dut, Model(), name="test_directed_pulse")
    attach_checker(h)
    await h.start()
    for s in [0, 1, 0, 1, 0, 0, 1, 0]:
        await FallingEdge(dut.clk)
        dut.data_in.value = s
    await h.drain()
    h.finish()


@cocotb.test()
async def test_ignore_long_high(dut):
    h = make(dut, Model(), name="test_ignore_long_high")
    attach_checker(h)
    await h.start()
    for s in [0, 1, 1, 0, 1, 1, 1, 0, 1, 0]:
        await FallingEdge(dut.clk)
        dut.data_in.value = s
    await h.drain()
    h.finish()
"""
    tb_file = tmp_path / "test_pulse_detect.py"
    tb_file.write_text(tb_code, encoding="utf-8")
    return {
        "SpecWriter": {"out/spec.md": PULSE_SPEC_MD},
        "Architect": {
            "out/ir/pulse_detect.ir.json": json.dumps(top_ir),
            "out/ir/pulse_detect_core.ir.json": json.dumps(child_l1),
            "out/assumptions.json": json.dumps({"assumptions": [{"text": "하위 모듈 하나로 구성", "affects": []}]}),
        },
        "Author|__c1": {"out/pulse_detect_core.ir.json": json.dumps(core_py)},
        "Author": {"out/pulse_detect_core.ir.json": json.dumps(core_sv)},
        "Verifier": {"out/test_pulse_detect.py": f"@{tb_file}"},
    }


def test_b1_rtllm_pulse_detect_e2e(tmp_path: Path):
    """RTLLM 2.0 pulse_detect 파일럿 문제를 로드하여 SPEC 변환 → 파이프라인(SV & Python 후보) → iverilog 채점 → Yosys PPA까지 검증한다."""
    pytest.importorskip("cocotb_tools.runner")
    if not shutil.which("verilator") or not shutil.which("iverilog") or not shutil.which("yosys"):
        pytest.skip("verilator/iverilog/yosys 필요")

    pilot_item = materialize_builtin_pilot(tmp_path / "dataset")
    loaded = load_rtllm_dir(tmp_path / "dataset", subset="sequential")
    assert len(loaded) == 1 and loaded[0].id == "pulse_detect" and loaded[0].top == "pulse_detect"

    roles = _fake_roles(tmp_path, _pulse_detect_fake_files(tmp_path))
    res = run_bench_item(pilot_item, tmp_path / "out" / "pulse_detect", roles, isolate=False, auto_approve=True)

    assert res.spec_gate_ok
    assert res.pipeline_status == "pass"
    assert res.bench_pass_chosen
    assert res.bench_pass_any
    assert res.candidates["c0"]["lang"] == "sv" and res.candidates["c0"]["bench_eval"]["ok"]
    assert res.candidates["c1"]["lang"] == "py" and res.candidates["c1"]["bench_eval"]["ok"]
    assert res.ppa_chosen is not None and res.ppa_chosen["synth_ok"] and res.ppa_chosen["comb_loops"] == 0

    summary = write_summary([res], tmp_path / "out")
    assert summary["rates"]["bench_pass_chosen"] == 1.0
    assert (tmp_path / "out" / "summary.md").exists()


# ---------------------------------------------------------------- 외부 TB 보정
def _axis_item_and_l1(tmp_path: Path):
    golden_cfg = json.loads((G / "golden.json").read_text(encoding="utf-8"))
    params = {k: str(v) for k, v in golden_cfg["upstream_params"]["axis_adapter"].items()}
    upstream_v = G / "upstream" / "rtl" / "axis_adapter.v"
    item = BenchItem(benchmark="rtllm", id="axis_adapter", category="sequential", top="axis_adapter",
                     prompt_text=(G / "spec.md").read_text(encoding="utf-8") + "\nClock: posedge clk, synchronous active-high reset rst.",
                     header_sv=str(upstream_v), params=params, reference_rtl=[str(upstream_v)])
    top_l1, tables, _ = extract_header_l1(item, tmp_path / "l1")
    return item, top_l1, tables


def _revised_spec(tmp_path: Path, alarm: bool = True) -> Path:
    text = (G / "block_spec.md").read_text(encoding="utf-8")
    text = text.replace("m_axis_tkeep은 항상 1이다.", "m_axis_tkeep은 리셋이 풀린 뒤 항상 1이다.")
    if alarm:
        text += "- Q-02 [SPEC 재검토 알람] 외부 테스트벤치 근거로 ADP-04를 바꿈: 리셋 중 값은 정하지 않음. 근거: testbench.v 40ns\n"
    p = tmp_path / ("spec_alarm.md" if alarm else "spec_noalarm.md")
    p.write_text(text, encoding="utf-8")
    return p


CHANGE = [{"reqs": ["ADP-04"], "before": "m_axis_tkeep은 항상 1이다.", "after": "m_axis_tkeep은 리셋이 풀린 뒤 항상 1이다.",
           "evidence": "testbench.v 40ns: m_axis_tkeep 비교는 리셋 해제 뒤부터", "reason": "SPEC이 리셋 중 값을 외부 기준과 다르게 정함"}]


def test_ext_fix_check_requires_change_reasons_and_alarm(tmp_path: Path):
    """고친 SPEC은 게이트를 통과하고, 바뀌었고, 바꾼 요구사항마다 수정 사유와 10절 재검토 알람이 있어야 한다."""
    from experiments.bench.common import ext_fix

    _, top_l1, _ = _axis_item_and_l1(tmp_path)
    prev = (G / "block_spec.md").read_text(encoding="utf-8")
    chk = ext_fix.check(top_l1, prev)

    def work(spec: Path, changes) -> Path:
        w = tmp_path / f"w{len(list(tmp_path.glob('w*')))}"
        (w / "out").mkdir(parents=True)
        (w / "out" / "spec.md").write_text(spec.read_text(encoding="utf-8"), encoding="utf-8")
        (w / "out" / "changes.json").write_text(json.dumps(changes, ensure_ascii=False), encoding="utf-8")
        return w

    assert chk(work(_revised_spec(tmp_path), CHANGE)) == []
    assert any("바뀌지 않음" in e for e in chk(work(G / "block_spec.md", CHANGE)))
    assert any("재검토 알람" in e for e in chk(work(_revised_spec(tmp_path, alarm=False), CHANGE)))
    assert any("reason" in e for e in chk(work(_revised_spec(tmp_path), [{**CHANGE[0], "reason": ""}])))
    assert any("없는 요구사항" in e for e in chk(work(_revised_spec(tmp_path), [{**CHANGE[0], "reqs": ["ADP-99"]}])))
    assert any("하나 이상" in e for e in chk(work(_revised_spec(tmp_path), [])))


def test_ext_fix_revise_spec_with_agent_hides_reference_rtl(tmp_path: Path):
    """SpecWriter가 외부 TB, 후보 RTL, 로그를 받아 SPEC을 고친다. 정답 RTL은 작업 디렉터리에 들어가지 않는다."""
    from experiments.bench.common import ext_fix
    from experiments.bench.common.runner import _prepare_iverilog_tb

    item, top_l1, tables = _axis_item_and_l1(tmp_path)
    src = tmp_path / "ds"
    src.mkdir()
    (src / "testbench.v").write_text("module tb; endmodule\n", encoding="utf-8")
    (src / "expected.txt").write_text("1\n0\n", encoding="utf-8")
    shutil.copy(item.reference_rtl[0], src / "verified_axis_adapter.v")
    item.eval = EvalSpec(tb_files=[str(src / "testbench.v")])
    item.reference_rtl = [str(src / "verified_axis_adapter.v")]
    tb_dir = ext_fix.tb_bundle(item, tmp_path / "tb", _prepare_iverilog_tb)
    assert sorted(p.name for p in tb_dir.iterdir()) == ["expected.txt", "testbench.v"]

    rtl = tmp_path / "c0.sv"
    rtl.write_text("module axis_adapter; endmodule\n", encoding="utf-8")
    log = tmp_path / "sim.log"
    log.write_text("Failed at 40: m_axis_tkeep=0 (expected 1)\n", encoding="utf-8")
    cands = {"c0": {"rtl": [rtl], "result": {"ok": False, "stage": "sim"}, "logs": [log]}}
    task = ext_fix.make_task(item, "external_fail", G / "block_spec.md", tables, "module axis_adapter; endmodule", tb_dir, cands, {})
    assert "candidates/c0/sim.log" in task.inputs and "tb" in task.inputs
    assert not any("verified" in k or "upstream" in k for k in task.inputs)

    files = {"SpecWriter|외부 테스트벤치": {"out/spec.md": f"@{_revised_spec(tmp_path)}",
                                         "out/changes.json": json.dumps(CHANGE, ensure_ascii=False)}}
    new, changes, errs, res = ext_fix.revise_spec(item, tmp_path / "ext1", "external_fail", G / "block_spec.md", top_l1, tables,
                                                  "module axis_adapter; endmodule", tb_dir, cands, {}, _fake_roles(tmp_path, files),
                                                  isolate=False)
    assert res.ok and not errs and new is not None
    assert "리셋이 풀린 뒤" in new.read_text(encoding="utf-8")
    assert changes == CHANGE and (tmp_path / "ext1" / "changes.json").exists()


def _scored(status: str, chosen_ok: bool, c_ok: list[bool | None]) -> dict:
    cands = {f"c{i}": {"lang": "sv", "pipeline_ok": status == "pass", "repairs": 0, "py_equiv": {},
                       "bench_eval": {"ok": False, "skipped": "IR 미생성"} if ok is None else {"ok": ok, "stage": "sim"}, "ppa": None}
             for i, ok in enumerate(c_ok)}
    return {"rep": {"status": status, "chosen": {"m": 0} if status == "pass" else None, "review": [], "agent_runs": 3},
            "auto_decs": [], "candidates": cands, "eval_root": Path("/nonexistent"),
            "bench_pass_chosen": chosen_ok, "bench_pass_any": chosen_ok or any(c_ok), "ppa_chosen": None}


def test_ext_fix_trigger():
    from experiments.bench.common.runner import ext_fix_trigger

    item = BenchItem(benchmark="rtllm", id="x", category="sequential", top="x", prompt_text="", header_sv="")
    assert ext_fix_trigger(item, _scored("pass", True, [True, True])) is None
    assert ext_fix_trigger(item, _scored("pass", False, [False, False])) == "external_fail"
    assert ext_fix_trigger(item, _scored("waiting_approval", False, [True, False])) == "internal_mismatch"
    assert ext_fix_trigger(item, _scored("failed", False, [None, None])) is None          # 채점까지 간 후보가 없음: 근거 없음
    assert ext_fix_trigger(item, _scored("paused_quota", False, [False, None])) is None
    item.eval = EvalSpec(kind="yosys_lec")
    assert ext_fix_trigger(item, _scored("pass", False, [False, False])) is None


def test_run_bench_item_ext_fix_keeps_first_result(tmp_path: Path, monkeypatch):
    """외부 TB 보정 뒤에도 pass@1 필드는 첫 실행 결과 그대로이고, 보정 결과와 수정 사유는 ext_fix와 *_final에 따로 남는다."""
    from experiments.bench.common import runner as rmod

    spec_dir = tmp_path / "out" / "spec"
    spec_dir.mkdir(parents=True)
    shutil.copy(G / "block_spec.md", spec_dir / "spec.md")
    runs = iter([_scored("pass", False, [False, False]), _scored("pass", True, [True, False])])
    calls = []
    monkeypatch.setattr(rmod, "run_and_score", lambda item, run_dir, eval_root, spec, *a, **k: (calls.append((run_dir.name, spec)), next(runs))[1])
    revised = tmp_path / "rev.md"
    revised.write_text("x", encoding="utf-8")
    monkeypatch.setattr(rmod, "_ext_fix_spec", lambda item, rdir, trig, spec, out, scored, roles, iso: (revised, CHANGE, [], 1))

    item = BenchItem(benchmark="rtllm", id="axis_adapter", category="sequential", top="axis_adapter", prompt_text="", header_sv="")
    roles = Roles({}, {"author": 2}, {})
    res = rmod.run_bench_item(item, tmp_path / "out", roles, isolate=False, ext_fix_rounds=2)
    assert [c[0] for c in calls] == ["run", "run"] and calls[1][1] == revised
    assert res.bench_pass_chosen is False and res.bench_pass_any is False          # 첫 결과 그대로
    assert res.bench_pass_chosen_final and res.bench_pass_any_final
    assert len(res.ext_fix) == 1 and res.ext_fix[0]["trigger"] == "external_fail" and res.ext_fix[0]["changes"] == CHANGE
    assert res.agent_runs == 3 + 1 + 3

    s = write_summary([res], tmp_path / "out")
    assert s["counts"]["bench_pass_chosen"] == 0 and s["counts"]["bench_pass_chosen_final"] == 1
    assert "외부 TB 보정 뒤" in (tmp_path / "out" / "summary.md").read_text(encoding="utf-8")


# -- 시뮬레이션 모델 경로 --
TICK_L1 = {"module": "tick", "role": "block_top", "params": [{"name": "HALF", "type": "int", "default": "5"}], "domains": [],
           "ports": [{"name": "o", "dir": "output", "type": "logic", "domain": None}]}
TICK_MODEL = "`timescale 1ns/1ps\nmodule tick #(parameter HALF = 5) (output logic o);\n  initial o = 1'b0;\n  always #HALF o <= ~o;\nendmodule\n"
TICK_STUCK = "`timescale 1ns/1ps\nmodule tick #(parameter HALF = 5) (output logic o);\n  initial o = 1'b0;\nendmodule\n"
TICK_TB = """`timescale 1ns/1ps
module tick_tb;
  logic o;
  tick dut(.o(o));
  initial begin
    #2; if (o !== 1'b0) begin $display("TEST FAILED: t=2 o=%b", o); $finish; end
    #5; if (o !== 1'b1) begin $display("TEST FAILED: t=7 o=%b", o); $finish; end
    #5; if (o !== 1'b0) begin $display("TEST FAILED: t=12 o=%b", o); $finish; end
    $display("TEST PASSED"); $finish;
  end
endmodule
"""


def _tick_spec(tmp_path: Path, kind: str = "- 구현 종류: 시뮬레이션 모델") -> Path:
    from pipeline.importer import spec_tables
    t = spec_tables.tables_md(TICK_L1)
    p = tmp_path / "tick_spec.md"
    p.write_text(f"""# tick SPEC

## 1. 목적과 범위
시간에 따라 토글하는 출력.

{t}

## 5. 기능 요구사항
- FR-01 (필수) 출력 o는 시각 0에 0이고 HALF 시간마다 반전한다.

## 6. 타이밍과 성능
- TIM-01 (필수) 출력 o의 변화 간격은 HALF 시간이다.

## 7. 예외와 코너 케이스
- EXC-01 미정의: HALF가 0일 때의 출력 o

## 8. 예시 시나리오
### SC-01 정상: 토글
| 입력(시각) | 기대 출력 |
| --- | --- |
| 0 | 0 |

### SC-02 코너: 두 번째 토글
| 입력(시각) | 기대 출력 |
| --- | --- |
| 10 | 0 |

## 9. 구현 제약
{kind}

## 10. 열린 질문
- 없음
""", encoding="utf-8")
    return p


def test_spec_gate_requires_kind_for_block_without_inputs(tmp_path: Path):
    from pipeline.spec import check
    spec = load_spec(_tick_spec(tmp_path, ""))
    assert "NO_INPUT_KIND" in [i["code"] for i in check.run(spec)["issues"]]
    assert not check.is_sim_model(spec)
    spec = load_spec(_tick_spec(tmp_path))
    res = check.run(spec)
    assert res["gate"] == "pass", res["issues"]
    assert check.is_sim_model(spec)
    assert not check.is_sim_model(load_spec(G / "block_spec.md"))
    # 출력 포트만 있어도 클럭 도메인이 있으면 입력이 있는 블록이다
    clocked = {"ports": TICK_L1["ports"], "domains": [{"name": "sys", "clock": "clk"}]}
    assert check.check_impl_kind(load_spec(_tick_spec(tmp_path, "")), clocked) == []


def test_sim_model_gates(tmp_path: Path):
    from pipeline.agents import sim_model as sm
    (tmp_path / "out").mkdir()
    m, tb = tmp_path / "out" / "tick.sv", tmp_path / "out" / "tick_tb.sv"
    m.write_text(TICK_MODEL, encoding="utf-8")
    assert sm.check_model(TICK_L1)(tmp_path) == []
    m.write_text("module tick(output logic x); endmodule\n", encoding="utf-8")
    errs = " ".join(sm.check_model(TICK_L1)(tmp_path))
    assert "포트" in errs and "파라미터" in errs and "timescale" in errs

    tb.write_text(TICK_TB, encoding="utf-8")
    assert sm.check_tb(TICK_L1)(tmp_path) == []
    # 아무것도 검사하지 않는 테스트벤치와 끝나지 않는 테스트벤치는 가짜 모델에서 걸린다
    tb.write_text('`timescale 1ns/1ps\nmodule tick_tb; logic o; tick d(.o(o));\n'
                  'initial begin $display("TEST PASSED"); if (0) $display("TEST FAILED"); $finish; end endmodule\n', encoding="utf-8")
    assert "통과함" in " ".join(sm.check_tb(TICK_L1)(tmp_path))
    tb.write_text(TICK_TB.replace("logic o;", "logic o, x;").replace("$display(\"TEST PASSED\"); $finish;", "$display(\"TEST PASSED\");")
                  .replace("#2; if", "#2; forever begin #1 x = 0; end\n    if"), encoding="utf-8")
    sm.SIM_TIMEOUT_S, keep = 2, sm.SIM_TIMEOUT_S
    try:
        assert "timeout" in " ".join(sm.check_tb(TICK_L1)(tmp_path))
    finally:
        sm.SIM_TIMEOUT_S = keep


def test_run_sim_model_path_repairs_and_skips_ir(tmp_path: Path):
    """Architect, IR 없이 테스트벤치와 동작 모델만 돌고, 실패 로그로 고친다."""
    from pipeline.scheduler.run import Run
    from experiments.bench.common.runner import extract_candidate_rtl
    spec = _tick_spec(tmp_path)
    files = {"Verifier|시뮬레이션 모델": {"out/tick_tb.sv": TICK_TB},
             "Author|failures.json": {"out/tick.sv": TICK_MODEL},
             "Author|시뮬레이션 모델": {"out/tick.sv": TICK_STUCK}}
    run_dir = tmp_path / "run"
    rep = Run(run_dir, spec, _fake_roles(tmp_path, files), isolate=False).execute()
    assert rep["status"] == "pass" and rep["sim_model"], rep
    assert rep["chosen"] == {"tick": 0}
    assert rep["candidates"]["tick/c0"]["repairs"] == 1 and rep["candidates"]["tick/c1"]["status"] == "pass"
    assert "SIM_MODEL" in {r["code"] for r in rep["review"]}
    assert "architect" not in json.loads((run_dir / "state.json").read_text(encoding="utf-8"))["steps"]
    assert (run_dir / "rtl" / "tick.sv").read_text(encoding="utf-8") == TICK_MODEL
    assert [p.name for p in extract_candidate_rtl(run_dir, 1, tmp_path / "c1")] == ["tick.sv"]


def test_run_sim_model_fails_when_repairs_run_out(tmp_path: Path):
    from pipeline.scheduler.run import Run
    files = {"Verifier|시뮬레이션 모델": {"out/tick_tb.sv": TICK_TB}, "Author|시뮬레이션 모델": {"out/tick.sv": TICK_STUCK}}
    rep = Run(tmp_path / "run", _tick_spec(tmp_path), _fake_roles(tmp_path, files), isolate=False).execute()
    assert rep["status"] == "failed" and not rep["chosen"]
    assert rep["candidates"]["tick/c0"]["repairs"] == 2 and "TEST FAILED" in rep["candidates"]["tick/c0"]["reason"]


def test_bench_sim_model_scores_without_ppa(tmp_path: Path):
    """외부 TB가 출력 변화 시각에 값을 읽어도 비블로킹 대입 모델은 바뀌기 전 값을 보아 통과한다."""
    from experiments.bench.common.runner import run_and_score
    tb = tmp_path / "ext_tb.v"
    tb.write_text("""module ext_tb;
  wire o; reg res = 0; integer e = 0;
  tick d(.o(o));
  initial begin
    repeat (10) begin #5; e = (res == o) ? e : e + 1; res = res + 1; end
    if (e == 0) $display("Your Design Passed"); else $display("failures %d", e);
    $finish;
  end
endmodule
""", encoding="utf-8")
    item = BenchItem("rtllm", "tick", "combinational", "tick", "p", "module tick(output o); endmodule", eval=EvalSpec(tb_files=[str(tb)]))
    files = {"Verifier|시뮬레이션 모델": {"out/tick_tb.sv": TICK_TB}, "Author|시뮬레이션 모델": {"out/tick.sv": TICK_MODEL}}
    scored = run_and_score(item, tmp_path / "run", tmp_path / "eval", _tick_spec(tmp_path), _fake_roles(tmp_path, files), [], isolate=False)
    assert scored["bench_pass_chosen"] and scored["ppa_chosen"] is None
    assert scored["candidates"]["c0"]["lang"] == "sim_model" and scored["candidates"]["c0"]["ppa"] is None
