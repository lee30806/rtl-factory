"""Python 동작 조각: 번역, reads/writes 계산, 오류, 소스맵, 실행기, Author 언어 게이트."""
import copy
import json

import pytest

from pipeline.agents import author
from pipeline.classifier.classify import classify_findings
from pipeline.emitter.sv import emit
from pipeline.ir.model import Module, load
from pipeline.ir.pyfrag import FragError, Widths, lower_body
from pipeline.ir.pysim import PySim, SimError
from pipeline.validator.checks import validate
from pipeline.verify.run import Finding
from test_pipeline import ROOT

PY = ROOT / "tests" / "fixtures" / "py"


def _raw(name="pkt_parser"):
    return json.loads((PY / f"{name}.ir.json").read_text())


def _blk(raw, bid):
    return next(b for b in raw["blocks"] if b["id"] == bid)


@pytest.mark.parametrize("name", ["pkt_parser", "py_ops"])
def test_py_fixtures_validate(name):
    m = load(PY / f"{name}.ir.json")
    assert m.lang == "py" and not m.lower_errors
    assert validate(m) == []


def test_reads_writes_computed_and_seq_nonblocking():
    m = load(PY / "pkt_parser.ir.json")
    k = next(b for b in m.blocks if b["id"] == "k_cnt")
    assert set(k["writes"]) == {"pkt_valid", "byte_cnt"}
    assert set(k["reads"]) == {"state_q", "s_valid", "s_data", "byte_cnt"}   # HEADER, PAYLOAD은 상수
    assert "<=" in k["body"] and " = " not in k["body"].replace("<=", "")
    fsm = next(b for b in m.blocks if b["kind"] == "fsm")
    assert set(fsm["reads"]) == {"sync_hit", "s_valid", "byte_cnt"}
    assert m.source_raw["blocks"][1]["transitions"][2]["cond"] == "s_valid and byte_cnt == u(8, 1)"   # 원문은 그대로


def test_width_casts_make_widths_explicit():
    wd = Widths.of(_raw("py_ops"))
    sv = lower_body("sum9 = a + b\ninv16 = ~a\nneg8 = -a + K\ncmp8 = (a + b) == 0", "comb", wd).sv.splitlines()
    assert sv[0] == "sum9 = (9'(a) + 9'(b));"            # 문맥 폭으로 확장: 자리올림을 지킴
    assert sv[1] == "inv16 = (~16'(a));"                  # SV처럼 확장한 뒤 반전
    assert sv[2] == "neg8 = 8'(((-32'(a)) + K));"         # int 파라미터 때문에 문맥이 32비트, 대상 폭으로 자름
    assert sv[3] == "cmp8 = ((a + b) == 8'd0);"           # 리터럴은 문맥 폭을 따름


@pytest.mark.parametrize("body,msg", [
    ("sum9 = a / b", "연산자"),
    ("sum9 = a < b < 1", "비교는 하나씩"),
    ("sum9 = foo(a)", "호출할 수 있는 것은"),
    ("sum9 = cat(a, 1)", "cat 안의 정수는 u"),
    ("cmp8 = 2", "폭 1에 맞지 않음"),
    ("while a:\n    pass", "쓸 수 없는 문장"),
    ("sum9 = nope", "모르는 이름"),
    ("K = a", "K에는 쓸 수 없음"),
])
def test_unsupported_python_is_reported_with_line(body, msg):
    with pytest.raises(FragError, match=msg):
        lower_body(body, "comb", Widths.of(_raw("py_ops")))


def test_fragment_error_is_v4_issue_on_the_block():
    raw = _raw()
    _blk(raw, "k_cnt")["body"] = "pkt_valid = False\nbyte_cnt = byte_cnt // 2"
    issues = validate(Module(raw))
    assert [(i.stage, i.node) for i in issues] == [("V4", "blocks/k_cnt")]
    assert "2번째 줄" in issues[0].message


