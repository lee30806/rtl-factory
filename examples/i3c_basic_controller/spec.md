# i3c_ctrl_top SPEC

Oct 8, 2026 · @Seungjoon Lee · 상태: 초안 (SPEC 검사 전)

MIPI I3C Basic v1.2의 SDR-Only Primary Controller Block Top입니다. 근거는 `[I3C §절 번호]`로 표시합니다. 범위 결정과 필수 요소 대응은 [README.md](README.md)에 있습니다.

## 1. 목적과 범위

`i3c_ctrl_top`은 SoC 호스트가 I3C Basic 버스의 Active Controller로 동작하게 하는 Block Top입니다. 호스트는 valid/ready 큐로 명령을 넣고 응답, 읽기 데이터, IBI 이벤트를 받습니다. 설정, 장치 주소 표, 장치 특성 표는 APB 레지스터로 다룹니다. 버스 쪽은 패드 밖에 있는 양방향 패드를 출력, 출력 enable, 입력, Pull-Up 제어 신호로 구동합니다.

범위에 포함되는 것은 다음과 같습니다.
- SDR 모드의 private 쓰기와 읽기
- Broadcast CCC와 Direct CCC
- ENTDAA 동적 주소 할당
- In-Band Interrupt 수신
- Hot-Join 요청 처리
- Legacy I2C 쓰기와 읽기 (Fast Mode, Fast Mode Plus)
- 오류 CE0, CE1, CE2의 검출과 복구
- 복구 패턴 생성 (HDR Exit Pattern, Target Reset Pattern, SCL 펄스)

범위 밖 항목은 다음과 같습니다.
- HDR 모드 전부, Secondary Controller와 Controller Role 이양, Timing Control, Multi-Lane, Controller Clock Stalling, Group Address
- Target 역할
- 동적 주소 값의 선택과 버스 초기화 순서. 이는 호스트 소프트웨어가 정합니다.
- 양방향 패드 셀과 Pull-Up 소자

## 2. 클럭, 리셋, 전력

| 도메인 | 클럭 포트 | 주파수 | 리셋 포트 | 리셋 극성 | 리셋 동기 | 도메인 간 관계 | 전력 도메인 | isolation | retention |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| sys | clk | 100 MHz | rst_n | low | 비동기 assert, 동기 deassert | 단일 도메인 | PD_SOC | 없음 | 없음 |

`scl_i`와 `sda_i`는 어느 클럭에도 동기되지 않은 비동기 입력입니다.

## 3. 인터페이스

| 포트 또는 번들 | 방향 | 타입 | 프로토콜 태그 | 도메인 |
| --- | --- | --- | --- | --- |
| clk | input | logic | – | sys |
| rst_n | input | logic | – | sys |
| **apb** (completer) | | | apb | sys |
| psel | input | logic | | |
| penable | input | logic | | |
| pwrite | input | logic | | |
| paddr | input | logic [11:0] | | |
| pwdata | input | logic [31:0] | | |
| prdata | output | logic [31:0] | | |
| pready | output | logic | | |
| pslverr | output | logic | | |
| **cmd** (sink) | | | valid_ready | sys |
| cmd_valid | input | logic | | |
| cmd_ready | output | logic | | |
| cmd_kind | input | logic [2:0] | | |
| cmd_tid | input | logic [3:0] | | |
| cmd_addr | input | logic [6:0] | | |
| cmd_rnw | input | logic | | |
| cmd_ccc | input | logic [7:0] | | |
| cmd_db_en | input | logic | | |
| cmd_db | input | logic [7:0] | | |
| cmd_len | input | logic [7:0] | | |
| cmd_hdr7e | input | logic | | |
| cmd_term_sr | input | logic | | |
| cmd_i2c_fmp | input | logic | | |
| cmd_dat_idx | input | logic [3:0] | | |
| cmd_daa_cnt | input | logic [3:0] | | |
| **txd** (sink) | | | valid_ready | sys |
| txd_valid | input | logic | | |
| txd_ready | output | logic | | |
| txd_data | input | logic [7:0] | | |
| **rxd** (source) | | | valid_ready | sys |
| rxd_valid | output | logic | | |
| rxd_ready | input | logic | | |
| rxd_data | output | logic [7:0] | | |
| rxd_last | output | logic | | |
| **resp** (source) | | | valid_ready | sys |
| resp_valid | output | logic | | |
| resp_ready | input | logic | | |
| resp_tid | output | logic [3:0] | | |
| resp_status | output | logic [3:0] | | |
| resp_len | output | logic [7:0] | | |
| **ibi** (source) | | | valid_ready | sys |
| ibi_valid | output | logic | | |
| ibi_ready | input | logic | | |
| ibi_kind | output | logic [1:0] | | |
| ibi_addr | output | logic [6:0] | | |
| ibi_acked | output | logic | | |
| ibi_len | output | logic [2:0] | | |
| ibi_data | output | logic [31:0] | | |
| ibi_trunc | output | logic | | |
| scl_o | output | logic | – | sys |
| scl_oe | output | logic | – | sys |
| scl_i | input | logic | – | 비동기 |
| sda_o | output | logic | – | sys |
| sda_oe | output | logic | – | sys |
| sda_i | input | logic | – | 비동기 |
| sda_pu_en | output | logic | – | sys |
| sda_hk_en | output | logic | – | sys |
| scl_hk_en | output | logic | – | sys |
| irq | output | logic | – | sys |

