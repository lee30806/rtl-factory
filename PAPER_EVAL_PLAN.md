# PAPER_EVAL_PLAN

2026-11-16 · rtl-factory 논문의 평가 설계(벤치마크 구성, 비교 엔진, 프로토콜)와 그 전에 끝낼 하네스 결함 수정 계획.
근거 자료: `build/bench/chipverilog_paper_protocol/{REPORT,RTLF_PAPER_FORMAT}.md`, `build/bench/OTHER_BENCH_SUMMARY.md`,
`build/bench/COMPARE_pymtl_rtl_factory.md`, `BENCHMARK_PLAN.md`.

## 1. 논문이 보일 주장 (가설)

평가표는 아래 주장을 하나씩 확인하거나 반박하도록 짠다. 통과율 하나로는 H2~H4를 보일 수 없다.

| ID | 주장 | 확인하는 실험 |
| --- | --- | --- |
| H1 | 같은 모델·같은 입력에서 rtl-factory가 기존 엔진보다 기능 통과율이 높다 | 본 결과표 (5절 T1) |
| H2 | 그 차이는 호출 수가 많아서가 아니라 구조(SPEC·IR·검증·Repair) 덕분이다 | 비용 맞춤 비교 (T2), 구성 요소 제거 (T3) |
| H3 | 명세가 길고 모호하거나 순차 동작이 복잡할수록 이득이 커진다. 반대로 원문이 구현 수준으로 자세한 계층형 IP에서는 손해가 있다 | 난이도·유형별 분석 (T4) |
| H4 | 통과율 밖의 이점: 결과마다 SPEC·IR 추적이 남고, 내부 검증 판정이 외부 판정과 맞으며, 합성 가능한 RTL이 나온다 | rtl-factory 고유 지표 (T5) |

지금까지 나온 결과로는 H1이 RTLLM(+6%p)에서만 보이고, ChipVerilog에서는 반대다 (haiku 단독 생성 pass@5 17과제 대 파이프라인 12과제).
H3의 "손해" 쪽은 숨기지 말고 한계로 쓰는 편이 심사에서 유리하다.

## 2. 벤치마크 구성

### 2.1 본 평가 (컴파일 + 시뮬레이션 판정)

| 트랙 | 문제 수 | 채점 | 이 트랙이 보이는 것 | 비고 |
| --- | --- | --- | --- | --- |
| **CVDP v1.1.0** `cid003` + `cid005` | 150 (결함 2 제외) | 공식 cocotb 하니스 (Docker 없이) | 산업 명세→RTL, 모듈 재사용. **주 변별 트랙** | 정답 RTL 비공개. 헤더 있는 문제 8개뿐이라 HeaderWriter가 필요한데, 모든 엔진에 같은 헤더를 준다 |
| **RealBench** | 64 (모듈 60, 시스템 4) | 원본 Verilator TB (정답 모델과 비교) | 긴 문서형 명세 + 그림, 시스템 통합. **SPEC 단계 효과가 가장 잘 드러날 트랙** | 암호화 배포라 학습 오염이 가장 적다 |
| **ChipVerilog-TB** | 12 | 공식 `formal_equivalence.py`의 자체 검사 TB 경로 | 공개 IP(OpenCores) 시뮬레이션 판정 | mips_16 7, fpu 3, cordic, i2c_master_top. 작고 치우쳐서 **보조 트랙**으로 둔다 |
| **RTLLM 2.0** | 50 | 원본 `testbench.v` (iverilog) | 기초 역량, 회귀 확인 | 포화 (단독 80%). 차이를 주장하는 근거로 쓰지 않는다 |

합계 276문제. 결과는 트랙별로 따로 보고한다. 문제 수가 달라 합산 평균은 CVDP에 끌려가므로, 합산은 매크로 평균으로 하나만 낸다.

### 2.2 보조 평가 (등가 판정, 부록)

