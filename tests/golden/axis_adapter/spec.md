# axis_adapter 요구사항 (32비트 → 8비트 설정, 이식 시험용)

- ADP-01: 입력 한 비트(beat)의 유효 바이트(tkeep)를 낮은 바이트부터 차례로 8비트 출력으로 낸다.
- ADP-02: 입력 비트의 tlast는 그 비트의 마지막 유효 바이트 출력에만 붙인다.
- ADP-03: tid, tdest, tuser는 입력 비트의 값을 그 비트에서 나온 모든 출력 바이트에 붙인다.
- ADP-04: 출력 tkeep은 항상 1이다 (출력 keep 비활성 설정).
- ADP-05: 출력은 AXI-Stream valid/ready 규칙을 지킨다.
