"""rtlf: RTL 생성과 원본 RTL → IR·SPEC 변환의 CLI 진입점을 한 명령으로 묶는 실행기. 인자는 그대로 해당 모듈에 넘긴다.

    rtlf                         명령 목록
    rtlf <그룹>                  그룹의 명령 목록
    rtlf <그룹> <명령> [인자...]  실행 (도움말은 각 명령의 --help)
    rtlf <명령> [인자...]        그룹이 없는 명령 (run, approve)

`pip install -e .` 뒤에는 `rtlf`, 설치하지 않았으면 저장소 루트에서 `python3 -m pipeline`으로 부른다.
"""
from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (그룹, 명령) → (종류, 대상, 설명). 종류 "module"은 python -m, "path"는 저장소 안 스크립트 파일
COMMANDS: dict[tuple[str, str], tuple[str, str, str]] = {
    ("spec", "pdf"): ("module", "pipeline.spec.pdf", "PDF SPEC → Markdown"),
    ("spec", "waveform"): ("module", "pipeline.spec.waveform", "타이밍 다이어그램 → WaveJSON (비전 coding agent)"),
    ("spec", "regmap"): ("module", "pipeline.spec.regmap", "레지스터 맵(xlsx, CSV) 파싱과 검사"),
    ("spec", "check"): ("module", "pipeline.spec.check", "SPEC 검사 게이트 (툴 검사, 검토 항목)"),
    ("spec", "llm-check"): ("module", "pipeline.spec.llm_check", "SPEC LLM 검사 (모호한 문장, 모순, 빠진 코너 케이스)"),
    ("spec", "l1"): ("module", "pipeline.spec.l1", "SPEC 2~4절 표 → Block Top L1"),
    ("", "run"): ("module", "pipeline.scheduler.run", "파이프라인 실행 (SPEC → RTL)"),
    ("", "approve"): ("module", "pipeline.scheduler.approve", "실행이 기다리는 사람 결정 보기와 결정"),
    ("port", ""): ("module", "pipeline.scheduler.port", "이식 흐름 (원본 RTL → 이식 IR, SPEC): start, answer, resume, status"),
    ("ir", "r1"): ("module", "pipeline.importer.r1", "원본 RTL → 모듈별 L1·L0 초안 (이식 R1만 따로)"),
    ("ir", "patch"): ("module", "pipeline.patch.apply", "IR에 patch 적용 (쓰기·동결 범위 강제)"),
    ("ir", "emit"): ("module", "pipeline.emitter.sv", "IR → SystemVerilog, 소스맵"),
    ("ir", "pyequiv"): ("module", "pipeline.verify.pyequiv", "번역기 검사: lang: py 설계 모델 대 방출 RTL"),
    ("tb", "gen"): ("module", "pipeline.tb.gen", "L1 → cocotb 하니스"),
    ("tb", "report"): ("module", "pipeline.tb.report", "sim 불일치 리포트와 FSM 전이 커버리지"),
    ("verify", ""): ("module", "pipeline.verify.run", "E1 Verilator lint, E2 Verilator sim: lint, sim"),
    ("report", ""): ("module", "pipeline.feedback.reports", "외부 검사 리포트 파서 (config/report_formats)"),
    ("tool", ""): ("path", "pipeline/agents/pl.py", "에이전트 도구: validate, emit, fanin, pysim, porteq, refsim 등"),
}
GROUPS = {
    "spec": "SPEC 변환과 검사",
    "port": "원본 RTL 이식",
    "ir": "IR 도구",
    "tb": "테스트벤치",
    "verify": "Verify",
    "report": "외부 리포트",
    "tool": "에이전트 도구",
}


def _usage(group: str | None = None) -> str:
    lines = []
    if group is None:
        lines += ["사용법: rtlf <그룹> <명령> [인자...]  또는  rtlf <명령> [인자...]", ""]
    rows = []
    for (g, c), (_, _, desc) in COMMANDS.items():
        if group is not None and g != group:
            continue
        rows.append((" ".join(x for x in (g, c) if x), desc))
    w = max(len(r[0]) for r in rows)
    lines += [f"  {name.ljust(w)}  {desc}" for name, desc in rows]
    lines += ["", "각 명령의 인자는 `rtlf ... <명령> --help`로 봅니다."]
    return "\n".join(lines)


def resolve(argv: list[str]) -> tuple[tuple[str, str] | None, list[str]]:
    """argv에서 명령을 찾아 (키, 남은 인자)를 낸다. 못 찾으면 키는 None."""
    if not argv:
        return None, argv
    head = argv[0]
    if ("", head) in COMMANDS:
        return ("", head), argv[1:]
    if (head, "") in COMMANDS:
        return (head, ""), argv[1:]
    if head in GROUPS and len(argv) > 1 and (head, argv[1]) in COMMANDS:
        return (head, argv[1]), argv[2:]
    return None, argv


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_usage())
        return 0
    key, rest = resolve(argv)
    if key is None:
        group = argv[0] if argv[0] in GROUPS else None
        if group is not None and (len(argv) == 1 or argv[1] in ("-h", "--help")):
            print(_usage(group))
            return 0
        print(f"rtlf: 모르는 명령: {' '.join(argv[:2])}\n", file=sys.stderr)
        print(_usage(group), file=sys.stderr)
        return 2
    kind, target, _ = COMMANDS[key]
    sys.path.insert(0, str(ROOT))
    if key == ("tool", ""):
        os.environ.setdefault("PL_NO_LOG", "1")   # .tools.log는 에이전트의 도구 사용 측정용
    sys.argv = [target] + rest   # runpy가 argv[0]을 대상 파일 경로로 바꾸므로 사용법에는 원래 모듈 이름이 나온다
    try:
        if kind == "module":
            runpy.run_module(target, run_name="__main__", alter_sys=True)
        else:
            runpy.run_path(str(ROOT / target), run_name="__main__")
    except SystemExit as e:
        if e.code is None or isinstance(e.code, int):
            return e.code or 0
        print(e.code, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