### 명령 필드

| `cmd_kind` | 이름 | 쓰는 필드 |
| --- | --- | --- |
| 0 | PRIV: I3C private 전송 | addr, rnw, len, hdr7e, term_sr |
| 1 | CCC_B: Broadcast CCC | ccc, db_en, db, len |
| 2 | CCC_D: Direct CCC (Target 하나) | ccc, db_en, db, addr, rnw, len |
| 3 | ENTDAA | dat_idx, daa_cnt |
| 4 | I2C: Legacy I2C 전송 | addr, rnw, len, hdr7e, i2c_fmp |
| 5 | RECOVER: 복구 패턴 | ccc[1:0] (패턴 종류), len (SCL 펄스 수) |
| 6, 7 | 예약 | – |

| `resp_status` | 이름 | 뜻 |
| --- | --- | --- |
| 0 | OK | 정상 완료 |
| 1 | ADDR_NACK | Target 주소가 NACK됨 |
| 2 | BCAST_NACK | Broadcast Address 7'h7E가 재시도 후에도 NACK됨 (CE2) |
| 3 | CCC_FORMAT | Direct GET CCC의 응답 형식 오류가 재시도 후에도 계속됨 (CE0) |
| 4 | MONITOR | 보낸 값과 관측한 SDA 값이 재시도 후에도 다름 (CE1) |
| 5 | INVALID | 명령 필드가 허용 범위 밖 |
| 6 | I2C_DATA_NACK | Legacy I2C 쓰기 데이터 바이트가 NACK됨 |
| 7\~15 | 예약 | – |

| `ibi_kind` | 뜻 |
| --- | --- |
| 0 | In-Band Interrupt |
| 1 | Hot-Join 요청 |
| 2 | Controller Role 요청 (항상 NACK) |
| 3 | 예약 |

## 4. 파라미터

| 이름 | 타입 | 기본값 | 허용 범위 |
| --- | --- | --- | --- |
| TX_FIFO_DEPTH | int | 64 | 16\~255 |
| RX_FIFO_DEPTH | int | 64 | 16\~255 |
| CMD_FIFO_DEPTH | int | 8 | 2\~16 |
| RESP_FIFO_DEPTH | int | 8 | 2\~16 |
| IBI_FIFO_DEPTH | int | 8 | 2\~16 |
| DAT_DEPTH | int | 8 | 1\~16 |

레지스터 맵은 [registers.csv](registers.csv)를 따릅니다.

## 5. 기능 요구사항

의무 수준은 (필수), (권장), (허용)으로 표시합니다.

### 버스 신호 구동

