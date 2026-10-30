"""외부 TB 보정: 외부 테스트벤치 결과를 근거로 SPEC을 고친다. 고친 SPEC으로 파이프라인을 다시 돌리는 것은 runner가 한다.

외부 테스트벤치가 설계의 최종 기준이다. 두 경우에 SPEC을 고친다.
- external_fail: 모든 후보가 외부 TB에서 실패함. 외부 TB가 기대하는 동작을 SPEC이 정하지 않았거나 다르게 정했다.
- internal_mismatch: 외부 TB를 통과한 후보가 있는데 내부 검증(Verifier 테스트, Arbiter 판정)이 그 후보를 통과시키지 못함.
  내부 테스트가 외부 기준과 다르게 SPEC을 읽었다.
고친 SPEC은 파이프라인이 RTL과 내부 테스트를 처음부터 다시 만드는 기준이 되므로, 내부 검증과의 불일치도 함께 풀린다.
바꾼 요구사항마다 수정 사유(`changes.json`)와 10절 `[SPEC 재검토 알람]`을 남긴다.
정답 RTL(reference_rtl)은 주지 않는다. 외부 TB와 그 데이터 파일, 후보 RTL, 채점 로그만 준다.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from experiments.bench.common.schema import BenchItem
from pipeline.agents import roles as roles_mod, spec_writer
from pipeline.agents.task import RUNS, Task, TaskResult, run_task
from pipeline.spec.parse import load as load_spec

OUT = "out/spec.md"
CHANGES = "out/changes.json"
ALARM = "[SPEC 재검토 알람]"

SITUATION = {
    "external_fail": "모든 후보가 외부 테스트벤치에서 실패했다.",
    "internal_mismatch": "외부 테스트벤치를 통과한 후보가 있지만, 파이프라인 내부 검증(Verifier 테스트, Arbiter 판정)이 그 후보를 "
                         "통과시키지 못했다. 내부 테스트가 외부 기준과 다르게 SPEC을 읽었다. `internal.json`에 내부 검증의 검토 "
                         "항목과 외부 테스트벤치를 통과한 후보가 있다.",
}

TASK = """\
# 과제: 외부 테스트벤치 결과로 SPEC 고치기 ({top})

너는 RTL 생성 파이프라인의 SpecWriter 에이전트다. `spec.md`만 보고 만든 RTL 후보들을 이 설계의 외부 테스트벤치로 채점했다.
{situation}
외부 테스트벤치가 이 설계의 최종 기준이다. 외부 테스트벤치가 기대하는 동작을 SPEC이 하나로 정하도록 고친 SPEC 전체를 `{out}`에 쓴다.
고친 SPEC으로 파이프라인이 RTL과 내부 테스트를 처음부터 다시 만든다.

## 입력
- `spec.md`: 지금 SPEC
- `prompt.txt`: 원본 자연어 명세. `header.sv`: 모듈 헤더. `tables.md`: 2~4절 표
- `template.md`: SPEC 작성 템플릿 (형식 규칙)
- `tb/`: 외부 테스트벤치와 그 데이터 파일 (테스트벤치가 읽는 기대값 파일 포함)
- `candidates/c<k>/rtl/`: 후보 RTL. `candidates/c<k>/result.json`: 외부 채점 결과 (`ok`, 실패 단계, 로그 끝).
  `candidates/c<k>/compile.log`, `sim.log`: 컴파일과 시뮬레이션 로그 (있을 때만)
- `internal.json`: 파이프라인 결과 상태와 내부 검증의 검토 항목(`review`: 코드와 메시지), 외부 테스트벤치를 통과한 후보

## 할 일
1. 외부 테스트벤치가 어떤 입력을 언제 넣고, 어떤 출력을 언제 기대하는지 읽는다. 기대값 파일과 비교하면 그 값도 본다.
   실패한 후보는 로그의 첫 불일치(시각이나 사이클, 기대값, 실제값)부터 본다.
