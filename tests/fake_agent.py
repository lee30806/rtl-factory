"""테스트용 가짜 coding agent. 작업 디렉터리(cwd)에서 FAKE_MODE에 따라 행동한다."""
import json
import os
import subprocess
import sys
from pathlib import Path

FIX = Path(__file__).resolve().parent / "fixtures" / "pkt_parser.ir.json"
mode = os.environ["FAKE_MODE"]
cwd = Path.cwd()
assert (cwd / "TASK.md").exists()
out = cwd / "out" / "pkt_parser.ir.json"


def validate():
    subprocess.run([sys.executable, "tools/validate_ir.py", str(out.relative_to(cwd))], capture_output=True)


if mode == "good":
    out.write_text(FIX.read_text())
    validate()
elif mode == "bad_then_good":
    # 첫 실행은 틀린 파일, 재시도(TASK.md에 검사 결과 포함)에서 고침
    if "이전 시도의 검사 결과" in (cwd / "TASK.md").read_text():
        out.write_text(FIX.read_text())
    else:
        out.write_text('{"module": "pkt_parser"}')
elif mode == "touch_l1":
    out.write_text(FIX.read_text())
    (cwd / "l1.json").write_text("{}")
elif mode == "crash":
    sys.exit(3)
elif mode == "wave":
    assert sys.argv[1].startswith("@") and (cwd / sys.argv[1][1:]).exists()  # {image} 첨부 확인
    (cwd / "out" / "wave.json").write_text(os.environ["FAKE_WAVE"])
elif mode == "questions":
    assert (cwd / "spec.md").exists()
    (cwd / "out" / "questions.json").write_text(os.environ["FAKE_QUESTIONS"])
elif mode == "questions_touch_spec":
    (cwd / "out" / "questions.json").write_text('{"items": []}')
    (cwd / "spec.md").write_text("changed")
elif mode == "task_write":
    (cwd / "out" / "result.txt").write_text("done")
elif mode == "task_escape":
    (cwd / "out" / "result.txt").write_text("done")
    (cwd / "input.txt").write_text("changed")
elif mode == "task_retry":
    good = "이전 시도의 검사 결과" in (cwd / "TASK.md").read_text()
    (cwd / "out" / "result.txt").write_text("good" if good else "bad")
elif mode == "task_tools":
    r = subprocess.run([sys.executable, "tools/pl.py", "validate", "in/ir.json"], capture_output=True, text=True)
    (cwd / "out" / "result.txt").write_text(r.stdout)
elif mode == "quota_cc":   # Claude Code CLI: 종료 코드 1, 표준 출력에 안내
    print("You've hit your session limit · resets 1:20am (Asia/Seoul)")
    sys.exit(1)
elif mode == "quota":
    print("error: Individual quota reached. RESOURCE_EXHAUSTED (code 429)", file=sys.stderr)
    sys.exit(3)
elif mode == "roles":
    if os.environ.get("FAKE_DELAY"):   # 실행이 진행 중인 동안 다른 일을 시험하려고 (Seat 초기화 등)
        import time
        time.sleep(float(os.environ["FAKE_DELAY"]))
    # FAKE_FILES: {"Architect": {"상대 경로": 내용 또는 "@원본 파일"}, "Author": {...}, ...}. 스케줄러 흐름 시험용.
    # FAKE_LOG가 있으면 실행한 역할을 한 줄씩 남긴다 (재개 시험에서 다시 실행됐는지 보려고).
    task = (cwd / "TASK.md").read_text()
    # 키가 "역할|표지"면 과제 본문이나 작업 디렉터리 이름에 표지가 있을 때만 맞는다 (먼저 적은 키가 우선).
    for role, files in json.loads(os.environ["FAKE_FILES"]).items():
        name, _, marker = role.partition("|")
        if f"{name} 에이전트" in task and (marker in task or marker in str(cwd)):
            if os.environ.get("FAKE_LOG"):
                with open(os.environ["FAKE_LOG"], "a") as f:
                    f.write(role.replace(" ", "_") + "\n")
            for rel, content in files.items():
                p = cwd / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(Path(content[1:]).read_text() if content.startswith("@") else content)
            break
