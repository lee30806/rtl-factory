# ChipVerilog 외 벤치마크 결과 정리

2026-11-16 · `build/bench/`의 실행 기록(`summary.json`, 로그)을 모은 것. 새로 실행한 것은 없다.

## 0. 데이터셋별 상태

| 데이터셋 (BENCHMARK_PLAN) | 상태 | 대표 실행 |
| --- | --- | --- |
| **RTLLM 2.0** (50문제) | 전체 실행 완료 | `rtllm_full_sonnet55_medium` (2026-10-31 ~ 11-04) |
| **RTL-BenchLS** 왕복 (B5, 별도 트랙) | 부분 실행. 335문제 중 132문제 | `rtl_benchls_slice01_sonnet55_medium_lecloop` (~11-06) |
| **RealBench** (64) | 어댑터·채점만 준비. **실행 결과 없음** | – |
| **CVDP v1.1.0** (152) | 어댑터·채점만 준비. **실행 결과 없음** | – |

## 1. RTLLM 2.0 (50문제, 원본 `testbench.v` + iverilog)

### 1.1 총괄

| 조건 | 모델 | 최종 출력 통과 (pass@1) | 후보 중 하나라도 | 외부 TB 보정 뒤 (최종 / 아무 후보) | 문제당 호출 | 문제당 분 |
| --- | --- | --- | --- | --- | --- | --- |
| **단독 생성** (원문 + 헤더, 1회 호출) | sonnet 5.5 medium | 40/50 (80%) | 40/50 | – | 1 | 0.3 |
| **rtl-factory** (2후보 SV·Py, 내부 Verifier) | sonnet 5.5 medium | **43/50 (86%)** | 44/50 (88%) | 45/50 (90%) / 49/50 (98%) | 11.2 | 3.1 |
| rtl-factory, SPEC만 sonnet | SPEC sonnet 5.5 + 나머지 haiku 5.5 | 38/50 (76%) | 42/50 (84%) | (보정 도입 전) | 7.4 | 6.0 |
| rtl-factory, SPEC sonnet (이전 버전) | 전부 sonnet 5.5 | 38/50 (76%) | 44/50 (88%) | (보정 도입 전) | 8.3 | 3.5 |

- 핵심 비교는 같은 모델(sonnet 5.5)의 단독 생성 80% 대 rtl-factory 86%로, **+6%p**다. 후보 기준으로는 88%, 외부 TB 보정을 넣으면 최종 90%다.
- 외부 TB 보정은 RTLLM 테스트벤치 결과를 근거로 SPEC을 고쳐 다시 돌린 결과다. 채점 TB를 본 결과라 pass@1에 넣지 않는다.
- 아래 두 행(10-28, 10-29)은 외부 TB 보정, golden 선택, SPEC 규칙 개선이 들어가기 전의 코드다.
- 같은 날 실행한 `harness` 1차(10-31)는 36문제가 1분 안에 실패로 끝나 무효로 보고 `harness_r2`로 다시 돌렸다. 그 1차의 8/50은 위 표에 넣지 않았다.
- 후보별 (r2): SV 후보 기능 통과 85.7% (49문제), Python 후보 85.7% (49문제). 시뮬레이션 모델 경로는 2문제에서 둘 다 통과했다.

### 1.2 실패 문제

| 문제 | 단독 | rtl-factory (r2) | 비고 |
| --- | --- | --- | --- |
| asyn_fifo | ✗ | ✗ | 두 조건 모두 실패. 외부 TB 보정 실험(worst*)에서 따로 다룸 |
| fixed_point_adder | ✗ | ✗ | 두 조건 모두 실패 |
| freq_divbyodd | ✗ | ✗ | 두 조건 모두 실패 |
| serial2parallel | ✗ | ✗ | 두 조건 모두 실패. 외부 TB 보정 뒤 최종 통과 (`rtllm_extfix`) |
| multi_pipe_8bit, synchronizer, alu | ✓ | ✗ | rtl-factory만 실패 |
| fixed_point_substractor, ring_counter, fsm, freq_divbyfrac, pulse_detect, clkgenerator | ✗ | ✓ | rtl-factory만 통과 |

### 1.3 이전 35문제(동기식 서브셋) 실행 (참고, 2026-10-23 ~ 26)

| 조건 | 최종 통과 | 후보 중 하나라도 |
| --- | --- | --- |
| 단독 생성 (`rtllm_standalone`) | 34/35 (97.1%) | 34/35 |
| rtl-factory (`rtllm_harness`) | 20/35 (57.1%) | 30/35 (85.7%) |

초기 하니스로 돌린 결과다. 이후 SPEC 규칙과 선택 로직을 고쳐 1.1의 결과가 되었다. 모델 설정은 각 실행의 roles 설정을 따른다.

## 2. RTL-BenchLS 왕복 (B5, Yosys 등가)

원본 RTL에서 SPEC을 뽑고, 그 SPEC만으로 RTL을 다시 만들어 원본과 등가인지 본다.

| 실행 | 모델 | 문제 | SPEC 생성 | 1회 왕복 등가 (pass@round1) | SPEC 보정(R6) 뒤 | 문제당 호출 / 분 |
| --- | --- | --- | --- | --- | --- | --- |
| slice01 (LEC 루프) | sonnet 5.5 medium | 132 / 335 (진행 중 멈춤) | 123/132 (93.2%) | **111/132 (84.1%)**, 그중 BMC로만 확인 12 | 111/132 (보정 효과 없음) | 6.6 / 4.8 |
| 파일럿 (하니스) | sonnet 5.5 medium | 6 | 6/6 | 4/6 | 4/6 | 11.7 / 12.3 |
| 파일럿 (단독) | sonnet 5.5 medium | 6 | 6/6 | 4/6 | – | – |

- 등가 채점은 Yosys(`equiv_*` 귀납, 실패 시 miter + sat)다. 공식 채점(JasperGold SEC, Conformal LEC)과 다르다.
- slice01은 335문제 중 132번째에서 멈췄다. 나머지 203문제는 실행하지 않았다.

## 3. 요약

1. **RTLLM 2.0:** sonnet 5.5에서 rtl-factory 86%로, 단독 생성 80%보다 6%p 높다. 단독이 못 푼 6문제를 풀고, 단독이 푼 3문제를 놓친다.
2. **RTL-BenchLS:** 132문제에서 1회 왕복 등가 84.1%다. SPEC 보정 루프는 추가로 통과시킨 문제가 없다.
3. **RealBench, CVDP:** 아직 실행하지 않았다. 11-13 확정한 4종 중 두 개가 결과가 없다.
4. **ChipVerilog와의 대비:** 포화된 RTLLM에서는 rtl-factory가 단독보다 낫지만, ChipVerilog(haiku)에서는 단독 생성보다 낮다 (`chipverilog_paper_protocol/RTLF_PAPER_FORMAT.md`).
   하위 모듈이 있는 큰 IP에서 하네스 결함과 SPEC 손실이 커진다.
