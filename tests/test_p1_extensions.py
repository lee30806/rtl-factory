"""이식 시험(P1)에서 추가한 표현: 연결 왼쪽의 쓰기, gray 동기화, 추론형 메모리, 동기화 셀 리셋 극성."""
import copy
import json

import pytest

from pipeline.emitter.sv import EmitError, emit
from pipeline.ir.model import Module
from test_pipeline import ROOT

FIFO = json.loads((ROOT / "tests/golden/axis_async_fifo/ir/axis_async_fifo.ir.json").read_text())
TX = json.loads((ROOT / "tests/golden/uart/ir/uart_tx.ir.json").read_text())


def _v(raw):
    from pipeline.validator.checks import validate
    return [str(i) for i in validate(Module(raw), {})]


def _blk(raw, bid):
    return next(b for b in raw["blocks"] if b["id"] == bid)


def test_golden_irs_are_clean():
    assert _v(FIFO) == [] and _v(TX) == []


def test_concat_lhs_members_are_writes():
    raw = copy.deepcopy(TX)
    k = _blk(raw, "k_tx")
    k["writes"].remove("txd")
    assert any("선언 밖 쓰기: ['txd']" in i for i in _v(raw))


@pytest.mark.parametrize("edit, expect", [
    (lambda r: _blk(r, "c_wr_gray").update(style="2ff"), "2ff는 1비트만"),
    (lambda r: _blk(r, "c_wr_gray").update(dst="s_rst_sync3_reg", dst_domain="s"), "gray 동기화의 src와 dst 타입이 다름"),
    (lambda r: _blk(r, "k_mem_wr").update(domain="m"), "메모리는 쓰기 포트 도메인"),
    (lambda r: _blk(r, "k_mem")["ports"].__setitem__(1, {"name": "rd", "kind": "read", "domain": "s"}), "메모리는 읽기 포트 도메인"),
])
def test_cdc_and_memory_rules(edit, expect):
    raw = copy.deepcopy(FIFO)
    edit(raw)
    assert any(expect in i for i in _v(raw)), _v(raw)


def test_emitter_memory_gray_and_reset_polarity():
    text = emit(Module(FIFO)).text
    assert "logic [WIDTH-1:0] mem [2**ADDR_WIDTH];" in text
    assert "sync_2ff #(.WIDTH($bits(wr_ptr_gray_sync2_reg)), .ASYNC_RST(1'b0)) u_c_wr_gray (.clk(m_clk), .rst_n(!m_rst)," in text
    assert "sync_2ff #(.ASYNC_RST(1'b0)) u_c_s_rst (.clk(s_clk), .rst_n(!s_rst), .d(m_side_ready_reg), .q(m_side_ready_sync));" in text
    raw = copy.deepcopy(FIFO)
    _blk(raw, "k_mem")["impl"] = "macro"
    with pytest.raises(EmitError):
        emit(Module(raw))
