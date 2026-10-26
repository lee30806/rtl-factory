"""벤치마크 입력 변환기 (BENCHMARK_PLAN 4.2절): 모듈 헤더 SV + 자연어 프롬프트 → L1 추출 → 2~4절 표 → 10개 섹션 spec.md."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pyslang

from experiments.bench.common.schema import BenchItem
from pipeline.agents import roles as roles_mod, spec_writer
from pipeline.agents.task import ROOT, Task, TaskResult, run_task
from pipeline.importer import r1 as r1_mod, r2 as r2_mod, spec_tables
from pipeline.importer.sources import Design, Inputs

TEMPLATE = ROOT / "docs" / "SPEC 작성 템플릿.md"
OUT = "out/spec.md"

CLK_RE = re.compile(r"(?:^|_|w|r)(clk|clock)(?:$|_|\d|[a-z])", re.I)
RST_RE = re.compile(r"(?:^|_|w|r|a|b)(a?rst_?n?|a?reset_?n?|clr|clear)(?:$|_|\d)", re.I)

BENCH_SPEC_TASK = """\
# 과제: 벤치마크 문제 {top}의 10개 섹션 SPEC 쓰기

너는 RTL 생성 파이프라인의 SpecWriter 에이전트다. 벤치마크 자연어 명세(`prompt.txt`)와 모듈 헤더(`header.sv`)를 읽고
파이프라인 표준 10개 섹션 SPEC(`{out}`)을 작성한다.
이 SPEC만 보고 Architect, Author, Verifier 에이전트들이 설계를 처음부터 구현하고 검증한다.

## 입력
- `template.md`: SPEC 작성 템플릿. 형식 규칙(제목, 10개 섹션 이름과 순서, 요구사항 ID 줄, 시나리오 표, 열린 질문)을 그대로 따른다
- `tables.md`: 모듈 헤더에서 결정론적으로 생성한 2~4절 표. **글자 하나 바꾸지 않고** 그대로 2, 3, 4절에 넣는다
- `prompt.txt`: 벤치마크 원본 자연어 명세 전문
- `header.sv`: 포트와 파라미터가 선언된 모듈 헤더
- `notes.json`: 고정 파라미터, 매크로 상수, 재사용 하위 셀 정보

## 할 일
`{out}`에 10개 섹션 SPEC을 쓴다.
- 제목은 반드시 `# {top} SPEC`으로 쓴다.
- **2절, 3절, 4절은 `tables.md`의 내용을 글자 하나 바꾸지 않고 그대로 복사해 넣는다.**
- 1절 (`## 1. 목적과 범위`): 모듈의 목적을 한 문단으로 설명하고, 범위 밖 항목을 명시한다.
- 5절 (`## 5. 기능 요구사항`): `- REQ-01 (필수) ...` 형식으로 모든 출력 포트와 입력 포트가 최소 한 번 이상 등장하도록
  동작을 구체적인 수치·조건·타이밍으로 기술한다.
  - **중요 (`spec_gate` 규칙):** 3절 인터페이스 표에 있는 모든 포트 이름(번들인 경우 번들 이름 또는 하위 신호 이름, 클럭·리셋 제외)이
    5~7절 요구사항 문장에 최소 한 번 이상 그대로 적혀야 하며, 모든 출력 포트의 구동 조건과 값이 정의되어야 한다.
  - **한 문장 규칙:** 각 요구사항 ID 줄(`- ID (필수) ...`)에는 마침표로 끝나는 **문장 하나만** 쓴다 (두 문장 이상 쓰면 `REQ_MULTI` 경고가 발생함).
  - **프롬프트 충실성 규칙:** `prompt.txt`에 적히지 않은 리셋 해제 직후 첫 사이클 특수 동작(예: 리셋 중의 값을 첫 입력 비트로 간주할지 여부 등)을 임의로 지어내어 필수 요구사항으로 강제하지 않는다. 리셋 해제 후에는 명시적으로 인가된 입력 시퀀스에 대해서만 출력을 규정한다.
