# axis_async_fifo SPEC

Oct 11, 2026 · 이식 시험 블록의 Block Top SPEC (원본: verilog-axis `axis_async_fifo`, MIT). DATA_WIDTH=8, 프레임 FIFO 끔, pause 사용 설정

## 1. 목적과 범위

`axis_async_fifo`는 s_clk 도메인의 AXI-Stream을 받아 m_clk 도메인으로 같은 순서로 넘기는 비동기 FIFO입니다. 두 도메인의 리셋을 따로 받고, 한쪽이 리셋되면 진행 중인 프레임을 정리합니다. 출력을 멈추는 pause 요청을 양쪽에서 받습니다.

범위 밖 항목은 프레임 FIFO 모드(프레임 단위 저장, 넘치는 프레임 버림), 출력 FIFO, tkeep 전달입니다.

## 2. 클럭, 리셋, 전력

| 도메인 | 클럭 포트 | 주파수 | 리셋 포트 | 리셋 극성 | 리셋 동기 | 도메인 간 관계 | 전력 도메인 | isolation | retention |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| s | s_clk | 100 MHz | s_rst | high | 동기 | m과 비동기 | 없음 | 없음 | 없음 |
| m | m_clk | 90.9 MHz | m_rst | high | 동기 | s와 비동기 | 없음 | 없음 | 없음 |

## 3. 인터페이스

| 포트 또는 번들 | 방향 | 타입 | 프로토콜 태그 | 도메인 |
| --- | --- | --- | --- | --- |
| s_clk | input | logic | – | s |
| s_rst | input | logic | – | s |
| m_clk | input | logic | – | m |
| m_rst | input | logic | – | m |
| **s_axis** (sink) | | | valid_ready | s |
| s_axis_tdata | input | logic [DATA_WIDTH-1:0] | | |
| s_axis_tkeep | input | logic [KEEP_WIDTH-1:0] | | |
| s_axis_tvalid | input | logic | | |
| s_axis_tready | output | logic | | |
| s_axis_tlast | input | logic | | |
| s_axis_tid | input | logic [ID_WIDTH-1:0] | | |
| s_axis_tdest | input | logic [DEST_WIDTH-1:0] | | |
| s_axis_tuser | input | logic [USER_WIDTH-1:0] | | |
| **m_axis** (source) | | | valid_ready | m |
| m_axis_tdata | output | logic [DATA_WIDTH-1:0] | | |
| m_axis_tkeep | output | logic [KEEP_WIDTH-1:0] | | |
| m_axis_tvalid | output | logic | | |
| m_axis_tready | input | logic | | |
| m_axis_tlast | output | logic | | |
| m_axis_tid | output | logic [ID_WIDTH-1:0] | | |
| m_axis_tdest | output | logic [DEST_WIDTH-1:0] | | |
| m_axis_tuser | output | logic [USER_WIDTH-1:0] | | |
| s_pause_req | input | logic | – | s |
| s_pause_ack | output | logic | – | s |
| m_pause_req | input | logic | – | m |
| m_pause_ack | output | logic | – | m |
| s_status_depth | output | logic [10:0] | – | s |
| s_status_depth_commit | output | logic [10:0] | – | s |
| s_status_overflow | output | logic | – | s |
| s_status_bad_frame | output | logic | – | s |
| s_status_good_frame | output | logic | – | s |
| m_status_depth | output | logic [10:0] | – | m |
| m_status_depth_commit | output | logic [10:0] | – | m |
| m_status_overflow | output | logic | – | m |
| m_status_bad_frame | output | logic | – | m |
| m_status_good_frame | output | logic | – | m |

## 4. 파라미터

원본과 같은 이름의 파라미터를 둡니다. 테스트벤치가 DEPTH, FRAME_FIFO, DROP_BAD_FRAME, DROP_WHEN_FULL, MARK_WHEN_FULL, DROP_OVERSIZE_FRAME 값을 읽습니다.

| 이름 | 타입 | 기본값 | 허용 범위 |
| --- | --- | --- | --- |
| DEPTH | int | 1024 | 1024 |
| DATA_WIDTH | int | 8 | 8 |
| KEEP_ENABLE | int | 0 | 0 |
| KEEP_WIDTH | int | 1 | 1 |
| LAST_ENABLE | int | 1 | 1 |
| ID_ENABLE | int | 1 | 1 |
| ID_WIDTH | int | 8 | 8 |
| DEST_ENABLE | int | 1 | 1 |
| DEST_WIDTH | int | 8 | 8 |
| USER_ENABLE | int | 1 | 1 |
| USER_WIDTH | int | 1 | 1 |
| RAM_PIPELINE | int | 1 | 1 |
| OUTPUT_FIFO_ENABLE | int | 0 | 0 |
| FRAME_FIFO | int | 0 | 0 |
| USER_BAD_FRAME_VALUE | int | 1 | 1 |
| USER_BAD_FRAME_MASK | int | 1 | 1 |
| DROP_OVERSIZE_FRAME | int | 0 | 0 |
| DROP_BAD_FRAME | int | 0 | 0 |
| DROP_WHEN_FULL | int | 0 | 0 |
| MARK_WHEN_FULL | int | 0 | 0 |
| PAUSE_ENABLE | int | 1 | 1 |
| FRAME_PAUSE | int | 1 | 1 |

