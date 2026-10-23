"""P5 환류 입력: 양식 파일 기반 리포트 파서와 SPEC 개정 영향."""
import json
import shutil
import subprocess

import pytest

from pipeline.feedback import reports, spec_diff
from test_pipeline import ROOT

SPEC = ROOT / "tests" / "golden" / "axis_adapter" / "block_spec.md"


def test_all_format_files_are_valid():
    fmts = reports.formats()
    assert {"verilator", "iverilog"} <= set(fmts)
    for name, f in fmts.items():
        assert reports.check_format(f) == [], name


def test_verilator_format_matches_pipeline_parser():
    from pipeline.verify.run import parse_verilator
    text = ("%Warning-UNUSEDSIGNAL: rtl/a.sv:12:5: Signal is not used: 'x'\n"
            "%Error: rtl/a.sv:3:1: syntax error\n%Error: Exiting due to 1 error(s)\n")
    assert reports.parse_named("verilator", text) == parse_verilator(text)


@pytest.mark.skipif(not shutil.which("iverilog"), reason="iverilog 없음")
def test_iverilog_format_on_real_output(tmp_path):
    src = tmp_path / "t.sv"
    src.write_text("module t(input a, output b);\n  assign b = c;\nendmodule\n")
    p = subprocess.run(["iverilog", "-g2012", "-o", "/dev/null", str(src)], capture_output=True, text=True)
    fs = reports.parse_named("iverilog", p.stdout + p.stderr)
    assert fs and all(f.file == str(src) and f.line == 2 and f.severity == "error" for f in fs)


def test_custom_format_continuation_ignore_and_errors():
    fmt = {"tool": "superlint", "patterns": [{"regex": r"^(?P<severity>E|W) \[(?P<rule>\w+)\] (?P<file>\S+):(?P<line>\d+) (?P<message>.*)$"}],
           "continuation": r"^\s{4}\S", "ignore": [r"^Summary"], "severity_map": {"E": "error", "W": "warning"}}
    fs = reports.parse(fmt, "E [W123] a.sv:7 width mismatch\n    on signal x\nSummary: 1 error\nW [L9] b.sv:2 unused\n")
    assert [(f.rule, f.file, f.line, f.severity) for f in fs] == [("W123", "a.sv", 7, "error"), ("L9", "b.sv", 2, "warning")]
    assert fs[0].message == "width mismatch on signal x"
    bad = {"tool": "x", "patterns": [{"regex": r"(?P<where>\d+)"}]}
    assert any("모르는 그룹" in e for e in reports.check_format(bad))
    with pytest.raises(reports.FormatError):
        reports.parse(bad, "1")
    with pytest.raises(reports.FormatError):
        reports.parse_named("no_such_format", "")


def test_spec_diff(tmp_path):
    text = SPEC.read_text()
    line = next(ln for ln in text.splitlines() if ln.lstrip().startswith("- ADP-01"))
    new = tmp_path / "new.md"
    new.write_text(text.replace(line, line + " 문장 추가.", 1))
    d = spec_diff.diff(SPEC, new)
    assert d["changed"] == ["ADP-01"] and not d["added"] and not d["removed"] and not d["l1_changed"]
    assert spec_diff.affected_reqs(d) == ["ADP-01"]
    # 포트 표(인터페이스)를 바꾸면 L1이 바뀐 것: 처음부터 다시 실행
    port = next(ln for ln in text.splitlines() if ln.startswith("|") and "s_axis_tdata" in ln)
    new.write_text(text.replace(port, port.replace("s_axis_tdata", "s_axis_tdata2"), 1))
    assert spec_diff.diff(SPEC, new)["l1_changed"]


def test_spec_diff_timing_exception_and_constraints(tmp_path):
    """6, 7번 섹션의 문자 섞인 ID(ADP-T01, ADP-E01)도 요구사항이고, 9번 섹션(구현 제약)이 바뀌면 처음부터 다시 실행한다."""
    text = SPEC.read_text()
    new = tmp_path / "new.md"
    t01 = next(ln for ln in text.splitlines() if ln.startswith("- ADP-T01"))
    e01 = next(ln for ln in text.splitlines() if ln.startswith("- ADP-E01"))
    new.write_text(text.replace(t01, t01 + " 문장 추가.").replace(e01, e01 + " 문장 추가."))
    d = spec_diff.diff(SPEC, new)
    assert d["changed"] == ["ADP-E01", "ADP-T01"] and not d["constraints_changed"] and not spec_diff.full_rerun(d)
    new.write_text(text.replace("### 필수 구조\n", "### 필수 구조\n- 출력은 레지스터에서 낸다.\n", 1))
    d = spec_diff.diff(SPEC, new)
    assert d["constraints_changed"] and not d["l1_changed"] and spec_diff.full_rerun(d)


def test_feedback_schema_rejects_unknown_form():
    from jsonschema import Draft202012Validator
    schema = json.loads((ROOT / "schema" / "feedback.schema.json").read_text())
    v = Draft202012Validator(schema)
    assert not list(v.iter_errors({"form": "generator_bug", "by": "a", "description": "d"}))
    assert list(v.iter_errors({"form": "rewrite_rtl", "by": "a"}))
    assert list(v.iter_errors({"form": "config_change", "by": "a", "config": {}}))   # 빈 설정
