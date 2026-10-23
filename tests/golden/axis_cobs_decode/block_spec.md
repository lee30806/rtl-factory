# axis_cobs_decode SPEC

Oct 11, 2026 · 이식 시험 블록의 Block Top SPEC (원본: verilog-axis `axis_cobs_decode`, MIT)

## 1. 목적과 범위

`axis_cobs_decode`는 COBS(Consistent Overhead Byte Stuffing)로 인코딩된 AXI-Stream 바이트 프레임을 받아 원래 바이트열로 디코딩해 내보내는 Block Top입니다. 프레임 안의 형식 오류를 찾아 출력 프레임의 tuser로 알립니다.

범위 밖 항목은 COBS 인코딩, 8비트가 아닌 데이터 폭, 프레임 버퍼링입니다.

## 2. 클럭, 리셋, 전력

| 도메인 | 클럭 포트 | 주파수 | 리셋 포트 | 리셋 극성 | 리셋 동기 | 도메인 간 관계 | 전력 도메인 | isolation | retention |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sys | clk | 100 MHz | rst | high | 동기 | 단일 도메인 | 없음 | 없음 | 없음 |

## 3. 인터페이스

| 포트 또는 번들 | 방향 | 타입 | 프로토콜 태그 | 도메인 |
| --- | --- | --- | --- | --- |
| clk | input | logic | – | sys |
| rst | input | logic | – | sys |
| **s_axis** (sink) | | | valid_ready | sys |
| s_axis_tdata | input | logic [7:0] | | |
| s_axis_tvalid | input | logic | | |
| s_axis_tready | output | logic | | |
| s_axis_tlast | input | logic | | |
| s_axis_tuser | input | logic | | |
| **m_axis** (source) | | | valid_ready | sys |
| m_axis_tdata | output | logic [7:0] | | |
| m_axis_tvalid | output | logic | | |
| m_axis_tready | input | logic | | |
| m_axis_tlast | output | logic | | |
| m_axis_tuser | output | logic | | |

## 4. 파라미터

파라미터가 없습니다.

## 5. 기능 요구사항

COBS 인코딩: 프레임은 코드 바이트 C와 그 뒤의 데이터 바이트 C-1개로 이루어진 세그먼트들의 열입니다. C가 255가 아니고 세그먼트가 프레임의 마지막이 아니면, 디코딩할 때 세그먼트 뒤에 0x00을 하나 넣습니다. 인코딩된 바이트에는 0x00이 없습니다.

- COBS-01 (필수) s_axis_tvalid=1이고 s_axis_tready=1인 사이클에 받은 s_axis_tdata 바이트열을 세그먼트 단위로 디코딩해 데이터 바이트와 세그먼트 사이의 0x00을 순서대로 m_axis_tdata로 낸다. 코드 바이트 자체는 내지 않는다.
- COBS-02 (필수) 디코딩한 프레임의 마지막 바이트에서 m_axis_tlast=1로 한다.
- COBS-03 (필수) 오류가 없는 프레임의 출력 바이트는 모두 m_axis_tuser=0이다.
- COBS-04 (필수) 프레임이 시작되기 전(디코더가 프레임 사이에 있을 때) 들어오는 0x00 바이트는 버린다.
- COBS-05 (필수) 입력 프레임은 마지막 바이트의 s_axis_tlast=1로 끝나거나, 세그먼트가 끝난 위치에 오는 0x00 바이트로 끝난다. 0x00으로 끝난 경우 출력 프레임의 마지막 바이트는 m_axis_tuser에 그 0x00 바이트의 s_axis_tuser 값을 싣는다.
- COBS-06 (필수) 세그먼트 데이터 중간에 0x00 바이트가 오면 형식 오류로 보고, 지금까지의 출력 프레임을 m_axis_tlast=1, m_axis_tuser=1인 바이트로 끝낸 뒤 다음 프레임을 기다린다.
- COBS-07 (필수) s_axis_tlast=1이 세그먼트가 끝나는 위치가 아닌 곳에 오거나, 마지막 바이트의 s_axis_tuser=1이면 오류로 보고, 출력 프레임을 m_axis_tlast=1, m_axis_tuser=1인 바이트로 끝낸다.
- COBS-08 (필수) 출력은 AXI-Stream valid/ready 규칙을 지킨다. m_axis_tvalid=1이면 m_axis_tready=1을 볼 때까지 m_axis_tvalid와 데이터를 유지한다.
- COBS-09 (필수) m_axis_tready=0이면 s_axis_tready를 0으로 해 입력을 멈추고, 데이터를 잃지 않는다.

## 6. 타이밍과 성능

- COBS-T01 (필수) 출력이 막히지 않으면 입력 바이트 한 개를 한 클럭에 받는다.
- COBS-T02 (권장) 입력 바이트에서 대응하는 출력 바이트까지의 지연은 3클럭 이하이다.

## 7. 예외와 코너 케이스

- COBS-E01 (필수) rst=1을 본 다음 클럭부터 rst=1인 동안 m_axis_tvalid=0, s_axis_tready=0이고, 리셋 뒤에는 새 프레임을 기다린다.
- COBS-E02 미정의: 코드 바이트 하나(0x01)와 tlast만으로 된 빈 프레임의 출력

## 8. 예시 시나리오

### SC-01 정상: 0x00이 들어 있는 3바이트

| 입력 (s_axis, 마지막 바이트에 tlast) | 기대 출력 (m_axis) |
| --- | --- |
| 02 11 02 22 | 11 00 22(tlast=1), tuser 모두 0 |

### SC-02 코너: 세그먼트 중간의 0x00

| 입력 | 기대 출력 |
| --- | --- |
| 04 11 00 | 11(tlast=1, tuser=1) 이후 다음 프레임을 기다림 |

## 9. 구현 제약

### 필수 구조
- 단일 클럭 도메인으로 구현한다.
- 디코더 상태(프레임 사이, 세그먼트 데이터, 다음 코드 바이트)는 fsm 블록으로 작성한다.

### 금지 사항
- 프레임 전체를 저장하는 버퍼

## 10. 열린 질문

- Q-01 빈 프레임을 출력에 빈 프레임(tlast만)으로 낼지
