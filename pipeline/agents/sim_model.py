"""시뮬레이션 모델 경로: SPEC 9절이 `구현 종류: 시뮬레이션 모델`로 밝힌 설계를 IR 없이 동작 모델로 만들고 검증한다.

합성할 수 없는 시간 기반 동작(입력 없이 지연만으로 바뀌는 출력, `#` 지연, initial에 의존하는 동작)은 IR이 표현하지 못하고
Verifier 하니스도 돌릴 수 없다. 이 경로는 Author가 동작 모델을, Verifier가 자체 판정 테스트벤치를 서로 보지 않고 쓰고,
결정론적 도구(iverilog, vvp)가 판정한다. 합성과 PPA는 하지 않는다.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import pyslang

from pipeline.agents.task import Task
from pipeline.importer.sources import Design, Inputs, SourceError

PASS, FAIL = "TEST PASSED", "TEST FAILED"
SIM_TIMEOUT_S = 60

AUTHOR_TASK = """\
# 과제: {top} 시뮬레이션 모델 작성

너는 RTL 생성 파이프라인의 Author 에이전트다. 이 설계는 합성하지 않는 시뮬레이션 모델이다. `spec.md`를 따라 동작 모델을 `{out}`에 쓴다.

## 입력
- `spec.md`: SPEC. 5~7절의 동작과 9절의 구현 제약을 따른다
- `l1.json`: 모듈 이름, 파라미터, 포트. 그대로 지킨다
{repair}
## 규칙
- `initial`, `#` 지연, `always`/`forever`를 쓸 수 있다. 합성 RTL 규칙은 적용되지 않는다.
- 출력은 비블로킹 대입(`<=`)으로 바꾼다. 출력이 바뀌는 바로 그 시각에 값을 읽는 테스트벤치가 바뀌기 전 값을 보게 하기 위해서다.
- 첫 줄에 `` `timescale 1ns/1ps``를 쓴다. 모델을 테스트벤치보다 먼저 컴파일하므로 없으면 시간 단위가 테스트벤치와 달라진다.
- 시간은 파라미터로 계산하고, 파일 하나에 `{top}` 모듈 하나를 쓴다. `$finish`, 테스트벤치, 마크다운 울타리는 넣지 않는다.
- `{out}` 외의 파일은 만들거나 고치지 않는다.
"""

REPAIR_NOTE = """- `prev.sv`: 앞서 쓴 모델
- `failures.json`: 테스트벤치별 실패 로그. `prev.sv`에서 시작해 원인을 고친 모델 전체를 쓴다
"""

VERIFIER_TASK = """\
# 과제: {top} 시뮬레이션 모델의 테스트벤치 작성

너는 RTL 생성 파이프라인의 Verifier 에이전트다. 설계의 구현을 보지 않고 `spec.md`만으로 자체 판정 테스트벤치를 `{out}`에 쓴다.

## 입력
- `spec.md`: SPEC. 5~8절의 필수 요구사항을 검사한다
- `l1.json`: 모듈 이름, 파라미터, 포트

## 규칙
- 최상위 모듈 이름은 `{top}_tb`이고 `{top}`을 인스턴스한다. 첫 줄에 `` `timescale 1ns/1ps``를 쓴다.
- 출력이 바뀌는 시각과 겹치지 않는 시각에 값을 읽는다 (같은 시각에 읽으면 시뮬레이터 순서에 따라 결과가 달라진다).
- 요구사항이 정한 값과 시간을 여러 주기 또는 여러 입력에 걸쳐 확인한다. 값이 시간에 따라 바뀌어야 하면 바뀌는 것을 확인한다.
- 모든 검사가 맞으면 `$display("{passed}")`를, 틀리면 `$display("{failed}: <시각, 기대값, 실제값>")`를 내고 `$finish`로 끝낸다.
  모델이 멈춰도 끝나도록 시뮬레이션 시간 상한을 둔다. `$fatal`, 파일 입출력은 쓰지 않는다.
