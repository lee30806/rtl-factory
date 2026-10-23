"""SPEC 검사 게이트의 LLM 검사. coding agent가 모호한 문장, 모순, 빠진 코너 케이스를 질문으로 낸다.

에이전트는 작업 디렉터리의 `spec.md`와 TASK.md만 보고 `out/questions.json`을 쓴다. 툴은 결과의 형식, 인용한
요구사항 ID, 쓰기 범위를 확인하고 질문을 검토 항목으로 바꾼다. 답하지 않고 넘긴 질문은 "해석이 갈린 지점"으로
최종 승인의 검토 항목에 남는다.

    python3 -m pipeline.spec.llm_check <spec.md> --agents <agents.json> --agent <이름> -o <dir>
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from pipeline.agents.runner import AgentConfig, load_agents, run_agent, scope_violations, snapshot
from pipeline.spec.issues import SpecIssue
from pipeline.spec.parse import Spec, load

KINDS = {"ambiguous": "모호한 문장", "contradiction": "서로 모순되는 요구사항", "missing_corner": "빠진 코너 케이스"}

TASK = """\
# 과제: SPEC 검토 질문 작성

`spec.md`는 하드웨어 블록의 SPEC이다. RTL을 쓰는 사람과 검증하는 사람이 같은 SPEC을 다르게 읽을 수 있는 곳을 찾아
작성자에게 보낼 질문으로 만든다.

## 찾을 것
- ambiguous: 둘 이상으로 해석되는 요구사항 문장 (수치, 조건, 순서가 빠짐)
- contradiction: 서로 맞지 않는 요구사항
- missing_corner: 정의되지 않았고 "미정의"로도 표시되지 않은 코너 케이스

## 결과
`out/questions.json`에 아래 형태의 JSON 하나를 쓴다. 질문은 {max_items}개 이하로, 중요한 것부터 쓴다.

{{"items": [{{"kind": "ambiguous|contradiction|missing_corner", "refs": ["관련 요구사항 ID"], "question": "작성자에게 묻는 문장", "why": "그렇게 본 이유"}}]}}

- refs에는 spec.md에 실제로 있는 요구사항 ID만 쓴다 (예: FR-BUS-01, TR-07, EX-02). 관련 ID가 없으면 빈 목록.
- 10번 섹션(열린 질문)에 이미 있는 것은 다시 쓰지 않는다.
- `out/questions.json` 외의 파일은 만들거나 고치지 않는다.
"""


def validate(raw: object, spec: Spec, fname: str) -> tuple[list[SpecIssue], list[str]]:
    """(검토 항목, 형식 문제)."""
    problems: list[str] = []
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        return [], ["최상위가 {\"items\": [...]} 형태가 아님"]
    known = {r.id: r for r in spec.requirements}
    out = []
    for n, it in enumerate(raw["items"]):
        if not isinstance(it, dict) or it.get("kind") not in KINDS or not str(it.get("question", "")).strip():
            problems.append(f"항목 {n}: kind({sorted(KINDS)})나 question이 없음")
            continue
        refs = [r for r in it.get("refs", []) if isinstance(r, str)]
        unknown = [r for r in refs if r not in known]
        if unknown:
            problems.append(f"항목 {n}: SPEC에 없는 요구사항 인용 {unknown}")
        line = next((known[r].line for r in refs if r in known), None)
        msg = f"[{KINDS[it['kind']]}] {it['question'].strip()}"
        if refs:
            msg += f" (관련: {', '.join(refs)})"
        if it.get("why"):
            msg += f" — 근거: {str(it['why']).strip()}"
        out.append(SpecIssue(f"LLM_{it['kind'].upper()}", "question", f"{fname}:{line}" if line else fname, msg, "llm"))
    return out, problems


def run(spec_path: Path, cfg: AgentConfig, work: Path, max_items: int = 15) -> dict:
    spec = load(spec_path)
    if work.exists():
        shutil.rmtree(work)
    (work / "out").mkdir(parents=True)
    shutil.copy(spec_path, work / "spec.md")
    before = snapshot(work, ("out/",))
    r = run_agent(cfg, work, TASK.format(max_items=max_items))
    scope = scope_violations(before, snapshot(work, ("out/", "TASK.md")))
    res = {"agent": cfg.name, "model": cfg.model, "duration_s": round(r.duration_s, 1), "exit_ok": r.exit_ok,
           "scope_violations": scope, "problems": [], "review": []}
    out = work / "out" / "questions.json"
    if not out.exists():
        res["problems"].append("결과 파일 out/questions.json이 없음")
        return res
    try:
        raw = json.loads(out.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        res["problems"].append(f"JSON 파싱 실패: {e}")
        return res
    items, problems = validate(raw, spec, spec_path.name)
    res["problems"] += problems + [f"쓰기 범위 밖 변경: {s}" for s in scope]
    res["review"] = [i.to_dict() for i in items]
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SPEC LLM 검사 (coding agent)")
    ap.add_argument("spec")
    ap.add_argument("--agents", required=True)
    ap.add_argument("--agent", required=True)
    ap.add_argument("--max-items", type=int, default=15)
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args(argv)
    cfg = next(a for a in load_agents(args.agents) if a.name == args.agent)
    out = Path(args.out)
    res = run(Path(args.spec), cfg, out / "work", args.max_items)
    (out / "llm_review.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{res['agent']} {res['duration_s']}초: 질문 {len(res['review'])}개, 문제 {res['problems'] or '없음'}")
    for i in res["review"]:
        print(f"  {i['where']}: {i['message'][:150]}")
    return 0 if not res["problems"] else 1


if __name__ == "__main__":
    sys.exit(main())
