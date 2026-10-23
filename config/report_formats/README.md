# 외부 검사 리포트 양식

외부 툴(Superlint, Xprop, DFT, DCLint, 합성, LEC 등)의 리포트를 파이프라인에 넣으려면 이 디렉터리에 양식 파일 `<이름>.json`을 하나 더합니다.
코드를 고칠 필요는 없습니다. 파서(`pipeline/feedback/reports.py`)가 리포트를 줄마다 읽어 `Finding`(툴, 규칙, 파일, 줄, 메시지, 심각도)으로 바꾸고,
Classifier가 Emitter 소스맵으로 IR 노드에 대응시킵니다. LLM이 쓴 조각 안이면 Repair로, 골격이면 생성기 버그로 보냅니다.

## 형식

```json
{
 "tool": "superlint",
 "description": "양식 설명과 예시 줄",
 "patterns": [
  {"regex": "^(?P<severity>Error|Warning)\\s+\\[(?P<rule>\\w+)\\]\\s+(?P<file>\\S+):(?P<line>\\d+)\\s+(?P<message>.*)$",
   "defaults": {"rule": "SUPERLINT"}}
 ],
 "continuation": "^\\s{4,}\\S",
 "ignore": ["^Summary"],
 "severity_map": {"Error": "error", "Warning": "warning", "Info": "info"},
 "default_rule": "REPORT"
}
```

| 키 | 필수 | 뜻 |
| --- | --- | --- |
| `tool` | 예 | 툴 이름. Finding의 tool이 된다 |
| `patterns` | 예 | 정규식 목록. 줄마다 순서대로 맞춰 보고 처음 맞은 것을 쓴다. 이름 있는 그룹 `severity`, `rule`, `file`, `line`, `message`만 쓸 수 있다 |
| `patterns[].defaults` | 아니요 | 그룹이 없거나 비었을 때 쓰는 값 |
| `continuation` | 아니요 | 어느 패턴에도 맞지 않고 이 정규식에 맞는 줄은 직전 결과의 메시지에 이어 붙인다 (여러 줄 메시지) |
| `ignore` | 아니요 | 이 정규식에 맞는 줄은 건너뛴다 (요약 줄 등) |
| `severity_map` | 아니요 | 툴의 심각도 표기 → `error`, `warning`, `info`. 대소문자는 가리지 않는다 |
| `default_rule` | 아니요 | `rule`이 없을 때의 규칙 이름 |

- `file`은 방출된 RTL 파일 이름(`<모듈>.sv`)과 같은 stem이어야 소스맵으로 대응된다. 경로 앞부분은 상관없다.
- `line`이 없는 결과(파일 전체, 설계 전체에 대한 지적)는 노드에 대응시킬 수 없으므로 사람에게 보낸다.
- 양식을 확인하려면 `python3 -m pipeline.feedback.reports <이름> <리포트 파일>`로 결과를 본다.

## 지금 있는 양식

| 이름 | 툴 | 비고 |
| --- | --- | --- |
| `verilator` | Verilator lint | 파이프라인 E1과 같은 출력 |
| `iverilog` | Icarus Verilog | 컴파일, elaboration 오류 |

사내 툴(Superlint, Xprop, DFT, DCLint, 합성, LEC)의 양식은 실제 리포트 샘플을 받으면 더합니다.
