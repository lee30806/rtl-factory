"""Verifier 에이전트: SPEC과 동결된 L1만 보고 참조 모델, 속성, 시나리오를 쓴다.

볼 수 있는 것: SPEC, 동결된 L1, L1에서 생성한 하니스, 테스트벤치 안내서. L2, L3, RTL은 입력에 없다 (격리).
쓸 수 있는 것: `out/test_<module>.py` 하나 (cocotb 테스트 모듈, `Model` 클래스 포함).
종료 게이트: 파이썬 문법, `Model.on_input`이 무작위 입력에서 예외 없이 돌 것, cocotb 테스트가 둘 이상일 것.

수정 작업(`make_revise_task`): 사람이 승인한 기댓값 수정안(Arbiter의 test_wrong 판정)대로 자기 테스트를 고친다.
게이트는 같고, 테스트가 실제로 바뀌어야 한다.

보강 작업(`make_augment_task`): 통과한 RTL 후보들이 같은 무작위 자극에 다른 출력을 냈을 때(후보 간 차분 시뮬레이션),
그 상황을 SPEC으로 판정하는 directed 테스트를 더한다. SPEC이 정하지 않은 동작이면 테스트를 바꾸지 않고 그렇다고 쓴다.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

from pipeline.agents.task import ROOT, SPEC_GUIDE, Task
from pipeline.ir.model import Module
from pipeline.tb.gen import harness_config, harness_source

GUIDE = ROOT / "pipeline" / "agents" / "prompts" / "tb_guide.md"

TASK = """\
# 과제: {module} 모듈의 참조 모델과 테스트 작성

너는 RTL 생성 파이프라인의 Verifier 에이전트다. RTL은 볼 수 없다. SPEC과 인터페이스(L1)만 보고, RTL이 SPEC을
지키는지 판정할 참조 모델과 테스트를 쓴다.

## 핵심 원칙 (SPEC-First & 웹 검색 금지)
1. **SPEC은 유일한 진실 원천(Single Source of Truth)이다.**
   - 무엇이 맞는 동작인지는 오직 `spec.md`가 결정한다. SPEC이 정하지 않은 동작(미정의)이나 "구현에 따름"으로 명시된 부분은 검사하지 않는다.
2. **웹 검색 및 외부 역공학 절대 금지 (NO WebSearch / NO WebFetch / Zero-Leakage Isolation)**:
   - `search_web`, `WebSearch`, `WebFetch` 등 웹 검색이나 외부 네트워크 접근 도구를 절대로 호출하지 마라.
   - 현재 작업 디렉터리(`.`) 바깥의 호스트 파일 시스템(상위 디렉터리 `..`, `/Users/...`, `build/datasets/...`, 원본/정답 RTL 파일 등)을 탐색하거나 읽는 행위는 엄격히 금지된다.
   - 오직 현재 작업 디렉터리에 제공된 로컬 파일(`spec.md`, `l1.json`, `tb_guide.md` 등)만 참조하여 참조 모델과 테스트를 작성하라.
3. **스펙 요구사항 최우선 원칙 (SPEC-First vs Schema/Lint Constraints)**:
   - **SPEC 요구사항과 IR 스키마 또는 정적 린트 규칙 간에 충돌이 발생할 경우, 항상 SPEC 요구사항을 최우선으로 반영한다.**
   - 참조 모델과 테스트벤치는 임의의 스키마 단순화가 아닌 SPEC 원문의 엄밀한 요구사항을 기준으로 검증해야 한다.

## 입력
- `spec_guide.md`: SPEC 읽기 안내. 절마다 목적과 구속력이 적혀 있다. `spec.md`보다 먼저 읽는다
- `spec.md`: SPEC. 5~7번 섹션의 요구사항, 8번 섹션의 예시 시나리오. 10절 [LESSON]의 추정값도 (필수)로 확정된 계약이라 검사 대상이다.
  `미정의:` 줄의 입력에는 기대값을 두지 않는다
