# axis_cobs_decode 요구사항 (원본 README와 동작에서 발췌, 이식 시험용)

- COBS-01: 입력 프레임은 COBS로 인코딩된 바이트열이며 마지막 바이트에 tlast가 붙는다. 출력은 디코딩한 원래 바이트열이다.
- COBS-02: 출력 프레임의 마지막 바이트에 tlast를 붙인다.
- COBS-03: 정상 프레임의 출력에는 tuser(오류 표시)를 붙이지 않는다.
- COBS-04: 출력은 AXI-Stream valid/ready 규칙을 지킨다 (ready를 볼 때까지 valid와 data를 유지).
