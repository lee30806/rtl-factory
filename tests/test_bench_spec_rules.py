"""벤치 SPEC의 원문 보존 규칙: 원문 부록, 출력 미정의 금지, 원문 커버리지, 이름 붙은 레지스터, 디코드 표의 포트 참조."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.bench.common.to_spec import (  # noqa: E402
    APPENDIX_HEAD, OUT, bench_spec_check, coverage_errors, named_registers, prompt_coverage, strip_prompt_appendix,
    undefined_output_errors, with_prompt_appendix,
)
from pipeline.spec.check import run as check_run  # noqa: E402
from pipeline.spec.parse import parse  # noqa: E402

HEAD = """# dec SPEC

## 1. 목적과 범위
x

## 2. 클럭, 리셋, 전력

| 도메인 | 클럭 포트 | 주파수 | 리셋 포트 | 리셋 극성 | 리셋 동기 | 도메인 간 관계 | 전력 도메인 | isolation | retention |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sys | clk | 100 MHz | rst_n | low | 비동기 | – | 없음 | 없음 | 없음 |

## 3. 인터페이스

| 포트 또는 번들 | 방향 | 타입 | 프로토콜 태그 | 도메인 |
| --- | --- | --- | --- | --- |
| adr | input | logic [1:0] | – | sys |
| dout | output | logic [7:0] | – | sys |
| busy | output | logic | – | sys |

## 4. 파라미터

| 이름 | 타입 | 기본값 | 허용 범위 |
| --- | --- | --- | --- |
"""

TAIL = """
## 6. 타이밍과 성능

- TIM-01 (필수) busy는 adr이 바뀐 다음 clk 에지에 갱신된다.

## 7. 예외와 코너 케이스

- EXC-01 (필수) rst_n=0인 동안 busy는 0이다.
{exc}
## 8. 예시 시나리오

### SC-01 정상: 읽기

| 입력 | 기대 출력 |
| --- | --- |
| adr=0 | dout=1 |

### SC-02 코너: 리셋

| 입력 | 기대 출력 |
| --- | --- |
| rst_n=0 | busy=0 |

## 9. 구현 제약

### 필수 구조
- 없음

### 금지 사항
- 의도적 래치

## 10. 열린 질문

- 없음
"""

SEC5_TABLE = """
## 5. 기능 요구사항

- FR-RD-01 (필수) adr에 따른 읽기 데이터는 아래 표를 따른다.