- 6절 (`## 6. 타이밍과 성능`): `- TIM-01 (필수) ...` 형식으로 클럭 에지 기준 출력 갱신 지연과 처리율을 한 줄에 한 문장씩 명시한다.
  - **지연을 지어내지 않는다:** `prompt.txt`에 없는 정확한 지연 사이클 수, 핸드셰이크 응답 사이클, 사이클 번호를 정하지 않는다.
    결과를 알리는 출력 플래그(예: `o_en`, `valid`, `done`, `rdy`)가 있으면 "o_en=1인 사이클의 result는 그 전에 i_en=1로 받은 입력들의
    결과를 받은 순서대로 낸다"처럼 플래그 기준으로 쓰고, 지연은 "구현에 따름(검증하지 않음)"으로 둔다. "여러 단의 레지스터"처럼
    수를 정하지 않은 표현을 특정 사이클 수로 바꾸지 않는다: 지어낸 값은 그와 다른 지연을 가진 맞는 설계를 실패시킨다.
- 7절 (`## 7. 예외와 코너 케이스`): `- EXC-01 (필수) ...` 형식으로 리셋 인가 시 모든 출력 포트의 초기값과 경계 조건 동작을 한 줄에 한 문장씩 명시한다.
  - `tables.md` 2절에서 리셋 포트가 `없음`인 도메인의 출력에는 초기값(시뮬레이션 시작 값)을 쓰지 않는다. `prompt.txt`가 initial 문이나
    초기값을 말해도 그렇다: 파이프라인은 initial 문을 쓸 수 없어 보장할 수 없다. 첫 값이 정해지기 전의 출력은 `- EXC-02 미정의: ...` 형식으로 쓴다
    (spec_gate `INIT_NO_RESET` 오류).
  - `tables.md` 2절에서 리셋 동기가 `동기`인 도메인은 "리셋이 걸린 그 사이클"이 아니라 "리셋 신호를 본 다음 사이클부터 출력이 초기화된다"라고 쓴다.
  - `tables.md` 2절에 서로 다른 클럭의 리셋 포트가 2개 이상(예: `wrstn`, `rrstn` 또는 `arstn`, `brstn`) 있으면, 모든 리셋 포트 이름을 한 문장에 넣어 "모든 리셋(<포트 이름들>)을 함께, 가장 느린 클럭 4사이클 이상 건다."라는 필수 요구사항 문장과 한쪽만 리셋했을 때의 미정의 문장(`- EXC-02 미정의: ...` 형식)을 반드시 포함한다.
- **조합 블록:** `tables.md` 2절이 도메인 "없음", 도메인 간 관계 "조합 블록"인 행 하나뿐이면 클럭과 리셋이 없는 조합 블록이다.
  6절에는 클럭 지연 대신 "출력은 레지스터를 거치지 않는 입력의 조합 함수다." 같은 문장을 쓰고, 7절에는 리셋 문장 없이
  경계 입력(최댓값, 0, 넘침, 0으로 나누기 등)의 출력만 쓴다. 사이클, 클럭 에지, 리셋, 초기값이라는 말을 쓰지 않는다.
- 8절 (`## 8. 예시 시나리오`): `### SC-01 정상: ...`와 `### SC-02 코너: ...` 두 개 이상의 하위 제목(`###`)과 마크다운 표(`| 입력 | 기대 출력 |`)를 작성한다. 특정 사이클 번호 배열 길이를 강제하기보다 입력 시퀀스에 따른 기대 출력 시퀀스로 기술한다.
- 9절 (`## 9. 구현 제약`): `### 필수 구조`와 `### 금지 사항`을 적는다 (`notes.json`에 상수나 하위 셀이 있으면 명시).
- 10절 (`## 10. 열린 질문`): 없으면 `- 없음`으로 적는다.

작성 후 반드시 아래 명령을 실행해 `"ok": true`가 나오는지 확인하고, 오류가 있으면 모두 고친 뒤 종료한다:
```bash
python3 tools/pl.py spec_gate {out} --tables tables.md --top {top}
```