- `l1.json`: 동결된 L1 (포트, 번들, 도메인, 파라미터)
- `harness/{module}_harness.py`: L1에서 생성한 하니스 (읽기만)
- `tb_guide.md`: 테스트벤치 작성 안내. 먼저 읽는다

## 할 일
1. `tb_guide.md`를 읽는다.
2. `{out}`에 cocotb 테스트 모듈을 쓴다. `Model` 클래스(참조 모델)와 `@cocotb.test()` 테스트 함수를 둘 이상 둔다.
   하니스는 `from {module}_harness import make`로 가져온다.
3. SPEC의 요구사항 ID를 기대 출력, 속성, `cover_protocol` 중 하나 이상으로 모두 실행한다. 8번 섹션의 시나리오는 directed 테스트로 쓴다.
4. `python3 tools/pl.py model {out} l1.json`이 `"ok": true`가 될 때까지 고친다.

## 규칙
- `{out}` 외의 파일은 만들거나 고치지 않는다.
- SPEC이 정하지 않은 동작(미정의)은 검사하지 않는다.
- SPEC이 "구현에 따름(검증하지 않음)"이라고 한 값은 검사하지 않는다. 범위로 정한 값은 그 범위로 검사하고, 경계를 좁히거나
  참고로 적힌 한 값(예: "참조 묶음에서는 7")으로 바꾸지 않는다.
- **미세 타이밍 샘플링 및 검증 (Check-then-Update 원칙):**
  - DUT 출력이 동기 등록 출력(`seq`)인 경우, 클럭 상승 에지에서 값이 갱신되므로 검사 시점은 해당 상승 에지 직후(예: `await FallingEdge(dut.clk)` 또는 `#1 step`)여야 한다.
  - **순차 회로 참조 모델 상태 갱신 순서**: 속성 검사(CycleTracker)에서 현재 사이클의 등록 출력을 먼저 참조 모델의 현재 상태와 비교한 후(`Check`), 그 다음 사이클을 위해 상태 갱신 함수(`step()`)를 호출(`Update`)한다. `step()`을 먼저 호출해 미래 상태(Q_n+1)를 현재 사이클 출력(Q_n)과 비교하는 오류를 범하지 않는다.
  - 에지 검출 펄스가 특정 클럭 에지 Tn에 1주기 동안 발생하는 경우, Tn과 Tn+1 사이에서 검사해야 하며, 타이밍 엇갈림으로 인해 펄스가 클럭 에지 전에 사라지거나 지나친 후 검사하지 않는다.
"""


REVISE = """\
# 과제: {module} 모듈 테스트의 기댓값 수정 (사람 승인됨)

너는 RTL 생성 파이프라인의 Verifier 에이전트다. 네가 쓴 테스트 `{out}`가 RTL과 다른 기댓값을 냈고, Arbiter가 SPEC을 근거로
테스트 쪽이 틀렸다고 판정했다. 사람이 그 수정안을 승인했다. 수정안대로 테스트를 고친다. RTL은 볼 수 없다.

## 입력
- `{out}`: 지금 테스트 (이 파일을 고친다)
- `approved.json`: 승인된 수정안 목록. 항목마다 근거 요구사항(`reqs`), 판정 이유(`reason`), 수정안(`proposal`),
  불일치 내용(`first_failure`)이 있다
- `spec.md`, `l1.json`, `harness/{module}_harness.py`, `tb_guide.md`: 처음 테스트를 쓸 때와 같다

## 할 일
1. `approved.json`의 수정안이 가리키는 참조 모델, 속성, 시나리오를 SPEC 구절에 맞게 고친다.
   - 특히 Arbiter가 "1사이클 앞선 기댓값 계산", "동기화 시점 오류", "Check-then-Update 위반"을 지적한 경우, 특정 사이클의 if문 예외 처리로 땜질하지 말고 참조 모델의 호출 구조를 "선 검증 후 갱신(Check-then-Update)" 구조로 근본적으로 개편한다.
2. 수정안과 관계없는 기댓값은 바꾸지 않는다. 검사를 지우거나 약하게 만드는 것으로 해결하지 않는다.
   SPEC이 정한 동작은 계속 검사해야 한다.
