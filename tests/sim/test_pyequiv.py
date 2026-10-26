"""번역기 검사: 설계 모델(pysim) 대 방출한 RTL. 손으로 쓴 SV 모듈과도 대조하고, 틀린 RTL을 잡는지 본다."""
import json
import shutil

import pytest

pytest.importorskip("cocotb_tools.runner")
if not shutil.which("verilator"):
    pytest.skip("verilator 없음", allow_module_level=True)

from pipeline.emitter.sv import emit  # noqa: E402
from pipeline.ir.model import Module, load  # noqa: E402
from pipeline.verify.pyequiv import check  # noqa: E402
from test_pipeline import ROOT  # noqa: E402

PY = ROOT / "tests" / "fixtures" / "py"


def _emit(tmp_path, m, text=None, lib=None):
    f = tmp_path / f"{m.name}.sv"
    f.write_text(text if text is not None else emit(m, lib).text)
    return f


@pytest.mark.parametrize("name", ["pkt_parser", "py_ops", "py_ram"])
def test_translation_matches_model(tmp_path, name):
    m = load(PY / f"{name}.ir.json")
    res = check(name, {name: m}, [_emit(tmp_path, m)], tmp_path / "b", cycles=1500)
    assert res["ok"] and res["mismatch"] is None


def test_model_matches_hand_written_sv_module(tmp_path):
    """같은 동작을 SV 조각으로 쓴 모듈(기존 픽스처)의 RTL과 Python 조각 모듈의 설계 모델이 같다."""
    py = load(PY / "pkt_parser.ir.json")
    sv = Module(json.loads((ROOT / "tests" / "fixtures" / "pkt_parser.ir.json").read_text()))
    res = check("pkt_parser", {"pkt_parser": py}, [_emit(tmp_path, sv)], tmp_path / "b", cycles=1500)
    assert res["ok"]


def test_wrong_rtl_is_caught(tmp_path):
    m = load(PY / "py_ops.ir.json")
    text = emit(m).text
    assert "(acc - {b, a})" in text
    res = check("py_ops", {"py_ops": m}, [_emit(tmp_path, m, text.replace("(acc - {b, a})", "(acc + {b, a})"))],
                tmp_path / "b", cycles=500)
    assert not res["ok"] and res["mismatch"]["output"] == "acc"


def test_parameterized_module_with_overridden_params(tmp_path):
    """폭이 파라미터 식인 캐스트: 기본값이 아닌 파라미터로 인스턴스해도 설계 모델과 RTL이 같고, 손으로 쓴 SV 판과도 같다."""
    core = load(PY / "axis_adapter.ir.json")
    over = {"S_DATA_WIDTH": "64", "S_KEEP_WIDTH": "8", "M_DATA_WIDTH": "16", "M_KEEP_ENABLE": "1", "M_KEEP_WIDTH": "2",
            "ID_WIDTH": "4", "DEST_WIDTH": "5", "USER_WIDTH": "3"}

    def sub(t):
        for k, v in over.items():
            t = t.replace(k, v)
        return t
    ports = [{**{k: v for k, v in p.items() if k != "signals"},
              "signals": [{k: (sub(v) if k == "type" else v) for k, v in s.items() if k not in ("kind", "reset")}
                          for s in p["signals"]]} for p in core.source_raw["ports"]]
    wrap = Module({"module": "wrap", "role": "block_top", "params": [], "domains": core.raw["domains"], "ports": ports,
                   "instances": [{"name": "u", "module": "axis_adapter", "params": over, "domain_map": {"sys": "sys"}}],
                   "connections": [{"id": "c_s", "from": "self.s_axis", "to": "u.s_axis"},
                                   {"id": "c_m", "from": "u.m_axis", "to": "self.m_axis"}]})
    lib = {"wrap": wrap, "axis_adapter": core}
    files = [_emit(tmp_path, wrap, lib=lib), _emit(tmp_path, core)]
    assert "(~(S_KEEP_WIDTH)'(0))" in files[1].read_text()
    assert check("wrap", lib, files, tmp_path / "b", cycles=1500)["ok"]
    golden = Module(json.loads((ROOT / "tests" / "golden" / "axis_adapter" / "ir" / "axis_adapter.ir.json").read_text()))
    (tmp_path / "g").mkdir()
    files[1] = _emit(tmp_path / "g", golden)   # SV 조각으로 쓴 원본
    assert check("wrap", lib, files, tmp_path / "b2", cycles=1500)["ok"]


def test_wrong_memory_rtl_is_caught(tmp_path):
    m = load(PY / "py_ram.ir.json")
    text = emit(m).text
    assert "mem[waddr] <= word;" in text
    res = check("py_ram", {"py_ram": m}, [_emit(tmp_path, m, text.replace("mem[waddr] <= word;", "mem[waddr] <= wdata;"))],
                tmp_path / "b", cycles=1500)
    assert not res["ok"] and res["mismatch"]["output"] in ("rdata", "rhi")