## 규칙
- `{out}` 외의 파일은 만들거나 고치지 않는다.
- 요구사항 줄은 반드시 `- <ID> (필수) <문장>` 형식을 지킨다 (괄호 안 의무 수준 필수). 미정의는 `- <ID> 미정의: <문장>`으로 쓴다.
"""


def _read_header_text(header_sv: str) -> str:
    if "\n" not in header_sv and len(header_sv) < 255:
        p = Path(header_sv)
        if p.exists() and p.is_file():
            return p.read_text(encoding="utf-8")
    return header_sv


def strip_to_header(sv_text: str, top: str) -> str:
    """모듈 선언부터 포트 목록 끝(`);`)까지만 남기고 본문은 비운 헤더 SV를 만든다."""
    pattern = re.compile(rf"\bmodule\s+{re.escape(top)}\b", re.M)
    m = pattern.search(sv_text)
    if m:
        pos = m.start()
        depth = 0
        seen_paren = False
        i = m.end()
        n = len(sv_text)
        while i < n:
            # 주석 건너뛰기
            if sv_text[i : i + 2] == "//":
                nl = sv_text.find("\n", i + 2)
                i = n if nl == -1 else nl + 1
                continue
            if sv_text[i : i + 2] == "/*":
                endc = sv_text.find("*/", i + 2)
                i = n if endc == -1 else endc + 2
                continue
            ch = sv_text[i]
            if ch == "(":
                depth += 1
                seen_paren = True
            elif ch == ")":
                depth -= 1
            elif ch == ";" and seen_paren and depth == 0:
                return sv_text[pos : i + 1].rstrip() + "\nendmodule\n"
            i += 1
    if "endmodule" not in sv_text:
        return sv_text.rstrip() + "\nendmodule\n"
    return sv_text


def _extract_ref_clock_reset_hint(ref_files: list[str]) -> str:
    """BENCHMARK_PLAN 4.1절: reference_rtl에서 always @(...) 민감도 줄과 리셋 조건 줄만 뽑아 R2 힌트로 쓴다 (에이전트에는 절대 노출하지 않음)."""
    hints: list[str] = []
    for rf in ref_files:
        p = Path(rf)
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            s = line.strip()
            if "always" in s or re.search(r"\bif\s*\([^)]*(?:rst|reset)", s, re.I):
                hints.append(s)
    return "\n".join(hints)


def infer_reset_and_clock_traits(prompt_text: str, clk_port: str, rst_port: str | None, ref_hint: str = "") -> tuple[str, str, bool]:
    """프롬프트·포트 이름·R2 힌트에서 (clock_edge: 'pos'|'neg'|'both', reset_active: 'high'|'low', reset_async: bool)을 추론한다."""
    combined = (prompt_text + "\n" + ref_hint).lower()
    text_low = prompt_text.lower()
    hint_low = ref_hint.lower()
    ck_l = re.escape(clk_port.lower())
    # 클럭 에지 (양쪽 에지 사용 여부 포함)
    has_pos_hint = bool(re.search(rf"posedge\s+{ck_l}", hint_low))
    has_neg_hint = bool(re.search(rf"negedge\s+{ck_l}", hint_low))
    dual_in_prompt = bool(re.search(r"(both\s+(the\s+)?(rising|positive)\s+and\s+(falling|negative)\s+edges|(rising|positive)\s+and\s+(falling|negative)\s+edges\s+of\s+(the\s+)?clock)", text_low))
    if (has_pos_hint and has_neg_hint) or dual_in_prompt:
        clk_edge = "both"
    elif (has_neg_hint and not has_pos_hint) or re.search(rf"negedge\s+{ck_l}|(falling|negative)\s+edge\s+of\s+.*{ck_l}|하강\s*에지", text_low):
        clk_edge = "neg"
    else:
        clk_edge = "pos"

    if rst_port is None:
        return clk_edge, "high", False

    rl = re.escape(rst_port.lower())
    # else if (rst == 0) 같은 비활성 분기 문구는 제거한 뒤 극성 판별
    cleaned_combined = re.sub(rf"else\s+if\s*\(\s*{rl}\s*==\s*(?:1'b)?0\s*\)", "", combined)
    # 리셋 극성
    rst_low_name = bool(re.search(r"(_n|n)$", rst_port, re.I)) and not rst_port.lower().endswith("en")
    explicit_low = bool(
        re.search(
            rf"active[- ]low|low[- ]active|[!~]\s*{rl}\b|negedge\s+{rl}\b|\bif\s*\(\s*{rl}\s*==\s*(?:1'b)?0\s*\)|극성\s*\|\s*low|저활성",
            cleaned_combined,
        )
    )
    explicit_high = bool(
        re.search(
            rf"active[- ]high|high[- ]active|posedge\s+{rl}\b|\bif\s*\(\s*{rl}\s*(?:==\s*(?:1'b)?1)?\s*\)|(?:reset|rst)\s*\({rl}\)\s*(?:signal\s*)?is\s*high|극성\s*\|\s*high|고활성",
            cleaned_combined,
        )
    )
    if explicit_high and not explicit_low:
        active = "high"
    elif explicit_low and not explicit_high:
        active = "low"
    elif rst_low_name:
        active = "low"
    else:
        active = "high"

    # 동기 / 비동기 리셋
    async_name = bool(re.match(r"^a(rst|reset)", rst_port, re.I))
    explicit_async = bool(re.search(rf"async(hronous|hronously)?\s+reset|비동기\s*리셋|(?:or|,)\s*(?:posedge|negedge)?\s*{rl}\b", combined))
    explicit_sync = bool(re.search(r"\bsync(hronous|hronously)?\s+reset|동기\s*리셋|리셋\s*동기\s*\|\s*동기", text_low))
    if hint_low and not re.search(rf"(?:or|,)\s*(?:posedge|negedge)?\s*{rl}\b", hint_low) and re.search(rf"always\s*@\s*\(\s*(?:posedge|negedge)\s+{ck_l}\s*\)", hint_low):
        is_async = False
    elif explicit_sync and not explicit_async:
        is_async = False
    elif explicit_async or async_name:
        is_async = True
    else:
        is_async = False
    return clk_edge, active, is_async


def _inject_dummy_seq_if_empty(sv_text: str, top: str, prompt_text: str, incdirs: list[Path], defines: dict[str, str],
                               params: dict[str, str], tmp_dir: Path, ref_hint: str = "") -> Path:
    """빈 모듈 헤더(`module top(...); endmodule`)에 클럭/리셋 더미 순차 블록과 파라미터 참조를 넣어 r1이 도메인·포트·파라미터를 추출하게 한다."""
    raw_file = tmp_dir / f"{top}_raw.sv"
    if "endmodule" not in sv_text:
        sv_text = sv_text.rstrip() + "\nendmodule\n"
    raw_file.write_text(sv_text, encoding="utf-8")
    design = Design(Inputs([raw_file], top, params, incdirs, defines))
    body = design.top.body
    SK = pyslang.SymbolKind
    has_seq = any(m.kind == SK.ProceduralBlock for m in body)
    if has_seq:
        return raw_file

    in_ports = [p.name for p in body.portList if p.kind == SK.Port and p.direction == pyslang.ArgumentDirection.In]
    out_ports = [p.name for p in body.portList if p.kind == SK.Port and p.direction == pyslang.ArgumentDirection.Out]
    param_names = [m.name for m in body if m.kind == SK.Parameter and not m.isLocalParam]

    clks = [p for p in in_ports if CLK_RE.search(p)]
    rsts = [p for p in in_ports if RST_RE.search(p) and p not in clks]
    data_ins = [p for p in in_ports if p not in clks and p not in rsts]

    if not clks:
        return raw_file

    # 클럭-리셋 짝짓기
    pairs: list[tuple[str, str | None]] = []
    if len(clks) == 1:
        pairs.append((clks[0], rsts[0] if rsts else None))
    else:
        used_rsts: set[str] = set()
        for ck in clks:
            prefix = re.sub(r"^(clk|clock)_?|_?(clk|clock)$", "", ck, flags=re.I).strip("_")
            match_r = next((r for r in rsts if r not in used_rsts and prefix and r.lower().startswith(prefix.lower())), None)
            if match_r is None and len(rsts) == 1:
                match_r = rsts[0]
            if match_r:
                used_rsts.add(match_r)
            pairs.append((ck, match_r))

    lines = []
    if param_names:
        expr = " + ".join(f"int'({p})" for p in param_names)
        lines.append(f"  wire [31:0] __bench_p = 32'({expr});")
    else:
        lines.append("  wire [31:0] __bench_p = 32'd0;")

    any_prefix_out = any(
        any(op.lower().startswith(re.sub(r"^(clk|clock)_?|_?(clk|clock)$", "", ck, flags=re.I).strip("_").lower()) for op in out_ports)
        for ck, _ in pairs
        if re.sub(r"^(clk|clock)_?|_?(clk|clock)$", "", ck, flags=re.I).strip("_")
    )

    for idx, (ck, rst) in enumerate(pairs):
        edge, active, is_async = infer_reset_and_clock_traits(prompt_text, ck, rst, ref_hint=ref_hint)
        edges = ["posedge", "negedge"] if edge == "both" else (["negedge"] if edge == "neg" else ["posedge"])
        prefix = re.sub(r"^(clk|clock)_?|_?(clk|clock)$", "", ck, flags=re.I).strip("_").lower()
        if len(pairs) > 1 and prefix:
            dom_ins = [p for p in data_ins if p.lower().startswith(prefix)] or (data_ins if idx == 0 else [])
            if any_prefix_out:
                dom_outs = [p for p in out_ports if p.lower().startswith(prefix)]
            else:
                dom_outs = out_ports if idx == len(pairs) - 1 else []
        else:
            dom_ins = data_ins
            dom_outs = out_ports
        read_terms = ["__bench_p", *dom_ins]
        rhs = "|{" + ", ".join(read_terms) + "}" if read_terms else "1'b0"
        dregs: list[str] = []
        for e_idx, ck_kw in enumerate(edges):
            dreg = f"__bench_d{idx}_{e_idx}"
            dregs.append(dreg)
            lines.append(f"  reg {dreg};")
            erhs = rhs if e_idx == 0 else dregs[0]
            if rst:
                rst_kw = "posedge" if active == "high" else "negedge"
                sens = f"{ck_kw} {ck} or {rst_kw} {rst}" if is_async else f"{ck_kw} {ck}"
                cond = rst if active == "high" else f"!{rst}"
                lines.append(f"  always @({sens}) if ({cond}) {dreg} <= 1'b0; else {dreg} <= {erhs};")
            else:
                lines.append(f"  always @({ck_kw} {ck}) {dreg} <= {erhs};")
        comb_dreg = " | ".join(dregs)
        for op in dom_outs:
            p_sym = body.findPort(op)
            is_var = getattr(getattr(p_sym, "internalSymbol", None), "kind", None) == SK.Variable
            if not is_var:
                lines.append(f"  assign {op} = ({comb_dreg}) ? '0 : '0;")
            else:
                lines.insert(-len(edges), f"  always @(posedge {ck}) {op} <= '0;") if False else None

    idx = sv_text.rfind("endmodule")
    augmented = sv_text[:idx] + "\n" + "\n".join(lines) + "\nendmodule\n"
    aug_file = tmp_dir / f"{top}_aug.sv"
    aug_file.write_text(augmented, encoding="utf-8")
    return aug_file


def extract_header_l1(item: BenchItem, work_dir: Path) -> tuple[dict, str, dict]:
    """모듈 헤더 SV와 프롬프트에서 Block Top L1과 2~4절 마크다운 표(`tables.md`), `notes.json`을 결정론적으로 추출한다."""
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    raw_text = _read_header_text(item.header_sv)
    clean_hdr = strip_to_header(raw_text, item.top)
    (work_dir / "header.sv").write_text(clean_hdr, encoding="utf-8")

    incdirs = [Path(p).resolve() for p in item.incdirs]
    cells = [Path(p).resolve() for p in item.cells if Path(p).exists()]
    ref_hint = _extract_ref_clock_reset_hint(item.reference_rtl)
    aug_file = _inject_dummy_seq_if_empty(clean_hdr, item.top, item.prompt_text, incdirs, item.defines, item.params, work_dir, ref_hint=ref_hint)

    inp = Inputs([*cells, aug_file], item.top, dict(item.params), incdirs, dict(item.defines))
    r1_res = r1_mod.run(inp, work_dir)
    if r1_res.get("rejects") or not r1_res.get("ok"):
        raise RuntimeError(f"R1 헤더 추출 실패: {r1_res.get('rejects') or r1_res.get('validate')}")

    irs = {p.name.removesuffix(".ir.json"): json.loads(p.read_text(encoding="utf-8"))
           for p in sorted((work_dir / "r1" / "ir").glob("*.ir.json"))}
    req = r2_mod.make_request(r1_res, irs)

    # R2 무인 자동 응답
    answers: dict[str, str] = {}
    top_doms = irs[item.top].get("domains", [])
    default_dom = top_doms[0]["name"] if top_doms else "async"
    for q in req["items"]:
        qid, kind = q["id"], q["kind"]
        if kind == "freq":
            answers[qid] = "100"
        elif kind == "relation":
            answers[qid] = "async"
        elif kind == "protocol":
            sigs = q.get("signals", [])
            answers[qid] = "valid_ready" if len(sigs) >= 3 else "none"
        elif kind == "review":
            if q.get("port"):
                answers[qid] = default_dom
            else:
                answers[qid] = "ok"

    errs = r2_mod.set_answers(req, answers)
    if errs:
        raise RuntimeError(f"R2 응답 설정 오류: {errs}")
    applied_irs, apply_errs = r2_mod.apply(req, r1_res, irs)
    if apply_errs:
        raise RuntimeError(f"R2 적용 오류: {apply_errs}")

    top_ir = applied_irs[item.top]
    top_l1 = {
        "module": top_ir["module"],
        "role": "block_top",
        "params": [p for p in top_ir.get("params", []) if not p.get("local")],
        "domains": top_ir.get("domains", []),
        "ports": [
            {k: v for k, v in p.items() if k not in ("kind", "reset")}
            if "bundle" not in p
            else {
                **p,
                "signals": [{k: v for k, v in s.items() if k not in ("kind", "reset")} for s in p["signals"]],
            }
            for p in top_ir.get("ports", [])
        ],
    }
    tables = spec_tables.tables_md(top_l1)
    notes = {
        "benchmark": item.benchmark,
        "id": item.id,
        "category": item.category,
        "params": item.params,
        "defines": item.defines,
        "macros": r1_res.get("macros", {}),
        "cells": [Path(c).name for c in item.cells],
    }
    (work_dir / "top.l1.json").write_text(json.dumps(top_l1, ensure_ascii=False, indent=2), encoding="utf-8")
    (work_dir / "tables.md").write_text(tables, encoding="utf-8")
    (work_dir / "notes.json").write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")
    return top_l1, tables, notes


def make_bench_spec_task(item: BenchItem, tables: str, header_text: str, notes: dict) -> Task:
    """정답 RTL(reference_rtl)을 절대 노출하지 않고 프롬프트와 헤더, 2~4절 표만 주는 BenchSpecWriter Task."""
    inputs: dict[str, str | Path] = {
        "template.md": TEMPLATE,
        "tables.md": tables,
        "prompt.txt": item.prompt_text,
        "header.sv": header_text,
        "notes.json": json.dumps(notes, ensure_ascii=False, indent=2),
    }
    text = BENCH_SPEC_TASK.format(top=item.top, out=OUT)
    return Task("spec_writer", f"bench_spec/{item.id}", text, inputs=inputs, outputs=[OUT], write_scope=["out/"])


def generate_spec(item: BenchItem, work_dir: Path, roles: roles_mod.Roles, isolate: bool = True) -> tuple[Path | None, list[str], TaskResult]:
    """BenchItem에서 L1 추출 → BenchSpecWriter 실행 → spec_gate 검증을 거쳐 고정된 spec.md를 생성한다."""
    work_dir = Path(work_dir).resolve()
    top_l1, tables, notes = extract_header_l1(item, work_dir / "l1_extract")
    header_text = (work_dir / "l1_extract" / "header.sv").read_text(encoding="utf-8")

    task = make_bench_spec_task(item, tables, header_text, notes)
    task.isolate = isolate
    cfg = roles.agents.get("spec_writer") or roles.agents.get("verifier")
    if isolate:
        from pipeline.agents.task import RUNS
        import hashlib
        key = hashlib.sha256(str(work_dir).encode()).hexdigest()[:8]
        agent_work = RUNS / f"bench-spec-{item.id}-{key}"
        run_root = RUNS
    else:
        agent_work = work_dir / "spec_writer_work"
        run_root = None

    res = run_task(cfg, task, agent_work, check=spec_writer.check(top_l1), retries=roles.limits.get("agent_retries", 2), run_root=run_root)
    if not res.ok:
        return None, res.errors, res

    spec_out = work_dir / "spec.md"
    spec_out.write_text(Path(res.outputs[OUT]).read_text(encoding="utf-8"), encoding="utf-8")
    gate_errs = spec_writer.gate(spec_out, top_l1)
    return (spec_out if not gate_errs else None), gate_errs, res
