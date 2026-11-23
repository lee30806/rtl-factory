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
import re
import tempfile
from pathlib import Path
from typing import Any

from experiments.bench.common.schema import BenchItem
from experiments.bench.common.to_spec import with_prompt_appendix
from pipeline.agents import roles as roles_mod, spec_writer
from pipeline.agents.task import RUNS, Task, TaskResult, run_task
from pipeline.spec.parse import load as load_spec

OUT = "out/spec.md"
CHANGES = "out/changes.json"
ALARM = "[SPEC 재검토 알람]"

_HOST_DATASET_PATH_RE = re.compile(
    r"/(?:[^\s\"'`()\[\]{}:;,]*/)*(?:datasets|ChipVerilog|Des|RealBench|RTLLM|cvdp_datasets)(?:/[^\s\"'`()\[\]{}:;,]*)*"
)


def sanitize_log_text(text: str, item: BenchItem | None = None) -> str:
    """로그 텍스트에서 정답 RTL(reference_rtl) 및 호스트 데이터셋 경로를 <REDACTED_REF>로 마스킹하여 유출을 차단한다."""
    if not text:
        return text
    if item is not None:
        for ref_p in (item.reference_rtl or []):
            candidates = {str(ref_p), str(Path(ref_p).resolve()), str(Path(ref_p).parent), str(Path(ref_p).resolve().parent)}
            for p_str in sorted(candidates, key=len, reverse=True):
                if p_str and p_str != ".":
                    text = text.replace(p_str, "<REDACTED_REF>")
    return _HOST_DATASET_PATH_RE.sub("<REDACTED_REF>", text)


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

## 핵심 원칙 (완전 격리 & SPEC-First & 외부 탐색/웹 검색 절대 금지)
1. **작업 디렉터리 외부 파일 시스템 접근 및 정답 RTL 탐색 절대 금지 (Zero-Leakage Isolation)**:
   - 현재 작업 디렉터리(`.`) 내부에 제공된 로컬 파일(`spec.md`, `prompt.txt`, `header.sv`, `tables.md`, `template.md`, `tb/`, `candidates/`, `internal.json`) 외에 상위 디렉터리(`..`)나 호스트 파일 시스템(예: `/Users/...`, `build/datasets/...`, 원본 정답 RTL 파일 등)을 탐색하거나 읽는 행위는 엄격히 금지된다.
   - 오직 작업 디렉터리에 제공된 외부 테스트벤치(`tb/`)와 반례/시뮬레이션 로그(`candidates/c<k>/counterexample.log`, `mismatch_trace.log`, `sim.log`, `result.json`)의 입출력 데이터만을 근거로 기대 동작을 연역적으로 추론하라.
2. **웹 검색 및 외부 역공학 절대 금지 (NO WebSearch / NO WebFetch)**:
   - `search_web`, `WebSearch`, `WebFetch` 등 웹 검색이나 외부 네트워크 접근 도구를 절대로 호출하지 마라.
3. **스펙 요구사항 최우선 원칙 (SPEC-First vs Schema/Lint Constraints)**:
   - **SPEC 요구사항과 IR 스키마 또는 정적 린트 규칙 간에 충돌이 발생할 경우, 항상 SPEC 요구사항을 최우선으로 명세한다.**
   - 하위 도구나 IR 스키마의 제약을 의식하여 외부 테스트벤치나 반례가 요구하는 동적 리셋(`except_prefix` 분기 등)이나 특수 제어 동작을 임의로 상수로 축소하거나 생략하지 마라.