| 트랙 | 문제 수 | 채점 | 쓰임 |
| --- | --- | --- | --- |
| ChipVerilog-EQ | 47 (64 − 결함 5 − TB 12) | 공식 도구 Yosys 등가 | 사이클 정확도까지 보는 엄격한 판정. 계층형 IP(or1200) 결과는 여기에만 있다 |
| ChipVerilog 논문 프로토콜 | 64 | 공식 도구 | ChipVerilog 논문 표 4와 직접 비교 (이미 수행: 단독 haiku·sonnet 5.5) |
| RTL-BenchLS 왕복 | 335 | Yosys LEC | 역방향(RTL→SPEC→RTL) 능력. 별도 절 |

### 2.3 결함·주의 목록 관리

- 문제별 제외·주의 표시는 `experiments/bench/datasets.json`의 `exclude`·`flagged`만 쓴다. 논문 부록에 그대로 싣는다.
- ChipVerilog-TB 12문제 목록과 출력만 하는 TB 4문제(IF_stage, mips_16_core_top, register_file, or1200_top)는 `flagged`로 남긴다.
- 모든 정답(있는 경우)을 후보로 넣어 채점해 보는 자기 채점을 트랙마다 한 번씩 돌리고 결과를 기록한다. ChipVerilog는 이미 했다.

## 3. 비교 엔진

| 엔진 | 설명 | 역할 |
| --- | --- | --- |
| E0 단독 생성 | 원문 + 헤더 → 모델 1회 호출 (`--mode standalone`) | 기준선 |
| E0-k 비용 맞춤 단독 | E0을 rtl-factory 문제당 평균 호출 수만큼 독립 샘플링하고 그중 컴파일 통과를 고름 (선택에 채점 TB를 쓰지 않음) | H2: 호출 수 효과 분리 |
| E1 SPEC 유도 단독 | SpecWriter → 단일 Author가 SV 작성 (pymtl `spec_guided`와 같은 구조) | H2: SPEC 단계만의 효과 |
| **E2 rtl-factory** | IR 파이프라인 (Architect → Author c0/c1 → Validator → 방출 → Verifier → Repair) | 제안 프레임워크 |
| E3 ACE-RTL, E4 MAGE, E5 VeriGraphi | pymtl 비교 실험의 외부 엔진 | 기존 연구 비교 |

- **같은 기반 모델:** 모든 엔진을 같은 모델로 돌리는 것이 원칙이다. 주 모델 1개(sonnet 5.5 medium 권장)로 전체를 돌리고, 보조 모델 1개(haiku 5.5)로 CVDP와 RealBench만 반복한다.
  외부 엔진이 특정 API에 묶여 있으면 모델 어댑터를 붙이고, 못 붙이면 그 엔진은 "원저자 설정" 열로 따로 보고한다.
- **같은 채점기:** pymtl 비교 실험의 `run_sim.sh`는 관대한 판정(컴파일만으로 PASS, lint로 PASS)이 있어 쓰지 않는다. 모든 엔진의 산출 RTL을 이 저장소의 트랙별 공식 채점기로 다시 채점한다.
- **같은 입력:** 원문 명세, 헤더, include, 제공 셀만 준다. 변환본이 덧붙인 "추가 엔지니어링 안내"는 넣지 않거나 모든 엔진에 똑같이 넣는다.

## 4. 실행 프로토콜

1. **독립 실행 5회:** 엔진마다 문제당 5회 독립 실행하고, 최종 출력 5개로 syntax/function pass@1·pass@5를 낸다 (ChipVerilog 논문과 같은 비편향 추정식).
   rtl-factory는 실행 한 번이 최종 출력 하나다. 내부 후보 c0/c1은 따로 집계해 T5에 쓴다.
2. **채점 TB 비노출:** 본 결과에는 외부 TB 피드백(외부 TB 보정, external_repair, 공식 채점을 E2 판정으로 쓰는 `skip_verifier` 루프)을 쓰지 않는다.
   외부 TB 보정 결과는 "오라클 피드백" 열로 분리해 부록에 둔다.