| 조건 | 출력 |
| --- | --- |
| adr=0 | dout=8'h01 |
| adr=1 | dout=8'h02 |
| 그 외 | dout=0 |
"""

TOP_L1 = {"module": "dec", "ports": [{"name": "adr", "dir": "input", "type": "logic [1:0]"},
                                     {"name": "dout", "dir": "output", "type": "logic [7:0]"},
                                     {"name": "busy", "dir": "output", "type": "logic"}]}


def test_decode_table_cells_count_as_port_references():
    spec = HEAD + SEC5_TABLE + TAIL.format(exc="")
    res = check_run(parse(spec))
    codes = {i["code"] for i in res["issues"] if i["severity"] == "error"}
    assert "OUTPUT_NO_BEHAVIOR" not in codes, res["issues"]
    assert "REQ_MULTI" not in {i["code"] for i in res["issues"]}


def test_appendix_is_not_parsed_and_is_idempotent():
    spec = HEAD + SEC5_TABLE + TAIL.format(exc="")
    prompt = "## 5. Fake section\n- FR-X-99 (필수) 가짜 요구사항이다.\n| a | b |\n| --- | --- |\n| 1 | 2 |\n- Q-77 가짜 질문\n"
    once = with_prompt_appendix(spec, prompt)
    twice = with_prompt_appendix(once, prompt)
    assert once == twice and once.count(APPENDIX_HEAD) == 1 and prompt.strip() in once
    s = parse(once)
    assert "FR-X-99" not in {r.id for r in s.requirements} and not any(q["id"] == "Q-77" for q in s.questions)
    assert len(s.sections) == 10 and strip_prompt_appendix(once).rstrip() == spec.rstrip()
    # 부록이 붙은 SPEC도 검사기를 그대로 통과한다
    assert check_run(s)["gate"] == check_run(parse(spec))["gate"]


def test_undefined_output_is_an_error_except_reset_and_initial():
    spec = HEAD + SEC5_TABLE + TAIL.format(
        exc="- EXC-02 미정의: adr이 3일 때의 dout 값\n- EXC-03 미정의: 리셋 전 busy의 첫 값\n- EXC-04 미정의: 내부 카운터의 폭\n")
    errs = undefined_output_errors(spec, TOP_L1)
    assert len(errs) == 1 and "EXC-02" in errs[0] and "dout" in errs[0]


def test_prompt_coverage_and_named_registers():
    prompt = ("sum_2 and exponent_3 feed sign_20; wb_dat_o[7:0] reads prer[15:8] at 0x01; ctr[7] is EN. "
              "The flag 1'b1 is ignored. data_in[31:0] is the input bus. sr[4:2] are reserved. "
              "Signals a_is_zero, b_is_inf, count_ready_0 gate the stages.")
    assert named_registers(prompt) == ["count_ready_0", "exponent_3", "sign_20", "sum_2"]
    spec = "# t SPEC\n- REQ-01 (필수) wb_dat_o는 prer[15:8]과 같다.\n- REQ-02 (필수) data_in는 입력이다.\n"
    cov = prompt_coverage(prompt, spec, "t")
    assert "1'b1" not in cov["missing"] and "data_in[31:0]" not in cov["missing"]   # 1비트 리터럴 제외, 전체 폭 표기는 이름으로 인정
    assert {"sum_2", "0x01", "ctr[7]", "sr[4:2]", "a_is_zero"} <= set(cov["missing"]) and cov["ratio"] < 0.8
    assert coverage_errors(prompt, spec, "t") and "PROMPT_COVERAGE" in coverage_errors(prompt, spec, "t")[0]
    assert coverage_errors(prompt, with_prompt_appendix(spec, prompt), "t")   # 부록은 커버리지로 치지 않는다
    assert coverage_errors("short a_b", spec, "t") == []                         # 토큰이 적으면 검사하지 않는다


def test_bench_spec_check_reports_coverage_once_and_undefined_always(tmp_path: Path):
    prompt = " ".join(f"reg_{i}[7:0] holds field_{i}" for i in range(20))
    spec = HEAD + SEC5_TABLE + TAIL.format(exc="- EXC-02 미정의: adr이 3일 때의 dout 값\n")
    (tmp_path / "out").mkdir()
    (tmp_path / OUT).write_text(spec, encoding="utf-8")
    chk = bench_spec_check(TOP_L1, prompt, "dec")
    first = chk(tmp_path)
    second = chk(tmp_path)
    assert any("PROMPT_COVERAGE" in e for e in first) and not any("PROMPT_COVERAGE" in e for e in second)
    assert any("UNDEFINED_OUTPUT" in e for e in first) and any("UNDEFINED_OUTPUT" in e for e in second)


def test_every_spec_reader_gets_the_reading_guide():
    """spec.md를 입력으로 받는 모든 역할 과제가 docs/SPEC 읽기 안내.md를 spec_guide.md로 함께 받는다."""
    from pipeline.agents import arbiter, architect, author, repair, sim_model, verifier
    from pipeline.agents.task import SPEC_GUIDE

    assert SPEC_GUIDE.exists() and "## 10. 열린 질문" in SPEC_GUIDE.read_text(encoding="utf-8")
    l1 = {"module": "m", "role": "block_top", "params": [], "domains": [], "ports": [{"name": "a", "dir": "input", "type": "logic"},
                                                                                     {"name": "y", "dir": "output", "type": "logic"}]}
    spec = Path("spec.md")
    tasks = [author.make_task("a", l1, spec), architect.make_task("b", l1, spec), verifier.make_task("v", l1, spec),
             repair.make_task("r", {"module": "m"}, spec, {}, []), arbiter.make_task("x", spec, {}, Path("t.py")),
             arbiter.make_r6_task("x6", spec, {}) if hasattr(arbiter, "make_r6_task") else None,
             sim_model.author_task("s", "m", spec, "{}"), sim_model.verifier_task("sv", "m", spec, "{}")]
    for t in filter(None, tasks):
        assert t.inputs.get("spec_guide.md") == SPEC_GUIDE, t.name
        assert "spec_guide.md" in t.text, t.name