## 입력
- `spec.md`: 지금 SPEC
- `prompt.txt`: 원본 자연어 명세. `header.sv`: 모듈 헤더. `tables.md`: 2~4절 표
- `template.md`: SPEC 작성 템플릿 (형식 규칙)
- `tb/`: 외부 테스트벤치와 그 데이터 파일 (테스트벤치가 읽는 기대값 파일 포함)
- `candidates/c<k>/rtl/`: 후보 RTL. `candidates/c<k>/result.json`: 외부 채점 결과 (`ok`, 실패 단계, 로그 끝).
  `candidates/c<k>/compile.log`, `sim.log`: 컴파일과 시뮬레이션 로그 (있을 때만)
  `candidates/c<k>/mismatch_trace.log`: 신호별 첫 불일치 사이클과 그 전후 사이클의 입력, 기대값(정답 출력), 실제값 (있을 때만)
  `candidates/c<k>/counterexample.log`: 테스트벤치 없이 정답과의 등가로 채점하는 문제의 반례. 같은 무작위 입력을 정답과 후보에 넣어
  처음 다른 사이클과 그 전후의 입력, 기대값, 실제값 (있을 때만). 이때 `tb/`는 비어 있고 이 반례가 외부 기준의 근거다
- `internal.json`: 파이프라인 결과 상태와 내부 검증의 검토 항목(`review`: 코드와 메시지), 외부 테스트벤치를 통과한 후보

## 할 일
1. 외부 테스트벤치가 어떤 입력을 언제 넣고, 어떤 출력을 언제 기대하는지 읽는다. 기대값 파일과 비교하면 그 값도 본다.
   실패한 후보는 로그의 첫 불일치(시각이나 사이클, 기대값, 실제값)부터 본다. `mismatch_trace.log`나 `counterexample.log`가 있으면
   그 전후 사이클의 입력 순서로 기대 동작을 추론한다 (개수만 보고 추정하지 않는다).
2. 기대 동작과 후보 동작의 차이를 SPEC 문장과 대조해 원인을 정한다: SPEC이 정하지 않음, 두 가지로 읽힘, 외부 기준과 다르게 정함,
   내부 테스트가 SPEC을 외부 기준과 다르게 읽음 중 무엇인지.
3. `spec.md`에서 시작해 해당 요구사항, 타이밍, 예외, 시나리오를 외부 테스트벤치가 기대하는 동작으로 고치거나 더한다.
   외부 테스트벤치가 표준 관행과 다른 동작(지연 사이클 수, 동기화 단 수, 토글 위상, 리셋 직후 값 등)을 기대해도 그대로 따른다.
   내부 테스트가 외부 기준과 다르게 읽은 문장은 한 가지로만 읽히게 고친다.
   - **미세 타이밍 및 레이턴시 추론 규칙:** 외부 테스트벤치의 입력 구동 시점(t_drive)과 출력 검사 시점(t_check), 그리고 그 사이의 클럭 에지(t_clk)를 정밀하게 분석한다:
     - **동일 사이클/즉시 등록 검사:** 입력 구동 후 첫 번째 클럭 에지(T_sample) 직후(예: 다음 하강 에지 `FallingEdge` 또는 T_sample + eps)에서 출력을 검사하면, 설계는 해당 클럭 에지 T_sample에서 즉시 출력을 1로 등록(Registered)해야 한다.
     - **지연 사이클 계산:** t_check가 T_sample로부터 K번째 클럭 상승 에지 이후에 발생하면, 파이프라인 지연은 정확히 K사이클이다.
     - **1사이클 지연 왜곡 방지:** "다음 사이클"이라는 모호한 표현을 쓰지 말고, "입력 천이를 처음 샘플링한 클럭 에지 Tn에서 등록되어 Tn+1까지 1사이클 동안 유지 (추가 2단 플립플롭 지연 없음)"와 같이 클럭 에지와 레지스터 단 수를 구체적으로 기술한다.
4. 10절에 바꾼 요구사항마다 `- Q-xx {alarm} 외부 테스트벤치 근거로 <요구사항 ID>를 바꿈: <무엇을 어떻게>. 근거: <파일과 시각, 기대값>`을
   더한다 (번호는 기존 질문 다음부터). 표준과 다른 동작이나 테스트벤치 결함으로 보이는 동작을 따랐으면 그 판단도 적는다.