3. **비용 기록:** 문제마다 에이전트 호출 수, 입력·출력 토큰, 벽시계 시간(사용량 한도 대기 제외)을 남긴다. 지금 `bench_result.json`에 호출 수와 분은 있지만 토큰은 없다 (6절 D9).
4. **채점 환경 고정:** iverilog, Verilator, Yosys, cocotb 버전을 기록한다. ChipVerilog는 공개 도구가 논문 캐시와 판정 로직이 달라서, 기존 엔진 결과도 같은 환경에서 다시 채점해 쓴다 (이미 확인: Yosys 0.62와 0.70의 판정 차이는 없음).
5. **실패 분류:** 컴파일 실패 / 기능 실패 / SPEC 실패 / 후보 없음 / 채점 도구 오류 / 시간 초과로 나누고, 채점 도구 오류는 엔진 성능에서 빼서 따로 표시한다.

## 5. 논문 표 구성

| 표 | 내용 |
| --- | --- |
| T1 본 결과 | 트랙(4) × 엔진: syntax pass@1·@5, function pass@1·@5 (주 모델) |
| T2 비용 맞춤 | 같은 호출·토큰 예산에서 E0-k 대 E2. 문제당 평균 비용과 통과율 |
| T3 구성 요소 제거 | E2에서 하나씩 뺌: SPEC 단계(원문 직접 → IR), Architect 분할, Verifier, Repair, 후보 2개 → 1개, Python 후보. CVDP + RealBench에서 |
| T4 난이도·유형 | 정답·명세 길이 구간, 하위 모듈 수(0/1/2+), 순차/조합, 트랙별 |
| T5 rtl-factory 고유 | 내부 Verifier 판정과 외부 판정의 일치율(오탐·미탐), 합성 성공률과 셀 수, SPEC 검토 알람·사람 확인 요청 수, 실패 원인 분포 |
| 부록 | ChipVerilog 논문 프로토콜 비교, ChipVerilog-EQ, RTL-BenchLS, 오라클 피드백 결과, 데이터 결함 목록 |

**통계:** 문제별로 짝지은 McNemar 검정(E2 대 각 엔진), pass@1의 부트스트랩 95% 신뢰 구간. 모델 2개 × 엔진 6개 × 5회라 실행량이 크다. 7절 단계 3에서 주 모델만 먼저 돌린다.

**오염:** ChipVerilog(OpenCores)는 원본을 외운 흔적이 있다 (fpu_round 통과 코드가 원본과 문장 순서까지 같음). 원본 일치도(토큰 n-gram 겹침)를 통과 후보마다 재서 부록에 싣는다.

## 6. 하네스 결함 수정 계획

11-07~09 ChipVerilog 64과제 실행에서 나온 결함과 그 뒤 확인된 것들. 상태는 2026-11-16 코드 기준이다.

