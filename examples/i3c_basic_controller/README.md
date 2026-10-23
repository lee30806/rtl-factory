# I3C Basic Controller 예시

Oct 8, 2026 · @Seungjoon Lee

MIPI I3C Basic v1.2 SDR-Only Primary Controller를 Block Top 하나로 정의한 예시입니다. 사내 Word 양식 대신 공개 PDF SPEC에서 출발해, SPEC 변환(P3)과 SPEC 검사 게이트, 그 뒤의 End-to-End 실행을 시험하는 데 씁니다.

| 파일 | 내용 |
| --- | --- |
| [spec.md](spec.md) | 10개 섹션 템플릿으로 작성한 Block Top SPEC 초안 (`i3c_ctrl_top`) |
| [registers.csv](registers.csv) | APB 레지스터 맵. 사내 흐름의 엑셀 레지스터 맵 자리를 대신함 |

## 출처

- MIPI Alliance Specification for I3C Basic, Version 1.2 Public Release Edition with Errata 01 (2026-03-13). 받는 곳: [MIPI I3C Basic 다운로드](https://www.mipi.org/mipi-i3c-basic-download)
- 원본 PDF는 `experiments/specs/`에 두며 저장소에 올리지 않습니다. 이 폴더의 문서는 원문을 옮겨 적지 않고 요구사항을 다시 쓴 것이며, 근거는 `[I3C §절 번호]`로 표시합니다.

## 범위 결정

| 항목 | 결정 | 근거 |
| --- | --- | --- |
| 장치 역할 | SDR-Only Primary Controller | [I3C §3.2.1.1] SDR만 지원하는 Primary Controller 역할 |
| 기능 범위 | SDR 필수 요소, In-Band Interrupt 수신, Hot-Join 처리, Legacy I2C(FM, FM+) 전송 | 사용자 결정 (2026-10-08) |
| 호스트 인터페이스 | 명령, 쓰기 데이터, 읽기 데이터, 응답, IBI를 valid/ready 큐로 주고받고, 설정은 APB 레지스터로 함 | 사용자 결정 (2026-10-08) |
| 클럭 | 단일 100 MHz 도메인. SCL과 SDA 입력은 2단 동기화 | 사용자 결정 (2026-10-08). IR v1이 다중 비트 CDC를 표현하지 못함 |
| 패드 | 양방향 패드는 Block Top 밖에 둠. 출력, 출력 enable, 입력, Pull-Up 제어로 나눈 포트를 씀 | IR v1이 inout을 표현하지 못함 |

### 제외한 선택 기능

HDR-DDR(F001), HDR-BT(F003), Secondary Controller(F004)와 Controller Role 이양, Timing Control(F005), Multi-Lane(F006), Controller Clock Stalling(F009), Alternative Electrical(F010), Group Address, SETAASA 자동 처리, Target 역할.

## 필수 요소 대응

SDR-Only Primary Controller의 책임[I3C §4.3.1.1, 표 4]과 이 SPEC의 요구사항 대응입니다.

| 책임 | 이 SPEC | 비고 |
| --- | --- | --- |
| SDA 중재 관리 (주소 중재, IBI, Hot-Join, 동적 주소) | FR-ARB-01\~04, FR-IBI-01\~07, FR-HJ-01\~04, FR-DAA-01\~08 | |
| 동적 주소 할당 | FR-DAA-01\~08 | ENTDAA는 전용 명령, SETDASA와 SETNEWDA는 Direct CCC 명령으로 처리 |
| Hot-Join 뒤 동적 주소 할당 | FR-HJ-01\~04, FR-DAA-01 | 할당 시점은 호스트가 정함 |
| 자기 동적 주소 할당 | FR-REG-04 | `OWN_DA` 레지스터 |
| Target 주소와 특성 보관 | FR-REG-02, FR-REG-03 | 장치 주소 표(DAT), 장치 특성 표(DCT) |
| HDR Exit Pattern 생성 | FR-ERR-03, FR-REC-01 | 오류 CE2 복구와 호스트 요청 |
| HDR 패턴 인식 (HDR Tolerant) | 해당 없음 | Target 쪽 요구. Controller는 HDR 모드에 들어가지 않음 |
| 필수 CCC (ENEC, DISEC, RSTDAA, SETMWL, SETMRL, GETMWL, GETMRL, GETSTATUS, RSTACT 등) | FR-CCC-01\~05 | CCC 코드는 호스트가 넣는 일반 CCC 명령으로 처리. 응답 길이 검사로 CE0 검출 |
| 오류 CE0, CE2 (필수), CE1 (권장) | FR-ERR-01\~05 | CE3은 Controller 이양을 하지 않으므로 발생하지 않음 |

## 상태

- [x] 범위 결정
- [x] SPEC 초안 (10개 섹션)
- [x] 레지스터 맵 초안
- [x] 툴 검사 일부: 모든 포트가 요구사항에서 참조됨, 모든 출력 포트에 동작 요구사항이 있음, 레지스터 참조가 맵에 있음, ID 중복 없음 (임시 스크립트로 확인. P3에서 정식 도구로 만듦)
- [ ] 나머지 툴 검사와 LLM 검사 (모호한 문장, 모순, 빠진 코너 케이스)
- [ ] 사람 검토와 SPEC 고정
