"""Porter 에이전트 (P11 R3): 원본 RTL 모듈 하나의 동작을 IR의 L2, L3로 옮긴다. 모듈 구조는 원본과 1:1이다.

볼 수 있는 것: 확정한 이 모듈의 IR 초안(L1, L0), 원본 모듈 글자와 헤더, 이식을 마친 하위 모듈 IR, 마무리할 연결과 R1 검토 내용,
IR 안내서와 예시. SPEC은 없다 (원본이 기준).
쓸 수 있는 것: `out/<모듈>.ir.json` 하나.
도구: `pl.py validate`(Validator), `pl.py porteq`(원본 RTL과 방출 RTL의 사이클 비교, R4와 같은 판정).
종료 게이트: Validator V1~V10 통과, L1과 인스턴스(이름, 모듈, 파라미터, domain_map)와 R2 연결이 초안 그대로, 조각 언어.
고치기 과제(make_fix_task): R4가 원본과 다르다고 판정하면 첫 차이와 자극 정보를 받아 IR을 고친다.
"""
from __future__ import annotations

import json
from pathlib import Path

from pipeline.agents.author import EXAMPLES, GUIDE, LANG_NOTE, PY_EXAMPLES
from pipeline.agents.task import Task
from pipeline.ir.model import Module, frozen_l1
from pipeline.validator.checks import errors, validate

TASK = """\
# 과제: 원본 RTL 모듈 {module}을 IR로 옮기기

너는 RTL 생성 파이프라인의 Porter 에이전트다. 원본 RTL 모듈 하나의 동작을 IR(L2, L3)로 옮긴다.
IR에서 방출한 RTL이 원본과 **사이클 단위로 같은 출력**을 내야 한다. 모듈 구조는 원본과 1:1이다.

## 입력
- `draft.json`: 이 모듈의 IR 초안. L1(params, domains, ports)과 L0(instances, connections, 연결용 wire 신호)이 확정되어 있다
- `source/{module}.sv`: 원본 모듈 글자. `source/headers/`: 원본이 include하는 헤더
- `notes.json`: R1이 남긴 것. `pending`(아래), `renames`(SV 예약어라 바꾼 이름: 원본 → IR), `macro_params`(매크로나 package
  상수에서 옮긴 localparam), `reviews`(이 모듈의 검토 항목: 리셋 방식, 초기값, 지운 파라미터 등)
- `lib/`: 이식을 마친 하위 모듈 IR{children}
- `guide.md`: IR 작성 안내. 먼저 읽는다
- `examples/`: 다른 모듈의 IR 예시 (형식 참고용)

## 할 일
1. `guide.md`를 읽고, 원본 모듈을 읽는다.
2. 초안을 그대로 포함하고 L2(types, signals, 출력 포트의 kind/reset)와 L3(blocks, 필요하면 functions와 assertions)를 채운 IR 전체를 `{out}`에 쓴다.
   - 원본의 `always @(posedge ...)`는 `seq` 블록(도메인의 클럭과 리셋은 Emitter가 씌움), 조합 로직은 `comb` 블록,
     상태 머신은 `fsm` 또는 `seq`로 옮긴다. 원본의 `function`/`task`(지연 없음)는 `functions`로 옮긴다 (본문이 모듈 신호를
     읽거나 쓰면 인자로 바꿈). 원본의 concurrent assertion(`assert property`)은 `assertions`로 옮긴다.
     `always @(negedge clk ...)`는 초안의 하강 에지 도메인(`edge: neg`)의 `seq` 블록이다.
   - `generate`는 이미 고정한 파라미터로 고른 갈래만 옮긴다 (`generate for`의 로직은 조각 안 `for` 루프).
   - 한 `always`에 리셋이 있는 레지스터와 없는 레지스터가 섞여 있으면 `seq` 블록을 둘로 나눈다 (V7).
   - 리셋이 없고 초기값(`reg x = v`)만 있는 레지스터는 같은 블록의 다른 레지스터에 맞춰 리셋값 v를 준다.
   - 원본의 폭 변환은 캐스트나 슬라이스로 명시한다.
   - `notes.json`의 `pending` 연결은 원본의 연결식 그대로 동작하게 만든다: 내부 wire 신호와 `comb` 블록으로 식을 만들고
     연결은 이름 하나(`self.<신호>`)로 잇는다. 원본에서 연결하지 않은 입력은 0으로 둔다.
3. `python3 tools/pl.py validate {out} --l1 draft.json{lib_arg}`가 `"ok": true`가 될 때까지 고친다.
4. `python3 tools/pl.py porteq {out}{lib_arg} --ref ref`로 원본과 비교한다. `"status"`가 `equal`이 될 때까지 고친다.
   `reset_only`(리셋 구간 직후에만 차이)는 원본의 리셋 없는 플롭 때문이면 그대로 두어도 된다 (리셋 방식이 섞인 원본은 R1이 거부함).

## 규칙
{lang_note}- `{out}` 외의 파일은 만들거나 고치지 않는다.
- 초안의 L1, instances, connections를 바꾸지 않는다. 이름도 원본(바꾼 이름 포함) 그대로 쓴다.
- 원본에 없는 동작을 더하거나 원본 동작을 "고치지" 않는다. 원본이 이상해 보여도 그대로 옮긴다.
- 원본의 task는 IR에 쓸 수 없다 (합성 RTL 규칙). 같은 동작을 `comb` 블록에 풀어 쓰거나 값을 돌려주는 function으로 옮긴다.
  `$display` 같은 시스템 task와 PLI 호출은 옮기지 않는다 (시뮬레이션 전용).
- `generate if`는 고정한 파라미터 값으로 고른 갈래만 옮긴다. R1이 지운 파라미터(`notes.json` reviews의 unused_param, 고정 값을 적음)는
  그 고정 값으로 고른 것이다 (2026-10-22 axis_async_fifo: PAUSE_ENABLE=1인데 기본값 0의 갈래를 옮김).
"""