def test_sourcemap_points_to_python_line_and_classifier_reports_it():
    m = load(PY / "pkt_parser.ir.json")
    e = emit(m)
    lines = e.text.splitlines()
    py = [x for x in e.sourcemap if x.get("py_line") == 5 and x["node"] == "blocks/k_cnt/body"]
    assert py and "byte_cnt <= (byte_cnt - 8'd1);" in lines[py[0]["start"] - 1]
    f = Finding("verilator", "WIDTH", "out/pkt_parser.sv", py[0]["start"], "x", severity="warning")
    c = classify_findings([f], {"pkt_parser": e.sourcemap}, {"pkt_parser": "module"})[0]
    assert c.type == "lint_fragment" and c.node == "blocks/k_cnt/body" and "Python 조각 5번째 줄" in c.message


def test_pysim_parses_packet():
    sim = PySim(load(PY / "pkt_parser.ir.json"))
    sim.set({"rst_n": 0})
    sim.step()
    sim.set({"rst_n": 1})
    seen = []
    for v, d in [(1, 0xA5), (1, 2), (1, 7), (1, 8), (0, 0), (0, 0)]:   # 동기 바이트, 길이 2, 페이로드 2바이트
        sim.set({"s_valid": v, "s_data": d})
        sim.step()
        seen.append(sim.outputs()["pkt_valid"])
    assert seen == [0, 0, 0, 1, 0, 0]


def test_pysim_hierarchy_and_unsupported():
    child = _raw()
    top = {"module": "top", "role": "block_top", "domains": child["domains"],
           "ports": [{"name": "i_v", "dir": "input", "type": "logic", "domain": "sys"},
                     {"name": "i_d", "dir": "input", "type": "logic [7:0]", "domain": "sys"},
                     {"name": "o_rdy", "dir": "output", "type": "logic", "domain": "sys"},
                     {"name": "o_pkt", "dir": "output", "type": "logic", "domain": "sys"}],
           "instances": [{"name": "u_p", "module": "pkt_parser", "domain_map": {"sys": "sys"}}],
           "connections": [{"id": "c1", "from": "self.i_v", "to": "u_p.s_valid"},
                           {"id": "c2", "from": "self.i_d", "to": "u_p.s_data"},
                           {"id": "c3", "from": "u_p.s_ready", "to": "self.o_rdy"},
                           {"id": "c4", "from": "u_p.pkt_valid", "to": "self.o_pkt"}]}
    lib = {"pkt_parser": Module(child)}
    sim = PySim(Module(top), lib)
    sim.set({"rst_n": 1, "i_v": 1, "i_d": 0xA5})
    sim.settle()
    assert sim.outputs()["o_rdy"] == 1 and sim.val["u_p.sync_hit"] == 1
    sv = json.loads((ROOT / "tests" / "fixtures" / "pkt_parser.ir.json").read_text())
    with pytest.raises(SimError, match="SV 조각"):
        PySim(Module(sv))


def test_author_gate_checks_language(tmp_path):
    raw = _raw()
    l1 = {k: raw[k] for k in ("module", "role", "params", "domains", "ports")}
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "pkt_parser.ir.json").write_text(json.dumps(raw))
    assert author.check(l1, lang="py")(tmp_path) == []
    assert any("[lang]" in e for e in author.check(l1, lang="sv")(tmp_path))
    sv = copy.deepcopy(raw)
    sv.pop("lang")
    (tmp_path / "out" / "pkt_parser.ir.json").write_text(json.dumps(sv))
    assert any("[lang]" in e for e in author.check(l1, lang="py")(tmp_path))
    assert "Python 조각" in author.make_task("a", l1, ROOT / "tests" / "fixtures" / "pkt_parser.spec.md", lang="py").text


def test_roles_language_per_candidate(tmp_path):
    from pipeline.agents import roles

    cfg = json.loads((ROOT / "config" / "roles.json").read_text())
    cfg["agents"] = str((ROOT / cfg["agents"]).resolve())
    cfg["roles"]["author"]["langs"] = ["sv", "py"]
    p = tmp_path / "roles.json"
    p.write_text(json.dumps(cfg))
    r = roles.load(p)
    assert [r.lang_of(c) for c in range(3)] == ["sv", "py", "sv"]
    assert roles.load(ROOT / "config" / "roles.json").lang_of(1) == "py"   # 2026-10-14 사용자 결정: 후보 하나만 Python


