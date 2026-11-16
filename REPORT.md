# ChipVerilog 단독 생성 재현 — 논문 프로토콜, 64과제

2026-11-15 · 실행 2026-11-14 ~ 2026-11-15 (사용량 한도로 한 번 끊겼다가 이어서 실행)

## 1. 조건

| 항목 | 값 |
| --- | --- |
| 대상 | ChipVerilog 64과제 × 5샘플 = 모델당 320후보 |
| 입력 | `Src/**/description.txt`만. 논문 `run_codex_batch_txt.sh`와 같은 system/user 프롬프트, 같은 과제 이름 매핑 (`fpu_double`→`fpu`, `mips_16/*`→`mips_<top>`) |
| 생성 | `claude -p`, 도구 없음, 설정·MCP·세션 없음, 샘플마다 독립 요청. 피드백·정답·이전 출력 없음 |
| 모델 | claude-haiku-5-5, claude-sonnet-5-5 (effort 기본값) |
| 채점 | 공식 `tools/formal_equivalence.py verify`. `status == pass`만 기능 통과, `compile_gate_status == pass`가 문법 통과 |
| 지표 | `METRIC_DEFINITION.md`의 비편향 pass@k, 64과제 평균. 스크립트가 논문 캐시로 논문 표를 소수점까지 그대로 재현함 |

## 2. 채점 환경 차이 (먼저 확인한 것)

논문은 Yosys 0.62를 요구하고 이 머신은 Yosys 0.70이다. 0.70에서는 메모리 셀(`$mem_v2`)에 SAT 모델이 없어
등가 증명이 `equivalence_error`로 끝나는 경우가 있다 (예: mips_register_file은 논문 캐시 4/5 통과, 로컬 재채점 0/5).
**Yosys 0.62로 다시 확인 (2026-11-16):** 소스에서 빌드한 Yosys 0.62로 haiku 184개, sonnet 177개 실패 후보(컴파일 실패 제외)를 다시 채점했다. 361개 모두 판정이 같았다.
논문 Claude의 mips_register_file 통과 후보 4개도 0.62에서 같은 `$mem_v2` 오류가 난다. 차이는 Yosys 버전이 아니라 **채점 도구 로직**에서 온다.
논문 캐시는 이 후보들을 `simulation_tb`(제공 TB 시뮬레이션, "plain Verilog tb compile fallback")로 통과시켰다. 공개된 도구는 같은 TB를 "print-only tb, not a functional verdict"로 보고
등가 증명으로 넘긴다. 논문 캐시와 로컬 재채점을 비교하면 `simulation_tb` 통과가 `equivalence_error`로 바뀐 후보는 Claude 6개, GPT 7개, DeepSeek 2개다.
`bounded_seq` 실패가 `induction` 통과로 바뀐 후보도 Claude 5개, GPT 3개 있다.
그래서 **논문의 캐시 후보 960개를 같은 로컬 환경에서 다시 채점**해 같은 조건의 비교 기준(`(local)` 행)을 만들었다.
로컬 재채점은 논문 수치를 조금만 낮춘다 (Claude 기능 pass@1 17.50 → 16.88). 대부분의 `equivalence_error`는 원래 실패였던 후보가 라벨만 바뀐 것이다.

## 3. 총괄 (%, 64과제 평균)

| 모델 | 채점 | 문법 pass@1 | 문법 pass@5 | 기능 pass@1 | 기능 pass@5 |
| --- | --- | --- | --- | --- | --- |
| Claude Opus 4.5 (논문) | 논문 캐시 | 78.75 | 96.88 | 17.50 | 35.94 |
| GPT-5.4 (논문) | 논문 캐시 | 83.12 | 93.75 | 23.12 | 37.50 |
| DeepSeek V4 Pro (논문) | 논문 캐시 | 45.94 | 78.12 | 13.44 | 23.44 |
| Claude Opus 4.5 (논문 후보) | 로컬 재채점 | 79.37 | 98.44 | 16.88 | 32.81 |
| GPT-5.4 (논문 후보) | 로컬 재채점 | 84.38 | 95.31 | 21.25 | 34.38 |
| DeepSeek V4 Pro (논문 후보) | 로컬 재채점 | 45.94 | 78.12 | 13.12 | 23.44 |
| **haiku 5.5** | 로컬 | 80.62 | 90.62 | **23.12** | 26.56 |
| **sonnet 5.5** | 로컬 | 83.12 | 93.75 | **27.81** | **39.06** |