5. `{changes}`에 바꾼 내용과 수정 사유를 JSON 배열로 쓴다. 항목 하나는 아래 형식이다.
   {{"reqs": ["바꾸거나 더한 요구사항 ID"], "before": "이전 문장 (새로 더했으면 빈 문자열)", "after": "새 문장",
    "evidence": "외부 테스트벤치의 근거 (파일, 시각이나 사이클, 기대값)", "reason": "수정 사유 (2번의 원인 가운데 무엇이고 왜 이렇게 고치는지)"}}
6. `python3 tools/pl.py spec_gate {out} --tables tables.md --top {top}`이 `"ok": true`가 될 때까지 고친다.

## 규칙
- `{out}`, `{changes}` 외의 파일은 만들거나 고치지 않는다.
- 3절의 포트(이름·방향·타입)와 4절의 파라미터는 `tables.md` 그대로 둔다. 단, 외부 테스트벤치나 `prompt.txt`가 2절의 리셋 동기(`동기`/`비동기`)나 리셋 극성(`high`/`low`)을 다르게 기대하는 경우에는 2절 표의 리셋 동기·극성도 함께 고친다.
- 요구사항은 포트에서 관찰되는 값과 사이클로 쓴다. 후보 RTL의 코드 구조를 옮기지 않는다. 내부 구조는 `prompt.txt`가 명시한 것만 쓴다.
- 미세 타이밍(에지 검출, 펄스, 플래그 유효 구간): 외부 테스트벤치가 특정 클럭 에지 직후(하강 에지 등)에 검사하는 펄스는 입력 천이를 샘플링한 그 클럭 에지부터 1사이클 동안 유지되는 동기 등록 출력으로 기술하며, 불필요한 2단 쉬프트 레지스터 지연이나 글리치를 유발하는 조합 출력을 요구사항에 넣지 않는다.
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
        res_json = json.dumps(c["result"], ensure_ascii=False, indent=1)
        inputs[f"candidates/{ck}/result.json"] = sanitize_log_text(res_json, item)
        for f in c["logs"]:
            p = Path(f)
            if p.name == "equivalence.log":
                continue
            log_txt = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
            inputs[f"candidates/{ck}/{p.name}"] = sanitize_log_text(log_txt, item)
    text = TASK.format(top=item.top, situation=SITUATION[trigger], out=OUT, changes=CHANGES, alarm=ALARM)
    return Task("spec_writer", f"bench_extfix/{item.id}", text, inputs=inputs, outputs=[OUT, CHANGES], write_scope=["out/"])


