# axis_crc_append SPEC

Oct 12, 2026 · P8 첫 Block Top End-to-End용 신규 블록 (이식 블록이 아님, 원본 RTL 없음)

## 1. 목적과 범위

`axis_crc_append`는 AXI-Stream 바이트 프레임을 그대로 통과시키면서 프레임마다 CRC-16을 계산해 프레임 끝에 2바이트로 덧붙이는 Block Top입니다. 덧붙인 프레임의 수를 상태 출력으로 냅니다.

범위 밖 항목은 CRC 검사(수신 쪽), 8비트가 아닌 데이터 폭, 다른 CRC 다항식의 선택, 프레임 버퍼링입니다.

## 2. 클럭, 리셋, 전력

| 도메인 | 클럭 포트 | 주파수 | 리셋 포트 | 리셋 극성 | 리셋 동기 | 도메인 간 관계 | 전력 도메인 | isolation | retention |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sys | clk | 200 MHz | rst | high | 동기 | 단일 도메인 | 없음 | 없음 | 없음 |

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
| stat_frames | output | logic [15:0] | – | sys |

## 4. 파라미터

파라미터가 없습니다.

## 5. 기능 요구사항

CRC 정의 (CRC-16/CCITT-FALSE): 다항식 0x1021, 초깃값 0xFFFF, 입력과 출력의 비트 반전 없음, 최종 XOR 없음. 바이트마다 상위 비트부터 넣는다. 바이트열 "123456789"(0x31\~0x39)의 CRC는 0x29B1이다.

- CRC-01 (필수) s_axis_tvalid=1이고 s_axis_tready=1인 사이클에 받은 s_axis_tdata 바이트를 받은 순서대로 바꾸지 않고 m_axis_tdata로 낸다.
- CRC-02 (필수) 데이터 바이트를 낼 때 m_axis_tlast는 0이다.
- CRC-03 (필수) s_axis_tlast=1인 바이트(프레임의 마지막 데이터 바이트)를 낸 뒤 그 프레임의 CRC를 상위 바이트, 하위 바이트 순으로 낸다.
- CRC-04 (필수) CRC 하위 바이트에서 m_axis_tlast=1이다.
- CRC-05 (필수) CRC는 프레임마다 초깃값 0xFFFF에서 새로 시작해 그 프레임의 데이터 바이트만으로 계산한다 (CRC 바이트는 넣지 않음).
- CRC-06 (필수) 프레임의 마지막 데이터 바이트에서 s_axis_tuser=1이면 CRC 하위 바이트에서 m_axis_tuser=1이다.
- CRC-07 (필수) CRC 하위 바이트가 아닌 출력 바이트의 m_axis_tuser는 0이다 (마지막이 아닌 데이터 바이트의 s_axis_tuser는 무시함).
- CRC-08 (필수) stat_frames는 CRC 하위 바이트가 나간 사이클(m_axis_tvalid=1이고 m_axis_tready=1)의 다음 사이클에 1 늘고 0xFFFF 다음은 0이다.
- CRC-09 (필수) 오류 표시(tuser=1)가 있는 프레임도 stat_frames에 센다.
- CRC-10 (필수) m_axis_tvalid=1이면 m_axis_tready=1을 볼 때까지 m_axis_tvalid와 m_axis 데이터를 유지한다.
- CRC-11 (필수) m_axis_tready=0으로 출력이 막혀도 바이트를 잃거나 반복하지 않는다.
- CRC-12 (필수) CRC 두 바이트를 내는 동안 s_axis_tready=0이다.

## 6. 타이밍과 성능

- CRC-T01 (필수) 출력이 막히지 않으면 데이터 바이트 하나를 한 클럭에 받는다. N바이트 프레임은 CRC 두 바이트를 포함해 파이프라인 지연(1클럭)을 감안하여 N+3클럭 안에 모두 나간다 (출력이 막히지 않을 때 연속 스트리밍 처리율은 1바이트/클럭 유지).
- CRC-T02 (권장) 입력 바이트에서 대응하는 출력 바이트까지의 지연은 2클럭 이하이다.
- CRC-T03 (필수) 200 MHz(주기 5 ns)에서 타이밍을 만족한다.

## 7. 예외와 코너 케이스

- CRC-E01 (필수) rst=1을 본 다음 클럭부터 m_axis_tvalid=0, s_axis_tready=0, stat_frames=0이다. 리셋 뒤에는 새 프레임의 첫 바이트를 기다린다.
- CRC-E02 (필수) 1바이트 프레임(첫 바이트에 s_axis_tlast=1)도 데이터 1바이트와 CRC 2바이트로 낸다.
- CRC-E03 미정의: 리셋이 프레임 도중에 걸렸을 때 그 프레임의 이미 나간 바이트들의 처리 (리셋 뒤에는 CRC-E01을 따름)

## 8. 예시 시나리오

### SC-01 정상: 표준 검사 문자열

| 입력 (s_axis, 마지막 바이트에 tlast) | 기대 출력 (m_axis) |
| --- | --- |
| 31 32 33 34 35 36 37 38 39 | 31 32 33 34 35 36 37 38 39 29 B1(tlast=1), tuser 모두 0, 그 뒤 stat_frames 0→1 |

### SC-02 코너: 1바이트 오류 프레임

| 입력 | 기대 출력 |
| --- | --- |
| 00(tlast=1, tuser=1) | 00 E1 F0(tlast=1, tuser=1), 그 밖의 tuser 0 |

### SC-03 코너: 연속 두 프레임과 backpressure

| 입력 | 기대 출력 |
| --- | --- |
| A5 5A(tlast=1), 이어서 31 32 33 34 35 36 37 38 39(tlast=1). m_axis_tready를 한 클럭씩 0으로 섞음 | A5 5A 04 3B(tlast=1), 31 … 39 29 B1(tlast=1). 바이트를 잃거나 반복하지 않음, stat_frames 0→1→2 |

## 9. 구현 제약

### 필수 구조
- 단일 클럭 도메인으로 구현한다.
- 상태(데이터, CRC 상위 바이트, CRC 하위 바이트)는 fsm 블록으로 작성한다.
- CRC는 바이트 하나를 한 클럭에 갱신하는 조합 논리로 계산한다.
- 입출력 인터페이스의 조합 통과 경로(feedthrough)를 차단하기 위해 출력 레지스터(Registered Output) 또는 스키드 버퍼(Skid Buffer / Register Slice) 구조를 적용한다.

### 금지 사항
- 프레임 전체를 저장하는 버퍼
- 래치
- s_axis_* 포트에서 m_axis_* 포트로의 순수 조합 통과 경로
- m_axis_tready에서 s_axis_tready로의 순수 조합 통과 경로

## 10. 열린 질문

- Q-01 CRC 바이트 순서(상위 먼저)를 나중에 파라미터로 바꿀 수 있게 할지