3. `python3 tools/pl.py model {out} l1.json`이 `"ok": true`가 될 때까지 고친다.

## 규칙
- `{out}` 외의 파일은 만들거나 고치지 않는다.
"""


AUGMENT = """\
# 과제: {module} 모듈 테스트 보강 (후보 간 차분)

너는 RTL 생성 파이프라인의 Verifier 에이전트다. 네 테스트를 모두 통과한 RTL 후보 둘(A, B)에 같은 무작위 자극을 넣었더니
출력이 달랐다. 테스트가 그 상황을 검사하지 않는다는 뜻이다. RTL은 볼 수 없다.

## 입력
- `{out}`: 지금 테스트 (이 파일을 고친다)
- `diff.json`: 차이 리포트
  - `mode`: `steady`(처음 리셋 뒤 리셋 없음) 또는 `reset`(중간에 리셋을 넣음)
  - `difference`: 첫 차이. 출력 번들 트랜잭션 순서에서 몇 번째(`index`)가 어떻게 달랐는지, A와 B의 그 트랜잭션과 직전 몇 개.
    트랜잭션은 `[도메인, 사이클, 값]`이다. `count`면 한쪽이 트랜잭션을 더 적게 냈다 (멈춤일 수 있다)
  - `scenario`(있으면): 차이를 유지하는 가장 작은 자극으로 줄인 것. `background`(배경 트래픽)와 `events`(시간순 사건:
    리셋, 출력 backpressure, 단독 입력). 이 사건들이 차이의 원인이다. 테스트는 이 상황을 재현한다
  - `events`(scenario가 없을 때): 차이 전 자극 사건 `[도메인, 사이클, 신호, 값]`
  - `inputs_accepted`: 각 후보가 받은 입력 트랜잭션 수
  - `previous_attempt`(있으면): 이전에 네가 보강한 테스트를 후보들에 돌린 결과. 모두 실패했으면 테스트의 결함을,
    모두 통과했으면 상황을 재현하지 못한 것을 먼저 의심한다
- `spec.md`, `l1.json`, `harness/{module}_harness.py`, `tb_guide.md`: 처음 테스트를 쓸 때와 같다

## 할 일
1. `events`와 `difference`로 어떤 상황(예: 프레임 도중 한쪽 리셋, 긴 backpressure 뒤 리셋, pause와 리셋의 조합)에서
   차이가 났는지 파악한다. 어느 후보가 맞는지는 SPEC으로 판단한다. 다수결이나 짐작으로 정하지 않는다.
2. SPEC이 그 상황의 출력을 정하면: 그 상황을 재현하는 directed 테스트를 `{out}`에 더한다. 기존 테스트와 기댓값은 그대로 둔다.
   상황을 일반화해 쓴다 (사이클 번호를 그대로 옮기지 말고, "프레임 도중 리셋" 같은 상황을 만든다).
   기대 출력이 끝까지 오지 않는 경우(멈춤)도 실패로 잡히게 한다 (`h.drain()`은 기대 출력이 다 올 때까지 기다린다).
3. SPEC이 정하지 않은 동작이면: 테스트를 바꾸지 말고 `out/diff_verdict.json`에
   `{{"spec_defined": false, "reason": "SPEC의 어느 부분이 이 상황을 정하지 않는지"}}`를 쓴다.
4. 테스트를 바꿨으면 `python3 tools/pl.py model {out} l1.json`이 `"ok": true`가 될 때까지 고친다.

## 규칙
- `out/` 밖의 파일은 만들거나 고치지 않는다.
"""

DIFF_VERDICT = "out/diff_verdict.json"

SPEC_UPDATE = """\
# 과제: {module} 모듈 테스트를 개정된 SPEC에 맞추기

너는 RTL 생성 파이프라인의 Verifier 에이전트다. SPEC이 개정되었다(사람 승인). 네 테스트 `{out}`를 새 SPEC에 맞춘다. RTL은 볼 수 없다.

