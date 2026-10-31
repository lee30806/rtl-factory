"""SPEC 검사 게이트의 툴 검사. 결과는 검토 항목(review)으로 모은다.

검사
- 필수 섹션 10개와 제목, 순서
- 2~4번 섹션 표의 파싱과, 표에서 만든 Block Top L1의 Validator V1~V3
- 요구사항: ID 중복, 의무 수준 표시(필수/권장/허용/미정의), 인용한 ID가 있는지, 한 문장에 동작 하나(경고)
- 포트: 출력 포트마다 그 포트를 다루는 요구사항이 있는지(오류), 입력 포트가 요구사항에서 참조되는지(경고)
- 번들의 프로토콜 태그가 표준 목록에 있는지(경고)
- 예시 시나리오: 정상 하나와 코너 하나 이상, 시나리오마다 입력과 기대 출력이 있는 표
- 리셋 문장(경고): 동기 리셋 도메인에서 "리셋이 1인 동안/사이클에 출력이 ~"처럼 리셋이 걸린 그 사이클을 요구하는 문장
  (P8 CRC-E01, P8a COBS-E01·AFIFO-E02: 레지스터 출력은 리셋을 본 다음 사이클에야 바뀜). 리셋 도메인이 둘 이상(다른
  클럭)인데 모든 리셋을 함께 거는 사용 조건 문장이 없음 (P11 axis_async_fifo: 한쪽 리셋 동작은 두 클럭 위상에 따라 달라짐)
- 리셋 없는 도메인(오류): 그 도메인의 출력에 초기값을 정한 문장 (RTLLM right_shifter: IR에 initial이 없어 만족할 수 없음).
  시뮬레이션 모델은 IR을 쓰지 않으므로 예외
- 구현 종류(오류): 입력 포트가 없는 블록은 9절에 `구현 종류: 합성` 또는 `구현 종류: 시뮬레이션 모델`을 밝힘.
  입력이 없으면 합성 회로는 상수만 낼 수 있어 시간에 따라 바뀌는 출력은 시뮬레이션 모델이다
- 레지스터 맵을 주면 레지스터 검사와 SPEC 대조 (`regmap.py`)
검토 항목으로 올리는 것: 미정의 동작(7번 섹션의 "미정의"), 열린 질문(10번 섹션), 위 검사의 경고,
LLM 검사의 질문(`--llm`, llm_check.py)

    python3 -m pipeline.spec.check <spec.md> [--regmap registers.csv] [--array DCT=DAT_DEPTH] -o <dir>
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from pipeline.spec.issues import SpecIssue, errors
from pipeline.spec.parse import TEMPLATE, Spec, load

PROTOCOLS = {"valid_ready"}   # 임시 표준 (experiments/p0/substitutes.md). 사내 목록을 받으면 바꿈
LEVELS = {"필수", "권장", "허용", "미정의"}
KIND_RE = re.compile(r"^\s*-\s*구현 종류:\s*(합성|시뮬레이션 모델)\s*$")
INIT_WORDS = re.compile(r"초기\s*(?:값|상태|화\s*시점)|초깃값|시뮬레이션\s*시작|전원\s*인가|power[- ]on|\binitial\b", re.I)


def _word(name: str) -> re.Pattern:
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")


def check_sections(spec: Spec) -> list[SpecIssue]:
    out, f = [], _name(spec)
    for n, title in TEMPLATE.items():
        s = spec.sections.get(n)
        if s is None:
            out.append(SpecIssue("SEC_MISSING", "error", f, f"{n}번 섹션 '{title}'이 없음"))
        elif s.title != title:
            out.append(SpecIssue("SEC_TITLE", "warning", f"{f}:{s.line}", f"{n}번 섹션 제목이 템플릿과 다름: '{s.title}' (템플릿: '{title}')"))
    lines = [spec.sections[n].line for n in sorted(spec.sections)]
    if lines != sorted(lines):
        out.append(SpecIssue("SEC_ORDER", "error", f, "섹션 순서가 번호 순서가 아님"))
    return out


def _name(spec: Spec) -> str:
    return spec.path.name if spec.path else "spec"


def check_l1(spec: Spec) -> tuple[dict | None, list[SpecIssue]]:
    from pipeline.ir.model import Module
    from pipeline.spec.l1 import SpecTableError, block_top_l1
    from pipeline.validator.checks import validate

    f = _name(spec)
    try:
        ir, prov = block_top_l1(spec)
    except (SpecTableError, KeyError) as e:
        return None, [SpecIssue("TABLE_PARSE", "error", f, f"2~4번 섹션 표에서 L1을 만들 수 없음: {e}")]
    out = []
    for i in validate(Module(ir), {}, upto=3):
        if "tristate 블록 하나가 맡아야" in i.message:
            continue   # SPEC 표의 L1에는 블록이 없다. inout을 맡는 tristate는 Architect와 Author가 둔다 (2026-10-22 od_top)
        line = prov.get(i.node) or prov.get(i.node.rsplit("/", 1)[0])
        out.append(SpecIssue(f"L1_{i.stage}", "error", f"{f}:{line}" if line else i.node, i.message))
    for p in ir["ports"]:
        if "bundle" in p and p["protocol"] not in PROTOCOLS:
            out.append(SpecIssue("PROTOCOL_TAG", "warning", f'{f}:{prov.get("ports/" + p["bundle"])}',
                                 f'번들 {p["bundle"]}의 프로토콜 태그 {p["protocol"]!r}가 표준 목록({sorted(PROTOCOLS)})에 없음. '
                                 "하니스와 프로토콜 체커를 만들 수 없음"))
    return ir, out


def check_requirements(spec: Spec) -> list[SpecIssue]:
    out, f = [], _name(spec)
    ids = [r.id for r in spec.requirements]
    for rid in sorted({i for i in ids if ids.count(i) > 1}):
        lines = [str(r.line) for r in spec.requirements if r.id == rid]
        out.append(SpecIssue("REQ_DUP", "error", f"{f}:{lines[0]}", f"요구사항 ID {rid}가 여러 번 나옴 (줄 {', '.join(lines)})"))
    known = set(ids)
    for r in spec.requirements:
        w = f"{f}:{r.line}"
        if r.section in (5, 6, 7) and r.level not in LEVELS:
            out.append(SpecIssue("REQ_LEVEL", "error", w, f"{r.id}에 의무 수준(필수, 권장, 허용) 또는 '미정의' 표시가 없음"))
        for ref in r.refs:
            if ref not in known:
                out.append(SpecIssue("REQ_REF", "error", w, f"{r.id}가 없는 요구사항 {ref}를 인용함"))
        body = re.sub(r"`[^`]*`|\([^)]*\)|\[[^\]]*\]", "", r.text)
        if len(re.findall(r"[다음함됨짐임]\.(?:\s|$)", body)) > 1:
            out.append(SpecIssue("REQ_MULTI", "warning", w, f"{r.id}에 문장이 둘 이상 있음. 한 문장에 동작 하나만 씀"))
    return out


def check_ports(spec: Spec, ir: dict | None) -> list[SpecIssue]:
    if ir is None:
        return []
    from pipeline.spec.l1 import ports as port_rows

    f = _name(spec)
    _, prov = port_rows(spec, ir["domains"])
    text = "\n".join(r.text for r in spec.requirements if r.section in (5, 6, 7))
    out = []
    for p in ir["ports"]:
        sigs = p["signals"] if "bundle" in p else [p]
        for s in sigs:
            if _word(s["name"]).search(text):
                continue
            node = f'ports/{p["bundle"]}/{s["name"]}' if "bundle" in p else f'ports/{s["name"]}'
            w = f"{f}:{prov.get(node)}"
            if s["dir"] == "output":
                out.append(SpecIssue("OUTPUT_NO_BEHAVIOR", "error", w, f"출력 {s['name']}의 동작을 정한 요구사항이 없음 (5~7번 섹션에서 참조되지 않음)"))
            else:
                out.append(SpecIssue("PORT_UNREFERENCED", "warning", w, f"입력 {s['name']}이 요구사항에서 참조되지 않음"))
    return out


def check_scenarios(spec: Spec) -> list[SpecIssue]:
    f = _name(spec)
    out = []
    kinds = {s["kind"] for s in spec.scenarios}
    for k in ("정상", "코너"):
        if k not in kinds:
            out.append(SpecIssue("SCENARIO_MISSING", "error", f, f"{k} 예시 시나리오가 없음 (Block Top당 정상 하나, 코너 하나 이상)"))
    for s in spec.scenarios:
        t = s["table"]
        if t is None or not any("입력" in h for h in t.header) or not any("기대" in h for h in t.header) or not t.rows:
            out.append(SpecIssue("SCENARIO_TABLE", "error", f"{f}:{s['line']}", f"{s['id']}에 입력과 기대 출력이 있는 표가 없음"))
    return out


def impl_kind(spec: Spec) -> str | None:
    """9절의 구현 종류 줄."""
    sec = spec.sections.get(9)
    return next((m.group(1) for _, ln in (sec.lines if sec else []) if (m := KIND_RE.match(ln))), None)


def is_sim_model(spec: Spec) -> bool:
    return impl_kind(spec) == "시뮬레이션 모델"


def check_impl_kind(spec: Spec, ir: dict | None) -> list[SpecIssue]:
    if ir is None or impl_kind(spec) or ir["domains"]:   # 클럭, 리셋은 도메인에 있어 포트 목록에 없다
        return []
    if any(s["dir"] == "input" for p in ir["ports"] for s in (p["signals"] if "bundle" in p else [p])):
        return []
    return [SpecIssue("NO_INPUT_KIND", "error", _name(spec),
                      "입력 포트가 없는 블록임. 9절에 `- 구현 종류: 시뮬레이션 모델`(시간에 따라 바뀌는 출력) 또는 "
                      "`- 구현 종류: 합성`(상수 출력)을 밝힌다")]


def check_resets(spec: Spec, ir: dict | None) -> list[SpecIssue]:
    if ir is None:
        return []
    f, out = _name(spec), []
    reqs = [r for r in spec.requirements if r.section in (5, 6, 7) and r.level != "미정의"]
    rdoms = [d for d in ir["domains"] if d.get("reset")]
    for d in rdoms:
        rs = d["reset"]
        if rs.get("async"):
            continue
        port, act = rs["port"], "0" if rs["active"] == "low" else "1"
        same = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(port)}\s*(?:=|가|이)\s*{act}\s*(?:인|이 된|가 된)\s*(?:동안|사이클)")
        for r in reqs:
            if same.search(r.text) and not re.search(r"다음\s*(?:사이클|클럭)", r.text):
                out.append(SpecIssue("RESET_SAME_CYCLE", "warning", f"{f}:{r.line}",
                                     f"{r.id}: 동기 리셋({port})이 걸린 그 사이클의 출력을 정함. 레지스터 출력은 리셋을 본 다음 "
                                     f"사이클부터 바뀌므로 \"{port}={act}을 본 다음 사이클부터\"처럼 쓴다"))
    # 리셋 없는 도메인의 출력에 초기값을 요구하는 문장: IR에는 initial이 없고 시뮬레이터는 초기값을 무작위로 두므로
    # 어떤 RTL도 만족할 수 없다 (RTLLM right_shifter, square_wave)
    norst = {d["name"] for d in ir["domains"] if not d.get("reset")}
    outs = [s["name"] for p in ir["ports"] for s in (p["signals"] if "bundle" in p else [p])
            if s["dir"] == "output" and (s.get("domain") or p.get("domain")) in norst]
    for r in [] if is_sim_model(spec) else reqs:
        if INIT_WORDS.search(r.text) and (hit := [o for o in outs if _word(o).search(r.text)]):
            out.append(SpecIssue("INIT_NO_RESET", "error", f"{f}:{r.line}",
                                 f"{r.id}: 리셋이 없는 도메인의 출력({', '.join(hit)})에 초기값을 정함. 리셋 없이는 초기값을 "
                                 "보장할 수 없으므로(initial 문 금지) 리셋을 두거나, 첫 값이 정해지기 전 출력을 '미정의'로 쓴다"))
    clocks = {d["clock"] for d in rdoms}
    ports = sorted({d["reset"]["port"] for d in rdoms})
    if len(clocks) > 1 and len(ports) > 1 and not any(
            "함께" in r.text and all(_word(p).search(r.text) for p in ports) for r in reqs):
        out.append(SpecIssue("MULTI_RESET_USAGE", "warning", f,
                             f"리셋 도메인이 클럭 {len(clocks)}개에 걸쳐 있는데 리셋 {', '.join(ports)}을 함께 거는 사용 조건 문장이 없음. "
                             "\"모든 리셋을 함께, 가장 느린 클럭 N사이클 이상 건다\"와 한쪽만 리셋했을 때(미정의)를 쓴다"))
    return out


def review_items(spec: Spec) -> list[SpecIssue]:
    """사람이 최종 승인에서 볼 항목: 미정의 동작과 열린 질문."""
    f = _name(spec)
    out = [SpecIssue("UNDEFINED", "info", f"{f}:{r.line}", f"{r.id} 미정의: {r.text}") for r in spec.requirements if r.level == "미정의"]
    out += [SpecIssue("SPEC_REVIEW" if "재검토" in q["text"] else "OPEN_QUESTION", "question", f"{f}:{q['line']}", f"{q['id']} {q['text']}", "open_question") for q in spec.questions]
    return out


def run(spec: Spec, regmap: Path | None = None, array_depth: dict[str, str] | None = None,
        llm_review: list[dict] | None = None) -> dict:
    """llm_review: llm_check.py가 낸 질문 (검토 항목에 그대로 더함)."""
    issues = check_sections(spec)
    ir, l1 = check_l1(spec)
    issues += l1 + check_requirements(spec) + check_ports(spec, ir) + check_scenarios(spec) + check_resets(spec, ir) + check_impl_kind(spec, ir)
    if regmap:
        from pipeline.spec import regmap as rm

        fields, ri = rm.parse(regmap)
        params = {p["name"]: int(p["default"]) for p in (ir or {}).get("params", []) if str(p["default"]).isdigit()}
        addr_bits = None
        for p in (ir or {}).get("ports", []):
            for s in p.get("signals", []):
                m = re.search(r"\[(\d+):0\]", s["type"])
                if p.get("protocol") == "apb" and s["name"].endswith("addr") and m:
                    addr_bits = int(m.group(1)) + 1
        issues += ri + rm.check(fields, params, array_depth, addr_bits, Path(regmap).name) + rm.cross_check(spec, fields)
    items = review_items(spec)
    return {"spec": str(spec.path) if spec.path else None, "module": spec.module,
            "requirements": len(spec.requirements), "gate": "pass" if not errors(issues) else "fail",
            "issues": [i.to_dict() for i in issues],
            "review": [i.to_dict() for i in items + [x for x in issues if x.severity == "warning"]] + list(llm_review or []),
            "l1": ir}


def _esc(text: str) -> str:
    return text.replace("|", "\\|").replace("~", "\\~")


def to_markdown(res: dict) -> str:
    lines = [f"# SPEC 검사: {res['module']}", "", f"- 게이트: **{res['gate']}**",
             f"- 요구사항: {res['requirements']}개",
             f"- 오류 {sum(i['severity'] == 'error' for i in res['issues'])}건, 경고 {sum(i['severity'] == 'warning' for i in res['issues'])}건",
             "", "## 오류와 경고", "", "| 심각도 | 코드 | 위치 | 내용 |", "| --- | --- | --- | --- |"]
    for i in res["issues"]:
        lines.append(f"| {i['severity']} | {i['code']} | {i['where']} | {_esc(i['message'])} |")
    lines += ["", "## 검토 항목 (최종 승인에서 확인)", "", "| 출처 | 코드 | 위치 | 내용 |", "| --- | --- | --- | --- |"]
    for i in res["review"]:
        lines.append(f"| {i['source']} | {i['code']} | {i['where']} | {_esc(i['message'])} |")
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SPEC 툴 검사")
    ap.add_argument("spec")
    ap.add_argument("--regmap")
    ap.add_argument("--array", action="append", default=[])
    ap.add_argument("--llm", help="llm_check.py의 llm_review.json (질문을 검토 항목에 더함)")
    ap.add_argument("-o", "--out", required=True)
    args = ap.parse_args(argv)
    llm = json.loads(Path(args.llm).read_text(encoding="utf-8"))["review"] if args.llm else None
    res = run(load(args.spec), Path(args.regmap) if args.regmap else None, dict(a.split("=", 1) for a in args.array), llm)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "review.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "review.md").write_text(to_markdown(res), encoding="utf-8")
    if res["l1"]:
        (out / f"{res['module']}.ir.json").write_text(json.dumps(res["l1"], ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"게이트 {res['gate']}: 오류 {sum(i['severity'] == 'error' for i in res['issues'])}, "
          f"경고 {sum(i['severity'] == 'warning' for i in res['issues'])}, 검토 항목 {len(res['review'])} → {out}/review.md")
    return 0 if res["gate"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
