"""P11 실행기 시험용 가짜 coding agent. 작업 디렉터리의 입력 파일로 역할을 알아낸다.

- Porter(draft.json): FAKE_PORTED/<모듈>.ir.json을 out/에 쓴다. FAKE_BAD_FIRST="옛=>새"면 첫 시도(prev.ir.json 없음)는
  body의 글자를 바꾼 IR을 쓴다 (R4가 원본과 다르다고 판정하게).
- SpecWriter(tables.md): FAKE_SPEC_BASE의 2~4절을 tables.md로 바꿔 out/spec.md에 쓴다. 고치기(spec_prev.md)면 5절에 요구사항을 하나 더한다.
- Arbiter(r6.json): spec_gap 판정.
"""
import json
import os
import re
from pathlib import Path

cwd = Path.cwd()
assert (cwd / "TASK.md").exists()
out = cwd / "out"
out.mkdir(exist_ok=True)

if (cwd / "draft.json").exists():
    draft = json.loads((cwd / "draft.json").read_text())
    name = draft["module"]
    text = (Path(os.environ["FAKE_PORTED"]) / f"{name}.ir.json").read_text()
    bad = os.environ.get("FAKE_BAD_FIRST")
    if bad and not (cwd / "prev.ir.json").exists():
        old, new = bad.split("=>")
        assert old in text, old
        text = text.replace(old, new, 1)
    ir = json.loads(text)
    ir["domains"] = draft["domains"]   # R2에서 확정한 주파수와 관계 (실제 Porter도 초안 L1을 그대로 둠)
    (out / f"{name}.ir.json").write_text(json.dumps(ir))
elif (cwd / "tables.md").exists():
    assert (cwd / "ref" / "ref.json").exists()   # refsim용 원본 묶음
    base = Path(os.environ["FAKE_SPEC_BASE"]).read_text()
    tables = (cwd / "tables.md").read_text()
    head, rest = base.split("\n## 2.", 1)
    tail = rest[rest.index("\n## 5."):]
    spec = head + "\n" + tables + tail
    if (cwd / "spec_prev.md").exists():
        spec = (cwd / "spec_prev.md").read_text()
        spec = re.sub(r"(\n## 6\.)", "\n- FAKE-99 (필수) R6 차이에서 더한 요구사항.\n\\1", spec, count=1)
    (out / "spec.md").write_text(spec)
elif (cwd / "r6.json").exists():
    (out / "verdict.json").write_text(json.dumps({"verdict": "spec_gap", "reqs": [], "reason": "SPEC이 정하지 않음",
                                                  "proposal": "요구사항을 더함"}))
else:
    raise SystemExit("모르는 과제")