2. 기대 동작과 후보 동작의 차이를 SPEC 문장과 대조해 원인을 정한다: SPEC이 정하지 않음, 두 가지로 읽힘, 외부 기준과 다르게 정함,
   내부 테스트가 SPEC을 외부 기준과 다르게 읽음 중 무엇인지.
3. `spec.md`에서 시작해 해당 요구사항, 타이밍, 예외, 시나리오를 외부 테스트벤치가 기대하는 동작으로 고치거나 더한다.
   외부 테스트벤치가 표준 관행과 다른 동작(지연 사이클 수, 동기화 단 수, 토글 위상, 리셋 직후 값 등)을 기대해도 그대로 따른다.
   내부 테스트가 외부 기준과 다르게 읽은 문장은 한 가지로만 읽히게 고친다.
4. 10절에 바꾼 요구사항마다 `- Q-xx {alarm} 외부 테스트벤치 근거로 <요구사항 ID>를 바꿈: <무엇을 어떻게>. 근거: <파일과 시각, 기대값>`을
   더한다 (번호는 기존 질문 다음부터). 표준과 다른 동작이나 테스트벤치 결함으로 보이는 동작을 따랐으면 그 판단도 적는다.
5. `{changes}`에 바꾼 내용과 수정 사유를 JSON 배열로 쓴다. 항목 하나는 아래 형식이다.
   {{"reqs": ["바꾸거나 더한 요구사항 ID"], "before": "이전 문장 (새로 더했으면 빈 문자열)", "after": "새 문장",
    "evidence": "외부 테스트벤치의 근거 (파일, 시각이나 사이클, 기대값)", "reason": "수정 사유 (2번의 원인 가운데 무엇이고 왜 이렇게 고치는지)"}}
6. `python3 tools/pl.py spec_gate {out} --tables tables.md --top {top}`이 `"ok": true`가 될 때까지 고친다.