def test_memory_word_access():
    """메모리는 mem[주소]로 한 워드씩: SV 배열 접근으로 옮기고, reads/writes에 메모리 이름이 들어간다. 실행기도 같은 의미."""
    m = load(PY / "py_ram.ir.json")
    assert not m.lower_errors and validate(m) == []
    k = {b["id"]: b for b in m.blocks}
    assert k["k_write"]["body"].splitlines()[1].strip() == "mem[waddr] <= word;" and "mem" in k["k_write"]["writes"]
    assert "mem" in k["k_read"]["reads"]
    sim = PySim(m)
    sim.set({"rst": 1})
    sim.step()
    sim.set({"rst": 0, "we": 1, "waddr": 5, "wdata": 0x10})
    sim.step()
    sim.set({"we": 0, "re": 1, "raddr": 5})
    sim.step()
    assert sim.outputs()["rdata"] == (0x10 ^ 0x5A) + 1 and sim.outputs()["rhi"] == ((0x10 ^ 0x5A) + 1) >> 4
    sim.set({"raddr": 6})   # 쓴 적 없는 워드는 0
    sim.step()
    assert sim.outputs()["rdata"] == 1


@pytest.mark.parametrize("body,msg", [
    ("if we:\n    mem[waddr][3] = 1", "워드의 일부"),
    ("if we:\n    mem[3:0] = word", "한 워드씩"),
    ("if we:\n    mem = word", "한 워드씩"),
])
def test_memory_misuse_is_reported(body, msg):
    raw = _raw("py_ram")
    _blk(raw, "k_write")["body"] = body
    issues = validate(Module(raw))
    assert issues and msg in issues[0].message


def test_memory_write_only_in_seq():
    raw = _raw("py_ram")
    _blk(raw, "k_word")["body"] = "mem[waddr] = word"
    assert any("seq 블록에서만" in i.message for i in validate(Module(raw)))


def test_sv_only_width_expression_is_a_fragment_error_not_a_crash():
    """Python 식으로 읽을 수 없는 SV 식을 Python 조각 모듈의 타입에 쓰면 실행기를 죽이지 않고 조각 오류로 낸다 (2026-10-20 axis_async_fifo R6)."""
    raw = {"module": "m", "role": "module", "lang": "py", "params": [{"name": "DEPTH", "type": "int", "default": "16"}],
           "domains": [], "ports": [], "signals": [{"name": "p", "kind": "wire", "type": "logic [DEPTH'(3):0]"}], "blocks": []}
    errs = Module(raw).lower_errors
    assert errs and "local 파라미터" in errs[0][1]


def test_clog2_in_types_is_read_symbolically():
    """동결된 L1 포트 타입의 `$clog2(DEPTH)`를 읽는다 (2026-10-22 axis_async_fifo R6: Python 후보가 이 때문에 매번 실패).
    폭은 파라미터 값으로 세고, 방출하는 캐스트에는 `$clog2(DEPTH)`가 그대로 남는다."""
    from pipeline.ir.pyfrag import Lin, _const_expr, clog2, const_int
    assert [clog2(v) for v in (0, 1, 2, 3, 4, 1024, 1025)] == [0, 0, 1, 2, 2, 10, 11]
    assert const_int(_const_expr("$clog2(DEPTH)+1"), {"DEPTH": 1024}) == 11
    w = Lin.of(_const_expr("$clog2(DEPTH)"), {"DEPTH"}, {"DEPTH": 1024}) + Lin(const=1)
    assert w.sv() == "$clog2(DEPTH)+1" and w.value({"DEPTH": 1024}) == 11 and w.value({"DEPTH": 16}) == 5
    raw = {"module": "m", "role": "module", "lang": "py", "params": [{"name": "DEPTH", "type": "int", "default": "16"}],
           "domains": [], "ports": [], "signals": [{"name": "p", "kind": "wire", "type": "logic [$clog2(DEPTH):0]"}], "blocks": []}
    assert Module(raw).lower_errors == []
