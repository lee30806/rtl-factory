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