- FR-BUS-00 (필수) rst_n=0인 동안 모든 버스 출력과 Pull-Up 제어를 0으로 하고, 모든 큐를 비우고, 레지스터를 리셋값으로 둔다.
- FR-BUS-01 (필수) CTRL.enable이 0이면 scl_oe, sda_oe, sda_pu_en, sda_hk_en, scl_hk_en을 모두 0으로 유지하고 새 명령을 시작하지 않는다.
- FR-BUS-02 (필수) CTRL.enable이 0에서 1로 바뀌면 scl_oe=1, scl_o=1로 SCL을 High로 구동하고, SDA와 SCL이 High로 BUS_IDLE.idle 기간 동안 유지된 뒤에 첫 START를 낸다. [I3C §4.3.3.2.3]
- FR-BUS-03 (필수) Open Drain 구간에서 Low를 낼 때는 sda_oe=1, sda_o=0으로 하고, High를 낼 때는 sda_oe=0으로 SDA를 놓는다. [I3C §4.3.2.2.1]
- FR-BUS-04 (필수) Open Drain 구간에서는 sda_pu_en=1로 Open Drain Pull-Up을 켜고, Push-Pull 구간과 버스 대기 중에는 sda_pu_en=0으로 끈다. [I3C §4.3.3.1]
- FR-BUS-05 (필수) sda_hk_en과 scl_hk_en은 CTRL.enable과 CTRL.hk_en이 모두 1일 때 1이다. [I3C §4.3.3.1]
- FR-BUS-06 (필수) Push-Pull 구간에서는 sda_oe=1로 sda_o에 비트 값을 구동하고, Target이 구동하는 비트 동안에는 sda_oe=0으로 놓는다.
- FR-BUS-07 (필수) START는 SCL이 High인 동안 SDA를 High에서 Low로, STOP은 SCL이 High인 동안 SDA를 Low에서 High로 바꿔 만든다. Repeated START(Sr)는 STOP 없이 다시 내는 START이다.
- FR-BUS-08 (필수) START 뒤의 주소 헤더는 Open Drain으로 내고 중재 규칙(FR-ARB-01)을 따른다. Sr 뒤의 주소 헤더는 주소와 RnW를 Push-Pull로, ACK/NACK 비트는 Open Drain으로 처리한다. [I3C §4.3.2.2, §4.3.2.2.4]
- FR-BUS-09 (필수) I3C 쓰기 데이터 바이트는 MSB부터 8비트를 내고, 9번째 비트로 `XOR(data[7:0]) ^ 1` 값의 홀수 패리티를 낸다. [I3C §4.3.2.3.3]
- FR-BUS-10 (필수) I3C 읽기에서는 바이트마다 8비트를 받은 뒤 9번째 T 비트를 본다. T=0이면 읽기를 끝내고 STOP 또는 Sr을 낸다. T=1이고 더 받을 바이트가 남았으면 다음 바이트를 받는다. [I3C §4.3.2.3.4]
- FR-BUS-11 (필수) I3C 읽기에서 cmd_len 바이트를 받았는데 T=1이면, SCL 상승 뒤 SDA를 Low로 당겨 읽기를 중단하고 STOP을 낸다. [I3C §4.3.2.3.4]

### 주소 중재

- FR-ARB-01 (필수) 중재 대상 주소 헤더에서 0 비트는 SCL 하강 뒤 SDA를 Low로 당기고, 1 비트는 SDA를 놓은 뒤 SCL 상승 시점의 SDA를 본다. 1을 냈는데 SDA가 Low이면 중재에서 진 것으로 본다. [I3C §4.3.2.2.1]
- FR-ARB-02 (필수) 중재에서 지면 그 헤더의 나머지 주소 비트와 RnW 비트를 수신해 이긴 요청을 FR-IBI, FR-HJ 규칙으로 처리한다.
- FR-ARB-03 (필수) 버스가 Bus Available 상태(SDA, SCL이 BUS_AVAL 기간 이상 High)에서 sda_i가 Low로 떨어지면 SCL을 Low로 당기고 SDA를 Low로 함께 당긴 뒤, Target이 내는 주소 헤더를 수신한다. [I3C §4.3.2.2]
- FR-ARB-04 (필수) 중재에서 져 요청을 처리한 뒤에는, 진행하려던 명령을 처음부터 다시 START로 시작한다.

### Private 전송 (cmd_kind=0)

- FR-PRIV-01 (필수) cmd_hdr7e=1이면 START, 7'h7E/W, ACK 확인, Sr 순서로 Broadcast Address를 먼저 낸 뒤 Target 주소 헤더를 낸다. cmd_hdr7e=0이면 START 바로 뒤에 Target 주소 헤더를 낸다. [I3C §4.3.2.2.3]
- FR-PRIV-02 (필수) Target 주소 헤더가 ACK되면 cmd_rnw=0일 때 TX FIFO에서 cmd_len 바이트를 FR-BUS-09 형식으로 쓰고, cmd_rnw=1일 때 FR-BUS-10, FR-BUS-11 규칙으로 최대 cmd_len 바이트를 읽는다.
- FR-PRIV-03 (필수) 전송이 끝나면 cmd_term_sr=0일 때 STOP을 내고, cmd_term_sr=1일 때 STOP 없이 다음 명령의 주소 헤더를 Sr로 시작한다.
- FR-PRIV-04 (필수) cmd_term_sr=1인 명령 뒤에 다음 명령이 CMD_GAP 기간 안에 없으면 STOP을 낸다.

### CCC (cmd_kind=1, 2)

