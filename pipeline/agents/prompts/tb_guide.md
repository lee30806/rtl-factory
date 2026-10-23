# 테스트벤치 작성 안내 (Verifier용)

하니스(클럭, 리셋, 번들 드라이버와 모니터, 프로토콜 체커, 스코어보드)는 툴이 L1에서 만든다. 너는 참조 모델, 속성, 시나리오를 쓴다. RTL과 내부 신호는 볼 수 없고, L1의 포트만 쓴다.

## 하니스

```python
import cocotb
from <module>_harness import make        # tools/pl.py harness로 만든 파일. 실행 때는 툴이 제공한다

class Model:
    streams = {"m_axis"}                  # 비교할 출력 (번들 이름이나 단독 출력 이름). 상태 출력처럼 예측하지 않는 것은 빼고 속성으로 본다
    def on_input(self, stream, txn):      # 입력 번들의 전송마다 불림. txn은 그 번들의 data 신호 값 dict (valid/ready 제외)
        return [("m_axis", {...}, "REQ-ID")]   # 기대 출력 목록. (stream, txn[, 요구사항 ID 또는 ID 목록])

@cocotb.test()
async def basic(dut):
    h = make(dut, Model(), name="basic")  # name은 테스트마다 달라야 한다
    h.add_property("이름", "<도메인>", lambda s, cycle: None if 조건 else "실패 사유", "REQ-ID")   # s: 그 도메인의 L1 포트 값 dict (모든 도메인의 리셋 포트 포함, 클럭 제외)
    h.cover_protocol("m_axis", "REQ-ID")   # valid/ready 규칙 체커가 이 번들을 검사하면 그 요구사항을 실행한 것으로 셈
    h.set_ready("m_axis", [1, 0, 1])       # 출력 번들의 ready 패턴 (backpressure). 패턴은 끝나면 처음부터 되풀이한다
    h.set_ready("m_axis", [0] * 100, then=1)   # 처음 100사이클만 막고 그 뒤로 계속 받기 (then 값을 유지)
    h.allow_overwrite("m_axis")            # SPEC이 받지 않은 값을 덮어쓰도록 정한 번들만: ready 전 data 변화를 T2 위반으로 보지 않음
    await h.start()                       # 클럭 시작, 리셋, 표본 시작
    h.send("s_axis", {"s_axis_tdata": 1, "s_axis_tlast": 0}, idle=0)   # 입력 번들에 전송 하나 (data 신호 모두 지정)
    await h.drain()                       # 입력을 다 보내고 기대 출력이 다 올 때까지 기다림
    h.finish()                            # 불일치, 프로토콜 위반, 속성 위반이 있으면 AssertionError
```

- 사이클 번호는 리셋이 풀린 첫 사이클을 0으로 센다 (도메인마다 자기 클럭). `await h.start()` 바로 뒤에 넣은 첫 전송은 `idle=N`이면 사이클 N에 valid=1로 나간다 (idle=0이면 사이클 0). 그 뒤 전송의 idle은 앞 전송이 받힌 다음 사이클부터 센다: 사이클 c에 받히면 다음 전송은 사이클 c+1+N에 나간다.
- 기대 출력과 실제 출력은 순서대로 비교한다. 출력이 기대보다 먼저 와도 된다 (프레임 끝에서야 기대를 알 수 있는 모델).
- 1비트 단독 출력은 1인 사이클마다, 여러 비트 단독 출력은 값이 바뀔 때마다 트랜잭션 하나가 된다.
- 리셋이나 pause 같은 입력은 `dut.<포트>.value = 1`로 직접 바꿀 수 있다. 클럭 에지는 `from cocotb.triggers import ClockCycles`, `await ClockCycles(dut.<클럭>, n)`.
- 테스트 도중 리셋을 걸면 하니스가 그 도메인 입력 번들의 남은 전송(`h.send`로 넣었지만 아직 받지 않은 것)을 버린다. 리셋 뒤에 보낼 것은 리셋을 푼 뒤 다시 `h.send`로 넣는다.
- 속성 표본은 하강 에지 뒤에 읽는다. 동기 리셋이면 표본에서 리셋이 1인 사이클의 출력은 아직 리셋 전 값이고, 다음 사이클 표본부터 리셋 값이다.
- 하강 에지 도메인(L1 도메인의 `edge`가 `neg`)은 반대다: 하니스가 상승 에지에서 그 도메인의 입력을 바꾸고 표본을 읽으며, DUT는 하강 에지에서 받는다. 그 도메인의 입력을 직접 바꿀 때는 `await RisingEdge(dut.<클럭>)` 뒤에 바꾼다.
- inout 포트 P는 하니스에서 `P_pad_o`, `P_pad_oe`(바깥 장치가 내는 값과 구동 여부)와 `P_pad_val`(패드 값)로 다룬다. 바깥 장치가 구동하는 동안(`P_pad_oe=1`) 패드 값이 바깥 장치의 값과 다르면 DUT가 같은 패드를 다른 값으로 구동한 것이므로 하니스가 동시 구동 충돌(T2)로 실패시킨다. open-drain 버스에서 바깥 장치가 1을 낼 때는 `P_pad_oe=0`으로 놓아 풀업에 맡긴다.
- 클럭이 없는 조합 블록(L1의 `domains`가 빔)은 하니스가 가상 클럭 `tb_comb_clk` 하나의 도메인 `comb`로 돌린다. 모든 포트가 `comb`
  도메인이고 리셋은 없다. 입력 번들이 없으므로 `on_input`이 불리지 않는다: 모델은 `streams = set()`으로 두고, 출력은
  `h.add_property(이름, "comb", fn, REQ)`로 같은 표본의 입력과 비교한다. 입력은 `await FallingEdge(dut.tb_comb_clk)` 뒤에
  `dut.<포트>.value`로 바꾸고, 같은 사이클 표본에서 출력이 이미 그 입력의 함수여야 한다. backpressure와 idle 규칙은 해당 없음.