- 같은 환경 기준으로 sonnet 5.5는 기능 pass@1(27.8)과 pass@5(39.1) 모두 논문 최고(GPT-5.4: 21.3 / 34.4)보다 높다.
- haiku 5.5는 pass@1이 GPT-5.4보다 높지만 pass@5는 26.6으로 낮다. 샘플 간 다양성이 적어 같은 과제를 계속 맞히거나 계속 틀린다.
- 처음 "논문이 훨씬 높다"고 보였던 80~98%는 **문법(컴파일) 통과율**이다. 기능 통과율은 논문도 17~23% (pass@1)다.

## 4. 계열별 (통과 샘플 수 / 1개 이상 통과한 과제 수)

| 계열 | 과제 | Claude(로컬) | GPT(로컬) | DeepSeek(로컬) | haiku 5.5 | sonnet 5.5 |
| --- | --- | --- | --- | --- | --- | --- |
| or1200 | 38 | 25 / 12 | 35 / 13 | 20 / 8 | 45 / 10 | 48 / 15 |
| mips_16 | 11 | 27 / 7 | 30 / 7 | 21 / 6 | 26 / 6 | 30 / 6 |
| double_fpu | 9 | 2 / 2 | 3 / 2 | 1 / 1 | 0 / 0 | 1 / 1 |
| i2c | 3 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 5 / 2 |
| cordic | 3 | 0 / 0 | 0 / 0 | 0 / 0 | 3 / 1 | 5 / 1 |

- sonnet 5.5만 푼 과제: cordic, i2c_master_byte_ctrl, i2c_master_top, or1200_dmmu_top, or1200_ic_top, or1200_sprs.
- 논문 모델 중 하나는 풀었는데 sonnet 5.5가 못 푼 과제: fpu_addsub_pipeline, fpu_exceptions, fpu_sub, mips_IF_stage,
  mips_mips_16_core_top, mips_register_file, or1200_if, or1200_reg2mem.
- FPU 계열은 모든 모델이 거의 못 푼다. 원문이 단 구성·반올림 구조를 다 말하지 않는다. sonnet의 fpu_round 통과 1건은 원본 OpenCores 코드와 문장 순서까지 같아서 원본을 기억해 낸 것으로 보인다.

## 5. rtl-factory 파이프라인과의 관계

| 실행 | 조건 | 통과 과제 |
| --- | --- | --- |
| 파이프라인 haiku 5.5 (2026-11-09) | pymtl 변환 프롬프트, 공식 채점을 외부 판정으로 Repair 루프에 넣음 (정답 참조) | 12 / 64 (18.8%) |
| 단독 haiku 5.5 (이번) | 원문만, 5샘플, 피드백 없음 | 17 / 64 (pass@5 26.6%) |
| 단독 sonnet 5.5 (이번) | 원문만, 5샘플, 피드백 없음 | 25 / 64 (pass@5 39.1%) |

같은 haiku로 단독 생성(5샘플)이 파이프라인(2후보 + Repair)보다 많은 과제를 푼다. 파이프라인은 정답 등가 피드백까지 받는데도 그렇다.
SPEC 변환에서 원문 정보가 빠지는 손실(4.2절 3항 보상 이전 실행)과 후보 수 차이가 원인 후보다. 파이프라인 재실행 결과가 있어야 판단할 수 있다.

## 6. 재현 방법

```bash
D=build/bench/chipverilog_paper_protocol
cd build/datasets/ChipVerilog
$D/run_all.sh claude-sonnet-5-5 sonnet55   # 생성 + 공식 채점 (기존 후보는 건너뜀)
python3 -I $D/metrics.py $D                # 논문·로컬 재채점·우리 모델 pass@k 표와 과제별 통과 수
```

`gen.py`와 `run_all.sh`의 경로는 실행 당시 scratchpad를 가리키므로 다시 쓰려면 경로 상수를 바꾼다.

## 7. 파일

- `Result/{haiku55,sonnet55}/<과제>/` 후보 `.v`와 샘플별 `result.json`, 로그
- `regrade/Result/{claude,codex,deepseek}/` 논문 후보의 로컬 재채점
- `metrics.csv` 총괄 표와 과제별 통과 수
- `y062/Result/{haiku55,sonnet55}/` 실패 후보의 Yosys 0.62 재채점 (`regrade062.sh`). Yosys 0.62 바이너리는 `build/tools/yosys-0.62/yosys`
