# axis_adapter SPEC

Oct 11, 2026 · 이식 시험 블록의 Block Top SPEC (원본: verilog-axis `axis_adapter`, MIT). 32비트 입력 → 8비트 출력 설정

## 1. 목적과 범위

`axis_adapter`는 32비트 AXI-Stream을 8비트 AXI-Stream으로 바꾸는 폭 변환기입니다. 입력 비트(beat)의 유효 바이트를 낮은 바이트부터 하나씩 내보냅니다.

범위 밖 항목은 8비트에서 32비트로 넓히는 변환, 같은 폭의 통과, 입력 tkeep 비활성 설정입니다.

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
| s_axis_tdata | input | logic [S_DATA_WIDTH-1:0] | | |
| s_axis_tkeep | input | logic [S_KEEP_WIDTH-1:0] | | |
| s_axis_tvalid | input | logic | | |
| s_axis_tready | output | logic | | |
| s_axis_tlast | input | logic | | |
| s_axis_tid | input | logic [ID_WIDTH-1:0] | | |
| s_axis_tdest | input | logic [DEST_WIDTH-1:0] | | |
| s_axis_tuser | input | logic [USER_WIDTH-1:0] | | |
| **m_axis** (source) | | | valid_ready | sys |
| m_axis_tdata | output | logic [M_DATA_WIDTH-1:0] | | |
| m_axis_tkeep | output | logic [M_KEEP_WIDTH-1:0] | | |
| m_axis_tvalid | output | logic | | |
| m_axis_tready | input | logic | | |
| m_axis_tlast | output | logic | | |
| m_axis_tid | output | logic [ID_WIDTH-1:0] | | |
| m_axis_tdest | output | logic [DEST_WIDTH-1:0] | | |
| m_axis_tuser | output | logic [USER_WIDTH-1:0] | | |

## 4. 파라미터

| 이름 | 타입 | 기본값 | 허용 범위 |
| --- | --- | --- | --- |
| S_DATA_WIDTH | int | 32 | 32 (이 설정) |
| S_KEEP_ENABLE | int | 1 | 1 |
| S_KEEP_WIDTH | int | 4 | 4 |
| M_DATA_WIDTH | int | 8 | 8 |
| M_KEEP_ENABLE | int | 0 | 0 |
| M_KEEP_WIDTH | int | 1 | 1 |
| ID_ENABLE | int | 1 | 1 |
| ID_WIDTH | int | 8 | 1\~32 |
| DEST_ENABLE | int | 1 | 1 |
| DEST_WIDTH | int | 8 | 1\~32 |
| USER_ENABLE | int | 1 | 1 |
| USER_WIDTH | int | 1 | 1\~32 |

## 5. 기능 요구사항

- ADP-01 (필수) s_axis_tvalid=1이고 s_axis_tready=1인 사이클에 받은 입력 비트(s_axis_tdata)의 바이트 레인 중 s_axis_tkeep이 1인 레인을, 레인 0부터 높은 레인 순서로 하나씩 m_axis_tdata로 낸다. tkeep은 레인 0부터 연속으로 1이라고 가정한다.
- ADP-02 (필수) 입력 비트의 s_axis_tlast=1은 그 비트에서 나온 마지막 출력 바이트에만 m_axis_tlast=1로 싣고, 나머지 출력 바이트는 m_axis_tlast=0이다.
- ADP-03 (필수) 입력 비트의 s_axis_tid, s_axis_tdest, s_axis_tuser 값을 그 비트에서 나온 모든 출력 바이트의 m_axis_tid, m_axis_tdest, m_axis_tuser로 낸다.
- ADP-04 (필수) m_axis_tkeep은 항상 1이다.
- ADP-05 (필수) 출력은 AXI-Stream valid/ready 규칙을 지킨다. m_axis_tvalid=1이면 m_axis_tready=1을 볼 때까지 m_axis_tvalid와 데이터를 유지한다.
- ADP-06 (필수) 아직 다 내보내지 못한 입력 비트가 있으면 s_axis_tready=0으로 다음 입력을 받지 않는다. 입력 데이터를 잃지 않는다.

## 6. 타이밍과 성능

- ADP-T01 (필수) 출력이 막히지 않으면 출력 바이트를 매 클럭 하나씩 낸다.

## 7. 예외와 코너 케이스

- ADP-E01 (필수) rst=1을 본 다음 클럭부터 rst=1인 동안 m_axis_tvalid=0이고 받아 둔 입력은 버린다.
- ADP-E02 미정의: tkeep이 0이거나 레인 0부터 연속이 아닌 입력 비트의 출력

## 8. 예시 시나리오

### SC-01 정상: 6바이트 프레임

| 입력 비트 (tdata, tkeep, tlast) | 기대 출력 바이트 (tdata, tlast) |
| --- | --- |
| (0x44332211, 1111, 0), (0x00006655, 0011, 1) | (11,0) (22,0) (33,0) (44,0) (55,0) (66,1) |

### SC-02 코너: 1바이트 마지막 비트에 tuser

| 입력 | 기대 출력 |
| --- | --- |
| (0x000000AA, 0001, tlast=1, tuser=1) | (AA, tlast=1, tuser=1) |

## 9. 구현 제약

### 필수 구조
- 단일 클럭 도메인으로 구현한다.

### 금지 사항
- 입력 비트를 둘 이상 저장하는 버퍼

## 10. 열린 질문

- Q-01 tkeep이 연속이 아닌 입력을 지원할지