def test_wire_between_instances_uses_parent_names(tmp_path):
    """인스턴스끼리 잇는 wire의 타입에 자식 파라미터(DW)를 그대로 쓰면 부모에서 정의되지 않는다 (P8a async_fifo).
    인스턴스가 준 값으로 바꿔 선언하고, 설계 모델과 RTL이 같다."""
    ram = load(PY / "py_ram.ir.json")
    sys_ = ram.raw["domains"]
    ports = [{"name": n, "dir": "input", "type": t, "domain": "sys"} for n, t in
             (("we", "logic"), ("waddr", "logic [2:0]"), ("wdata", "logic [W-1:0]"), ("re", "logic"), ("raddr", "logic [2:0]"))]
    ports.append({"name": "out", "dir": "output", "type": "logic [W-1:0]", "domain": "sys"})
    inst = lambda n: {"name": n, "module": "py_ram", "params": {"DW": "W", "AW": "3"}, "domain_map": {"sys": "sys"}}  # noqa: E731
    conns = [{"id": f"c_{p}_{i}", "from": f"self.{p}", "to": f"{i}.{p}"} for i in ("u_a", "u_b")
             for p in ("we", "waddr", "re", "raddr")]
    conns += [{"id": "c_in", "from": "self.wdata", "to": "u_a.wdata"}, {"id": "c_mid", "from": "u_a.rdata", "to": "u_b.wdata"},
              {"id": "c_out", "from": "u_b.rdata", "to": "self.out"}]
    top = Module({"module": "ram2", "role": "block_top", "params": [{"name": "W", "type": "int", "default": "8"}],
                  "domains": sys_, "ports": ports, "instances": [inst("u_a"), inst("u_b")], "connections": conns})
    lib = {"ram2": top, "py_ram": ram}
    files = [_emit(tmp_path, top, lib=lib), _emit(tmp_path, ram)]
    text = files[0].read_text()
    assert "logic [(W)-1:0] u_a_rdata;" in text and "[DW" not in text
    assert check("ram2", lib, files, tmp_path / "b", cycles=1000)["ok"]


def test_instance_output_to_parent_port_and_sibling(tmp_path):
    """인스턴스 출력이 부모 포트와 다른 인스턴스로 함께 나가면 둘 다 같은 넷을 읽는다 (전에는 형제 쪽이 구동 없는 wire)."""
    dom = [{"name": "sys", "clock": "clk", "edge": "pos", "reset": {"port": "rst", "active": "high", "async": False}}]
    thru = Module({"module": "thru", "role": "module", "lang": "py", "params": [], "domains": dom,
                   "ports": [{"name": "i", "dir": "input", "type": "logic [3:0]", "domain": "sys"},
                             {"name": "o", "dir": "output", "type": "logic [3:0]", "domain": "sys", "kind": "wire"}],
                   "blocks": [{"id": "k", "kind": "comb", "body": "o = i + 1"}]})
    top = Module({"module": "fan", "role": "block_top", "params": [], "domains": dom,
                  "ports": [{"name": "x", "dir": "input", "type": "logic [3:0]", "domain": "sys"},
                            {"name": "y", "dir": "output", "type": "logic [3:0]", "domain": "sys"},
                            {"name": "z", "dir": "output", "type": "logic [3:0]", "domain": "sys"}],
                  "instances": [{"name": "u_a", "module": "thru", "domain_map": {"sys": "sys"}},
                                {"name": "u_b", "module": "thru", "domain_map": {"sys": "sys"}}],
                  "connections": [{"id": "c0", "from": "self.x", "to": "u_a.i"}, {"id": "c1", "from": "u_a.o", "to": "self.y"},
                                  {"id": "c2", "from": "u_a.o", "to": "u_b.i"}, {"id": "c3", "from": "u_b.o", "to": "self.z"}]})
    lib = {"fan": top, "thru": thru}
    files = [_emit(tmp_path, top, lib=lib), _emit(tmp_path, thru)]
    assert "u_a_o" not in files[0].read_text()
    assert check("fan", lib, files, tmp_path / "b", cycles=200)["ok"]


def test_async_reset_with_nonzero_value_from_time_zero(tmp_path):
    """비동기 리셋값이 0이 아닌 블록: 처음 거는 리셋이 에지가 되어 RTL도 리셋값을 낸다 (RTLLM traffic_light, freq_divbyodd)."""
    m = Module({
        "module": "rst10", "role": "module", "lang": "py",
        "domains": [{"name": "sys", "clock": "clk", "edge": "pos",
                     "reset": {"port": "rst_n", "active": "low", "async": True}}],
        "ports": [{"name": "dec", "dir": "input", "type": "logic", "domain": "sys"},
                  {"name": "cnt", "dir": "output", "type": "logic [7:0]", "domain": "sys", "kind": "reg",
                   "reset": "8'd10"}],
        "blocks": [{"id": "k_cnt", "kind": "seq", "domain": "sys", "body": "if dec:\n    cnt = cnt - 1"}],
    })
    res = check("rst10", {"rst10": m}, [_emit(tmp_path, m)], tmp_path / "b", cycles=300)
    assert res["ok"], res["mismatch"]
