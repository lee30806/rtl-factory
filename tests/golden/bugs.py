"""golden 블록 버그 주입 (P2 불일치 리포트 시험, P4 Repair 평가). 이름 → (블록, IR을 고치는 함수, 원인 노드)."""


def _blk(raw, bid):
    return next(b for b in raw["blocks"] if b["id"] == bid)


def _tr(raw, bid, tid):
    return next(t for t in _blk(raw, bid)["transitions"] if t["id"] == tid)


BUGS = {
    # FSM 전이 조건: 세그먼트 길이를 하나 틀리게 셈
    "cobs_transition": ("axis_cobs_decode",
                        lambda r: _tr(r, "k_fsm", "t_seg_next").update(cond=_tr(r, "k_fsm", "t_seg_next")["cond"].replace("8'd1", "8'd2")),
                        "blocks/k_fsm/transitions/t_seg_next"),
    # datapath: 출력 바이트를 잘못된 레인에서 꺼냄
    "adapter_shift": ("axis_adapter",
                      lambda r: _blk(r, "k_data").update(body=_blk(r, "k_data")["body"].replace(
                          "s_axis_tdata_reg <= s_axis_tdata_reg >> SEG_DATA_WIDTH;", "s_axis_tdata_reg <= s_axis_tdata_reg >> (SEG_DATA_WIDTH * 2);")),
                      "blocks/k_data"),
    # 다중 클럭: 출력 필드 위치를 틀림
    "fifo_slice": ("axis_async_fifo",
                   lambda r: _blk(r, "k_m_comb").update(body=_blk(r, "k_m_comb")["body"].replace(
                       "m_axis_tid = m_axis_pipe_reg1[ID_OFFSET +: ID_WIDTH];", "m_axis_tid = m_axis_pipe_reg1[ID_OFFSET+1 +: ID_WIDTH];")),
                   "blocks/k_m_comb"),
}