| ID | 결함 | 영향 (근거 실행) | 상태 | 할 일 | 완료 확인 |
| --- | --- | --- | --- | --- | --- |
| D1 | 재사용 셀과 같은 이름 모듈이 후보 파일에 중복 정의되어 컴파일 실패 | ChipVerilog cross_module 8과제 | **코드 반영됨** (`dedupe_modules`를 ChipVerilog·RealBench·CVDP 채점이 씀) | 저장된 8과제 후보를 다시 채점만 해서 확인. 회귀 시험 추가 | 8과제 모두 compile_fail이 아님 |
| D2 | 방출 RTL의 메모리 배열이 Yosys `$mem_v2`가 되어 공식 등가 검사가 오류 | ChipVerilog 12과제 (EQ 트랙만 해당) | 미해결 | 방출기에 "작은 배열을 레지스터로 펼침" 옵션을 두고 EQ 트랙에서만 켠다. 대안으로 모든 엔진 후보에 같은 `memory_map` 전처리를 하고 부록에 명시한다. 본 평가(TB 트랙)는 영향 없음 | 12과제 후보를 다시 채점해 equivalence_error 0 |
| D3 | external 모듈을 방출하려다 검증 중단 | if_stage 1과제 (보류) | **코드 반영됨** (verify가 external은 rtl_files를 참조) | if_stage 재실행 | 방출 오류 없이 채점까지 감 |
| D4 | 헤더 추출 실패로 SPEC 실패 | fpu(셀 재정의), i2c_master_bit_ctrl(동기·비동기 리셋 혼재), or1200_top(식별자) 3과제 | 미확인 | 세 과제를 현재 코드로 SPEC 단계만 실행 (`--mode spec`). 남는 실패는 헤더 추출기 수정 또는 `flagged` 처리 | SPEC 게이트 통과 |
| D5 | 후보 없음 (Author/Repair가 IR을 못 만듦) | 6과제 (i2c 2, mips 2, or1200 2) | 원인 미분석 | 작업 로그에서 Validator 거절 사유를 분류해, 하네스 문제(입력 누락, 셀 연결)와 모델 실패를 나눈다 | 원인표 작성, 하네스 원인은 수정 |
| D6 | 출력만 하는 TB를 판정 근거로 셈 | ChipVerilog TB 16 → 실제 판정 가능 12 | 미해결 | `chipverilog_tb_files`에 공식 도구의 `tb_is_self_checking` 판정을 적용해 SPEC 보정·Repair 근거에서 뺀다 | 시험: 4개 TB가 제외됨 |
| D7 | 고칠 수 없는 실패(채점 도구 오류, 중복 정의)도 Repair에 넣어 호출 낭비 | 11-09 실행 20과제가 Repair 상한까지 소모 | 미해결 | 외부 판정 결과를 "후보 결함 / 채점 환경 결함"으로 분류해 후자는 Repair하지 않고 리뷰로 남긴다 | 채점 오류 후보의 Repair 호출 0 |
| D8 | ChipVerilog 입력이 pymtl 변환본(원문 + 추가 안내) | 단독 생성과 입력이 달라 비교 불가 | 미해결 | ChipVerilog 로더에 원문 `description.txt` 입력 모드를 추가하고 논문 실험에서는 그것만 쓴다 | 같은 과제에서 E0과 E2의 입력 텍스트가 같음 |
| D9 | 토큰 사용량을 기록하지 않음 | T2 비용 비교 불가 | 미해결 | 에이전트 실행 결과에서 입력·출력 토큰을 모아 `bench_result.json`에 합산 | 요약표에 토큰 열 |
| D10 | 외부 엔진 결과를 관대한 채점(`run_sim.sh`)으로 판정 | pymtl 비교 28과제: 27 → 20 등 부풀려짐 | 확인됨 | 외부 엔진 산출물을 트랙별 공식 채점기로 다시 채점하는 스크립트 | 모든 엔진이 같은 채점 함수 사용 |
| D11 | 원문 보존 보상·SPEC 읽기 안내 이전 결과만 있음 | ChipVerilog 64과제 결과가 11-14 개선 전 | 코드 반영됨, 재실행 안 됨 | D1~D8 반영 뒤 한 번에 재실행 | – |

### 6.1 1단계 진행 결과 (2026-11-16)

