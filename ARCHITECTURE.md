# ARCHITECTURE

> 이 문서는 원본 저장소 [chip-design-department](https://github.com/lee30806/chip-design-department)에서 복사했습니다. rtl-factory에는 RTL 생성 파이프라인과 원본 RTL → IR·SPEC 변환만 있고, 부서 계층(gateway, department, API, EDA 래퍼)과 P0 실험, PROGRESS.md는 원본 저장소에 있습니다.

Oct 8, 2026 · @Seungjoon Lee

Chip Design Department의 아키텍처 개요입니다. 시스템은 SPEC에서 RTL Freeze까지를 수행합니다.

이 문서는 두 설계 문서를 한곳에 모은 지도입니다. 세부 결정과 근거는 원문을 따릅니다.

- [Chip Design Department 설계](https://github.com/lee30806/chip-design-department/blob/main/docs/Chip%20Design%20Department%20설계.md): 부서 계층, Seat, 환류, 승인, 인프라, Web UI
- [LLM 기반 RTL 생성 파이프라인 설계](./docs/LLM%20기반%20RTL%20생성%20파이프라인%20설계.md): SPEC 규격, 파이프라인, IR 스키마, Validator, 검증, 이식 시험
- [RTL 코딩 규칙](./docs/RTL%20코딩%20규칙.md): 생성 RTL이 지키는 규칙과 어느 검사가 강제하는지
- [용어집](./docs/용어집.md): 문서와 코드에서 쓰는 말

---

## 1. 핵심 원칙

| 원칙 | 내용 |
| --- | --- |
| 두 계층 | 부서 계층(영속적 Seat)은 계획, 운영, 사람과의 대화, 독립 검증을 맡습니다. 실행 계층(파이프라인)은 결정론적 스케줄러와 단기 에이전트로 RTL을 생성하고 검증합니다. |
| IR이 유일한 소스 | RTL은 항상 IR에서 방출합니다. RTL을 IR로 되돌리는 역파싱은 없습니다. 손으로 고친 RTL은 다음 방출에서 사라지므로, 모든 수정은 파이프라인 입력으로 들어갑니다. |
| 구조는 툴, 동작은 LLM | 계층, 인터페이스, 상태 선언은 JSON입니다. 동작은 타입이 붙은 슬롯 안의 조각이고, 조각 언어는 모듈마다 SV 또는 Python(사이클 단위)입니다. Python 조각도 IR 슬롯 안에 두므로 소스맵은 SV 줄 → (IR 노드, 조각 줄) 한 번으로 끝납니다. 툴은 구조(포트, 리셋, 드라이버, 도메인)를 보장하고, 오류는 조각 하나로 국소화됩니다. |
| 결정론적 제어 흐름 | 순서, 분기, 반복 상한, 게이트는 스케줄러가 정합니다. 에이전트끼리는 메시지를 주고받지 않고 산출물로만 통신합니다. |
| 생성과 검증의 격리 | Author와 Verifier는 서로의 산출물을 보거나 고칠 수 없고, 서로 다른 계열의 모델을 권장합니다. 같은 계열이면 허용하되 스케줄러가 경고하고 검토 항목에 남깁니다. DV Pod는 파이프라인 테스트를 보지 않고 따로 검증합니다. |
| coding agent 기반 | LLM은 모두 coding agent 세션으로 씁니다. Seat는 OpenRig가 관리하는 장기 세션이고, 파이프라인 에이전트는 스케줄러가 작업마다 띄우는 headless 단기 세션입니다. 모델 API를 직접 호출하는 코드는 두지 않습니다. 기본 런타임은 Claude Code입니다. |
| 대화는 OpenRig로 | 사람의 대화는 Web UI → 게이트웨이 → OpenRig를 거쳐 각 Seat에 전달됩니다. |
| 단일 수정 창구 | 다른 Pod에서 나온 수정 요구는 Design-lead를 거쳐 네 가지 환류 형태 중 하나로만 들어갑니다. |
| 사람 승인 세 곳 | SPEC 고정, 최종 승인, RTL Freeze입니다. 실행 중의 게이트는 멈추지 않고 검토 항목으로 기록합니다. |
| 폐쇄망 | 파이프라인을 포함한 전체를 폐쇄망과 로컬 모델(비전 모델 포함)로만 실행합니다. |

## 2. 시스템 구성

```mermaid
flowchart TB
  Human([사람 · Web UI])
  GW[게이트웨이<br/>Python · 다중 사용자 · 승인 기록]

  subgraph DEPT[부서 계층 · OpenRig · coding agent 장기 세션]
    direction TB
    CL[Chip-lead]
    subgraph DP[Design Pod]
      DL[Design-lead]
      AE[Architecture Engineer]
      RE[RTL Engineer]
      SE[Sanity Engineer]
    end
    subgraph DVP[DV Pod]
      DVL[DV-lead · vPlan · TB · Checker/Assertion]
    end
    subgraph PIP[PI Pod]
      PIL[PI-lead · Synthesis · LEC · Power Intent · STA · SDC Lint]
    end
  end

  subgraph EXEC[실행 계층 · RTL 생성 파이프라인]
    SCH[결정론적 스케줄러]
    AG[Architect · Author · Verifier · Repair · Arbiter<br/>이식: Porter · SpecWriter<br/>headless coding agent]
    TL[Validator · Emitter · Verify · Classifier]
  end

  INFRA[(모델 서빙 · EDA 실행 환경 · Git 저장소)]

  Human --> GW
  GW -- 대화 · OpenRig 메시지 --> DEPT
  GW -- 승인 · 조회 --> SCH
  RE -- 실행 제출 --> SCH
  SCH -- 상태와 결과 --> DEPT
  DL -- 환류 --> SCH
  SCH --> AG & TL
  DVP -- 수정 요구 --> DL
  PIP -- 수정 요구 --> DL
  SCH -- 승인된 RTL --> DVP & PIP
  EXEC --- INFRA
  DEPT --- INFRA
```

| 계층 | 맡는 일 | 에이전트의 수명 | 제어 방식 |
| --- | --- | --- | --- |
| 사람 인터페이스 (Web UI) | 상태 열람, Seat와의 대화, lead에게 작업 지시, 승인 | — | 게이트웨이의 역할별 권한. 대화는 OpenRig를 거쳐 Seat에 전달 |
| 부서 계층 | 계획, 파이프라인 운영, 독립 검증, 구현 검증 | 영속적 Seat. 마일스톤 단위로 세션 초기화 | lead의 조율과 메시지 |
| 실행 계층 | SPEC에서 RTL을 생성하고 검증 | 모듈 단위의 단기 에이전트 | 결정론적 스케줄러 |
| 인프라 | 세션, 모델, EDA 툴, 저장소 | — | 폐쇄망 |

## 3. 부서 계층

총괄 1개와 Design, DV, PI 세 Pod로 이루어지며, Seat는 모두 15개입니다.

| Pod | Seat | 핵심 책임 |
| --- | --- | --- |
| 총괄 | Chip-lead | 마일스톤 계획, Block Top별 실행 순서, 최종 승인 자료 취합 |
| Design | Design-lead | 수정 요구의 단일 창구. 환류 분류. Repair 충돌 결정 (고칠 모듈 지정 또는 Architect 재구성) |
| | Architecture Engineer | SPEC 변환 결과 정리, 검사 질문의 답 초안. 정본은 사람이 확정 |
| | RTL Engineer | 파이프라인 실행 제출과 감시, 멈춘 모듈의 1차 분류. IR과 RTL을 직접 고치지 않음 |
| | Sanity Engineer | Lint(E3 결과를 넘겨받음), Superlint, Xprop, DFT, DCLint, IP-XACT, EPC |
| DV | DV-lead, vPlan, TB, Checker/Assertion | 파이프라인 Verifier와 독립된 검증. 고정된 SPEC과 L1만 입력으로 받음 |
| PI | PI-lead, Synthesis, LEC, Power Intent, STA, SDC Lint | 승인된 RTL의 구현 가능성 판정. UPF 작성 |

이름이 비슷한 두 역할을 구분합니다.

- **Architecture Engineer(Seat).** 실행 전에 SPEC을 다룹니다.
- **Architect(파이프라인 에이전트).** 실행 중에 Block Top 내부를 모듈로 분해합니다.

## 4. 실행 계층: RTL 생성 파이프라인

### 4.1 데이터 흐름

```mermaid
flowchart LR
  SRC[Word/PDF SPEC<br/>엑셀 레지스터 맵<br/>Visio/SVG 다이어그램] --> CONV[SPEC 변환<br/>툴 + 비전 coding agent]
  CONV --> CHK{SPEC 검사<br/>툴 + LLM}
  CHK -- 질문 --> SRC
  CHK -- 사람 고정 --> ARCH[[Architect]]
  ARCH -- L0/L1 동결 --> AUTH[[Author ×2<br/>SV 또는 Python 조각]]
  ARCH --> VER[[Verifier ×2]]
  AUTH -- py 조각 --> LOW[조각 번역기<br/>Python → SV]
  LOW --> VAL
  AUTH --> VAL[Validator V1~V11]
  VAL --> EMIT[Emitter<br/>SV · 소스맵 · SDC 골격]
  EMIT --> VFY[Verify E1~E3<br/>py: 번역기 검사]
  VER -- 참조 모델 · 테스트 --> VFY
  VFY -- 실패 --> CLS[Classifier]
  CLS --> REP[[Repair]]
  REP -- patch --> VAL
  REP -. 형제끼리 원인을 넘김 .-> DLD{Design-lead}
  DLD -. 고칠 모듈 지정 .-> REP
  DLD -. 근본 원인 수정 .-> ARCH
  CLS -. 기댓값 의심 .-> ARB[[Arbiter]]
  ARB -. 사람 승인 .-> VER
  VFY -- 모든 모듈 PASS --> INT[통합 E1~E3<br/>Block Top · Top]
  INT --> OUT[승인 대상 RTL<br/>소스맵 · 검증 결과 · 검토 항목]
```

이중 괄호 상자가 LLM 에이전트이고, 나머지는 결정론적 툴입니다. LLM 에이전트는 모두 스케줄러가 작업마다 headless로 띄우는 coding agent 세션입니다. 작업 디렉터리에 과제 파일, 입력 산출물, 도구를 두고 실행하며, 종료 후 정해진 경로의 산출물을 회수합니다.

### 4.2 구성요소

| 구성요소 | 종류 | 입력 | 출력 |
| --- | --- | --- | --- |
| SPEC 변환 | 툴, 비전 coding agent | Word/PDF, 다이어그램 원본, 엑셀 | 10개 섹션 Markdown, WaveJSON, 경계 신호 목록 |
| SPEC 검사 | 툴, LLM | Markdown 초안 | 고정된 SPEC, 또는 작성자에게 돌려줄 질문 목록 |
| Architect | LLM | 고정된 SPEC, Block Top L1 | 모듈 트리, 하위 L1, `top`/`block_top` 연결, 가정 목록, 하위 모듈 사이 연결 계약 |
| Author | LLM | SPEC 해당 부분, 동결된 L1, 연결 계약, 후보의 조각 언어 | 자기 모듈의 L2, L3 (SV 또는 Python 조각) |
| 조각 번역기 | 툴 | `lang: py` IR | SV 조각 IR, 계산한 reads/writes, 문장별 줄 대응. 폭 확장과 절단은 파라미터 식 캐스트로 명시 |
| Validator | 툴 | IR | 통과, 또는 노드가 지정된 오류 |
| Emitter | 툴 | 검증된 IR | SV 파일, 소스맵, SDC 골격 |
| Verifier | LLM | 고정된 SPEC, 동결된 L1 | Python 참조 모델, 시나리오, 속성 체커 |
| 설계 모델 실행기 | 툴 | `lang: py` IR | 사이클 단위 실행 (RTL 없이 돌려 보기, 번역기 검사의 기준) |
| Verify | 툴 | SV, 테스트 | E1 Verilator lint, 번역기 검사(`py` 모듈: 설계 모델 대 RTL), E2 Verilator sim, E3 VC SpyGlass |
| Classifier | 툴 | 실패 결과, 소스맵 | 실패 유형(11종), 대상 노드 |
| Repair | LLM | SPEC, IR, 실패 리포트, patch 이력 | `{op, target, field, value}` patch 목록 |
| Arbiter | LLM | SPEC, 불일치 트레이스, 양쪽 기댓값 | 테스트 수정안. 사람 승인 필요 |

### 4.3 실행 규칙

- **모듈 루프.** 트리 아래에서 위로 진행하고, 같은 레벨은 병렬로 실행합니다. 부모는 PASS한 실제 자식을 포함해 검증합니다.
- **중복과 선택.** Author 후보는 모든 Verifier의 테스트를 통과해야 합니다. 그중 Repair 횟수와 lint 경고가 적은 후보를 고릅니다.
- **차분 검사.** Verifier 간 차분은 SPEC의 모호함을 찾고 Arbiter로 보냅니다. Author 후보 간 차분은 테스트의 구멍을 찾고 Verifier로 보냅니다.
- **루프 제어.** 반복 상한과 예산 상한을 둡니다. IR 해시로 진동을 감지합니다. patch 뒤에는 항상 Validator부터 다시 실행합니다.
- **동결 해제.** 인터페이스 오류는 Architect로 올리고 영향 모듈만 다시 실행합니다. 같은 설계에서 두 번째 동결 해제가 필요해지면 실행을 중단합니다.
- **실행 중 멈춤.** 기댓값 변경이 생기거나 실패를 사람에게 넘겨야 하면 해당 모듈만 멈춥니다. 사람에게 넘기는 실패는 생성기 버그, 자식 모듈 원인 의심, 진동, 모든 후보 탈락입니다. 나머지 모듈은 계속 진행합니다.
- **Repair 충돌.** 형제 모듈의 Repair들이 같은 실패의 원인을 서로 넘기면 그 후보 묶음을 멈추고 Design-lead가 결정합니다. 고칠 모듈을 지정하면 그 모듈의 Repair만 돕니다. Architect에 근본 원인 수정을 요청하면 모듈 트리를 다시 정하고(Block Top L1과 Verifier 테스트는 그대로) 모든 후보를 Author부터 다시 돌립니다.

### 4.4 에이전트 경계

| 에이전트 | 인스턴스 | 볼 수 있는 것 | 쓸 수 있는 것 |
| --- | --- | --- | --- |
| Architect | 설계당 1 | 고정된 SPEC, Block Top L1 | 모듈 트리, 하위 L1, 가정 목록, 연결 계약 |
| Author | 모듈당 2 | SPEC 해당 부분, 동결된 L1, 연결 계약 | 자기 후보의 L2, L3 |
| Verifier | Block Top당 2, 하위 모듈당 1 | 고정된 SPEC, 동결된 L1 | 참조 모델, 테스트 |
| Repair | 후보당 1 | SPEC, 자기 후보 IR, 실패 리포트, patch 이력 | patch (L2, L3, 연결만. 동결 범위, 테스트, waiver는 불가) |
| Arbiter | 불일치당 1 | SPEC, 트레이스, 양쪽 기댓값 | 수정 제안 |

쓰기 범위는 프롬프트가 아니라 patch 적용 툴, Validator V11(동결 해시 비교), 실행 전후의 작업 디렉터리 비교가 강제합니다.

### 4.5 원본 RTL에서 IR과 SPEC 만들기 (이식 흐름)

검증된 원본 RTL이 있는 블록은 이 흐름으로 IR과 SPEC을 함께 만듭니다. 결과는 golden 블록과 같은 묶음(`ir/*.ir.json`, `spec.md`, 생성 하니스, `golden.json`)입니다. 이 묶음은 일반 파이프라인에 그대로 넣을 수 있습니다. 묶음의 정본은 원본과 모듈 구조가 1:1인 이식 IR입니다. SPEC만으로 다시 만든 재생성 IR은 SPEC 충실도 판정에만 쓰고, 묶음에는 리포트로만 남깁니다. 세부는 [IMPLEMENTATION.md P11](./IMPLEMENTATION.md)을 따릅니다.

```mermaid
flowchart LR
  SRC[원본 RTL<br/>.v · .sv · 헤더 · define] --> R1[R1 구조 추출<br/>pyslang elaborate]
  R1 --> R2{R2 L1 확정<br/>사용자 답}
  R2 --> R3[[R3 Porter<br/>모듈별 1:1 이식]]
  R3 --> R4[R4 사이클 등가<br/>원본 대 방출 RTL]
  R4 -- 차이 --> R3
  R4 -- 모든 모듈 같음 --> R5[[R5 SpecWriter<br/>2~4절은 L1에서]]
  R5 --> R6[R6 SPEC만으로 재생성<br/>일반 파이프라인]
  R6 --> CMP[원본과 트랜잭션 비교<br/>Verifier 테스트를 원본에]
  CMP -- 차이 --> JUD[[Arbiter 판정]]
  JUD -- spec_gap --> REV[[SpecWriter 고치기<br/>refsim · 실패 테스트 자극]]
  REV --> R6
  CMP -- 같음 --> PKG[묶음<br/>이식 IR · SPEC · 하니스]
  JUD -- regen_defect --> PKG
```

| 단계 | 하는 일 | 방식 | 통과 조건 |
| --- | --- | --- | --- |
| R1 구조 추출 | 고정한 파라미터와 define으로 elaborate해 모듈 트리, 포트, 파라미터, 도메인 후보, 번들 후보를 뽑고, 모듈마다 L1과 L0 초안을 냅니다. Verilog와 SV를 섞어 받고, 헤더와 매크로, `generate`, Block Top 경계의 inout(`tristate`)을 처리합니다 | 결정론 | 초안 L1이 V1~V3 통과. 표현할 수 없는 구문은 거부 목록으로 |
| R2 L1 확정 | 도구로 알 수 없는 것(주파수, 도메인 관계, 프로토콜 태그)과 R1 검토 항목을 사용자에게 묻고 멈춥니다. 에이전트는 추정하지 않습니다 | 사람 | 모든 항목에 답, 반영한 L1이 V1~V3 통과 |
| R3 동작 이식 | Porter가 원본 모듈의 동작을 L2/L3으로 옮깁니다. 모듈 이름, 계층, 인스턴스, 포트는 원본과 1:1이고 Architect를 거치지 않습니다 | 에이전트 | Validator 통과, L1과 트리가 R2 결과와 같음 |
| R4 이식 등가 | 아래 모듈부터 원본 RTL과 방출 RTL에 같은 자극을 넣고 모든 출력을 사이클 단위로 비교합니다. 차이는 Porter 고치기 과제로 돌아갑니다 | 결정론 + 에이전트 | 모든 모듈 `equal` (리셋 직후만 다르면 `reset_only`, 검토 항목) |
| R5 SPEC 작성 | 2~4절은 L1에서 만들고, 나머지 절은 SpecWriter가 원본 RTL과 이식 IR을 읽고 씁니다. 원본을 읽어 짐작한 동작은 `refsim`으로 원본에서 확인한 값으로 씁니다 | 결정론 + 에이전트 | SPEC 검사 게이트, SPEC에서 만든 L1 = IR의 L1 |
| R6 SPEC 충실도 | SPEC만 입력으로 일반 파이프라인을 돌려 재생성하고, 재생성 RTL과 원본 RTL을 트랜잭션 단위로 비교합니다. 재생성 실행의 Verifier 테스트도 원본에 돌립니다 | 일반 파이프라인 + 결정론 + 에이전트 | 트랜잭션 차분 없음, Verifier 테스트가 원본에서 통과 |

**R6 규칙.**
- **재생성 분할 상한.** 재생성 실행의 Architect는 원본 모듈 수(top 빼고, 적어도 1)를 넘게 나누지 못합니다. 상한은 실행 상태에 남아 재구성과 이어 가기에도 같게 적용됩니다. 작은 블록을 둘로 나눌 때마다 모듈 사이 계약이 어긋났기 때문입니다.
- **다중 클럭 리셋.** 리셋 도메인이 둘 이상이고 클럭이 다르면, SPEC은 리셋을 사용 조건으로 씁니다(모든 리셋을 함께, 가장 느린 클럭 N사이클 이상. 한쪽 리셋은 미정의). 두 클럭의 위상에 따라 달라지는 사이클 수는 범위와 "구현에 따름(검증하지 않음)"으로 씁니다. 비교도 이에 맞춰 모든 도메인에 같은 시각 구간으로 리셋을 걸고(`joint_reset`), 마지막 리셋이 풀린 뒤의 출력만 봅니다. 리셋마다 입력 순서를 처음부터 다시 쓰고, 앞부분이 같고 입력을 더 받은 쪽이 출력도 더 낸 끝 개수 차이는 ready 지연 차이로 봅니다. 한쪽 리셋과 위상까지 정확히 정하려던 SPEC은 고치기 5회 안에 끝나지 않았습니다 (2026-10-21~22 `axis_async_fifo`).
- **판정.** 비교가 다르거나, 재생성이 실패했어도 그 Verifier 테스트가 원본에서 실패하면 Arbiter가 판정합니다. 판정은 둘 중 하나입니다.
  - `spec_gap`: SPEC이 그 동작을 정하지 않았거나 두 가지로 읽힙니다. SPEC을 고칩니다.
  - `regen_defect`: SPEC이 분명히 정했는데 재생성 설계가 어겼습니다. 일반 파이프라인 결함으로 리포트하고 끝냅니다.
- **SPEC 고치기.** SpecWriter는 이전 SPEC, 판정, 원본에서 실패한 테스트의 자극을 받습니다. 자극은 하니스가 테스트마다 기록한 것으로, `refsim` 형식(받힌 입력, 사이클별 valid/ready/리셋)입니다. SpecWriter는 그 자극을 원본에 다시 넣어, 테스트 기대와 처음 어긋나는 출력이 어느 입력에서 나왔는지 찾은 뒤 문장을 고칩니다. 상한은 `spec_revise`이고, 남은 차이는 사람이 검토합니다.
- **멈춤과 이어 가기.** 재생성 실행이 사람 결정(기댓값 수정, Repair 충돌)이나 한도로 멈추면 실패로 보지 않습니다. `waiting_regen`으로 두었다가 `resume`에서 같은 실행을 이어 갑니다. 판정 뒤 SPEC을 고치기 전에 멈춘 실행은 상한을 올려 이어 가면 SPEC 고치기부터 합니다.

**이식 흐름의 에이전트 경계.**

| 에이전트 | 볼 수 있는 것 | 쓸 수 있는 것 | 도구 |
| --- | --- | --- | --- |
| Porter | 확정한 L1 초안, 원본 모듈과 헤더, 이식한 자식 IR, R4 차이(고치기 때) | 자기 모듈 IR의 L2, L3 | `porteq` (원본과 사이클 비교) |
| SpecWriter | 원본 RTL, 이식 IR, L1에서 만든 2~4절, 추출 메모, 원본 시뮬레이션 묶음. 고치기 때는 이전 SPEC, 판정, 실패한 테스트의 자극 | SPEC | `refsim` (원본에 정한 자극을 넣고 출력 트랜잭션과 사이클을 봄), `spec_gate` |
| Arbiter (R6 판정) | SPEC, 비교 결과 또는 원본에서 실패한 Verifier 테스트 | 판정과 근거 요구사항 ID | 없음 |

SpecWriter는 재생성 IR을 보지 않습니다. 판정 근거로 줄인 자극과 그때의 양쪽 출력만 받습니다. 재생성 실행은 SPEC만 보므로 원본 RTL을 볼 수 없습니다.

실제 에이전트(Claude Code, Haiku)로 `axis_cobs_decode`를 끝까지 돌렸습니다. SPEC을 다섯 번 고친 끝에 재생성 RTL이 원본과 같아졌습니다 (출력 644개, 지연 차이 0, 에이전트 실행 193회). 결정적이었던 것은 실패한 테스트의 자극을 SpecWriter가 원본에 다시 넣어 보게 한 것입니다.

## 5. IR

IR은 모듈당 JSON 파일 하나입니다. RTL의 유일한 소스이며, 다음 네 계층으로 이루어집니다.

| 계층 | 필드 | 내용 |
| --- | --- | --- |
| L0 | `instances`, `connections` | 인스턴스(파라미터 바인딩, `domain_map`)와 연결 |
| L1 | `params`, `domains`, `ports` | 파라미터, 클럭/리셋/전력 도메인, 포트 또는 프로토콜 번들 |
| L2 | `types`, `signals` | enum/struct 타입, `reg`/`wire` 신호 |
| L3 | `blocks`, `functions`, `assertions` | `comb`, `seq`, `fsm`, `cdc_sync`, `cdc_fifo`, `memory`, `tristate`. 도움 함수(function)와 SVA 속성(assert, assume, cover) |

- **모듈 role.** `top`, `block_top`, `module`, `external`의 네 가지입니다. `top`과 `block_top`은 인스턴스, 연결, 동기화 셀, 3상태 구동(`tristate`)만 가집니다. `external`은 L1만 가지며 등록된 RTL 파일을 그대로 참조합니다.
- **동결 범위.** 모든 모듈의 L1과 인스턴스 목록, `top`과 `block_top`의 연결, 동기화 셀, 신호, 타입입니다.
- **Validator.** V1 스키마부터 V11 동결까지 11단계로 검사합니다. 사용하는 수단은 JSON Schema, 심볼 테이블, pyslang AST, reads/writes 그래프입니다.
- **조각 언어.** 모듈의 `lang`이 `sv`(기본)면 조각은 SystemVerilog, `py`면 Python 문법의 사이클 단위 기술입니다. `py` 모듈은 읽을 때 SV 조각으로 낮추므로 Validator, Emitter, 하니스는 같은 경로를 탑니다. 설계 모델(`py` 조각)과 참조 모델(Verifier)은 서로 다른 에이전트가 따로 씁니다. 같은 모델을 RTL의 원천과 기댓값에 함께 쓰지 않습니다 (검증이 순환하므로). 설계 모델 대 RTL 비교는 번역기 검사로만 쓰고, 판정은 늘 RTL 시뮬레이션으로 합니다.
- **리셋 방식.** 한 설계의 리셋은 모두 비동기이거나 모두 동기입니다. 모듈 안의 도메인끼리, `domain_map`으로 잇는 부모와 자식 도메인끼리 같아야 합니다(V2). 블록 안 리셋 동기화(`reset.synchronize`, 비동기 assert와 동기 deassert)는 비동기로 셉니다. 추가 리셋(`reset.extra`, 소프트 리셋)은 동기 리셋이라 동기 설계에만 두고, 비동기 설계의 소프트 리셋은 동작으로 씁니다. 표준 셀(`sync_2ff`, `sync_pulse`, `cdc_fifo`)의 리셋도 설계의 방식을 따릅니다(`ASYNC_RST`). 원본 RTL을 이식할 때 리셋 방식이 섞여 있으면 R1이 거부하고(`mixed_reset`), 원본을 한 방식으로 고친 뒤 옮깁니다.
- **도메인 에지.** 도메인은 상승 에지(`pos`)나 하강 에지(`neg`)입니다. 하강 에지 도메인은 같은 클럭의 상승 에지 도메인과 한 클럭 그룹으로 묶어(반주기 경로) 서로 읽을 수 있습니다. 분주 관계, 동기화 셀, 리셋 동기화는 상승 에지 도메인에만 둡니다. 하니스와 차분 시뮬레이션은 하강 에지 도메인의 입력을 상승 에지에서 바꾸고, 설계 모델 실행기는 하강 에지 시각에 그 도메인을 진행합니다.
- **function과 SVA.** `functions`는 모듈 안의 automatic function입니다. 본문은 인자와 지역 변수만 읽고 쓰므로(모듈 신호는 인자로) 조각의 읽기·쓰기 분석이 그대로 맞습니다. `assertions`는 SVA 속성이고, Emitter가 도메인 클럭과 `disable iff`를 씌워 합성에서 빠지게 냅니다. 시뮬레이션에서 실패하면 멈추지 않고 보고 줄을 내며, Verify가 이를 그 테스트의 실패로 읽어 assert는 Repair로, assume은 Verifier로 보냅니다. **합성 RTL에는 task와 PLI를 쓰지 않습니다** (사내 규칙, 2026-10-22): `task`와 상수 함수(`$clog2`, `$bits`, `$signed` 등) 밖의 시스템 함수·task(`$display`, `$random`, DPI 등)는 Validator V4 오류입니다. SVA는 Emitter가 `` `ifndef SYNTHESIS ``로 감싸므로 이 규칙 밖입니다. 원본 RTL의 task는 Porter가 `comb` 블록이나 function으로 풀어 옮깁니다.
- **inout 패드.** 하니스와 설계 모델 실행기는 inout 포트 P를 바깥 장치의 `P_pad_o`, `P_pad_oe`와 패드 값 `P_pad_val`로 다룹니다 (블록 구동 > 바깥 구동 > 풀업). 바깥 장치가 구동하는데 패드 값이 다르면 블록이 같은 패드를 다른 값으로 구동한 것이므로, 하니스가 동시 구동 충돌로 실패시킵니다 (2-state 시뮬레이터에서도 잡힘).
- **v1 제외.** generate, 하위 모듈 사이의 3상태 버스, 의도적 래치는 아직 표현하지 못합니다. inout은 `top`/`block_top` 경계에서만 `tristate` 블록으로 표현하고, 하위 모듈은 `_o`, `_oe`, `_i` 포트로 나눕니다. 원본 RTL의 generate는 이식할 때 고정한 파라미터로 펼쳐 옮깁니다 (4.5).

## 6. 계층 간 인터페이스와 환류

부서 계층과 파이프라인은 아래 세 인터페이스로만 만납니다. RTL이나 IR을 직접 고치는 경로는 없습니다.

| 인터페이스 | 방향 | 내용 | 호출자 |
| --- | --- | --- | --- |
| 실행 제출 | 부서 → 파이프라인 | SPEC 버전과 설정(모델, 후보 개수, 예산)을 넘기고 실행 ID를 받음 | RTL Engineer |
| 상태와 결과 조회 | 파이프라인 → 부서 | 모듈별 상태, 검토 항목, 최종 보고서, 승인된 RTL | RTL Engineer, Chip-lead, 각 lead |
| 환류 | 부서 → 파이프라인 | 아래 네 형태의 수정 요구 | Design-lead |

| 환류 형태 | 발생원 | 파이프라인의 처리 |
| --- | --- | --- |
| 검사 실패 리포트 | Sanity, 합성, LEC | 소스맵으로 노드에 대응시킵니다. 조각 내부이면 Repair로, 골격이면 생성기 버그로 보냅니다. |
| SPEC 변경 요청 | DV 기능 버그, STA 구조 변경 | 사람 승인 뒤 SPEC을 개정하고 영향 모듈을 다시 실행합니다. DV 버그는 예시 시나리오로 추가합니다. |
| 생성기 버그 리포트 | Emitter 골격 코드의 위반 | Emitter 템플릿을 수정합니다. LLM으로 보내지 않습니다. |
| 설정 변경 | 모델, 후보 개수, 예산 | 다음 실행에 반영합니다. |

## 7. 진행 순서와 사람 승인

```mermaid
flowchart LR
  S1[1 · SPEC 고정] --> S2[2 · 파이프라인 실행<br/>병렬: vPlan · TB · Checker · UPF]
  S2 --> S3[3 · Sanity 정적 검사]
  S3 --> S4[4 · 최종 승인]
  S4 --> S5[5 · DV 독립 검증<br/>PI 합성 · LEC · STA · SDC]
  S5 --> S6[6 · RTL Freeze]
  S5 -. 환류 .-> S2
```

| 승인 지점 | 검토 대상 | 자료 준비 |
| --- | --- | --- |
| SPEC 고정 | 변환된 Markdown, WaveJSON과 원본의 대조, 검사 질문의 답 | Architecture Engineer |
| 최종 승인 | 검증 결과, 검토 항목 전체, 선택된 후보, 차분 검사 결과, Sanity 결과 | Chip-lead, RTL Engineer |
| RTL Freeze | DV의 검증 완료 판정, PI의 구현 가능성 판정 | Chip-lead, DV-lead, PI-lead |

같은 Block Top에서 환류에 따른 재실행이 3회 반복되거나 24시간 이상 진행이 없으면, 자동으로 멈추고 사람에게 알립니다.

## 8. 격리

OpenRig의 조율은 메시지와 규범에 기반하므로 접근을 막는 수단이 되지 못합니다. 그래서 격리는 OpenRig의 edge가 아니라 작업 디렉터리 권한, 저장소 권한, 파이프라인의 툴로 강제합니다.

| 규칙 | 강제 수단 |
| --- | --- |
| 수정 요구는 Design-lead만 제출 | 환류 인터페이스가 Design-lead의 제출만 받음 |
| DV Seat는 파이프라인 Verifier의 참조 모델과 테스트를 볼 수 없음 | 작업 디렉터리와 저장소 권한 분리 |
| 어떤 Seat도 IR과 방출된 RTL을 쓰지 못함 | 저장소 쓰기 권한은 파이프라인에만 부여 |
| 사람의 RTL 수정 지시는 SPEC 변경 요청으로 전환 | Seat의 기본 지침과 쓰기 권한 부재 |
| 파이프라인 에이전트는 Seat와 메시지를 주고받지 않음 | OpenRig에 등록하지 않는 headless 실행. 스케줄러가 입출력을 산출물로 제한 |
| Author ↔ Verifier, 후보끼리 서로의 산출물을 볼 수 없음 | 스케줄러의 입력 구성과 런타임 어댑터 |

## 9. 인프라와 배치

| 구성 | 역할 | 비고 |
| --- | --- | --- |
| OpenRig | Seat의 세션, 토폴로지(rig.yaml), 메시지와 큐 관리 | tmux 위에서 도는 단일 사용자 로컬 데몬. 스냅숏으로 복구 |
| 파이프라인 스케줄러 | 모듈 의존 그래프, 게이트, 자원 대기열, 상태 저장 | OpenRig의 서비스 묶음 방식으로 배치하고 RTL Engineer가 운영. Seat 세션과 상태를 분리 |
| coding agent 실행기 | 작업마다 격리된 작업 디렉터리에서 headless coding agent를 실행하고 산출물을 회수 | 런타임(Claude Code(기본), Pi, Codex, Antigravity(agy) 등)별 실행 명령 차이만 어댑터로 흡수. 도구는 CLI로 만들고 필요하면 MCP로도 노출 |
| 모델 서빙 | coding agent 런타임이 호출하는 언어 모델과 비전 모델 | 온프레미스 서빙과 호환 API 프록시. 런타임 외의 코드는 직접 호출하지 않음 |
| EDA 실행 환경 | Verilator, VC SpyGlass, DC, PowerPro/PowerArtist 등 | 표준 래퍼 스크립트만 호출. 라이선스 수에 맞춘 대기열 |
| 저장소 | SPEC, IR, RTL, 테스트, 리포트 | Seat별 Git worktree. 교환은 브랜치와 병합으로 수행 |
| 게이트웨이 + Web UI | 다중 사용자, 역할별 권한, 승인 기록, 환류 양식 강제, 대화 중계(OpenRig 데몬 API로 Seat에 전달) | Python 게이트웨이, React/TypeScript 프런트엔드. 인증은 자체 계정에서 사내 SSO로 교체 예정 |

Web UI는 승인 세 지점에 쓰이는 화면부터 만듭니다.

- **1차.** SPEC 작업대, 승인함, 실행 상세(읽기 전용)
- **2차.** 대시보드, 환류 관리, 리포트
- **3차.** 조직과 Seat, 설정

## 10. 추적성

- 모든 기능 요구사항에는 ID가 붙습니다. Verifier는 ID마다 검사를 하나 이상 대응시키고, 불일치 리포트와 Arbiter 판정은 이 ID를 인용합니다.
- 방출된 SV에는 `// @ir <id>` 주석과 소스맵이 붙습니다. 외부 툴의 파일과 줄 번호는 이것으로 IR 노드에 대응되고, Python 조각이면 조각 안의 줄(`py_line`)까지 대응됩니다.
- 모든 환류와 승인은 실행 ID, 요구사항 ID, SPEC 버전과 함께 기록됩니다.

## 11. 미결 사항

**실험으로 확인할 것**

- [ ] coding agent 런타임이 로컬 모델로 동작하는지. OpenRig Seat와 headless 실행 둘 다. Seat 경로는 Pi 런타임의 Seat별 `models.json`
- [x] rig.yaml의 edge 종류. `handoff`와 `can_message`는 없고 `delegates_to`, `escalates_to`, `collaborates_with`, `can_observe`, `spawned_by`가 있음
- [ ] 폐쇄망 설치 절차. npm 사내 미러와 데몬의 외부 호출(플러그인 자동 갱신. 실패해도 동작함) 처리
- [ ] coding agent(로컬 모델)가 스키마에 맞는 IR 파일을 쓰고 검사 도구를 쓰는 비율
- [ ] 비전 모델을 쓰는 coding agent의 타이밍 다이어그램 변환 정확도
- [ ] 게이트웨이 → OpenRig → Seat 대화 왕복 (보낸 사람 구분, 동시 전송 포함)

**남은 질문**

- [ ] EPC 스위칭 활동 정보를 파이프라인 시뮬레이션 파형에서 가져올지
- [ ] 이식 시험 대상 블록 3개와 원시 블록 비중의 기준선
- [ ] Repair 반복 상한 값 (이식 시험 뒤 결정)

**받아야 할 자료**

- [ ] Word/PDF SPEC 양식, 엑셀 레지스터 맵, 블록 다이어그램 원본
- [ ] VC SpyGlass 사내 템플릿, 표준 동기화 셀, 메모리 매크로 래퍼 규약, 표준 프로토콜 태그, 사내 코딩 가이드
- [ ] 로컬 모델 목록과 계열, 폐쇄망에 반입할 coding agent 런타임, 외부 모듈의 RTL 위치

**첫 단계는 블록 이식 시험입니다.** LLM을 붙이기 전에 검증된 기존 블록 3개를 손으로 IR에 옮깁니다. 대상은 FSM 중심, datapath 중심, 다중 클럭 블록이며, 이를 통해 스키마의 표현력과 Emitter의 정확성을 확인합니다.