## 5. 기능 요구사항

- AFIFO-01 (필수) s_axis_tvalid=1이고 s_axis_tready=1인 사이클에 받은 비트를 받은 순서대로 m_axis로 내며, s_axis_tdata, s_axis_tlast, s_axis_tid, s_axis_tdest, s_axis_tuser를 m_axis_tdata, m_axis_tlast, m_axis_tid, m_axis_tdest, m_axis_tuser로 그대로 옮긴다 (최대 1024비트 저장).
- AFIFO-02 (필수) m_axis_tkeep은 항상 1이고 s_axis_tkeep은 쓰지 않는다.
- AFIFO-03 (필수) FIFO가 가득 차 있거나 m 쪽이 리셋 중이면 s_axis_tready=0이다. 그 밖에는 s_axis_tready=1이다.
- AFIFO-04 (필수) 출력은 AXI-Stream valid/ready 규칙을 지킨다. m_axis_tvalid=1이면 m_axis_tready=1을 볼 때까지 m_axis_tvalid와 데이터를 유지한다.
- AFIFO-05 (필수) s 쪽이 리셋되면(s_rst) FIFO 내용을 버린다. 그때 m 쪽이 프레임을 내보내던 중이었으면(그 프레임의 tlast를 아직 내지 않았으면), m_axis_tlast=1, m_axis_tuser=1인 비트 하나를 더 내서 그 프레임을 끝낸다.
- AFIFO-06 (필수) m 쪽이 리셋되면(m_rst) FIFO 내용을 버리고 m_axis_tvalid=0으로 한다. 그때 s 쪽이 프레임을 받던 중이었으면, 그 프레임의 나머지 비트는 tlast를 받을 때까지 받아서(s_axis_tready=1) 버리고, tlast를 받은 사이클 다음에 s_status_overflow를 한 사이클 1로 한다.
- AFIFO-07 (필수) m_pause_req=1이거나 동기화된 s_pause_req=1이면, 내보내던 프레임의 tlast를 낸 뒤 m_axis_tvalid=0으로 출력을 멈추고 m_pause_ack=1로 한다. 요청이 모두 0이 되면 m_pause_ack=0으로 하고 출력을 다시 시작한다. s_pause_ack는 m_pause_ack를 s_clk로 동기화한 값이다.
- AFIFO-08 (필수) s_status_depth는 s 쪽에서 본 FIFO 안의 비트 수, m_status_depth는 m 쪽에서 본 FIFO 안의 비트 수이다. 두 값은 상대 도메인의 포인터를 동기화하는 지연만큼 늦을 수 있다. s_status_depth_commit은 s_status_depth와, m_status_depth_commit은 m_status_depth와 같다.
- AFIFO-09 (필수) m_status_overflow는 s_status_overflow 펄스를 m_clk로 옮긴 한 사이클 펄스이다. s_status_bad_frame, s_status_good_frame, m_status_bad_frame, m_status_good_frame은 항상 0이다.

## 6. 타이밍과 성능

- AFIFO-T01 (필수) 출력이 막히지 않으면 m_clk 한 클럭에 비트 하나를 낸다.
- AFIFO-T02 (권장) 비어 있는 FIFO에 들어온 비트가 m_axis에 나타나기까지는 m_clk 8클럭 이하이다.

## 7. 예외와 코너 케이스

- AFIFO-E01 (필수) 두 쪽이 함께 리셋되면 FIFO는 비고 m_axis_tvalid=0이며, 두 리셋이 모두 풀린 뒤 상대 쪽이 리셋에서 풀린 것을 확인할 때까지 s_axis_tready=0이다.
- AFIFO-E02 (필수) 한쪽 도메인만 먼저 리셋에서 풀려도 받은 데이터를 잃지 않는다. 상대 쪽이 아직 리셋 중이면 입력을 받지 않는다.
- AFIFO-E03 미정의: 리셋 중인 쪽의 상태 출력 값

## 8. 예시 시나리오

### SC-01 정상: 프레임 하나

| 입력 (s_axis) | 기대 출력 (m_axis) |
| --- | --- |
| 3비트 프레임 (01, 02, 03, 마지막에 tlast), tid=5 | 같은 순서의 3비트, 마지막에 tlast, tid=5 |

### SC-02 코너: 프레임 도중 s 쪽 리셋

| 입력 | 기대 출력 |
| --- | --- |
| 256비트 프레임을 보내는 도중 s_rst | 이미 나온 비트들 뒤에 tlast=1, tuser=1인 비트로 프레임이 끝남 |

## 9. 구현 제약

### 필수 구조
- 데이터는 추론형 메모리(`memory`, impl=inferred)에 저장한다. s 쪽이 쓰고 m 쪽이 읽는다.
- 도메인을 넘는 포인터는 gray 코드로 바꾼 뒤 `cdc_sync` style gray로 동기화한다.
- 도메인을 넘는 1비트 신호(리셋 상태, pause 요청과 응답, overflow 토글)는 `cdc_sync` style 2ff로 동기화한다.

### 금지 사항
- 다중 비트 이진 값의 직접 도메인 교차

## 10. 열린 질문

- Q-01 프레임 FIFO 모드를 지원할지
