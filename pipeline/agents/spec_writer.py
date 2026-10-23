"""SpecWriter 에이전트 (P11 R5, R6): 원본 RTL과 이식한 IR을 읽고 10개 섹션 SPEC을 쓴다.

볼 수 있는 것: 원본 RTL과 헤더, 이식 IR 묶음, 미리 만든 SPEC 2~4절 표(`tables.md`, L1에서 결정론적으로 만듦),
R1~R4가 남긴 메모(`notes.json`: define 설정, 헤더 상수, 지운 파라미터, 검토 항목, 리셋 직후 차이), SPEC 작성 템플릿,
원본 시뮬레이션 묶음(`ref/`).
쓸 수 있는 것: `out/spec.md` 하나.
도구: `pl.py refsim`(원본 RTL에 정한 자극을 넣고 출력 트랜잭션과 사이클을 봄), `pl.py spec_gate`.
종료 게이트: SPEC 검사 게이트 통과, SPEC 2~4절에서 만든 L1이 이식 IR의 L1과 같음(role, 허용 범위 제외), 제목의 모듈 이름.
고치기 과제(make_revise_task, R6): SPEC만으로 다시 만든 설계가 원본과 다르게 동작했고 Arbiter가 SPEC 누락으로 판정하면,
줄인 자극과 원본·재생성 출력을 받아 SPEC을 고친다.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.agents.task import ROOT, Task
from pipeline.importer.spec_tables import roundtrip_diff
from pipeline.spec.check import run as spec_check
from pipeline.spec.l1 import SpecTableError, block_top_l1
from pipeline.spec.parse import load

TEMPLATE = ROOT / "docs" / "SPEC 작성 템플릿.md"
OUT = "out/spec.md"

TASK = """\
# 과제: 원본 RTL {top}의 SPEC 쓰기

너는 RTL 생성 파이프라인의 SpecWriter 에이전트다. 원본 RTL이 하는 일을 10개 섹션 SPEC으로 쓴다.
이 SPEC만 보고 다른 에이전트들이 설계를 처음부터 다시 만든다. 다시 만든 설계는 원본과 같은 입력에 같은 출력 순서를 내야 한다.
그러니 블록 경계(포트)에서 관찰할 수 있는 동작을 빠짐없이, 하나로만 읽히게 쓴다.

## 입력
- `template.md`: SPEC 작성 템플릿. 형식 규칙(제목, 섹션 이름과 순서, 요구사항 줄, 시나리오 표, 열린 질문)을 그대로 따른다
- `tables.md`: 2~4절 표. 원본에서 추출하고 사용자가 확정한 것이다. **글자 하나 바꾸지 않고** 그대로 넣는다
- `source/`: 원본 RTL (모듈마다 파일, `headers/`)
- `ir/`: 원본을 1:1로 옮긴 IR (원본과 사이클 단위로 같음을 확인함). 원본보다 읽기 쉬우면 참고한다
- `notes.json`: define 설정, 헤더에서 온 상수, 고정 설정에서 지운 파라미터, 원본 추출의 검토 항목, 이식에서 리셋 직후 다른 곳
- `ref/`: 원본 시뮬레이션 묶음 (`refsim` 도구가 읽음)

## 원본으로 확인하기 (`refsim`)
`python3 tools/pl.py refsim ir/{top}.ir.json --ref ref --stim .refsim/stim1.json`은 원본 RTL에 네가 정한 자극을 넣고, 출력 번들마다
나간 트랜잭션(필드 값, 리셋이 풀린 뒤 사이클)을, 상태 출력 같은 단독 출력마다 값이 바뀐 사이클과 값(`singles`)을 낸다. 자극 파일은 `.refsim/` 아래에 둔다 (예: `.refsim/stim1.json`):
`{{"inputs": {{"<입력 번들>": [{{"<필드>": 값, ...}}, ...]}}, "valid": {{"<입력 번들>": [사이클별 0/1]}},
"ready": {{"<출력 번들>": [사이클별 0/1]}}, "single": {{"<단독 입력 포트>": [사이클별 값]}}, "cycles": N}}`.
`inputs` 말고는 모두 생략할 수 있다 (생략하면 valid=1, ready=1, 단독 입력 0). 필드는 포트 이름(`s_axis_tdata`)이나
번들 이름을 뺀 이름(`tdata`)으로 쓴다. 원본을 읽어 짐작한 동작, 특히 **오류 입력, 경계 값, 프레임 끝, backpressure에서의
출력**은 이 도구로 원본 출력을 확인한 뒤 쓴다. 출력의 필드 값(오류 출력의 data 값 포함), 지연 사이클 수도 원본이 낸 그대로 쓴다.