## 입력
- `{out}`: 지금 테스트 (이전 SPEC 기준. 이 파일을 고친다)
- `spec.md`: 새 SPEC, `spec_old.md`: 이전 SPEC
- `spec_diff.json`: 바뀐 것. `added`, `removed`, `changed`(요구사항 ID), `scenarios_added`, `scenarios_changed`, `scenarios_removed`
- `l1.json`, `harness/{module}_harness.py`, `tb_guide.md`: 처음 테스트를 쓸 때와 같다

## 할 일
1. 바뀐 요구사항의 기댓값, 속성을 새 SPEC대로 고친다. 더해진 요구사항은 검사를 더하고, 지워진 요구사항의 검사는 뺀다.
2. 더해지거나 바뀐 예시 시나리오(8번 섹션)는 directed 테스트로 쓴다. DV에서 찾은 기능 버그는 이 시나리오로 들어온다.
3. 바뀌지 않은 요구사항의 검사는 그대로 둔다.
4. 고칠 것이 없으면(바뀐 내용이 이 테스트와 관계없으면) 테스트를 바꾸지 말고 `out/diff_verdict.json`에
   `{{"no_change": true, "reason": "..."}}`를 쓴다.
5. 테스트를 바꿨으면 `python3 tools/pl.py model {out} l1.json`이 `"ok": true`가 될 때까지 고친다.

## 규칙
- `out/` 밖의 파일은 만들거나 고치지 않는다.
"""


def out_rel(module: str, tb_lang: str = "cocotb") -> str:
    return f"out/test_{module}.sv" if tb_lang == "sv" else f"out/test_{module}.py"


INOUT_NOTE = """
## 양방향 패드 (inout)
`l1.json`의 inout 포트 P는 하니스에서 세 포트로 다룬다. 테스트가 구동하는 바깥 장치의 값 `P_pad_o`와 구동 여부 `P_pad_oe`(입력),
패드에 보이는 값 `P_pad_val`(출력, 블록이 구동하면 블록의 값, 아무도 구동하지 않으면 풀업으로 1). 바깥 장치는 약하게 구동하므로
블록과 동시에 구동하면 블록의 값이 보인다. 참조 모델의 입력과 출력도 이 이름으로 쓴다.
"""

CELLS_NOTE = """
## 제공 외부 서브모듈 (`cells/`)
`cells/` 디렉터리에 블록 내부에서 인스턴스화되는 외부 서브모듈(메모리, 연산기 등)의 RTL 파일이 들어 있다.
외부 서브모듈의 초기값이나 읽기/쓰기 동작이 출력에 영향을 주면 해당 파일을 읽고 참조 모델이나 기댓값에 반영한다.
"""

SV_TASK = """\
# 과제: {module} 모듈의 SystemVerilog 자체 판정 테스트벤치 작성

너는 RTL 생성 파이프라인의 Verifier 에이전트다. RTL은 볼 수 없다. SPEC과 인터페이스(L1)만 보고, RTL이 SPEC을
지키는지 판정할 SystemVerilog-2012 테스트벤치를 `{out}`에 쓴다.

## 입력
- `spec_guide.md`: SPEC 읽기 안내. 절마다 목적과 구속력이 적혀 있다. `spec.md`보다 먼저 읽는다
- `spec.md`: SPEC. 5~7번 섹션의 요구사항, 8번 섹션의 예시 시나리오. 10절 [LESSON]의 추정값도 (필수)로 확정된 계약이라 검사 대상이다.
  `미정의:` 줄의 입력에는 기대값을 두지 않는다
- `l1.json`: 동결된 L1 (포트, 번들, 도메인, 파라미터)

## 규칙
- 첫 줄에 `` `timescale 1ns/1ps ``를 쓰고, 최상위 테스트벤치 모듈 이름은 `test_{module}`로 하며 `{module}`을 인스턴스화한다.
- 클럭이 있는 순차 모듈은 활성 클럭 에지와 겹치지 않는 비활성 에지(예: `posedge` 도메인이면 `@(negedge clk)`)에서 입력과 리셋을 구동하고,
  출력 검사는 활성 에지 직전이나 `@(posedge clk); #1;` 뒤에 수행해 시뮬레이터 레이스 컨디션이 없게 한다.
