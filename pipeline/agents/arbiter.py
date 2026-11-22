"""Arbiter 에이전트: RTL과 참조 모델(테스트)이 다를 때 어느 쪽이 SPEC과 맞는지 판정한다.

볼 수 있는 것: SPEC, 불일치 리포트(첫 불일치의 기댓값과 실제값, 트레이스, 관련 SPEC 구절), 테스트 파일(모델과 시나리오).
RTL과 IR은 보지 않는다 (판정 기준은 SPEC).
쓸 수 있는 것: `out/verdict.json`.
판정: rtl_wrong(RTL을 Repair로), test_wrong(기댓값 수정안, 사람 승인 필요), spec_ambiguous(검토 항목, 작성자에게 질문).
Repair가 L1 변경을 요청한 실패도 판정한다 (`repair_claim`). 이때 rtl_wrong이면 L1 변경 요청이 그대로 사람에게 간다.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.agents.task import SPEC_GUIDE, Task
from pipeline.spec.parse import load

VERDICTS = {"rtl_wrong", "test_wrong", "spec_ambiguous"}
OUT = "out/verdict.json"

TASK = """\
# 과제: 불일치 판정

너는 RTL 생성 파이프라인의 Arbiter 에이전트다. 시뮬레이션에서 RTL의 출력과 참조 모델의 기댓값이 달랐다.
SPEC을 근거로 어느 쪽이 맞는지 판정한다. RTL 코드는 볼 수 없다. 판정 기준은 SPEC뿐이다.

## 핵심 원칙 (SPEC-First & 웹 검색 금지)
1. **SPEC은 유일한 진실 원천(Single Source of Truth)이다.**
   - 판정 기준은 오직 `spec.md`뿐이다.
   - 외부 테스트벤치는 공식 채점 기준(Ground Truth)이며 최우선 순위(Priority 1)이지만:
     - **외부 TB 통과 (`external_tb.ok == true`)**: 내부 Verifier가 실패했더라도 RTL은 외부 공식 골든 규격을 이미 완벽히 만족한다. 따라서 내부 Verifier의 테스트 코드에 구현 결함이 있는 것이므로 **반드시 `test_wrong`으로 판정**한다.
     - **외부 TB 실패 (`external_tb.ok == false`)**:
       - 실패 원인이 SPEC 5~7절, 9절에 명시된 요구사항을 RTL이 어긴 것이면 **반드시 `rtl_wrong`으로 판정**한다.
       - 그러나 실패 원인이 **SPEC에 명시되지 않은 미정의 동작(예: 미정의 opcode, 코너 케이스, 미정의 리셋 벡터 등)이거나 SPEC과 외부 TB 간의 불일치**인 경우, 자의적 패치를 유도하지 말고 **`spec_ambiguous`**로 판정하거나 proposal에 `[SPEC-TB 불일치]에 따른 SPEC 보정(ext_fix) 필요`를 명시하여 SPEC이 먼저 개정되도록 유도한다.
2. **웹 검색 및 외부 역공학 절대 금지 (NO WebSearch / NO WebFetch)**:
   - `search_web`, `WebSearch`, `WebFetch` 등 웹 검색이나 외부 네트워크 접근 도구를 절대로 호출하지 마라.
   - 오직 작업 디렉터리의 로컬 파일(`spec.md`, `mismatch.json`, `test.py` 등)만 보고 논리적으로 판정하라.
3. **스펙 요구사항 최우선 원칙 (SPEC-First vs Schema/Lint Constraints)**:
   - **SPEC 요구사항과 IR 스키마 또는 정적 린트 규칙 간에 충돌이 발생할 경우, 항상 SPEC 요구사항을 최우선으로 반영한다.**
   - RTL이나 테스트벤치가 스키마 제약으로 인해 SPEC의 요구사항(예: 동적 비동기 리셋, 특수 인터페이스 등)을 왜곡하거나 누락한 경우, SPEC을 위반한 쪽의 결함으로 엄격히 판정한다.

## 입력
- `spec_guide.md`: SPEC 읽기 안내. 절마다 목적과 구속력이 적혀 있다. `spec.md`보다 먼저 읽는다
- `spec.md`: SPEC. 근거로 든 문장이 10절 [LESSON]의 추정값이면 판정에 "추정값"임을 적는다
- `mismatch.json`: 불일치 리포트. `first_failure`의 expected는 참조 모델의 기댓값, got은 RTL의 실제값이다.
  `fsm_trace`, `fanin.values`는 그 시점 전후의 내부 값, `spec.clauses`는 관련 요구사항이다