- FR-CCC-01 (필수) Broadcast CCC는 START, 7'h7E/W, ACK 확인, cmd_ccc 바이트, (cmd_db_en=1이면) cmd_db 바이트(Defining Byte), TX FIFO의 cmd_len 바이트, STOP 순서로 낸다. 각 바이트는 FR-BUS-09 형식이다. [I3C §4.3.7.1]
- FR-CCC-02 (필수) Direct CCC는 START, 7'h7E/W, ACK 확인, cmd_ccc 바이트, (cmd_db_en=1이면) cmd_db 바이트(Defining Byte), Sr, cmd_addr와 cmd_rnw 주소 헤더, ACK 확인, 데이터 cmd_len 바이트 쓰기 또는 읽기, STOP 순서로 낸다. [I3C §4.3.7.2.2]
- FR-CCC-03 (필수) cmd_kind=1인데 cmd_ccc[7]=1이거나, cmd_kind=2인데 cmd_ccc[7]=0이면 버스 동작 없이 INVALID로 응답한다. [I3C §4.3.7, 표 16]
- FR-CCC-04 (필수) cmd_ccc가 0x07(ENTDAA)이거나 0x20\~0x27(ENTHDR0\~7)이면 cmd_kind 1, 2 명령은 INVALID로 응답한다.
- FR-CCC-05 (필수) Direct GET CCC(cmd_kind=2, cmd_rnw=1)에서 Target이 cmd_len보다 적은 바이트를 보내고 T=0으로 끝내면 형식 오류(CE0)로 처리한다(FR-ERR-01). [I3C §4.3.8.2.1]

### 동적 주소 할당 (cmd_kind=3)

- FR-DAA-01 (필수) START, 7'h7E/W, ACK 확인, CCC 0x07을 낸다. [I3C §4.3.4.2]
- FR-DAA-02 (필수) 이어서 Sr, 7'h7E/R을 낸다. NACK이면 STOP을 내고 명령을 끝낸다.
- FR-DAA-03 (필수) ACK이면 Open Drain으로 64비트(48비트 Provisioned ID, BCR, DCR 순서, MSB부터)를 수신한다. [I3C §4.3.4.2]
- FR-DAA-04 (필수) 64비트 수신 뒤 DAT[cmd_dat_idx + n]의 동적 주소 7비트와 홀수 패리티 비트(`~XOR(addr[6:0])`)를 Push-Pull로 낸다. n은 이 명령에서 지금까지 할당에 성공한 수이다. [I3C §4.3.4.2]
- FR-DAA-05 (필수) 주소가 ACK되면 수신한 Provisioned ID, BCR, DCR을 DCT[cmd_dat_idx + n]에 기록하고 DAT의 assigned 비트를 1로 하고 n을 1 늘린다.
- FR-DAA-06 (필수) 주소가 NACK되면 n을 늘리지 않고 FR-DAA-02로 돌아간다.
- FR-DAA-07 (필수) n이 cmd_daa_cnt에 이르면 다음 7'h7E/R을 내지 않고 STOP을 낸다.
- FR-DAA-08 (필수) 응답의 resp_len은 이 명령에서 할당에 성공한 수 n이다.

### Legacy I2C (cmd_kind=4)

- FR-I2C-01 (필수) I2C 전송의 모든 비트는 Open Drain으로 내고, SCL 타이밍은 cmd_i2c_fmp=0이면 I2C_FM 레지스터, 1이면 I2C_FMP 레지스터를 쓴다. [I3C §4.3.11, 표 48]
- FR-I2C-02 (필수) cmd_hdr7e=1이면 7'h7E/W 헤더와 Sr을 먼저 낸다. [I3C 그림 175]
- FR-I2C-03 (필수) I2C 쓰기 바이트마다 9번째 비트에서 Target의 ACK를 본다. NACK이면 STOP을 내고 I2C_DATA_NACK으로 응답한다.
- FR-I2C-04 (필수) I2C 읽기에서는 마지막 바이트를 뺀 모든 바이트에 ACK를, 마지막(cmd_len번째) 바이트에 NACK을 낸 뒤 STOP을 낸다.

### In-Band Interrupt와 Hot-Join

- FR-IBI-01 (필수) 수신한 중재 헤더가 RnW=1이고, 주소가 assigned=1인 DAT 항목의 동적 주소와 같고, 그 항목의 ibi_en=1이고, IBI FIFO에 빈자리가 있으면 ACK한다. [I3C §4.3.6.2]
- FR-IBI-02 (필수) FR-IBI-01의 조건 중 하나라도 거짓이면 NACK하고 STOP을 낸다.
- FR-IBI-03 (필수) ACK한 IBI의 DAT 항목이 ibi_payload=1이면 FR-BUS-10 규칙으로 최대 4바이트(Mandatory Data Byte 포함)를 읽는다. [I3C §4.3.6.2.1]
- FR-IBI-04 (필수) 4번째 바이트의 T 비트가 1이면 FR-BUS-11처럼 읽기를 중단하고 ibi_trunc=1로 보고한다.
- FR-IBI-05 (필수) IBI를 처리하고 STOP을 낸 뒤 ibi 큐에 이벤트 하나를 넣는다. ibi_kind=0, ibi_addr=요청 주소, ibi_acked=ACK 여부, ibi_len=읽은 바이트 수, ibi_data는 첫 바이트를 [7:0]에 둔다.
- FR-IBI-06 (필수) IBI FIFO가 가득 차 NACK한 경우에는 이벤트를 넣지 않고 INT_STATUS.ibi_ovf를 1로 한다.
- FR-IBI-07 (필수) 중재 헤더가 RnW=0이고 주소가 7'h02가 아니면 Controller Role 요청으로 보고 NACK, STOP 뒤 ibi_kind=2, ibi_acked=0 이벤트를 넣는다. [I3C §4.3.2.2]
- FR-HJ-01 (필수) 중재 헤더가 7'h02/W이고 CTRL.hj_ack=1이면 ACK하고 STOP을 낸다. [I3C §4.3.5]
- FR-HJ-02 (필수) 중재 헤더가 7'h02/W이고 CTRL.hj_ack=0이면 NACK하고 STOP을 낸다.
- FR-HJ-03 (필수) Hot-Join 요청을 처리한 뒤 ibi_kind=1, ibi_addr=7'h02, ibi_acked=ACK 여부, ibi_len=0인 이벤트를 넣는다.
- FR-HJ-04 (필수) Hot-Join 뒤의 ENTDAA는 스스로 시작하지 않는다. 호스트가 ENTDAA 명령으로 시작한다.

