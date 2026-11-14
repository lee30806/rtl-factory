"""B3: RealBench 어댑터와 채점 (BENCHMARK_PLAN B3).

입력 둘을 합친다.
- 원본 데이터셋(`build/datasets/RealBench`): `<도메인>/<모듈>/<모듈>.md`(복호화된 명세. 블록도·FSM 그림 `figures/`를 참조)와
  `verification/`(Verilator 자체 검사 TB). 명세 본문은 원본 .md를 그대로 쓴다. `problems.jsonl`이 뒤에 붙이는 defines 문서와
  pymtl 변환본의 추가 안내문은 넣지 않는다: 매크로는 include 파일(`rtl/include`)로 주고, 추가 안내문은 원문에 없는 해석이다.
- pymtl 변환본(`build/cvdp_datasets/cvdp_realbench.jsonl`): 모듈 헤더(`metadata.header_sv`), include 파일, 시스템 레벨 하위 모듈,
  TB 묶음(`tb/`: *_testbench.sv, *_stimulus_gen.sv, *_ref.sv, 라이브러리 .v).

채점은 공식 흐름(run_verify.py: Verilator로 TB를 돌려 "Hint: Output 'x' has N mismatches" 줄이 없으면 통과)과 같다.
정답 모델(`tb/*_ref.sv`, `ref/gold.v`)은 채점에만 쓰고 에이전트에게 주지 않는다. 형식 검증(JasperGold)은 하지 않는다.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from experiments.bench.common.modules import merge_candidate, modules_in_files
from experiments.bench.common.schema import BenchItem, EvalSpec

ROOT = next((d for d in Path(__file__).resolve().parents if (d / "build" / "datasets").exists()), Path(__file__).resolve().parents[3])
CONV_DEFAULT = ROOT / "build" / "cvdp_datasets" / "cvdp_realbench.jsonl"
RAW_DEFAULT = ROOT / "build" / "datasets" / "RealBench"

# 공식 Makefile/run_sim.sh와 같은 Verilator 옵션 (경고는 치명으로 보지 않는다)
VERILATOR_FLAGS = ["-cc", "--exe", "--binary", "--timing", "--assert", "-j", "0", "-Wno-fatal",
                   "-Wno-DECLFILENAME", "-Wno-PINMISSING", "-Wno-IMPLICIT", "-Wno-WIDTHTRUNC", "-Wno-WIDTHEXPAND",
                   "-Wno-CASEINCOMPLETE", "-Wno-CASEOVERLAP", "-Wno-LATCH", "-Wno-UNOPTFLAT", "-Wno-MULTIDRIVEN",
                   "-Wno-ASCRANGE", "-Wno-EOFNEWLINE", "-Wno-TIMESCALEMOD"]
_MISMATCH_RE = re.compile(r"Hint: Output '(\w+)' has (\d+) mismatches\. First mismatch occurred at time (\d+)")
_TOTAL_RE = re.compile(r"Total mismatched samples is (\d+) out of (\d+) samples")
_FIG_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


def load_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def raw_doc(raw_root: Path, domain: str, module: str) -> Path:
    """원본 명세 .md. 시스템 레벨도 도메인 디렉터리 아래에 있다 (sdc/sdc_controller/sdc_controller.md)."""
    p = Path(raw_root) / domain / module / f"{module}.md"
    if not p.exists():
        raise FileNotFoundError(f"RealBench 원본 명세가 없음 (make decrypt 했는지 확인): {p}")
    return p


def figures_of(doc: Path, text: str) -> list[Path]:
    """명세가 참조하는 그림 가운데 실제로 있는 파일 (경로의 %20은 공백)."""
    out: list[Path] = []
    for rel in _FIG_RE.findall(text):
        p = doc.parent / rel.replace("%20", " ")
        if p.exists() and p not in out:
            out.append(p)
    return out


def _is_tb_side(rel: str) -> bool:
    return rel.startswith("tb/") and rel.endswith((".v", ".sv")) and not rel.endswith(("_testbench.sv", "_stimulus_gen.sv", "_ref.sv"))


def to_item(rec: dict, raw_root: Path, src_root: Path) -> BenchItem:
    h, meta = rec["harness"], rec["metadata"]
    # 시스템 레벨의 original_id는 system_ 접두사가 붙는다. 변환본의 top_module은 틀린 것이 있어(e203_cpu_top → configuration) 쓰지 않는다
    module, domain, level = re.sub(r"^system_", "", meta["original_id"]), meta["ip_domain"], meta["level"]
    d = Path(src_root).resolve() / rec["id"]
    for rel, text in h["files"].items():
        if rel.startswith("src/") or rel == "run_sim.sh":
            continue
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    doc = raw_doc(raw_root, domain, module)
    prompt = doc.read_text(encoding="utf-8", errors="replace")
    incs = sorted({str((d / rel).parent) for rel in h["files"] if rel.startswith("rtl/include/")})
    tb = sorted(rel for rel in h["files"] if rel.startswith("tb/"))
    testbench = [rel for rel in tb if rel.endswith("_testbench.sv")]
    stimulus = [rel for rel in tb if rel.endswith("_stimulus_gen.sv")]
    refs = [rel for rel in tb if rel.endswith("_ref.sv")] + (["ref/gold.v"] if "ref/gold.v" in h["files"] else [])
    # 후보가 쓸 수 있는 셀: 시스템 레벨의 하위 모듈과 TB 쪽 라이브러리(sirv_gnrl_* 등). defines·config는 include 파일이라 뺀다
    cells = [rel for rel in h["files"] if rel.startswith("rtl/submodules/")]
    cells += [rel for rel in tb if _is_tb_side(rel) and not re.search(r"defines|config", Path(rel).name)]
    if not testbench:
        raise FileNotFoundError(f"RealBench TB가 없음: {rec['id']}")
    tb_top = re.search(r"^\s*module\s+(\w+)", h["files"][testbench[0]], re.M)
    # 변환본 헤더는 정답의 포트 선언에서 만든 것이다. 모듈 이름이 다르면(변환 결함) 정답 파일 경로를 주어 strip_to_header가 포트만 뽑게 한다
    header = meta.get("header_sv", "")
    if not re.search(rf"^\s*module\s+{re.escape(module)}\b", header, re.M):
        header = str(d / "ref" / "gold.v")
    return BenchItem(
        benchmark="realbench",
        id=rec["id"],
        category=level,
        top=module,
        prompt_text=prompt,
        header_sv=header,
        incdirs=incs + [str(d / "tb"), str(d)],
        cells=[str(d / c) for c in cells],
        reference_rtl=[str(d / r) for r in refs],
        attachments=[str(f) for f in figures_of(doc, prompt)],
        eval=EvalSpec(kind="realbench", tb_files=[str(d / r) for r in testbench + stimulus],
                      extra={"harness_dir": str(d), "tb_top": tb_top.group(1) if tb_top else "tb",
                             "tb_side": [str(d / r) for r in tb if _is_tb_side(r)], "level": level, "domain": domain}),
    )


def load_realbench(conv_path: Path | None, src_root: Path, raw_root: Path | None = None) -> list[BenchItem]:
    return [to_item(r, Path(raw_root or RAW_DEFAULT), src_root) for r in load_records(Path(conv_path or CONV_DEFAULT))]


def diagnose(sim_log: str) -> tuple[list[dict[str, Any]], tuple[int, int] | None]:
    """TB 출력에서 (틀린 출력 목록, (총 불일치, 총 표본))을 뽑는다."""
    bad = [{"output": o, "mismatches": int(n), "first_time": int(t)} for o, n, t in _MISMATCH_RE.findall(sim_log)]
    tot = _TOTAL_RE.search(sim_log)
    return bad, ((int(tot.group(1)), int(tot.group(2))) if tot else None)


def evaluate(item: BenchItem, sv_files: list[Path], eval_dir: Path, timeout: int = 1800) -> dict[str, Any]:
    """후보 SV를 한 파일로 합쳐(TB 쪽이 따로 컴파일하는 모듈은 뺀다) 공식 TB와 함께 Verilator로 돌린다."""
    if not shutil.which("verilator"):
        return {"ok": False, "kind": "realbench", "stage": "tool_error", "log_tail": "verilator가 설치되어 있지 않음"}
    eval_dir = Path(eval_dir).resolve()
    eval_dir.mkdir(parents=True, exist_ok=True)
    x = item.eval.extra
    hdir = Path(x["harness_dir"])
    tb_side = [Path(p) for p in x.get("tb_side", [])]
    refs = [Path(p) for p in item.reference_rtl if Path(p).name.endswith("_ref.sv")]
    provided = modules_in_files(tb_side + refs)
    text, dropped = merge_candidate([Path(p) for p in sv_files], provided)
    cand = eval_dir / f"{item.top}.sv"
    cand.write_text(text, encoding="utf-8")
    tb_top = x.get("tb_top", "tb")
    files = [str(cand), *[str(Path(p)) for p in item.eval.tb_files], *[str(p) for p in refs], *[str(p) for p in tb_side]]
    incs = [f"-I{i}" for i in [str(eval_dir), *item.incdirs, str(hdir / "tb"), str(hdir / "rtl" / "include")]]
    cmd = ["verilator", *VERILATOR_FLAGS, *incs, "--Mdir", str(eval_dir / "obj_dir"), "--top", tb_top, *files]
    try:
        comp = subprocess.run(cmd, cwd=eval_dir, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "kind": "realbench", "stage": "compile", "log_tail": "Verilator 빌드 시간 초과"}
    (eval_dir / "compile.log").write_text(" ".join(cmd) + "\n" + comp.stdout + comp.stderr, encoding="utf-8")
    if comp.returncode != 0 or not (eval_dir / "obj_dir" / f"V{tb_top}").exists():
        errs = [ln for ln in (comp.stdout + comp.stderr).splitlines() if ln.startswith("%Error")]
        return {"ok": False, "kind": "realbench", "stage": "compile", "returncode": comp.returncode, "deduped_modules": dropped,
                "log_tail": ("\n".join(errs) or comp.stdout + comp.stderr)[-1500:]}
    try:
        sim = subprocess.run([str(eval_dir / "obj_dir" / f"V{tb_top}")], cwd=eval_dir, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "kind": "realbench", "stage": "sim_timeout", "deduped_modules": dropped, "log_tail": "시뮬레이션 시간 초과"}
    out = sim.stdout + sim.stderr
    (eval_dir / "sim.log").write_text(out, encoding="utf-8")
    bad, total = diagnose(out)
    ok = sim.returncode == 0 and not bad and total is not None and total[0] == 0 and total[1] > 0
    diag = ""
    if bad:
        diag = "틀린 출력: " + ", ".join(f"{b['output']} ({b['mismatches']}개, 첫 불일치 {b['first_time']}ps)" for b in bad[:8]) + "."
    elif total is None:
        diag = "TB가 결과 요약을 내지 않음 (시뮬레이션이 끝나지 않았거나 중단됨)."
    return {"ok": ok, "kind": "realbench", "stage": "sim", "returncode": sim.returncode, "mismatches": bad, "total": total,
            "deduped_modules": dropped, "diagnosis": diag,
            "log_tail": "" if ok else (diag + " " + out[-1200:])[:1500]}
