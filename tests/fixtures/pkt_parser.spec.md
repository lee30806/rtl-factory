# pkt_parser SPEC (테스트용 발췌)

- FR-01: 입력 바이트가 SYNC_BYTE(0xA5)이면 헤더를 기다린다.
- FR-02: 헤더 바이트는 페이로드 길이 N(1 이상)이다.
- FR-03: 페이로드 N바이트를 모두 받은 다음 사이클에 pkt_valid를 한 사이클 1로 만든다.
- FR-04: s_ready는 항상 1이다.
- FR-05: pkt_valid는 한 사이클 펄스다.