### 오류 처리

- FR-ERR-01 (필수) 형식 오류(CE0)가 나면 STOP을 내고 같은 명령을 RETRY.count회까지 다시 시도한다. 모두 실패하면 CCC_FORMAT으로 응답한다. [I3C §4.3.8.2.1]
- FR-ERR-02 (필수) Controller가 Push-Pull로 낸 비트를 SDA 샘플링 시점에 다시 읽어 낸 값과 다르면(CE1) 전송을 멈추고 STOP을 낸 뒤 RETRY.count회까지 다시 시도한다. 모두 실패하면 MONITOR로 응답한다. 동적 주소 중재 구간은 검사하지 않는다. [I3C §4.3.8.2.2]
- FR-ERR-03 (필수) 7'h7E 헤더가 NACK되면(CE2) HDR Exit Pattern과 STOP을 낸 뒤 RETRY.count회까지 다시 시도한다. 모두 실패하면 BCAST_NACK으로 응답한다. [I3C §4.3.8.2.3, §4.3.10.2]
- FR-ERR-04 (필수) Target 주소 헤더(Sr 뒤 또는 START 뒤)가 NACK되면 STOP을 내고 재시도 없이 ADDR_NACK으로 응답한다.
- FR-ERR-05 (필수) START를 내려는데 SDA가 STUCK.cycles 이상 Low이면 INT_STATUS.sda_stuck을 1로 하고 START를 내지 않는다. 진행 중이던 명령은 그대로 대기한다. [I3C §4.3.8.2.6]
- FR-ERR-06 (필수) SCL을 High로 구동하는 동안 동기화된 scl_i가 STUCK.cycles 이상 Low이면 INT_STATUS.scl_stuck을 1로 한다.

### 복구 패턴 (cmd_kind=5)

- FR-REC-01 (필수) cmd_ccc[1:0]=0이면 HDR Exit Pattern(SCL Low 상태에서 SDA 하강 4회)과 STOP을 낸다. [I3C §4.3.10.2]
- FR-REC-02 (필수) cmd_ccc[1:0]=1이면 Target Reset Pattern(SCL Low 상태에서 SDA 전이 14회, Sr, STOP)을 낸다. [I3C §4.3.9.3]
- FR-REC-03 (필수) cmd_ccc[1:0]=2이면 SDA를 놓은 채 SCL 펄스를 cmd_len회(1\~9) 내고 STOP을 낸다. [I3C §4.3.8.2.6]
- FR-REC-04 (필수) 패턴을 시작하려고 SCL을 Low로 당기기 전에 Target이 SDA를 Low로 당겼으면 패턴을 취소하고 FR-ARB-03으로 처리한 뒤 패턴을 다시 시작한다. [I3C §4.3.9.3]
- FR-REC-05 (필수) cmd_ccc[1:0]=3은 INVALID로 응답한다.

### 호스트 큐