- `test.py` (또는 `test.sv`): 참조 모델과 테스트 시나리오 (기댓값이 어떻게 만들어졌는지)
- `mismatch.json`에 `external_tb`가 있으면: **외부 테스트벤치는 공식 채점 기준(Ground Truth)이며 최우선 순위(Priority 1)**다.
  - **외부 TB 통과 (`external_tb.ok == true`)**: 내부 Verifier가 실패했더라도 RTL은 외부 공식 골든 규격을 이미 완벽히 만족한다. 따라서 내부 Verifier의 테스트 코드에 구현 결함(예: 과도한 제약, 클럭 하강 에지 대기 없는 샘플링 에지 타이밍 오차, 비동기 리셋 모델 동기화 불일치 등)이 있는 것이므로 **반드시 `test_wrong`으로 판정**한다. 정상인 RTL을 고치려 하지 않고 내부 테스트벤치를 수정·완화하거나 외부 TB 합격을 인정하도록 제안한다.
  - **외부 TB 실패 (`external_tb.ok == false`)**: 내부 Verifier가 통과했거나 실패했거나 관계없이, 공식 채점 기준을 만족하지 못했으므로 원칙적으로 **`rtl_wrong`으로 판정**하여 외부 TB 진단을 최우선 근거로 삼는다. 단, 실패 원인이 SPEC에 없는 미정의 동작이거나 SPEC-TB 충돌인 경우 `spec_ambiguous` 또는 SPEC 보정 필요를 명시한다.
- `mismatch.json`에 `other_verifiers_passed`가 있으면: **같은 RTL 후보가 다른 독립 Verifier(`other_verifiers_passed`)의 테스트벤치는 모두 통과**했다는 뜻이다.
  이 경우 RTL은 정상인데 실패한 테스트 코드에 구현 결함(예: 입력 구동 헬퍼 안의 `FallingEdge`/`RisingEdge`와 호출부의 클럭 대기가 겹쳐 클럭 에지가 2번 이상 지난 뒤
  1사이클 기댓값과 비교, 리셋 해제 타이밍 오류, `h.drain()` 동안 입력이 0으로 바뀌어 마지막 사이클 속성이 깨짐, 외부 서브모듈이나 미정의 초기값을 임의로 0으로 가정 등)이 있는지
  테스트의 클럭 에지 경과 횟수와 신호 구동 순서를 반드시 먼저 추적한다. 테스트가 에지를 더 보냈거나 자극/기댓값을 잘못 구성해 생긴 불일치면 `test_wrong`이다
- `mismatch.json`에 `repair_claim`이 있으면: Repair가 이 기댓값은 모듈 인터페이스(L1)를 바꾸지 않고는 맞출 수 없다고 주장했다
  (`reason`). SPEC이 정말 그 기댓값을 요구하는지 판정한다. SPEC 문장보다 테스트가 더 엄격하게 읽었으면
  (예: "리셋 동안"을 리셋이 걸린 바로 그 사이클로 읽음) test_wrong, SPEC이 분명히 요구하면 rtl_wrong이다
- `mismatch.json`에 `repeated_failure`가 있으면: Repair가 RTL을 여러 번 고쳤는데도 같은 테스트가 같은 이유로 실패했다.
  테스트가 SPEC에 없는 것을 요구하거나 SPEC보다 엄격하게 읽었는지 특히 살핀다. SPEC이 그 기댓값을 요구하면 rtl_wrong이다

## 할 일
SPEC 구절을 근거로 판정하고 `{out}`에 아래 JSON을 쓴다.

{{"verdict": "rtl_wrong | test_wrong | spec_ambiguous", "reqs": ["근거 요구사항 ID"], "reason": "SPEC 문장을 인용한 판정 이유 (수정 사유)",
  "proposal": "test_wrong이면 참조 모델이나 기댓값을 어떻게 고칠지, spec_ambiguous이면 작성자에게 물을 질문 및 테스트가 두 해석을 모두 허용하도록 완화하는 수정안"}}

- rtl_wrong: SPEC대로면 기댓값이 맞고 RTL이 틀림
- test_wrong: SPEC대로면 RTL이 맞고 기댓값(참조 모델이나 시나리오)이 틀림. 수정은 사람이 승인한 뒤에 적용된다
- spec_ambiguous: SPEC이 이 경우를 정하지 않았거나 두 해석이 모두 가능함. SPEC 안에서 서로 맞지 않는 요구도 여기다.
  예: 리셋이 없는 도메인(2번 섹션 리셋 포트 "없음")의 레지스터에 초기값을 요구함. initial 문을 쓸 수 없고
  시뮬레이터가 초기값을 무작위로 두므로 어떤 RTL도 만족할 수 없다 (rtl_wrong이 아님)