def check(top_l1: dict, prev_text: str, item: BenchItem | None = None):
    """SPEC 게이트, SPEC이 바뀌었는지, 수정 사유(changes.json)와 재검토 알람이 있는지, 호스트 데이터셋 경로 유출이 없는지."""
    spec_check = spec_writer.check(top_l1, prev_text, allow_domain_refine=True)

    def _check(work: Path) -> list[str]:
        errs = spec_check(work)
        try:
            changes_raw = (work / CHANGES).read_text(encoding="utf-8")
            changes = json.loads(changes_raw)
        except (OSError, json.JSONDecodeError) as e:
            return errs + [f"수정 사유를 읽을 수 없음 ({CHANGES}): {e}"]
        if not isinstance(changes, list) or not changes:
            return errs + [f"{CHANGES}는 바꾼 내용이 하나 이상인 JSON 배열이어야 함"]
        try:
            spec_raw = (work / OUT).read_text(encoding="utf-8")
            spec = load_spec(work / OUT)
        except (OSError, UnicodeDecodeError):
            return errs
        for doc_name, doc_txt in ((OUT, spec_raw), (CHANGES, changes_raw)):
            if _HOST_DATASET_PATH_RE.search(doc_txt):
                errs.append(f"격리 위반 (Zero-Leakage): {doc_name}에 호스트 데이터셋 또는 정답 RTL 경로 참조가 포함됨")
            elif item is not None:
                for ref_p in (item.reference_rtl or []):
                    if str(ref_p) in doc_txt or str(Path(ref_p).resolve()) in doc_txt:
                        errs.append(f"격리 위반 (Zero-Leakage): {doc_name}에 정답 RTL 절대 경로 참조가 포함됨")
                        break
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
    res = run_task(cfg, task, agent_work, check=check(top_l1, prev, item=item), retries=roles.limits.get("agent_retries", 2), run_root=run_root)
    if not res.ok:
        return None, [], res.errors, res
    out = work_dir / "spec.md"
    out.write_text(with_prompt_appendix(Path(res.outputs[OUT]).read_text(encoding="utf-8"), item.prompt_text), encoding="utf-8")
    changes = json.loads(Path(res.outputs[CHANGES]).read_text(encoding="utf-8"))
    (work_dir / "changes.json").write_text(json.dumps(changes, ensure_ascii=False, indent=1), encoding="utf-8")
    errs = spec_writer.gate(out, top_l1, allow_domain_refine=True)
    if not errs:
        from pipeline.importer import spec_tables
        from pipeline.spec.l1 import block_top_l1
        final_l1, _ = block_top_l1(load_spec(out))
        (work_dir / "top.l1.json").write_text(json.dumps(final_l1, ensure_ascii=False, indent=2), encoding="utf-8")
        (work_dir / "tables.md").write_text(spec_tables.tables_md(final_l1), encoding="utf-8")
    return (out if not errs else None), changes, errs, res


_TB_STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
_TB_FAIL_TOKEN = re.compile(r"fail|error|mismatch|wrong|incorrect", re.I)


def tb_is_self_checking(text: str) -> bool:
    """공식 도구(`formal_equivalence.tb_is_self_checking`)와 같은 규칙: 주석을 뺀 문자열 상수 중 하나라도 실패 판정을 말할 수 있으면 자체 검사 TB.
    출력만 하는 TB는 공식 채점이 판정에 쓰지 않고 등가 검사로 넘어가므로, 그 TB의 결과는 Repair·SPEC 보정의 근거가 되지 못한다."""
    text = re.sub(r"//.*", "", re.sub(r"/\*.*?\*/", "", text, flags=re.S))
    return any(_TB_FAIL_TOKEN.search(s) for s in _TB_STRING.findall(text))


def chipverilog_tb_files(item: BenchItem) -> list[Path]:
    """ChipVerilog 공식 채점이 판정에 쓰는 자체 검사 TB (정답 디렉터리의 *TB*.v / *tb*.v). 정답 RTL과 출력만 하는 TB는 제외.
    없으면 등가 채점만 하는 문제."""
    if item.eval.kind != "chipverilog" or not item.reference_rtl:
        return []
    des = Path(item.reference_rtl[0]).parent
    ref = {Path(p).resolve().name for p in item.reference_rtl}
    return sorted(f for f in des.glob("*.v") if f.name not in ref and re.search(r"tb|test", f.name, re.I)
                  and tb_is_self_checking(f.read_text(encoding="utf-8", errors="replace")))


def tb_bundle(item: BenchItem, dst: Path, prepare) -> Path:
    """외부 TB와 데이터 파일을 dst에 모은다 (채점에 쓴 것과 같은 보정을 거침). 정답 RTL은 넣지 않는다."""
    dst = Path(dst)
    dst.mkdir(parents=True, exist_ok=True)
    if item.eval.kind == "chipverilog":
        for f in chipverilog_tb_files(item):
            (dst / f.name).write_bytes(f.read_bytes())
        return dst
    with tempfile.TemporaryDirectory() as tmp:
        prepare(item, [Path(p).resolve() for p in item.eval.tb_files], Path(tmp))
        ref = {Path(p).resolve().name for p in item.reference_rtl}
        for f in Path(tmp).iterdir():
            if f.is_file() and f.name not in ref:
                (dst / f.name).write_bytes(f.read_bytes())
    return dst