- FR-HOST-01 (필수) 명령은 들어온 순서대로 한 번에 하나씩 실행하고, 명령마다 같은 cmd_tid를 resp_tid로 가진 응답을 정확히 하나 낸다.
- FR-HOST-02 (필수) 쓰기 명령(PRIV, I2C의 cmd_rnw=0, CCC_B, CCC_D의 cmd_rnw=0)은 TX FIFO에 cmd_len 바이트가 모두 들어온 뒤에 버스 동작을 시작한다.
- FR-HOST-03 (필수) 읽기 명령은 RX FIFO의 빈자리가 cmd_len 이상일 때 버스 동작을 시작한다.
- FR-HOST-04 (필수) 쓰기 명령이 오류로 끝나면 그 명령 몫으로 남은 TX FIFO 바이트를 버린다.
- FR-HOST-05 (필수) 읽은 바이트는 순서대로 rxd_data로 내고, 한 명령의 마지막 바이트에서 rxd_last=1로 한다. 읽은 바이트가 없으면 rxd에 아무것도 내지 않는다.
- FR-HOST-06 (필수) resp_len은 쓰기 명령에서는 Target에 보낸 데이터 바이트 수, 읽기 명령에서는 받은 데이터 바이트 수이다.
- FR-HOST-07 (필수) cmd_len이 TX_FIFO_DEPTH(쓰기) 또는 RX_FIFO_DEPTH(읽기)보다 크거나, 읽기 명령의 cmd_len이 0이거나, cmd_kind가 6 또는 7이면 버스 동작 없이 INVALID로 응답한다.
- FR-HOST-08 (필수) cmd_ready는 명령 FIFO에 빈자리가 있을 때 1이고, txd_ready는 TX FIFO에 빈자리가 있을 때 1이다.
- FR-HOST-10 (필수) cmd_valid=1이고 cmd_ready=1인 사이클에 명령 필드 전체를 명령 FIFO에 넣는다. txd_valid=1이고 txd_ready=1인 사이클에 txd_data를 TX FIFO에 넣는다.
- FR-HOST-11 (필수) rxd_valid, resp_valid, ibi_valid는 각 큐가 비어 있지 않을 때 1이고, rxd_ready, resp_ready, ibi_ready가 1인 사이클에 맨 앞 항목 하나를 내보낸다.
- FR-HOST-09 (필수) 버스 동작이 없는 명령(INVALID)도 이전 명령의 응답 뒤에 순서대로 응답한다.

### 레지스터

- FR-REG-01 (필수) APB 접근은 psel=1, penable=1인 사이클에 대기 없이 완료한다(pready=1). pwrite=1이면 paddr의 레지스터에 pwdata를 쓰고, pwrite=0이면 그 값을 prdata로 낸다. 정의되지 않은 paddr에 접근하면 pslverr=1이고 쓰기는 무시하며 prdata는 0이다.
- FR-REG-02 (필수) DAT 항목(DAT_DEPTH개)은 동적 주소, 정적 주소, ibi_en, ibi_payload, assigned 필드를 가지며 호스트가 읽고 쓴다.
- FR-REG-03 (필수) DCT 항목(DAT_DEPTH개)은 Provisioned ID, BCR, DCR을 가지며 호스트에게는 읽기 전용이다.
- FR-REG-04 (필수) OWN_DA 레지스터는 Controller 자신의 동적 주소를 보관한다. [I3C §4.3.1.1, 표 4]
- FR-REG-05 (필수) irq는 `|(INT_STATUS & INT_EN)`이다. INT_STATUS의 W1C 비트(ibi_ovf, sda_stuck, scl_stuck)는 1을 써서 지운다.
- FR-REG-06 (필수) INT_STATUS.resp_avail은 resp 큐가 비어 있지 않을 때 1, INT_STATUS.ibi_avail은 ibi 큐가 비어 있지 않을 때 1이다.

## 6. 타이밍과 성능

타이밍 레지스터 값은 clk 사이클 수입니다. 기본값은 100 MHz 기준입니다.

- TR-01 (필수) Push-Pull 구간의 SCL은 Low를 PP_TIMING.low 사이클, High를 PP_TIMING.high 사이클 유지한다. 기본값 4/4는 12.5 MHz이다. 3 미만으로 설정된 값은 3으로 다룬다. [I3C 표 50]
- TR-02 (필수) Open Drain 구간의 SCL은 Low를 OD_TIMING.low 사이클(기본 20, 200 ns), High를 OD_TIMING.high 사이클(기본 4, 40 ns) 유지한다. [I3C 표 49]
- TR-03 (필수) CTRL.enable 뒤 첫 Broadcast Address 헤더의 SCL High는 OD_TIMING.high_init 사이클(기본 20, 200 ns) 이상이다. [I3C 표 49, tHIGH_INIT]
- TR-04 (필수) Legacy I2C 구간의 SCL은 I2C_FM 레지스터(기본 Low 130, High 120, 400 kHz) 또는 I2C_FMP 레지스터(기본 Low 50, High 50, 1 MHz)를 따른다. [I3C 표 48]
- TR-05 (필수) FR-ARB-03에서 sda_i의 하강이 동기화되어 관측된 뒤 4\~20 사이클 안에 SCL을 Low로 당긴다. [I3C 표 49, tCAS]
- TR-06 (필수) START 뒤 첫 SCL 하강까지, 그리고 STOP 앞 마지막 SCL 상승부터 SDA 상승까지 각각 4 사이클 이상 둔다. [I3C 표 49, tCAS, tCBP]
- TR-07 (필수) Bus Available 판정 기간은 BUS_COND.aval 사이클(기본 100, 1 µs), Bus Idle 판정 기간은 BUS_IDLE.idle 사이클(기본 20000, 200 µs)이다. [I3C §4.3.3.2]
- TR-08 (필수) Target이 구동하는 비트는 Controller가 SCL 상승을 낸 뒤 SAMPLE.dly 사이클(기본 3) 시점의 동기화된 SDA 값으로 판정한다.
- TR-09 (필수) STOP 뒤 다음 START까지 BUS_COND.free 사이클(기본 50, 0.5 µs) 이상 둔다. [I3C §4.3.3.2.1]
- TR-10 (필수) 버스가 Bus Free이고 FR-HOST-02, FR-HOST-03 조건이 갖춰진 명령은 20 사이클 안에 START를 시작한다.
- TR-11 (필수) 타이밍 레지스터를 바꾸면 다음 START부터 적용한다.
- TR-12 (필수) rxd, resp, ibi 큐는 ready가 0인 동안 valid와 데이터를 그대로 유지한다. Controller는 이 큐들이 막혀도 SCL을 멈추지 않는다. 대신 FR-HOST-03, FR-IBI-01 조건으로 공간을 미리 확인한다.