- 5~7절의 모든 (필수) 요구사항과 8절의 예시 시나리오(directed 테스트) 및 무작위 자극 테스트를 포함한다.
- 불일치가 하나라도 발생하면 `$display("TEST FAILED: [test=%s] [req=%s] [cycle=%0d] expected=%0h got=%0h", ...);`를 출력하고 즉시 `$finish;`로 끝낸다.
- 모든 검사를 통과하면 `$display("TEST PASSED");`를 출력하고 `$finish;`로 끝낸다.
- 설계가 멈춰도 시뮬레이션이 끝나도록 별도 `initial` 블록에 충분한 시간 뒤 `$display("TEST FAILED: SIM_TIMEOUT"); $finish;` 워치독을 둔다.
- `$fatal`, DPI, 외부 파일 입출력, 마크다운 코드 울타리는 쓰지 않는다.
- `{out}` 외의 파일은 만들거나 고치지 않는다.
"""

SV_REVISE = """\
# 과제: {module} 모듈 SystemVerilog 테스트벤치의 기댓값 수정 (사람 승인됨)

너는 RTL 생성 파이프라인의 Verifier 에이전트다. 네가 쓴 테스트벤치 `{out}`가 RTL과 다른 기댓값을 냈고, Arbiter가 SPEC을 근거로
테스트 쪽이 틀렸다고 판정했다. 사람이 그 수정안을 승인했다. 수정안대로 `{out}`를 고친다. RTL은 볼 수 없다.

## 입력
- `{out}`: 지금 테스트벤치 (이 파일을 고친다)
- `approved.json`: 승인된 수정안 목록 (`reqs`, `reason`, `proposal`, `first_failure`)
- `spec.md`, `spec_guide.md`, `l1.json`: 처음 테스트를 쓸 때와 같다

## 할 일
1. `approved.json`의 수정안이 가리키는 기댓값, 클럭/리셋 구동 타이밍, 시나리오를 SPEC 구절에 맞게 고친다.
2. 수정안과 관계없는 검사는 그대로 유지하고, 통과 시 `$display("TEST PASSED");`, 실패 시 `$display("TEST FAILED: ...");`와 `$finish;` 규칙을 지킨다.
3. `{out}` 외의 파일은 만들거나 고치지 않는다.
"""

SV_SPEC_UPDATE = """\
# 과제: {module} 모듈 SystemVerilog 테스트벤치를 개정된 SPEC에 맞추기

너는 RTL 생성 파이프라인의 Verifier 에이전트다. SPEC이 개정되었다(사람 승인). 네 테스트벤치 `{out}`를 새 SPEC에 맞춘다. RTL은 볼 수 없다.

## 입력
- `{out}`: 지금 테스트벤치 (이전 SPEC 기준. 이 파일을 고친다)
- `spec.md`: 새 SPEC, `spec_old.md`: 이전 SPEC
- `spec_diff.json`: 바뀐 요구사항과 시나리오 목록
- `l1.json`: 동결된 L1

