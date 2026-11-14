"""B4: CVDP 어댑터와 채점 (BENCHMARK_PLAN B4). 명세→RTL 범주 `cid003`(Spec-to-RTL)과 `cid005`(모듈 재사용)만 쓴다.

입력은 NVIDIA CVDP 공개 JSONL(`build/datasets/CVDP_raw/cvdp_v1.0.3_*code_generation*.jsonl`) 원본이다. 두 형식이 있다.
- 비에이전틱: `input.prompt`, `input.context`(제공 파일), `output.context`(만들 파일 이름), `harness.files`
- 에이전틱: `prompt`, `context`, `patch`(만들 파일 이름), `harness`(파일 맵)
공개본에는 정답 RTL이 없다. 모듈 헤더도 프롬프트에 거의 없으므로(78개 중 2개) 헤더는 SPEC 작성 전에 HeaderWriter 에이전트가
프롬프트와 제공 문서만 보고 쓴다 (`to_spec.ensure_header`). 테스트(cocotb)는 공식 설정대로 헤더 작성에 쓰지 않는다.

채점은 공식 하니스(cocotb + Icarus)를 Docker 없이 그대로 돌린다: `src/.env`의 `/code`, `/src` 경로를 채점 디렉터리로 바꾸고
`src/test_runner.py`를 pytest로 실행해 종료 코드 0이면 통과다.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from experiments.bench.common.modules import merge_candidate, modules_in_files
from experiments.bench.common.schema import BenchItem, EvalSpec

ROOT = next((d for d in Path(__file__).resolve().parents if (d / "build" / "datasets").exists()), Path(__file__).resolve().parents[3])
RAW_DEFAULT = ROOT / "build" / "datasets" / "CVDP_raw"
CIDS = ("cid003", "cid005")
_MODULE_HDR_RE = re.compile(r"(?<![\w.])module\s+([A-Za-z_]\w*)\s*(?:#\s*\([\s\S]*?\)\s*)?\([\s\S]*?\)\s*;")
_DOC_SUFFIXES = (".md", ".txt", ".rst", ".json", ".csv", ".yaml", ".yml")


def _norm(rec: dict) -> dict:
    """두 형식을 한 모양으로: prompt, context, targets(만들 파일), files(하니스 파일 맵)."""
    if "input" in rec:
        h = rec["harness"]
        return {"id": rec["id"], "categories": rec["categories"], "prompt": rec["input"]["prompt"],
                "context": rec["input"].get("context", {}) or {}, "targets": list((rec.get("output") or {}).get("context", {}) or {}),
                "files": h.get("files", h), "track": "nonagentic"}
    h = rec["harness"]
    return {"id": rec["id"], "categories": rec["categories"], "prompt": rec["prompt"], "context": rec.get("context", {}) or {},
            "targets": list(rec.get("patch", {}) or {}), "files": h.get("files", h), "track": "agentic"}


def parse_env(text: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for ln in text.splitlines():
        if "=" in ln and not ln.strip().startswith("#"):
            k, v = ln.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def dataset_files(raw_dir: Path) -> list[Path]:
    """가장 새 버전(cvdp_v1.0.3_*)의 code_generation JSONL만 쓴다 (예전 버전은 같은 문제의 중복)."""
    files = sorted(Path(raw_dir).glob("cvdp_v*code_generation*.jsonl"))
    if not files:
        return []
    newest = max(re.match(r"(cvdp_v[\d.]+)_", f.name).group(1) for f in files if re.match(r"(cvdp_v[\d.]+)_", f.name))
    return [f for f in files if f.name.startswith(newest + "_")]


def load_records(raw_dir: Path, cids: tuple[str, ...] = CIDS) -> list[dict]:
    recs: list[dict] = []
    seen: set[str] = set()
    for f in dataset_files(raw_dir):
        for ln in f.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            r = json.loads(ln)
            if r.get("categories", [""])[0] in cids and r["id"] not in seen:
                seen.add(r["id"])
                recs.append(_norm(r) | {"source_file": f.name})
    return recs


def header_from_prompt(prompt: str, top: str) -> str:
    """프롬프트에 `module <top> (...);` 선언이 그대로 있으면 그것을 헤더로 쓴다. 없으면 빈 문자열 (HeaderWriter가 쓴다)."""
    for m in _MODULE_HDR_RE.finditer(prompt):
        if m.group(1) == top:
            return m.group(0) + "\nendmodule\n"
    return ""


def to_item(rec: dict, src_root: Path) -> BenchItem:
    n = rec if "files" in rec else _norm(rec)
    d = Path(src_root).resolve() / n["id"]
    env = parse_env(n["files"].get("src/.env", ""))
    top = env.get("TOPLEVEL", "")
    if not top:
        raise ValueError(f"CVDP 레코드에 TOPLEVEL이 없음: {n['id']}")
    for rel, text in {**n["context"], **n["files"]}.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    rtl_ctx = [rel for rel in n["context"] if rel.endswith((".v", ".sv", ".svh", ".vh")) and rel not in n["targets"]]
    docs = [rel for rel in n["context"] if rel not in rtl_ctx and rel.lower().endswith(_DOC_SUFFIXES)]
    prompt = n["prompt"].strip()
    for rel in docs:
        prompt += f"\n\n---\n## 제공 문서 `{rel}`\n\n{n['context'][rel].strip()}\n"
    if rtl_ctx:
        prompt += "\n\n---\n## 제공 RTL (재사용 셀, 수정하지 않는다)\n" + "\n".join(f"- `{rel}`" for rel in rtl_ctx) + "\n"
    cells = [str(d / rel) for rel in rtl_ctx if rel.endswith((".v", ".sv"))]
    incs = sorted({str((d / rel).parent) for rel in n["context"] if rel.endswith((".svh", ".vh", ".v", ".sv"))})
    tests = sorted(rel for rel in n["files"] if rel.startswith("src/") and rel.endswith(".py"))
    return BenchItem(
        benchmark="cvdp",
        id=n["id"],
        category=n["categories"][0],
        top=top,
        prompt_text=prompt,
        header_sv=header_from_prompt(n["prompt"], top),
        incdirs=incs,
        cells=cells,
        attachments=[],
        eval=EvalSpec(kind="cvdp", tb_files=[str(d / t) for t in tests],
                      extra={"harness_dir": str(d), "env": env, "targets": n["targets"], "context_rtl": [str(d / r) for r in rtl_ctx],
                             "track": n["track"], "difficulty": n["categories"][1] if len(n["categories"]) > 1 else "",
                             "source_file": rec.get("source_file", "")}),
    )


def load_cvdp(raw_dir: Path | None, src_root: Path, cids: tuple[str, ...] = CIDS) -> list[BenchItem]:
    items: list[BenchItem] = []
    for r in load_records(Path(raw_dir or RAW_DEFAULT), cids):
        try:
            items.append(to_item(r, src_root))
        except ValueError as e:   # 하니스에 TOPLEVEL이 없는 레코드는 공식 도구로도 채점할 수 없다
            print(f"[cvdp] 건너뜀: {e}", flush=True)
    return items


def _runner_file(src: Path) -> Path | None:
    for cand in ("test_runner.py", "runner.py"):
        if (src / cand).exists():
            return src / cand
    hits = sorted(p for p in src.glob("*runner*.py"))
    return hits[0] if hits else None


def evaluate(item: BenchItem, sv_files: list[Path], eval_dir: Path, timeout: int = 1800) -> dict[str, Any]:
    """채점 디렉터리에 하니스(src/)와 제공 RTL을 두고, 후보를 만들 파일 자리에 합쳐 넣은 뒤 cocotb 러너를 pytest로 돌린다."""
    if not shutil.which("iverilog"):
        return {"ok": False, "kind": "cvdp", "stage": "tool_error", "log_tail": "iverilog가 설치되어 있지 않음"}
    try:
        import cocotb_tools.runner  # noqa: F401
    except ImportError:
        return {"ok": False, "kind": "cvdp", "stage": "tool_error", "log_tail": "cocotb(cocotb_tools.runner)가 설치되어 있지 않음"}
    eval_dir = Path(eval_dir).resolve()
    if eval_dir.exists():
        shutil.rmtree(eval_dir)
    x = item.eval.extra
    hdir = Path(x["harness_dir"])
    shutil.copytree(hdir / "src", eval_dir / "src")
    ctx_rtl = [Path(p) for p in x.get("context_rtl", [])]
    for p in ctx_rtl:
        dst = eval_dir / p.relative_to(hdir)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
    text, dropped = merge_candidate([Path(p) for p in sv_files], modules_in_files(ctx_rtl))
    targets = x.get("targets") or [f"rtl/{item.top}.sv"]
    cand = eval_dir / targets[0]
    cand.parent.mkdir(parents=True, exist_ok=True)
    cand.write_text(text, encoding="utf-8")
    # .env의 컨테이너 경로를 채점 디렉터리로 바꾼다. 만들 파일들은 합친 후보 하나로 대신한다
    env = dict(x.get("env", {}))
    srcs: list[str] = []
    for s in env.get("VERILOG_SOURCES", "").split():
        rel = s.replace("/code/", "")
        if rel in targets:
            if str(cand) not in srcs:
                srcs.append(str(cand))
        else:
            srcs.append(str(eval_dir / rel))
    if str(cand) not in srcs:
        srcs.insert(0, str(cand))
    env["VERILOG_SOURCES"] = " ".join(srcs)
    env["PYTHONPATH"] = str(eval_dir / "src")
    env = {k: v.replace("/code/", f"{eval_dir}/").replace("/src", str(eval_dir / "src")) if k not in ("VERILOG_SOURCES", "PYTHONPATH") else v
           for k, v in env.items()}
    # 빠른 컴파일 게이트: 소스가 그대로 컴파일되지 않으면 cocotb를 돌리지 않는다
    gate = subprocess.run(["iverilog", "-g2012", "-o", str(eval_dir / "gate.vvp"), *[f"-I{i}" for i in item.incdirs], *srcs],
                          cwd=eval_dir, capture_output=True, text=True, timeout=120)
    (eval_dir / "compile.log").write_text(gate.stdout + gate.stderr, encoding="utf-8")
    if gate.returncode != 0:
        return {"ok": False, "kind": "cvdp", "stage": "compile", "returncode": gate.returncode, "deduped_modules": dropped,
                "log_tail": (gate.stdout + gate.stderr)[-1500:]}
    runner = _runner_file(eval_dir / "src")
    if runner is None:
        return {"ok": False, "kind": "cvdp", "stage": "tool_error", "log_tail": "하니스에 test_runner.py가 없음"}
    cmd = [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", str(runner)]
    try:
        sim = subprocess.run(cmd, cwd=eval_dir / "src", capture_output=True, text=True, timeout=timeout,
                             env={**os.environ, **env, "PYTHONDONTWRITEBYTECODE": "1"})
    except subprocess.TimeoutExpired:
        return {"ok": False, "kind": "cvdp", "stage": "sim_timeout", "deduped_modules": dropped, "log_tail": "cocotb 실행 시간 초과"}
    out = sim.stdout + sim.stderr
    (eval_dir / "sim.log").write_text(out, encoding="utf-8")
    ok = sim.returncode == 0
    # Repair가 볼 단서: 실패한 cocotb 테스트 이름과 단정 메시지
    fails = re.findall(r"^\s*(?:FAIL|FAILED)\s+(\S+)", out, re.M)
    asserts = [ln.strip() for ln in out.splitlines() if "AssertionError" in ln or "assert " in ln][:5]
    diag = ""
    if not ok:
        diag = (f"실패한 테스트 {sorted(set(fails))[:5]}. " if fails else "") + (" | ".join(asserts) if asserts else "")
    return {"ok": ok, "kind": "cvdp", "stage": "sim", "returncode": sim.returncode, "deduped_modules": dropped,
            "failed_tests": sorted(set(fails)), "diagnosis": diag[:600],
            "log_tail": "" if ok else (diag + "\n" + out[-1200:])[:1500]}