## 7. 예외와 코너 케이스

- EX-01 (필수) 쓰기 명령의 cmd_len이 0이면 주소 헤더만 내고 ACK 확인 뒤 STOP을 낸다. resp_len은 0이다.
- EX-02 (필수) 명령의 START 직전에 IBI 또는 Hot-Join 요청이 들어오면 요청을 먼저 처리하고 FR-ARB-04로 명령을 다시 시작한다. 같은 명령에서 이런 일이 몇 번 일어나도 응답은 하나이다.
- EX-03 (필수) ENTDAA 중 cmd_dat_idx + n이 DAT_DEPTH 이상이 되면 STOP을 내고 그때까지의 n으로 응답한다.
- EX-04 (필수) ENTDAA 대상 DAT 항목의 동적 주소가 7'h7E 또는 표 10[I3C §4.3.2.2.5]에서 쓸 수 없는 값이면 버스 동작 없이 INVALID로 응답한다.
- EX-05 (필수) Direct CCC와 private 전송에서 Target이 읽기를 cmd_len보다 일찍 끝내는 것은 오류가 아니다. 단, Direct GET CCC는 FR-CCC-05를 따른다.
- EX-06 (필수) CTRL.enable을 전송 도중 0으로 바꾸면 현재 바이트를 마치고 STOP을 낸 뒤 버스를 놓는다. 진행 중이던 명령도 응답을 하나 내며, 그 resp_status 값은 미정의이다.
- EX-07 미정의: ENTDAA 실행 중 호스트가 DAT를 고쳤을 때의 결과
- EX-08 미정의: 같은 동적 주소가 DAT의 여러 assigned 항목에 있을 때 IBI 수락 판정에 쓰이는 항목
- EX-09 미정의: 타이밍 레지스터에 I3C 규격을 어기는 값을 넣었을 때의 버스 동작

## 8. 예시 시나리오

표기: S=START, Sr=Repeated START, P=STOP, `/W`, `/R`=RnW, A=ACK, N=NACK, `T=x`=9번째 비트 값.

### SC-01 정상: Private 쓰기 2바이트

| 입력 | 기대 버스 순서 | 기대 출력 |
| --- | --- | --- |
| DAT[0]: dyn=7'h30, assigned=1. 명령: kind=0, tid=1, addr=7'h30, rnw=0, len=2, hdr7e=1, term_sr=0. txd: 0xA5, 0x01 | S 7E/W A, Sr 30/W A, A5 T=1, 01 T=0, P | resp: tid=1, status=OK, len=2 |

### SC-02 정상: Private 읽기, Target이 일찍 끝냄

| 입력 | 기대 버스 순서 | 기대 출력 |
| --- | --- | --- |
| 명령: kind=0, tid=2, addr=7'h30, rnw=1, len=4, hdr7e=1. Target은 0x11, 0x22를 보내고 끝냄 | S 7E/W A, Sr 30/R A, 11 T=1, 22 T=0, P | rxd: 0x11(last=0), 0x22(last=1). resp: tid=2, status=OK, len=2 |

### SC-03 정상: ENTDAA로 Target 2개 할당