## 할 일
`{out}`에 SPEC을 쓴다.
- 제목은 `# {top} SPEC`. 2~4절은 `tables.md` 그대로.
- 1절: 목적, 범위, 범위 밖. 고정 설정(파라미터 값, define)과 지운 파라미터를 범위에 적는다.
- 5절 기능 요구사항: 포트에서 보이는 동작. 데이터 변환, 순서, 프레이밍, 플래그와 상태 출력의 뜻, 프로토콜(valid/ready) 규칙.
- 6절 타이밍과 성능: 원본의 지연(입력에서 출력까지 몇 클럭), 처리율, backpressure에서의 동작. 원본이 고정 지연이면 그 값을 쓴다.
- 7절 예외와 코너 케이스: 리셋 중과 리셋 직후 출력, 가득 참/비어 있음, 잘못된 입력. 잘못된 입력마다 `refsim`으로 원본 출력을
  확인하고 그 출력(필드 값, tlast 같은 끝 표시, 오류 플래그)을 그대로 쓴다. 원본 출력이 입력에 따라 정해지면 "미정의"로 쓰지 않는다.
- 8절 시나리오: 정상 하나 이상, 코너(오류 입력 포함) 하나 이상. 입력과 기대 출력 표. 기대 출력은 `refsim`으로 원본에서 얻은 값이다.
- 9절 구현 제약: 헤더에서 온 상수(이름과 값), 반드시 지킬 구조(예: CDC 셀 종류)가 있으면.
- 10절 열린 질문: 원본에서 의도를 알 수 없는 것.
그다음 `python3 tools/pl.py spec_gate {out} --tables tables.md --top {top}`이 `"ok": true`가 될 때까지 고친다.

## 규칙
- `{out}` 외의 파일은 만들거나 고치지 않는다.
- 원본의 내부 구현(레지스터 이름, 상태 인코딩)을 요구사항으로 쓰지 않는다. 포트에서 보이는 동작만 쓴다.
- 원본 동작을 "고치거나" 원본에 없는 기능을 더하지 않는다.
- 리셋 도메인이 둘 이상(서로 다른 클럭)이면 리셋은 사용 조건으로 쓴다: "모든 도메인의 리셋을 함께, 가장 느린 클럭 4사이클
  이상 건다. 한 도메인만 리셋하는 것은 지원하지 않는다(미정의)." 그리고 리셋 때 내보내지 않은 항목의 처리, 리셋 중과 풀린 뒤
  출력(몇 사이클 안에 무엇을 내는지 범위), 풀린 뒤 처음 받는 입력부터의 동작을 쓴다. 한 도메인의 리셋이 다른 도메인에
  닿는 사이클, 동기화 단 수처럼 두 클럭의 위상에 따라 달라지는 사이클 수는 정확한 값 대신 범위로 쓰고 "구현에 따름(검증하지
  않음)"이라고 적는다. 한 도메인만 리셋했을 때 원본이 내는 출력은 SPEC에 쓰지 않는다. R6의 비교도 리셋을 함께 걸고 마지막
  리셋이 풀린 뒤의 출력만 본다. `refsim`으로 확인할 때도 `"reset": {{"<도메인>": [사이클별 0/1]}}`로 모든 도메인에 리셋을 함께 준다.
"""

REVISE = """\