| ID | 결과 |
| --- | --- |
| D1 | 확인함. 11-09 실행의 8과제 15후보를 지금 채점기로 다시 채점: 컴파일 실패 15 → 1 (남은 1개는 후보 결함, dmmu_top c0). 그러나 9개가 `equivalence_error`·`equivalence_timeout`으로 바뀌어 **D2가 다음 병목**이다 |
| D2 | 미착수. 위 재채점으로 영향이 커진 것을 확인 (cross_module 후보 15개 중 9개) |
| D3 | 코드 반영 확인. if_stage 헤더 추출 정상. 재실행은 2단계 파일럿에서 |
| D4 | fpu: 제공 셀(fpu_div.v 포트 재선언)만 slang이 거부하면 셀 없이 헤더를 추출하도록 수정. i2c_master_bit_ctrl: D12로 해결. or1200_top: 원본 description.txt가 선언 안 된 파라미터 `ppic_ints`를 포트 폭에 써서 데이터셋 결함으로 `flagged.header_defect`에 기록 |
| D5 | 원인 분류: i2c 2 = 헤더 변환 결함(D12, 수정), ex_stage = 재사용 셀 연결(현재 코드에서 셀을 external로 바꿈, 재실행 확인 필요), data_mem = IR이 비동기 읽기 메모리를 표현 못함(V8, 파이프라인 한계), or1200_immu_top = Author 실패(모델), or1200_iwb_biu = 다중 클럭 wire 도메인 판정(V8, 파이프라인 한계) |
| D6 | 완료. 공식 도구와 같은 자체 검사 판정. 64과제 중 12과제만 TB 근거 |
| D7 | 완료. 공식 판정이 `equivalence_error`·`equivalence_timeout`·`reference_fail`·도구 오류면 Repair하지 않고 `GRADER_DEFECT` 리뷰로 멈춤 |
| D8 | 완료. `run_bench --prompt-source raw`면 원본 description.txt만 입력 |
| D9 | 완료. claude 에이전트 설정에 JSON 출력을 켜고 토큰·비용을 작업 기록과 `bench_result.tokens`, `summary.json`의 `tokens_total`에 합산 |
| D12 (신규) | pymtl 변환본 헤더가 i2c 3과제에서 원문과 다름 (포트 누락, 산문 단어를 포트로 씀). 원본 선언 + 포트 설명 절로 헤더를 다시 만듦. 같은 과제에서 16비트 `clk_cnt`를 클럭으로, 동기 `rst`를 도메인 리셋으로 잡던 것도 고침 (여러 비트 포트는 클럭 아님, 포트 설명 줄의 동기/비동기 우선, 클럭 하나에 리셋이 여럿이면 비동기 리셋을 도메인 리셋으로). RTLLM 50, ChipVerilog 64, RealBench 64 헤더 추출 결과를 이전 코드와 비교해 의도한 4과제 외 변화 없음 |
| D13 (신규) | 원본 TB가 계층 경로로 내부 이름을 읽음 (`uut.dmem.ram`, `uut.reg_array`, `uut.pc`). SPEC 읽기 안내, Architect, Author 지침을 "원문 이름 그대로"로 바꾸고, 원문의 "instantiated as X"를 `notes.named_instances`로 SPEC 9절에 옮기게 함 |
| D14 (신규) | 헤더의 `define을 rtl-factory 방출 단계에서 Block Top 파일 끝에 넣음 (채점기는 후보를 고치지 않음). 처음 본 cordic TB 컴파일 실패는 셀 없이 다시 채점한 탓이었고, 지금 코드는 셀이 같은 매크로를 정의해 원래도 통과한다. 방출 RTL이 스스로 완결되도록 남김 |

공정성 관련으로, pymtl에서 지적된 "정답 RTL에서 클럭·리셋 힌트 추출"은 이 저장소에 이미 없다 (확인함).

## 7. 일정 (순서와 완료 기준)

| 단계 | 작업 | 완료 기준 | 예상 비용 |
| --- | --- | --- | --- |
| 1 | 결함 수정 D1·D3 확인, D6·D7·D8·D9 구현, 회귀 시험 | 시험 통과, D1·D3 재채점 확인 | 에이전트 호출 거의 없음 |
| 2 | 자기 채점 (CVDP는 정답이 없어 생략) + 파일럿: 트랙마다 5문제 × E0·E2 1회 | 채점 경로가 엔진 모두에서 끝까지 돎 | 소량 |
| 3 | 주 모델로 T1 (E0, E1, E2 × 276문제 × 5회) | T1 표 완성 | 가장 큼. E2만 문제당 약 10호출 × 5회 |
| 4 | 외부 엔진 E3~E5 실행과 공식 채점기 재채점 | T1에 외부 엔진 열 추가 | pymtl 환경 |
| 5 | T2 비용 맞춤 (E0-k), T3 제거 실험 (CVDP + RealBench, 1회씩부터) | T2·T3 표 | 중간 |
| 6 | T4·T5 분석, 통계 검정, 부록 (EQ 트랙, D2 반영 뒤) | 논문 표 전체 | 분석 위주 |

단계 3의 사용량을 줄이려면 E2의 5회 반복을 CVDP + RealBench에서 먼저 하고, RTLLM과 ChipVerilog-TB는 3회로 시작해 차이가 애매한 경우만 늘린다.

## 8. 결정이 필요한 것

1. 주 모델 (sonnet 5.5 medium 권장)과 보조 모델.
2. 외부 엔진(ACE-RTL, MAGE, VeriGraphi)을 같은 모델로 돌릴 수 있는지. pymtl 환경의 어댑터 확인이 필요하다.
3. ChipVerilog-EQ를 본문에 넣을지 부록으로 둘지. D2 해결 여부에 달려 있다.
