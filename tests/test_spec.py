"""P3: SPEC 파서, Block Top L1 생성."""
import pytest

from pipeline.ir.model import Module
from pipeline.spec.l1 import SpecTableError, block_top_l1
from pipeline.spec.parse import TEMPLATE, load, parse
from pipeline.validator.checks import validate
from test_pipeline import ROOT

I3C = ROOT / "examples" / "i3c_basic_controller" / "spec.md"


def test_parse_i3c_sections_requirements_scenarios():
    s = load(I3C)
    assert s.module == "i3c_ctrl_top" and sorted(s.sections) == list(TEMPLATE)
    assert len(s.requirements) == 97 and len({r.id for r in s.requirements}) == 97
    assert sum(r.level == "미정의" for r in s.requirements) == 3
    assert [x["id"] for x in s.scenarios] == [f"SC-0{i}" for i in range(1, 8)]
    assert all(x["table"] is not None for x in s.scenarios)
    assert [q["id"] for q in s.questions] == [f"Q-0{i}" for i in range(1, 8)]
    r = s.requirement("FR-PRIV-02")
    assert r.level == "필수" and r.section == 5 and set(r.refs) == {"FR-BUS-09", "FR-BUS-10", "FR-BUS-11"}
    assert I3C.read_text().splitlines()[r.line - 1].startswith("- FR-PRIV-02")


def test_i3c_block_top_l1_passes_v1_to_v3():
    ir, prov = block_top_l1(load(I3C))
    assert validate(Module(ir), {}, upto=3) == []
    assert ir["role"] == "block_top" and ir["domains"][0]["freq_mhz"] == 100.0
    assert ir["domains"][0]["reset"] == {"port": "rst_n", "active": "low", "async": True}
    names = [p.get("bundle") or p["name"] for p in ir["ports"]]
    assert "clk" not in names and "rst_n" not in names
    cmd = next(p for p in ir["ports"] if p.get("bundle") == "cmd")
    assert (cmd["protocol"], cmd["role"], len(cmd["signals"])) == ("valid_ready", "sink", 15)
    assert next(p for p in ir["ports"] if p.get("name") == "sda_i")["domain"] is None
    assert prov["ports/cmd/cmd_len"] > prov["ports/cmd"] and prov["params/DAT_DEPTH"] > 0


MINI = """# two_dom SPEC

## 1. 목적과 범위
x

## 2. 클럭, 리셋, 전력

| 도메인 | 클럭 포트 | 주파수 | 리셋 포트 | 리셋 극성 | 리셋 동기 | 도메인 간 관계 | 전력 도메인 | isolation | retention |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| a | clk_a | 250 MHz | rst_a | high | 동기 | b와 비동기 | PD_A | clamp0 | 없음 |
| b | clk_b | 32 kHz | rst_b_n | low | 동기 | a와 비동기 | PD_AON | 없음 | 있음 |

## 3. 인터페이스

| 포트 또는 번들 | 방향 | 타입 | 프로토콜 태그 | 도메인 |
| --- | --- | --- | --- | --- |
| **s** (sink) | | | valid_ready | a |
| s_valid | input | logic | | |
| s_ready | output | logic | | |
| x | output | logic [3:0] | – | b |
"""


def test_two_domain_spec():
    ir, _ = block_top_l1(parse(MINI))
    a, b = ir["domains"]
    assert a["async_to"] == ["b"] and b["async_to"] == ["a"] and b["freq_mhz"] == pytest.approx(0.032)
    assert a["reset"] == {"port": "rst_a", "active": "high", "async": False}
    assert ir["power_domains"] == [{"name": "PD_A", "isolation": "clamp0"}, {"name": "PD_AON", "retention": "있음"}]
    assert ir["params"] == []
    assert validate(Module(ir), {}, upto=3) == []


def test_missing_interface_table_is_an_error():
    with pytest.raises(SpecTableError):
        block_top_l1(parse(MINI.split("## 3.")[0]))


# ---------------------------------------------------------------- 레지스터 맵
from pipeline.spec.regmap import check as reg_check, cross_check, parse as reg_parse  # noqa: E402

REGS = ROOT / "examples" / "i3c_basic_controller" / "registers.csv"


def test_i3c_regmap_is_clean_and_matches_spec():
    fields, issues = reg_parse(REGS)
    assert issues == [] and len(fields) == 41 and len({f.register for f in fields}) == 17
    assert reg_check(fields, {"DAT_DEPTH": 8}, {"DCT": "DAT_DEPTH"}, addr_bits=12) == []
    spec = load(I3C)
    found = cross_check(spec, fields)
    assert [i.severity for i in found] == ["warning"] and "APB" in found[0].message  # "APB 레지스터로 다룹니다"