| 입력 | 기대 버스 순서 | 기대 출력 |
| --- | --- | --- |
| DAT[0].dyn=7'h30, DAT[1].dyn=7'h31. 명령: kind=3, tid=3, dat_idx=0, daa_cnt=4. Target X: PID 0x046A_0000_0001, BCR 0x06, DCR 0x00. Target Y: PID 0x046A_0000_0002, BCR 0x06, DCR 0x00 | S 7E/W A, 07 T=0, Sr 7E/R A, [X의 64비트], 30 PAR=1 A, Sr 7E/R A, [Y의 64비트], 31 PAR=0 A, Sr 7E/R N, P | DCT[0]=X, DCT[1]=Y, DAT[0..1].assigned=1. resp: tid=3, status=OK, len=2 |

### SC-04 코너: 명령 시작 중 IBI가 중재에서 이김

| 입력 | 기대 버스 순서 | 기대 출력 |
| --- | --- | --- |
| DAT[2]: dyn=7'h20, assigned=1, ibi_en=1, ibi_payload=1. SC-01 명령을 시작하는 START에서 Target 7'h20이 IBI를 요청하고 MDB 0x0A를 보냄 | S, [7E와 20의 중재에서 20이 이김] 20/R A, 0A T=0, P. 이어서 SC-01의 버스 순서 | ibi: kind=0, addr=7'h20, acked=1, len=1, data=0x0000000A, trunc=0. 그 뒤 resp: tid=1, status=OK, len=2 |

### SC-05 코너: Target 주소 NACK

| 입력 | 기대 버스 순서 | 기대 출력 |
| --- | --- | --- |
| 명령: kind=0, tid=5, addr=7'h35, rnw=0, len=1, hdr7e=1. txd: 0x55. 7'h35인 Target 없음 | S 7E/W A, Sr 35/W N, P | resp: tid=5, status=ADDR_NACK, len=0. TX FIFO의 0x55는 버려짐 |

### SC-06 코너: Broadcast Address NACK (CE2)

| 입력 | 기대 버스 순서 | 기대 출력 |
| --- | --- | --- |
| RETRY.count=1. 명령: kind=1, tid=6, ccc=0x06(RSTDAA), len=0. 버스에 I3C Target 없음 | S 7E/W N, HDR Exit Pattern, P, (BUS_COND.free 대기) S 7E/W N, HDR Exit Pattern, P | resp: tid=6, status=BCAST_NACK, len=0 |

### SC-07 코너: Bus Available 상태의 Hot-Join

| 입력 | 기대 버스 순서 | 기대 출력 |
| --- | --- | --- |
| CTRL.hj_ack=1. 버스 대기 중 새 Target이 SDA를 Low로 당기고 7'h02/W를 냄 | (Target S) 02/W A, P | ibi: kind=1, addr=7'h02, acked=1, len=0 |

## 9. 구현 제약

### 필수 구조
- 단일 클럭 도메인(sys)으로 구현한다.
- scl_i와 sda_i는 프로젝트 표준 2단 동기화 셀(`cdc_sync`, style 2ff)을 거쳐 쓴다.
- 버스 출력(scl_o, scl_oe, sda_o, sda_oe, sda_pu_en, sda_hk_en, scl_hk_en)은 플립플롭 출력으로 내서 글리치가 없게 한다.
- 리셋 중과 리셋 직후에는 모든 버스 출력의 enable과 Pull-Up 제어가 0이다.
- 내부 FIFO는 추론형 메모리(`memory`, impl=inferred)로 만든다.

### 금지 사항
- inout 포트, tri-state, 의도적 래치
- 클럭 게이팅, 생성 클럭, SCL을 클럭으로 쓰는 플립플롭
- 다중 비트 신호의 클럭 도메인 교차

## 10. 열린 질문

- Q-01 패드 라이브러리의 Pull-Up과 High-Keeper 제어 신호의 극성과 개수. 지금은 sda_pu_en, sda_hk_en, scl_hk_en 세 개를 가정한다.
- Q-02 IBI 페이로드가 4바이트를 넘는 Target을 지원해야 하는지
- Q-03 12.5 MHz에서 2단 동기화 지연과 패드 지연을 합친 SDA 샘플링 여유 (TR-08). 패드와 배선 지연 수치가 필요하다.
- Q-04 Direct CCC 하나로 여러 Target에 보내는 형식(Sr로 Target을 이어 붙임)이 필요한지. v1은 Target 하나씩 보낸다.
- Q-05 NACK한 IBI와 Hot-Join 뒤에 DISEC을 자동으로 보낼지. v1은 호스트가 보낸다.
- Q-06 GETMXDS로 알려진 읽기 응답 지연을 Controller가 반영할지. v1은 호스트가 SCL 타이밍을 낮춰 대응한다.
- Q-07 Mixed Bus에서 Legacy I2C 장치의 50 ns Spike Filter와 Open Drain High 기간(41 ns 이하) 제약을 레지스터 검사로 막을지. v1은 호스트 책임이다.
