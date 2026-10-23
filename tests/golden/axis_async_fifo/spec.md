# axis_async_fifo 요구사항 (FRAME_FIFO=0, pause 사용 설정, 이식 시험용)

- AFIFO-01: s_clk 도메인에서 받은 비트를 같은 순서로 m_clk 도메인에 낸다. tdata, tlast, tid, tdest, tuser를 그대로 옮긴다.
- AFIFO-02: 출력 tkeep은 항상 1이다 (KEEP_ENABLE=0).
- AFIFO-03: FIFO가 넘치지 않는 한 m_status_overflow는 0이다.
- AFIFO-04: 출력은 AXI-Stream valid/ready 규칙을 지킨다.
- AFIFO-05: m_pause_ack가 1인 동안 출력 valid는 0이다 (FRAME_PAUSE=1: 프레임 사이에서만 멈춤).