def _write(tmp_path, rows, name="r.csv"):
    p = tmp_path / name
    p.write_text("register,offset,field,bits,access,reset,description\n" + "\n".join(rows) + "\n")
    return p


@pytest.mark.parametrize("rows, code", [
    (["A,0x0,x,3:0,RW,0,", "A,0x0,y,4:2,RW,0,"], "REG_FIELD_OVERLAP"),
    (["A,0x0,x,0,RW,0,", "B,0x0,y,0,RW,0,"], "REG_WORD_OVERLAP"),
    (["A,0x2,x,0,RW,0,"], "REG_ALIGN"),
    (["A,0x0,x,1:0,RW,5,"], "REG_RESET"),
    (["A,0x0,x,0,RX,0,"], "REG_ACCESS"),
    (["A,0x0,x,32:0,RW,0,"], "REG_BITS"),
    (["A[i],0x100+0x10*i,x,0,RW,0,", "B,0x130,y,0,RW,0,"], "REG_WORD_OVERLAP"),  # A[3]이 0x130
    (["A,0x1000,x,0,RW,0,"], "REG_ADDR_RANGE"),
])
def test_regmap_errors(tmp_path, rows, code):
    fields, issues = reg_parse(_write(tmp_path, rows))
    issues += reg_check(fields, {"A_DEPTH": 4}, addr_bits=12)
    assert code in {i.code for i in issues}, issues