## SPEC 고치기 (R6)
`spec_prev.md`는 네가 앞서 쓴 SPEC이다. 이 SPEC만으로 다시 만든 설계가 원본과 다르게 동작했고, 판정자가 SPEC이 그 동작을
정하지 않았거나 두 가지로 읽힌다고 판정했다. `r6.json`에 판정 이유, 차이를 내는 가장 짧은 자극(`stimulus`), 원본과 재생성
설계의 출력(`ref`, `regen`)이 있다. `spec_prev.md`에서 시작해 원본 동작을 하나로 정하는 요구사항(또는 시나리오, 타이밍)을
더하거나 고친 SPEC 전체를 `{out}`에 쓰고, `spec_gate`를 다시 통과시킨다. 2~4절은 그대로 둔다.
고치기 전에 `r6.json`의 자극과 비슷한 경우(같은 오류의 다른 자리, 앞뒤 바이트를 바꾼 것)를 `refsim`으로 돌려,
더하는 문장이 이 사례 하나가 아니라 원본의 규칙과 맞는지 확인한다.
"""

REVISE_STIMS = """
`r6_stim/`에는 SPEC만 보고 쓴 Verifier 테스트 중 원본에서 실패한 것의 자극이 `refsim` 형식 그대로 있다 (파일 이름은
`v<Verifier 번호>_<테스트 이름>.json`, 실패 메시지는 `r6.json`의 `verifier_failures_on_original`). `reset`은 도메인별
사이클마다 리셋(1이면 걸림)이다. 고치기 전에 각 파일을 `refsim ... --stim r6_stim/<파일>`로 돌려 원본 출력을 보고,
실패 메시지의 기대와 처음 어긋나는 출력이 어떤 입력 바이트와 사이클에서 나왔는지 찾는다. 그 입력에서 짧은 자극을
`.refsim/`에 만들어 원본 규칙을 확인한 뒤, 기대가 그 출력이 되도록 SPEC 문장을 하나로 정한다.
"""


def make_task(name: str, top: str, tables: str, sources: dict[str, Path], irs: dict[str, dict], notes: dict,
              prev_spec: Path | None = None, r6: dict | None = None, ref_dir: Path | None = None,
              stims: dict[str, Path] | None = None) -> Task:
    """ref_dir: `porter.make_ref_dir`로 만든 원본 묶음 (`refsim`이 읽음). stims: 원본에서 실패한 Verifier 테스트의 자극."""
    inputs: dict = {"template.md": TEMPLATE, "tables.md": tables, "notes.json": json.dumps(notes, ensure_ascii=False, indent=1)}
    if ref_dir is not None:
        inputs["ref"] = ref_dir
    for rel, p in sources.items():
        inputs[f"source/{rel}"] = p
    for n, ir in irs.items():
        inputs[f"ir/{n}.ir.json"] = json.dumps(ir, ensure_ascii=False, indent=1)
    text = TASK.format(top=top, out=OUT)
    if prev_spec is not None:
        inputs["spec_prev.md"] = prev_spec
        inputs["r6.json"] = json.dumps(r6 or {}, ensure_ascii=False, indent=1)
        text += REVISE.format(out=OUT)
        if stims:
            for n, p in stims.items():
                inputs[f"r6_stim/{n}"] = p
            text += REVISE_STIMS
    return Task("spec_writer", name, text, inputs=inputs, outputs=[OUT], write_scope=["out/", ".refsim/"])


def gate(spec_path: Path, top_l1: dict) -> list[str]:
    """SPEC 검사 게이트 + 2~4절 왕복 + 제목."""
    try:
        spec = load(spec_path)
    except (OSError, UnicodeDecodeError) as e:
        return [f"SPEC을 읽을 수 없음: {e}"]
    errs = []
    if spec.module != top_l1["module"]:
        errs.append(f"제목의 모듈 이름 {spec.module!r}이 {top_l1['module']!r}가 아님")
    res = spec_check(spec)
    errs += [f"[SPEC 검사] {i['code']} {i['where']}: {i['message']}" for i in res["issues"] if i["severity"] == "error"]
    try:
        l1, _ = block_top_l1(spec)
    except SpecTableError as e:
        return errs + [f"[2~4절] {e}"]
    errs += [f"[2~4절] {d}" for d in roundtrip_diff(top_l1, l1)]
    return errs


def check(top_l1: dict, prev_text: str | None = None):
    def _check(work: Path) -> list[str]:
        p = work / OUT
        errs = gate(p, top_l1)
        if prev_text is not None and p.exists() and p.read_text(encoding="utf-8") == prev_text:
            errs.append("SPEC이 바뀌지 않음 (R6 차이를 정하는 요구사항을 더해야 함)")
        return errs
    return _check
