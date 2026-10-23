# mc_top SPEC

Oct 16, 2026 · P10 다중 클럭과 리셋 시험용 Block Top (비동기 도메인 둘, 분주 도메인 하나, 소프트 리셋 입력, 블록 안 리셋 동기화)

## 1. 목적과 범위

`mc_top`은 a 도메인에서 받은 바이트에 1을 더해 b 도메인으로 넘깁니다. a2 도메인(a에서 2분주)에서 받은 한 사이클 펄스를 b 도메인의 한 사이클 펄스로 옮기고, a 도메인에서 받은 바이트 수를 a2 도메인에서 셉니다.

범위 밖 항목은 전력 관리입니다.

## 2. 클럭, 리셋, 전력

| 도메인 | 클럭 포트 | 주파수 | 리셋 포트 | 리셋 극성 | 리셋 동기 | 도메인 간 관계 | 전력 도메인 | isolation | retention |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| a | a_clk | 100 MHz | a_rst_n | low | 비동기 (블록 안에서 동기화) | b와 비동기 | 없음 | 없음 | 없음 |
| a2 | a2_clk | 50 MHz | a_rst_n | low | 비동기 (블록 안에서 동기화) | a에서 2분주 | 없음 | 없음 | 없음 |
| b | b_clk | 75 MHz | b_rst_n | low | 비동기 assert, 동기 deassert | a와 비동기 | 없음 | 없음 | 없음 |

## 3. 인터페이스

| 포트 또는 번들 | 방향 | 타입 | 프로토콜 태그 | 도메인 |
| --- | --- | --- | --- | --- |
| a_clk | input | logic | – | a |
| a_rst_n | input | logic | – | a |
| a_soft_rst | input | logic | – | a |
| a2_clk | input | logic | – | a2 |
| b_clk | input | logic | – | b |
| b_rst_n | input | logic | – | b |
| **in** (sink) | | | valid_ready | a |
| in_valid | input | logic | | |
| in_ready | output | logic | | |
| in_data | input | logic [7:0] | | |
| **out** (source) | | | valid_ready | b |
| out_valid | output | logic | | |
| out_ready | input | logic | | |
| out_data | output | logic [7:0] | | |
| pulse_in | input | logic | – | a2 |
| pulse_out | output | logic | – | b |
| cnt_out | output | logic [3:0] | – | a2 |

## 4. 파라미터

| 이름 | 타입 | 기본값 | 허용 범위 |
| --- | --- | --- | --- |

## 5. 기능 요구사항

- MC-01 (필수) in_valid=1이고 in_ready=1인 사이클에 받은 in_data에 1을 더한 값을 받은 순서대로 out_data로 낸다.
- MC-02 (필수) out은 valid/ready 규칙을 지킨다. out_valid=1이면 out_ready=1을 볼 때까지 out_valid와 out_data를 유지한다.
- MC-03 (필수) a_rst_n=0이거나 a_soft_rst=1인 동안 in_ready=0이다.
- MC-04 (필수) a2_clk의 한 사이클 동안 pulse_in=1이면 pulse_out을 b_clk 한 사이클 동안 1로 한다.
- MC-05 (필수) cnt_out은 a 도메인에서 받은 바이트 수를 a2 도메인에서 센 값의 하위 4비트이다.

## 6. 타이밍과 성능

- MC-T01 (권장) 출력이 막히지 않으면 a_clk 두 클럭에 바이트 하나를 받는다.

## 7. 예외와 코너 케이스

- MC-E01 (필수) a_soft_rst=1이면 다음 a_clk 상승 에지에서 a 도메인의 상태를 리셋값으로 되돌린다.
- MC-E02 미정의: pulse_in 펄스 사이 간격이 a2_clk 세 사이클보다 짧을 때의 pulse_out

## 8. 예시 시나리오

### SC-01 정상: 바이트 둘

| 입력 (in) | 기대 출력 (out) |
| --- | --- |
| 0x10, 0x20 | 0x11, 0x21 |

### SC-02 코너: 소프트 리셋

| 입력 | 기대 출력 |
| --- | --- |
| 바이트를 보내는 도중 a_soft_rst=1 | 소프트 리셋 동안 in_ready=0 |

## 9. 구현 제약

### 필수 구조
- a에서 b로 가는 바이트는 `cdc_fifo`로 넘긴다.
- a2에서 b로 가는 펄스는 `cdc_sync` style pulse로 넘긴다.

## 10. 열린 질문

- Q-01 FIFO 깊이를 4보다 키울지