- `{out}` 외의 파일은 만들거나 고치지 않는다.
"""


def model_rel(top: str) -> str:
    return f"out/{top}.sv"


def tb_rel(top: str) -> str:
    return f"out/{top}_tb.sv"


def author_task(name: str, top: str, spec: Path, l1_json: str, prev: Path | None = None, failures: str | None = None) -> Task:
    inputs: dict = {"spec.md": Path(spec), "l1.json": l1_json}
    if prev is not None:
        inputs |= {"prev.sv": Path(prev), "failures.json": failures or "{}"}
    text = AUTHOR_TASK.format(top=top, out=model_rel(top), repair=REPAIR_NOTE if prev is not None else "")
    return Task("author", name, text, inputs=inputs, tools=False, outputs=[model_rel(top)], write_scope=["out/"])


def verifier_task(name: str, top: str, spec: Path, l1_json: str) -> Task:
    text = VERIFIER_TASK.format(top=top, out=tb_rel(top), passed=PASS, failed=FAIL)
    return Task("verifier", name, text, inputs={"spec.md": Path(spec), "l1.json": l1_json}, tools=False,
                outputs=[tb_rel(top)], write_scope=["out/"])


def _compile(files: list[Path], top: str, out: Path) -> str | None:
    """iverilog 컴파일. 오류 문구 또는 None."""
    if not shutil.which("iverilog") or not shutil.which("vvp"):
        return "iverilog/vvp가 설치되어 있지 않음"
    p = subprocess.run(["iverilog", "-g2012", "-s", top, "-o", str(out), *map(str, files)], capture_output=True, text=True, timeout=60)
    return None if p.returncode == 0 else (p.stdout + p.stderr)[-1500:]


def _interface(l1: dict) -> tuple[set, set]:
    ports = {(s["name"], s["dir"]) for p in l1["ports"] for s in (p["signals"] if "bundle" in p else [p])}
    return ports, {p["name"] for p in l1.get("params", [])}


def check_model(l1: dict):
    """종료 게이트: 컴파일되고, 포트와 파라미터가 L1과 같고, 지시를 어긴 구문이 없다."""
    def _check(work: Path) -> list[str]:
        top, f = l1["module"], work / model_rel(l1["module"])
        if err := _compile([f], top, work / "out" / "model.check"):
            return [f"컴파일 실패: {err}"]
        (work / "out" / "model.check").unlink(missing_ok=True)
        try:
            body = Design(Inputs([f], top)).top.body
        except SourceError as e:
            return [f"모듈을 읽을 수 없음: {e}"]
        SK = pyslang.SymbolKind
        got = {(p.name, "input" if p.direction == pyslang.ArgumentDirection.In else "output")
               for p in body.portList if p.kind == SK.Port}
        params = {m.name for m in body if m.kind == SK.Parameter and not m.isLocalParam}
        want_ports, want_params = _interface(l1)
        errs = [] if got == want_ports else [f"포트가 L1과 다름: {sorted(got ^ want_ports)}"]
        if params != want_params:
            errs.append(f"파라미터가 L1과 다름: {sorted(params ^ want_params)}")
        text = f.read_text(encoding="utf-8")
        if "`timescale" not in text:
            errs.append("첫 줄에 `timescale이 없음")
        if "$finish" in text:
            errs.append("모델에 $finish가 있음")
        return errs
    return _check


def stub(l1: dict) -> str:
    """출력을 구동하지 않는 가짜 모델."""
    params = ", ".join(f"parameter {p['name']} = {p['default']}" for p in l1.get("params", []))
    ports = ", ".join(f"{s['dir']} {s['type']} {s['name']}" for p in l1["ports"] for s in (p["signals"] if "bundle" in p else [p]))
    return f"module {l1['module']} {f'#({params}) ' if params else ''}({ports});\nendmodule\n"


def check_tb(l1: dict):
    """종료 게이트: 가짜 모델로 돌려 컴파일되고, 시간 안에 끝나고, 실패로 판정하는지 본다. 가짜 모델이 통과하면 동작을 검사하지 않는 것이다."""
    def _check(work: Path) -> list[str]:
        top, tb = l1["module"], work / tb_rel(l1["module"])
        text = tb.read_text(encoding="utf-8")
        errs = [f"테스트벤치에 없음: {w}" for w in (FAIL, "$finish", "`timescale") if w not in text]
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "stub.sv"
            fake.write_text(stub(l1), encoding="utf-8")
            r = run_tb(fake, tb, top, Path(tmp) / "b")
        if r["stage"] != "sim":
            errs.append(f"출력을 구동하지 않는 가짜 모델로 돌릴 수 없음 ({r['stage']}): {r['log']}")
        elif r["ok"]:
            errs.append("출력을 구동하지 않는 가짜 모델이 통과함. 요구사항의 값과 변화를 검사해야 함")
        return errs
    return _check


def run_tb(model: Path, tb: Path, top: str, build: Path) -> dict:
    """모델을 테스트벤치 하나로 돌린다. 통과는 PASS 문구가 있고 FAIL 문구가 없고 시간 안에 끝난 경우뿐이다."""
    build.mkdir(parents=True, exist_ok=True)
    vvp = build / "sim.vvp"
    if err := _compile([model, tb], f"{top}_tb", vvp):
        return {"ok": False, "stage": "compile", "log": err}
    try:
        p = subprocess.run(["vvp", str(vvp)], cwd=build, capture_output=True, text=True, timeout=SIM_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"ok": False, "stage": "timeout", "log": f"{SIM_TIMEOUT_S}초 안에 끝나지 않음"}
    log = p.stdout + p.stderr
    (build / "sim.log").write_text(log, encoding="utf-8")
    return {"ok": p.returncode == 0 and PASS in log and FAIL not in log, "stage": "sim", "log": log[-1500:]}