- 실패 메시지에 `[SVA] assertions/<id> ... assume 실패`가 있으면 테스트 입력이 설계의 입력 가정을 어긴 것이다. SPEC이 그 입력을 허용하는지 확인하고, 허용하지 않으면 입력을 고친다.
- 테스트는 SPEC의 요구사항 ID를 기대 출력, 속성, `cover_protocol`로 모두 하나 이상 실행해야 한다.

## 자주 틀린 것

지난 실행에서 사람이 기댓값 수정을 승인한 테스트 결함들이다. RTL이 맞는데 테스트가 실패했다.
- **고정 사이클로 기다린 뒤 개수 검사.** 1100개를 보내고 `ClockCycles(clk, 300)` 뒤에 "1026개를 받았다"를 검사하면, 사이클마다 하나씩
  받는 설계는 그때 300개 안팎이다. 개수나 상태가 찰 때까지 기다리려면 `await h.drain()`이나 조건이 될 때까지 도는 루프(넉넉한
  상한 포함)를 쓴다 (P11 axis_async_fifo sc03_full).
- **`set_ready` 패턴은 되풀이한다.** `[0] * 40 + [1]`은 41사이클마다 한 번만 받는다. "막았다가 계속 받기"는 `then=1` (P8a fill_up).
- **"구현에 따름"이나 범위로 정한 값을 한 값으로 검사.** "약 5에서 9 사이클"을 `5 <= lat <= 9`로, 참고값 7을 기대값으로 쓰지
  않는다. "구현에 따름(검증하지 않음)"이면 검사하지 않는다 (P11 TR-01).
- **동기 리셋이 걸린 그 사이클의 출력.** 표본에서 리셋이 1인 첫 사이클의 출력은 아직 리셋 전 값이다 (P8 CRC-E01, P8a COBS-E01).
- **리셋 뒤에도 보내던 프레임의 나머지.** 리셋은 그 도메인의 남은 전송을 버린다. 리셋 뒤 출력은 리셋 뒤에 `h.send`한 것에서 나온다
  (P8a async_fifo sc02).
- **다른 도메인의 사이클 번호.** 사이클 번호는 도메인마다 자기 클럭으로 센다. 한 도메인의 속성에서 다른 도메인의 사이클 번호를
  같은 시각으로 보지 않는다. 예: `ClockCycles(dut.s_clk, 1400)` 뒤에 `set_ready`를 바꾸면 m 쪽(더 느린 클럭)에서는 1400보다
  이른 사이클에 바뀐다. m 쪽 속성이 "m 사이클 1400까지"를 가정하면 `ClockCycles(dut.m_clk, ...)`로 기다린다 (P11 axis_async_fifo sc03).
- **직전 사이클 값으로 같은 사이클 규칙 검사.** "일시정지(ack=1) 동안 valid=0"은 같은 표본의 ack와 valid로 본다. 직전 표본의 ack를
  저장해 두고 이번 valid와 비교하면 풀린 첫 사이클을 위반으로 읽는다 (P11 axis_async_fifo FR-PAUSE-01).

## 시나리오

- SPEC 8번 섹션의 예시 시나리오는 그대로 directed 테스트로 쓴다.
- 무작위 테스트는 `random.Random(고정 시드)`를 쓴다.
- backpressure(`set_ready`)와 입력 공백(`idle`)이 있는 테스트를 하나 이상 둔다. (번들이 있는 블록만)

## 검사

- `python3 tools/pl.py model <테스트 파일> l1.json`: 모델을 시뮬레이터 없이 무작위 입력으로 돌려 예외가 없는지 본다.
  SPEC이 허용하지 않는 입력(미정의)이 있으면 `Model.legal(self, stream, txn) -> bool`을 두어 그런 입력을 거른다.
- 문법 오류가 없어야 한다 (`python3 -m py_compile <파일>`).