FIX = """\

## 원본과 다름 (R4 판정)
네가 쓴 IR(`prev.ir.json`)에서 방출한 RTL이 원본과 다른 출력을 냈다. `mismatch.json`에 첫 차이(도메인, 사이클, 포트, 원본 값,
IR 값)와 자극(모드, 시드)이 있다. `prev.ir.json`에서 시작해 고친 IR 전체를 `{out}`에 쓰고, 위의 3, 4번 확인을 다시 한다.
"""


def out_rel(module: str) -> str:
    return f"out/{module}.ir.json"


def make_task(name: str, draft: dict, source: Path, headers: list[Path], notes: dict, lib: dict[str, dict],
              ref_dir: Path, lang: str = "sv", prev: dict | None = None, mismatch: dict | None = None) -> Task:
    module = draft["module"]
    inputs: dict = {"draft.json": json.dumps(draft, ensure_ascii=False, indent=1), f"source/{module}.sv": source,
                    "notes.json": json.dumps(notes, ensure_ascii=False, indent=1), "guide.md": GUIDE, "ref": ref_dir}
    for h in headers:
        inputs[f"source/headers/{h.name}"] = h
    for e in EXAMPLES:
        inputs[f"examples/{e.name}"] = e
    for e in PY_EXAMPLES if lang == "py" else []:
        inputs[f"examples/py/{e.name}"] = e
    for cname, cir in lib.items():
        inputs[f"lib/{cname}.ir.json"] = json.dumps(cir, ensure_ascii=False, indent=1)
    out = out_rel(module)
    text = TASK.format(module=module, out=out, children=" (하위 인스턴스가 없으면 비어 있음)" if not lib else "",
                       lib_arg=" --lib lib" if lib else "", lang_note=LANG_NOTE[lang])
    if prev is not None:
        inputs["prev.ir.json"] = json.dumps(prev, ensure_ascii=False, indent=1)
        inputs["mismatch.json"] = json.dumps(mismatch or {}, ensure_ascii=False, indent=1)
        text += FIX.format(out=out)
    return Task("porter", name, text, inputs=inputs, outputs=[out], write_scope=["out/", ".porteq/"])


def frozen_l0(raw: dict) -> dict:
    """R2에서 확정한 구조: 인스턴스 전체와 연결 (Porter가 연결을 더할 수는 있지만 지우거나 바꾸지 않음)."""
    return {"instances": raw.get("instances", []), "connections": raw.get("connections", [])}


def check(draft: dict, lib: dict[str, dict], lang: str = "sv"):
    """종료 게이트: Validator, L1 동결, 인스턴스 동결, 초안 연결이 모두 남아 있음, 조각 언어."""
    def _check(work: Path) -> list[str]:
        try:
            raw = json.loads((work / out_rel(draft["module"])).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return [f"IR을 읽을 수 없음: {e}"]
        mods = {n: Module(c) for n, c in lib.items()}
        errs = [str(i) for i in errors(validate(Module(raw), mods))]
        mine, frozen = frozen_l1(raw), frozen_l1(draft)
        errs += [f"[동결] {k}: 초안 L1과 다름" for k in frozen if mine.get(k) != frozen.get(k)]
        if raw.get("instances", []) != draft.get("instances", []):
            errs.append("[동결] instances: 초안과 다름 (인스턴스 이름, 모듈, 파라미터, domain_map은 원본 그대로)")
        have = {(c["from"], c["to"]) for c in raw.get("connections", [])}
        lost = [c["id"] for c in draft.get("connections", []) if (c["from"], c["to"]) not in have]
        if lost:
            errs.append(f"[동결] 초안의 연결이 빠지거나 바뀜: {', '.join(lost[:10])}")
        if raw.get("lang", "sv") != lang:
            errs.append(f'[lang] 조각 언어는 "{lang}"임 (IR의 lang: "{raw.get("lang", "sv")}")')
        return errs
    return _check


def make_ref_dir(r1: dict, dest: Path) -> Path:
    """에이전트 작업 디렉터리에 둘 원본 묶음 (`pl.py porteq`가 읽음): 원본 파일과 상대 경로로 쓴 r1 요약."""
    import shutil

    dest.mkdir(parents=True, exist_ok=True)
    src = dest / "src"
    src.mkdir(exist_ok=True)
    units, headers, wrapped = [], [], []
    for k, u in enumerate(r1["units"]):
        name = f"{k:02d}_{Path(u).name}"
        shutil.copy(r1.get("sim_units", {}).get(u, u), src / name)   # 시뮬레이션 사본이 있으면 그것
        units.append(f"src/{name}")
        if u in r1.get("wrapped", []):
            wrapped.append(f"src/{name}")
    for h in r1.get("headers", []):
        shutil.copy(h, src / Path(h).name)
        headers.append(f"src/{Path(h).name}")
    summary = {"inputs": {**r1["inputs"], "incdirs": [], "files": []}, "units": units, "headers": headers, "wrapped": wrapped,
               "modules": {n: {k: m[k] for k in ("renames", "removed_params") if k in m} for n, m in r1["modules"].items()},
               "top": r1["top"],
               "order": r1["order"]}
    (dest / "ref.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return dest