## 할 일
1. 바뀐 요구사항과 시나리오의 기댓값을 새 SPEC대로 고친다.
2. 고칠 것이 없으면 `{out}`를 바꾸지 말고 `out/diff_verdict.json`에 `{{"no_change": true, "reason": "..."}}`를 쓴다.
3. `out/` 밖의 파일은 만들거나 고치지 않는다.
"""

STUB_DIR = (ROOT / "pipeline" / "emitter" / "stubs").resolve()


def _design_cells(cells: list[Path] | None) -> list[Path]:
    if not cells:
        return []
    out = []
    for c in cells:
        p = Path(c).resolve()
        if p.is_file() and p.parent != STUB_DIR:
            out.append(p)
    return out


def _inout(l1: dict, tb_lang: str = "cocotb") -> str:
    if tb_lang == "sv":
        return ""
    return INOUT_NOTE if any(p.get("dir") == "inout" for p in l1.get("ports", [])) else ""


def make_task(name: str, l1: dict, spec: str | Path, tb_lang: str = "cocotb", cells: list[Path] | None = None) -> Task:
    module = l1["module"]
    out = out_rel(module, tb_lang)
    dcells = _design_cells(cells)
    cell_map = {f"cells/{p.name}": p for p in dcells}
    cnote = CELLS_NOTE if dcells else ""
    if tb_lang == "sv":
        inputs = {"spec.md": Path(spec), "spec_guide.md": SPEC_GUIDE, "l1.json": json.dumps(l1, ensure_ascii=False, indent=1), **cell_map}
        return Task("verifier", name, SV_TASK.format(module=module, out=out) + cnote, inputs=inputs, tools=False,
                    outputs=[out], write_scope=["out/"])
    inputs = {"spec.md": Path(spec), "spec_guide.md": SPEC_GUIDE, "l1.json": json.dumps(l1, ensure_ascii=False, indent=1), "tb_guide.md": GUIDE,
              f"harness/{module}_harness.py": harness_source(Module(l1)), **cell_map}
    return Task("verifier", name, TASK.format(module=module, out=out) + _inout(l1, tb_lang) + cnote, inputs=inputs,
                outputs=[out], write_scope=["out/"])


def make_revise_task(name: str, l1: dict, spec: str | Path, test_file: Path, approved: list[dict],
                     tb_lang: str = "cocotb", cells: list[Path] | None = None) -> Task:
    module = l1["module"]
    out = out_rel(module, tb_lang)
    t = make_task(name, l1, spec, tb_lang=tb_lang, cells=cells)
    t.inputs[out] = Path(test_file)
    t.inputs["approved.json"] = json.dumps(approved, ensure_ascii=False, indent=1)
    cnote = CELLS_NOTE if _design_cells(cells) else ""
    tmpl = SV_REVISE if tb_lang == "sv" else REVISE
    t.text = tmpl.format(module=module, out=out) + _inout(l1, tb_lang) + cnote
    return t


def make_augment_task(name: str, l1: dict, spec: str | Path, test_file: Path, diff: dict,
                      tb_lang: str = "cocotb", cells: list[Path] | None = None) -> Task:
    module = l1["module"]
    out = out_rel(module, tb_lang)
    t = make_task(name, l1, spec, tb_lang=tb_lang, cells=cells)
    t.inputs[out] = Path(test_file)
    t.inputs["diff.json"] = json.dumps(diff, ensure_ascii=False, indent=1)
    cnote = CELLS_NOTE if _design_cells(cells) else ""
    t.text = AUGMENT.format(module=module, out=out) + _inout(l1, tb_lang) + cnote
    return t


def make_spec_update_task(name: str, l1: dict, spec: str | Path, old_spec: str | Path, test_file: Path, spec_diff: dict,
                          tb_lang: str = "cocotb", cells: list[Path] | None = None) -> Task:
    module = l1["module"]
    out = out_rel(module, tb_lang)
    t = make_task(name, l1, spec, tb_lang=tb_lang, cells=cells)
    t.inputs[out] = Path(test_file)
    t.inputs["spec_old.md"] = Path(old_spec)
    t.inputs["spec_diff.json"] = json.dumps(spec_diff, ensure_ascii=False, indent=1)
    cnote = CELLS_NOTE if _design_cells(cells) else ""
    tmpl = SV_SPEC_UPDATE if tb_lang == "sv" else SPEC_UPDATE
    t.text = tmpl.format(module=module, out=out) + _inout(l1, tb_lang) + cnote
    return t


def no_change(work: Path) -> str | None:
    """SPEC 개정 작업에서 Verifier가 고칠 것이 없다고 답했으면 그 이유."""
    try:
        v = json.loads((Path(work) / DIFF_VERDICT).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return str(v.get("reason", "")) if v.get("no_change") is True else None


def check_spec_update(l1: dict, before: str, tb_lang: str = "cocotb", cells: list[Path] | None = None):
    inner = check(l1, before, tb_lang=tb_lang, cells=cells)

    def _check(work: Path) -> list[str]:
        if (reason := no_change(work)) is not None:
            return [] if reason.strip() else ["no_change=true의 이유(reason)가 없음"]
        return inner(work)
    return _check


def spec_undefined(work: Path) -> str | None:
    """보강 작업에서 Verifier가 SPEC이 정하지 않은 동작이라고 답했으면 그 이유."""
    p = Path(work) / DIFF_VERDICT
    try:
        v = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return str(v.get("reason", "")) if v.get("spec_defined") is False else None


def check_augment(l1: dict, before: str, tb_lang: str = "cocotb", cells: list[Path] | None = None):
    """테스트를 보강했거나(일반 게이트 + 바뀜), SPEC이 정하지 않았다고 이유와 함께 답했으면 통과."""
    inner = check(l1, before, tb_lang=tb_lang, cells=cells)

    def _check(work: Path) -> list[str]:
        if (reason := spec_undefined(work)) is not None:
            return [] if reason.strip() else ["spec_defined=false의 이유(reason)가 없음"]
        return inner(work)
    return _check


def api_misuse(tree: ast.AST, l1: dict) -> list[str]:
    """하니스 API에 L1에 없는 이름을 넘긴 호출 (시뮬레이션 전에 잡는다. 실행하면 KeyError로 TB 실행 오류가 됨).
    send는 입력 번들, set_ready·allow_overwrite는 출력 번들, drive와 flag_input은 단독 입력,
    flag_output은 단독 출력, add_property의 둘째 인자는 도메인이다."""
    cfg = harness_config(Module(l1))
    in_b = {b["name"] for b in cfg["bundles"] if not b["dut_drives"]}
    out_b = {b["name"] for b in cfg["bundles"] if b["dut_drives"]}
    ins, outs = set(cfg["inputs"]), {o["name"] for o in cfg["outputs"]}
    single = "단독 포트는 h.drive(입력 열), h.flag_input/h.flag_output(valid 플래그 스트림), dut.<포트>.value로 다룬다"

    def names(want: set[str], what: str, extra: str = "") -> str:
        return f"{what}: {', '.join(sorted(want)) or '없음'}{extra}"
    rules = {   # 메서드 → (인자 위치, 허용 이름, 설명)
        "send": (0, in_b, names(in_b, "입력 번들", f". {single}")),
        "set_ready": (0, out_b, names(out_b, "출력 번들", f". {single}")),
        "allow_overwrite": (0, out_b, names(out_b, "출력 번들")),
        "drive": (0, ins, names(ins, "단독 입력", ". 입력 번들은 h.send")),
        "flag_input": (1, ins, names(ins, "단독 입력")),
        "flag_output": (1, outs, names(outs, "단독 출력")),
        "add_property": (1, set(cfg["clocks"]), names(set(cfg["clocks"]), "도메인")),
    }
    errs = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in rules):
            continue
        k, allowed, desc = rules[n.func.attr]
        args = [*n.args[k:k + 1]]
        if n.func.attr in ("flag_input", "flag_output") and len(n.args) > 2 and isinstance(n.args[2], (ast.List, ast.Tuple)):
            args += n.args[2].elts   # data 포트 목록
        for a in args:
            if isinstance(a, ast.Constant) and isinstance(a.value, str) and a.value not in allowed:
                errs.append(f"{n.lineno}번째 줄 h.{n.func.attr}({a.value!r}): 이 블록에 없는 이름 ({desc})")
    return errs[:20]


def _sv_stub(l1: dict) -> str:
    """출력을 구동하지 않는 가짜 DUT 모듈 (SV 테스트벤치 컴파일·판정 게이트용)."""
    import re
    params = ", ".join(f"parameter {p['name']} = {p['default']}" for p in l1.get("params", []))
    ports = []
    for p in l1.get("ports", []):
        for s in (p["signals"] if "bundle" in p else [p]):
            t = re.sub(r"\b(logic|reg)\b", "wire", s["type"]) if s["dir"] == "inout" else s["type"]
            ports.append(f"{s['dir']} {t} {s['name']}")
    return f"`timescale 1ns/1ps\nmodule {l1['module']} {f'#({params}) ' if params else ''}({', '.join(ports)});\nendmodule\n"


def check(l1: dict, before: str | None = None, tb_lang: str = "cocotb", cells: list[Path] | None = None):
    """before: 수정 작업이면 고치기 전 테스트 내용 (바뀌지 않았으면 거부)."""
    module = l1["module"]

    if tb_lang == "sv":
        from pipeline.agents import sim_model
        import tempfile

        def _check_sv(work: Path) -> list[str]:
            tb = work / out_rel(module, "sv")
            src = tb.read_text(encoding="utf-8")
            if before is not None and src == before:
                return ["테스트가 바뀌지 않음 (승인된 수정안을 반영해야 함)"]
            errs = [f"테스트벤치에 없음: {w}" for w in (sim_model.PASS, sim_model.FAIL, "$finish", "`timescale") if w not in src]
            with tempfile.TemporaryDirectory() as tmp:
                fake = Path(tmp) / "stub.sv"
                fake.write_text(_sv_stub(l1), encoding="utf-8")
                vvp = Path(tmp) / "sim.vvp"
                tb_top = f"test_{module}"
                if err := sim_model._compile([fake, tb], tb_top, vvp):
                    return errs + [f"가짜 DUT와 컴파일 실패 (모듈 이름 `{tb_top}` 또는 포트 불일치): {err}"]
                try:
                    p = subprocess.run(["vvp", str(vvp)], cwd=tmp, capture_output=True, text=True, timeout=sim_model.SIM_TIMEOUT_S)
                    log = p.stdout + p.stderr
                    if p.returncode == 0 and sim_model.PASS in log and sim_model.FAIL not in log:
                        errs.append("출력을 구동하지 않는 가짜 DUT가 통과함. 요구사항의 출력값과 변화를 실제로 검사해야 함")
                except subprocess.TimeoutExpired:
                    errs.append(f"가짜 DUT 실행이 {sim_model.SIM_TIMEOUT_S}초 안에 끝나지 않음 (워치독 타임아웃과 $finish 필요)")
            return errs
        return _check_sv

    def _check(work: Path) -> list[str]:
        src = (work / out_rel(module)).read_text(encoding="utf-8")
        if before is not None and src == before:
            return ["테스트가 바뀌지 않음 (승인된 수정안을 반영해야 함)"]
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            return [f"문법 오류: {e}"]
        tests = [f.name for f in ast.walk(tree) if isinstance(f, ast.AsyncFunctionDef)
                 and any("cocotb.test" in ast.unparse(d) for d in f.decorator_list)]
        errs = [] if len(tests) >= 2 else [f"cocotb 테스트가 둘 이상 있어야 함 (지금 {len(tests)}개)"]
        errs += api_misuse(tree, l1)
        if not any(isinstance(n, ast.ClassDef) and n.name == "Model" for n in ast.walk(tree)):
            errs.append("Model 클래스가 없음")
            return errs
        # 하니스 import는 모델 단독 실행에서 필요 없으므로, 하니스 디렉터리를 경로에 넣고 실행한다
        p = subprocess.run([sys.executable, str(ROOT / "pipeline" / "agents" / "pl.py"), "model", str(work / out_rel(module)),
                            str(work / "l1.json")], capture_output=True, text=True, cwd=work / "harness",
                           env={"PYTHONPATH": str(work / "harness"), "PATH": "/usr/bin:/bin", "PL_NO_LOG": "1"}, timeout=120)
        try:
            res = json.loads(p.stdout)
        except json.JSONDecodeError:
            return errs + [f"모델 단독 실행 실패: {(p.stderr or p.stdout)[-500:]}"]
        if not res.get("ok"):
            errs.append(f"모델 단독 실행에서 예외: {res.get('error')}")
        return errs
    return _check