## 규칙
- `{out}`, `{changes}` 외의 파일은 만들거나 고치지 않는다.
- 2~4절은 `tables.md` 그대로 둔다. 포트, 파라미터, 클럭과 리셋은 바꾸지 않는다.
- 요구사항은 포트에서 관찰되는 값과 사이클로 쓴다. 후보 RTL의 코드 구조를 옮기지 않는다. 내부 구조는 `prompt.txt`가 명시한 것만 쓴다.
- 외부 테스트벤치와 관계없는 요구사항은 바꾸지 않는다.
"""


def make_task(item: BenchItem, trigger: str, spec_path: Path, tables: str, header_text: str,
              tb_dir: Path, candidates: dict[str, dict[str, Any]], internal: dict[str, Any]) -> Task:
    """candidates: {"c0": {"rtl": [Path], "result": dict, "logs": [Path]}}."""
    inputs: dict[str, str | Path] = {
        "spec.md": Path(spec_path),
        "prompt.txt": item.prompt_text,
        "header.sv": header_text,
        "tables.md": tables,
        "template.md": spec_writer.TEMPLATE,
        "tb": Path(tb_dir),
        "internal.json": json.dumps(internal, ensure_ascii=False, indent=1),
    }
    for ck, c in candidates.items():
        for f in c["rtl"]:
            inputs[f"candidates/{ck}/rtl/{Path(f).name}"] = Path(f)
        inputs[f"candidates/{ck}/result.json"] = json.dumps(c["result"], ensure_ascii=False, indent=1)
        for f in c["logs"]:
            inputs[f"candidates/{ck}/{Path(f).name}"] = Path(f)
    text = TASK.format(top=item.top, situation=SITUATION[trigger], out=OUT, changes=CHANGES, alarm=ALARM)
    return Task("spec_writer", f"bench_extfix/{item.id}", text, inputs=inputs, outputs=[OUT, CHANGES], write_scope=["out/"])


def check(top_l1: dict, prev_text: str):
    """SPEC 게이트, SPEC이 바뀌었는지, 수정 사유(changes.json)와 재검토 알람이 있는지."""
    spec_check = spec_writer.check(top_l1, prev_text)

    def _check(work: Path) -> list[str]:
        errs = spec_check(work)
        try:
            changes = json.loads((work / CHANGES).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return errs + [f"수정 사유를 읽을 수 없음 ({CHANGES}): {e}"]
        if not isinstance(changes, list) or not changes:
            return errs + [f"{CHANGES}는 바꾼 내용이 하나 이상인 JSON 배열이어야 함"]
        try:
            spec = load_spec(work / OUT)
        except (OSError, UnicodeDecodeError):
            return errs
        known = {r.id for r in spec.requirements}
        for i, c in enumerate(changes):
            if not isinstance(c, dict):
                errs.append(f"{CHANGES}[{i}]가 객체가 아님")
                continue
            if not c.get("reqs"):
                errs.append(f"{CHANGES}[{i}]: 바꾼 요구사항 ID(reqs)가 없음")
            errs += [f"{CHANGES}[{i}]: 고친 SPEC에 없는 요구사항 {r}" for r in c.get("reqs") or [] if r not in known]
            for k in ("after", "evidence", "reason"):
                if not str(c.get(k, "")).strip():
                    errs.append(f"{CHANGES}[{i}]: {k}가 비어 있음")
        if not any(ALARM in q["text"] for q in spec.questions):
            errs.append(f"10절에 `- Q-xx {ALARM} ...` 항목이 없음 (바꾼 요구사항마다 하나)")
        return errs
    return _check


def revise_spec(item: BenchItem, work_dir: Path, trigger: str, spec_path: Path, top_l1: dict, tables: str, header_text: str,
                tb_dir: Path, candidates: dict[str, dict[str, Any]], internal: dict[str, Any], roles: roles_mod.Roles,
                isolate: bool = True) -> tuple[Path | None, list[dict], list[str], TaskResult]:
    """SPEC을 고쳐 work_dir/spec.md에 쓴다. 돌려주는 것: (고친 SPEC 또는 None, 수정 사유, 오류, 실행 결과)."""
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    task = make_task(item, trigger, spec_path, tables, header_text, tb_dir, candidates, internal)
    task.isolate = isolate
    cfg = roles.agents.get("spec_writer") or roles.agents.get("verifier")
    if isolate:
        key = hashlib.sha256(str(work_dir).encode()).hexdigest()[:8]
        agent_work, run_root = RUNS / f"bench-extfix-{item.id}-{key}", RUNS
    else:
        agent_work, run_root = work_dir / "agent_work", None
    prev = Path(spec_path).read_text(encoding="utf-8")
    res = run_task(cfg, task, agent_work, check=check(top_l1, prev), retries=roles.limits.get("agent_retries", 2), run_root=run_root)
    if not res.ok:
        return None, [], res.errors, res
    out = work_dir / "spec.md"
    out.write_text(Path(res.outputs[OUT]).read_text(encoding="utf-8"), encoding="utf-8")
    changes = json.loads(Path(res.outputs[CHANGES]).read_text(encoding="utf-8"))
    (work_dir / "changes.json").write_text(json.dumps(changes, ensure_ascii=False, indent=1), encoding="utf-8")
    errs = spec_writer.gate(out, top_l1)
    return (out if not errs else None), changes, errs, res


def tb_bundle(item: BenchItem, dst: Path, prepare) -> Path:
    """외부 TB와 데이터 파일을 dst에 모은다 (채점에 쓴 것과 같은 보정을 거침). 정답 RTL은 넣지 않는다."""
    dst = Path(dst)
    dst.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        prepare(item, [Path(p).resolve() for p in item.eval.tb_files], Path(tmp))
        ref = {Path(p).resolve().name for p in item.reference_rtl}
        for f in Path(tmp).iterdir():
            if f.is_file() and f.name not in ref:
                (dst / f.name).write_bytes(f.read_bytes())
    return dst