def test_regmap_xlsx_and_doc_table(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Register", "Offset", "Field", "Bits", "Access", "Reset", "Description"])
    ws.append(["CTRL", "0x000", "enable", "0", "RW", "0", "x"])
    ws.append(["CTRL", "0x000", "mode", "2:1", "RW", "0", "y"])
    p = tmp_path / "r.xlsx"
    wb.save(p)
    fields, issues = reg_parse(p)
    assert issues == [] and [f.name for f in fields] == ["enable", "mode"]
    doc = parse("# t SPEC\n\n## 5. 기능 요구사항\n\n| 레지스터 | 필드 | 비트 |\n| --- | --- | --- |\n"
                "| CTRL | mode | 3:1 |\n| CTRL | gone | 0 |\n\n- FR-1 (필수) CTRL.enable과 CTRL.speed를 본다.\n")
    msgs = [i.message for i in cross_check(doc, fields)]
    assert any("비트(3:1)" in m for m in msgs) and any("CTRL.gone" in m for m in msgs) and any("CTRL.speed" in m for m in msgs)


# ---------------------------------------------------------------- SPEC 툴 검사
from pipeline.spec.check import run as check_run  # noqa: E402

TEXT = I3C.read_text()


def _codes(text):
    res = check_run(parse(text))
    return res, {(i["severity"], i["code"]) for i in res["issues"]}


def test_i3c_passes_gate_with_review_items():
    res = check_run(load(I3C), REGS, {"DCT": "DAT_DEPTH"})
    assert res["gate"] == "pass"
    review = {i["code"] for i in res["review"]}
    assert {"UNDEFINED", "OPEN_QUESTION", "PROTOCOL_TAG"} <= review
    assert sum(i["code"] == "OPEN_QUESTION" for i in res["review"]) == 7
    assert sum(i["code"] == "UNDEFINED" for i in res["review"]) == 3


@pytest.mark.parametrize("edit, expect", [
    (lambda t: t.replace("## 9. 구현 제약", "## 9. 구현 메모"), ("warning", "SEC_TITLE")),
    (lambda t: t.replace("## 10. 열린 질문", "## 11. 기타"), ("error", "SEC_MISSING")),
    (lambda t: t.replace("- FR-BUS-01 (필수)", "- FR-BUS-00 (필수)"), ("error", "REQ_DUP")),
    (lambda t: t.replace("- FR-BUS-01 (필수)", "- FR-BUS-01"), ("error", "REQ_LEVEL")),
    (lambda t: t.replace("(FR-ARB-01)을 따른다", "(FR-ARB-99)을 따른다"), ("error", "REQ_REF")),
    (lambda t: t.replace("ibi_trunc", "ibi_cut").replace("| ibi_cut |", "| ibi_trunc |"), ("error", "OUTPUT_NO_BEHAVIOR")),
    (lambda t: t.replace("cmd_daa_cnt에", "카운트에"), ("warning", "PORT_UNREFERENCED")),
    (lambda t: t.replace("코너:", "예외:"), ("error", "SCENARIO_MISSING")),
    (lambda t: t.replace("| 기대 버스 순서 | 기대 출력 |", "| 버스 순서 | 출력 |", 1), ("error", "SCENARIO_TABLE")),
    (lambda t: t.replace("| cmd_len | input | logic [7:0] | | |", "| cmd_len | input | logic [7:0 | | |"), ("error", "L1_V3")),
    (lambda t: t.replace("| 도메인 | 클럭 포트 |", "| 영역 | 클럭 포트 |"), ("error", "TABLE_PARSE")),
])
def test_checks_catch_spec_defects(edit, expect):
    res, codes = _codes(edit(TEXT))
    assert expect in codes, sorted(codes)
    if expect[0] == "error":
        assert res["gate"] == "fail"


# ---------------------------------------------------------------- PDF 변환
def _make_pdf(path):
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    titles = list(TEMPLATE.values())
    for p in range(3):
        page = doc.new_page()
        page.insert_text((40, 30), "회사 SPEC 머리말", fontsize=8, fontname="korea")
        y = 70
        for n in range(p * 4 + 1, min(p * 4 + 5, 11)):
            page.insert_text((40, y), f"{n}. {titles[n - 1]}", fontsize=14, fontname="korea")
            page.insert_text((40, y + 20), f"{n}번 섹션의 본문 문장입니다.", fontsize=10, fontname="korea")
            y += 60
        if p == 0:  # 2번 섹션 아래에 선으로 그린 표
            x0, y0, w, h = 40, 320, 120, 20
            for r in range(3):
                for c in range(3):
                    page.draw_rect(pymupdf.Rect(x0 + c * w, y0 + r * h, x0 + (c + 1) * w, y0 + (r + 1) * h), color=(0, 0, 0))
                    page.insert_text((x0 + c * w + 4, y0 + r * h + 14), ["도메인", "clk", "rst_n"][c] if r == 0 else f"v{r}{c}",
                                     fontsize=10, fontname="korea")
    doc.set_toc([[1, f"{n}. {t}", (n - 1) // 4 + 1] for n, t in TEMPLATE.items()])
    doc.save(path)


def test_pdf_conversion_template_headings_and_table(tmp_path):
    from pipeline.spec.pdf import convert

    p = tmp_path / "s.pdf"
    _make_pdf(p)
    r = convert(p)
    rep = r["report"]
    assert rep["template_matched"] == 10 and rep["toc_check"]["ratio"] == 1.0
    assert rep["tables"] == 1 and "| 도메인 | clk | rst_n |" in r["markdown"]
    assert "머리말" not in r["markdown"]  # 쪽마다 반복되는 머리말은 뺌
    assert "## 2. 클럭, 리셋, 전력" in r["markdown"] and "5번 섹션의 본문 문장입니다." in r["markdown"]


@pytest.mark.skipif(not (ROOT / "experiments/specs/MIPI-I3C-Basic-Specification-v1-2-public-edition-er01.pdf").exists(),
                    reason="I3C 원본 PDF는 저장소에 없음 (experiments/specs/)")
def test_i3c_pdf_headings_match_toc():
    from pipeline.spec.pdf import convert

    r = convert(ROOT / "experiments/specs/MIPI-I3C-Basic-Specification-v1-2-public-edition-er01.pdf", range(36, 120))
    assert r["report"]["toc_check"]["ratio"] >= 0.95 and r["report"]["tables"] > 20 and r["report"]["figures"] >= 15


# ---------------------------------------------------------------- 타이밍 다이어그램
import json as _json  # noqa: E402
import sys as _sys  # noqa: E402
from pathlib import Path  # noqa: E402

from pipeline.agents.runner import AgentConfig  # noqa: E402

FAKE = str(ROOT / "tests" / "fake_agent.py")
WAVE = {"signal": [{"name": "clk", "wave": "p...."}, {"name": "req", "wave": "01.0."}]}


def _cfg(wave):
    return AgentConfig("fake", "Test", "fake", [_sys.executable, FAKE, "@{image}"], "m1",
                       env={"FAKE_MODE": "wave", "FAKE_WAVE": _json.dumps(wave)}, vision=True, timeout_s=60)


def test_waveform_convert_cache_and_lint(tmp_path):
    pytest.importorskip("pymupdf")
    from pipeline.spec.waveform import convert

    img = tmp_path / "fig.png"
    img.write_bytes(b"\x89PNG fake image bytes")
    r1 = convert(img, _cfg(WAVE), tmp_path / "cache")
    assert r1["issues"] == [] and not r1["cached"] and Path(r1["render"]).exists()
    r2 = convert(img, _cfg({"signal": []}), tmp_path / "cache")   # 같은 그림·모델이면 에이전트를 부르지 않음
    assert r2["cached"] and r2["wavejson"] == WAVE
    img.write_bytes(b"\x89PNG changed")                            # 그림이 바뀌면 다시 변환
    bad = {"signal": [{"name": "clk", "wave": "p...."}, {"name": "req", "wave": "01"}]}
    r3 = convert(img, _cfg(bad), tmp_path / "cache")
    assert not r3["cached"] and any("사이클 수가 다름" in i for i in r3["issues"])
    assert len(list((tmp_path / "cache").glob("*.json"))) == 1     # 실패한 결과는 캐시하지 않음


def test_waveform_annotations():
    from pipeline.spec.waveform import check_annotations

    spec = load(I3C)
    ok = _json.loads((ROOT / "examples/i3c_basic_controller/figures.json").read_text())
    assert check_annotations(spec, ok) == []
    bad = [{"figure": 1, "tag": "규범", "description": "x", "reqs": []},
           {"figure": 2, "tag": "규범", "description": "x", "reqs": ["FR-BUS-01", "TR-99"]},
           {"figure": 3, "tag": "예시", "description": "", "scenario": "SC-99"},
           {"figure": 4, "tag": "참고", "description": "x"}]
    codes = [(i.severity, i.code, i.where.split(":")[-1]) for i in check_annotations(spec, bad)]
    assert ("error", "WAVE_REQ", "Figure 1") in codes and ("warning", "WAVE_REQ", "Figure 2") in codes
    assert ("error", "WAVE_REQ", "Figure 2") in codes and ("error", "WAVE_DESC", "Figure 3") in codes
    assert ("warning", "WAVE_SCENARIO", "Figure 3") in codes and ("error", "WAVE_TAG", "Figure 4") in codes


# ---------------------------------------------------------------- LLM 검사
def _qcfg(mode, questions=None):
    return AgentConfig("fake", "Test", "fake", [_sys.executable, FAKE], "m1",
                       env={"FAKE_MODE": mode, "FAKE_QUESTIONS": _json.dumps(questions or {})}, timeout_s=60)


def test_llm_check_turns_questions_into_review_items(tmp_path):
    from pipeline.spec.llm_check import run as llm_run

    q = {"items": [
        {"kind": "ambiguous", "refs": ["TR-10"], "question": "20 사이클은 어느 시점부터 세는가?", "why": "시작점이 없음"},
        {"kind": "missing_corner", "refs": [], "question": "TX FIFO가 빈 채로 쓰기 명령이 오래 기다리면?"},
        {"kind": "contradiction", "refs": ["FR-ZZ-01"], "question": "x"},
        {"kind": "style", "question": "y"}]}
    res = llm_run(I3C, _qcfg("questions", q), tmp_path / "w")
    assert [i["code"] for i in res["review"]] == ["LLM_AMBIGUOUS", "LLM_MISSING_CORNER", "LLM_CONTRADICTION"]
    assert res["review"][0]["where"] == f"spec.md:{load(I3C).requirement('TR-10').line}" and res["review"][0]["source"] == "llm"
    assert any("FR-ZZ-01" in p for p in res["problems"]) and any("항목 3" in p for p in res["problems"])
    merged = check_run(load(I3C), llm_review=res["review"])
    assert sum(i["source"] == "llm" for i in merged["review"]) == 3


def test_llm_check_rejects_spec_edits(tmp_path):
    from pipeline.spec.llm_check import run as llm_run

    res = llm_run(I3C, _qcfg("questions_touch_spec"), tmp_path / "w")
    assert res["scope_violations"] == ["수정: spec.md"] and res["problems"]


def _reset_codes(text):
    return [(i["code"], i["message"]) for i in check_run(parse(text))["issues"]
            if i["code"] in ("RESET_SAME_CYCLE", "MULTI_RESET_USAGE")]


def test_sync_reset_same_cycle_phrasing_is_flagged():
    """동기 리셋에서 "rst=1인 동안 출력=0"은 리셋이 걸린 그 사이클을 요구해 레지스터 출력으로 지킬 수 없다 (P8 CRC-E01, P8a COBS-E01)."""
    text = (ROOT / "tests" / "golden" / "axis_cobs_decode" / "block_spec.md").read_text()
    assert _reset_codes(text) == []
    bad = text.replace("rst=1을 본 다음 클럭부터 rst=1인 동안", "rst=1인 동안", 1)
    codes = _reset_codes(bad)
    assert [c for c, _ in codes] == ["RESET_SAME_CYCLE"] and "COBS-E01" in codes[0][1]


def test_multi_clock_reset_usage_condition_is_required():
    """클럭이 다른 리셋 도메인이 둘 이상이면 모든 리셋을 함께 거는 사용 조건 문장이 있어야 한다 (P11 axis_async_fifo)."""
    text = (ROOT / "tests" / "fixtures" / "multiclock" / "mc_top.spec.md").read_text()
    assert [c for c, _ in _reset_codes(text)] == ["MULTI_RESET_USAGE"]
    line = next(ln for ln in text.splitlines() if ln.startswith("- MC-"))
    ok = text.replace(line, line + "\n- MC-90 (필수) a_rst_n과 b_rst_n은 함께, 가장 느린 클럭 4사이클 이상 건다.", 1)
    assert _reset_codes(ok) == []


NORESET_SPEC = """# sh8 SPEC

## 1. 목적과 범위

`sh8`은 리셋이 없는 8비트 오른쪽 시프트 레지스터입니다.

## 2. 클럭, 리셋, 전력

| 도메인 | 클럭 포트 | 주파수 | 리셋 포트 | 리셋 극성 | 리셋 동기 | 도메인 간 관계 | 전력 도메인 | isolation | retention |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sys | clk | 100 MHz | 없음 | – | – | 단일 | 없음 | 없음 | 없음 |

## 3. 인터페이스

| 포트 또는 번들 | 방향 | 타입 | 프로토콜 태그 | 도메인 |
| --- | --- | --- | --- | --- |
| clk | input | logic | – | sys |
| d | input | logic | – | sys |
| q | output | logic [7:0] | – | sys |

## 4. 파라미터

| 이름 | 타입 | 기본값 | 허용 범위 |
| --- | --- | --- | --- |

## 5. 기능 요구사항

- SH-01 (필수) clk 상승 에지마다 q는 {d, q[7:1]}이 된다.
- SH-02 (필수) q의 초기값은 8'h00이다.

## 6. 타이밍과 성능

- SH-T01 (필수) q는 레지스터 출력이다.

## 7. 예외와 코너 케이스

- SH-E01 미정의: 처음 8사이클 동안 d로 채워지지 않은 q의 비트.

## 8. 예시 시나리오

### SC-01 정상: 1 넣기

| 입력 | 기대 출력 |
| --- | --- |
| 사이클 0~7에 d=1 | 사이클 7에 q=8'hFF |

### SC-02 코너: 0 넣기

| 입력 | 기대 출력 |
| --- | --- |
| 사이클 0~7에 d=0 | 사이클 7에 q=8'h00 |

## 9. 구현 제약

- 리셋 포트를 두지 않는다.

## 10. 열린 질문

- 없음
"""


def test_initial_value_without_reset_is_an_error():
    """리셋 없는 도메인의 출력에 초기값을 정하면 어떤 RTL도 만족할 수 없다 (RTLLM right_shifter, square_wave)."""
    res = check_run(parse(NORESET_SPEC))
    hit = [i for i in res["issues"] if i["code"] == "INIT_NO_RESET"]
    assert len(hit) == 1 and "SH-02" in hit[0]["message"] and res["gate"] == "fail"
    ok = check_run(parse(NORESET_SPEC.replace("- SH-02 (필수) q의 초기값은 8'h00이다.\n", "")))
    assert not [i for i in ok["issues"] if i["code"] == "INIT_NO_RESET"] and ok["gate"] == "pass", ok["issues"]


def test_spec_l1_with_inout_passes_gate_without_tristate_block():
    """SPEC 표에서 만든 L1에는 블록이 없으므로 "inout은 tristate 블록 하나가 맡음"(V2)을 SPEC 게이트에서 보지 않는다
    (2026-10-22 od_top: SpecWriter의 맞는 SPEC이 이 규칙으로 실패)."""
    from pipeline.importer import spec_tables
    top = _json.loads((ROOT / "tests" / "fixtures" / "port" / "inout" / "ported" / "od_top.ir.json").read_text())
    text = "# od_top SPEC\n\n" + spec_tables.tables_md(top)
    from pipeline.spec.check import check_l1
    ir, issues = check_l1(parse(text))
    assert ir is not None and not [i for i in issues if i.severity == "error"], [i.message for i in issues]