## 규칙
- `{out}` 외의 파일은 만들거나 고치지 않는다.
"""


def make_task(name: str, spec: str | Path, mismatch: dict, test_file: Path) -> Task:
    inputs = {"spec.md": Path(spec), "spec_guide.md": SPEC_GUIDE, "mismatch.json": json.dumps(mismatch, ensure_ascii=False, indent=1), "test.py": test_file}
    if Path(test_file).suffix == ".sv":
        inputs["test.sv"] = test_file
    return Task("arbiter", name, TASK.format(out=OUT), inputs=inputs, tools=False, outputs=[OUT])


def check(spec: str | Path):
    sp = load(spec)
    known = {r.id for r in sp.requirements} | {s["id"] for s in sp.scenarios}

    def _check(work: Path) -> list[str]:
        try:
            v = json.loads((work / OUT).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return [f"판정을 읽을 수 없음: {e}"]
        errs = []
        if v.get("verdict") not in VERDICTS:
            errs.append(f"verdict는 {sorted(VERDICTS)} 중 하나여야 함")
        reqs = v.get("reqs") or []
        if not reqs:
            errs.append("근거 요구사항(reqs)이 없음")
        errs += [f"SPEC에 없는 요구사항: {r}" for r in reqs if r not in known]
        if not str(v.get("reason", "")).strip():
            errs.append("판정 이유(reason)가 없음")
        if v.get("verdict") in ("test_wrong", "spec_ambiguous") and not str(v.get("proposal", "")).strip():
            errs.append("test_wrong과 spec_ambiguous는 수정안이나 질문(proposal)이 필요함")
        return errs
    return _check


# ---------------------------------------------------------------- P11 R6
R6_VERDICTS = {"spec_gap", "regen_defect"}

R6_TASK = """\
# 과제: SPEC 충실도 판정 (P11 R6)

너는 RTL 생성 파이프라인의 Arbiter 에이전트다. 원본 RTL에서 SPEC을 썼고, 그 SPEC만으로 설계를 다시 만들었다.
같은 자극에서 원본과 다시 만든 설계(재생성)의 출력 순서가 달랐다. 어느 쪽 문제인지 SPEC을 근거로 판정한다.
원본은 기준이다 (원본 동작이 곧 의도한 동작).

## 입력
- `spec_guide.md`: SPEC 읽기 안내. 절마다 목적과 구속력이 적혀 있다. `spec.md`보다 먼저 읽는다
- `spec.md`: SPEC. 근거로 든 문장이 10절 [LESSON]의 추정값이면 판정에 "추정값"임을 적는다
- `r6.json`: `stimulus`(차이를 내는 가장 짧은 자극, 사건 시간순), `difference`(첫 차이: 스트림, 순번, 원본(a)과 재생성(b)의
  트랜잭션), `ref`/`regen`(각 출력 스트림의 트랜잭션 앞부분), `inputs_accepted`(받아들인 입력 수)

## 할 일
`{out}`에 아래 JSON을 쓴다.

{{"verdict": "spec_gap | regen_defect", "reqs": ["근거 요구사항 ID"], "reason": "SPEC 문장을 인용한 판정 이유",
  "proposal": "spec_gap이면 SPEC에 더하거나 고칠 문장 (원본 동작을 하나로 정하도록)"}}

- spec_gap: SPEC이 이 동작을 정하지 않았거나 두 가지로 읽힌다. 재생성 설계는 SPEC을 어기지 않았다
- regen_defect: SPEC이 원본 동작을 분명히 정했는데 재생성 설계가 어겼다 (`reqs`에 그 요구사항)

## 규칙
- `{out}` 외의 파일은 만들거나 고치지 않는다.
"""


def make_r6_task(name: str, spec: str | Path, r6: dict) -> Task:
    inputs = {"spec.md": Path(spec), "spec_guide.md": SPEC_GUIDE, "r6.json": json.dumps(r6, ensure_ascii=False, indent=1)}
    return Task("arbiter", name, R6_TASK.format(out=OUT), inputs=inputs, tools=False, outputs=[OUT])


def check_r6(spec: str | Path):
    known = {r.id for r in load(spec).requirements}

    def _check(work: Path) -> list[str]:
        try:
            v = json.loads((work / OUT).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return [f"판정을 읽을 수 없음: {e}"]
        errs = []
        if v.get("verdict") not in R6_VERDICTS:
            errs.append(f"verdict는 {sorted(R6_VERDICTS)} 중 하나여야 함")
        reqs = v.get("reqs") or []
        if v.get("verdict") == "regen_defect" and not reqs:
            errs.append("regen_defect는 근거 요구사항(reqs)이 필요함")
        errs += [f"SPEC에 없는 요구사항: {r}" for r in reqs if r not in known]
        if not str(v.get("reason", "")).strip():
            errs.append("판정 이유(reason)가 없음")
        if v.get("verdict") == "spec_gap" and not str(v.get("proposal", "")).strip():
            errs.append("spec_gap은 SPEC 수정안(proposal)이 필요함")
        return errs
    return _check
