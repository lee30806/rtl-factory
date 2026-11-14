"""B2: ChipVerilog 로더와 채점 (BENCHMARK_PLAN B2).

입력은 pymtl에서 엔진 간 공정 비교용으로 미리 만든 CVDP 형식 JSONL(`build/cvdp_datasets/cvdp_chipverilog.jsonl`)이다.
레코드의 `input`(원문 프롬프트 + 추가 프롬프트)을 SPEC 작성 입력으로 쓰고, `harness.files`에서 헤더 include와 재사용 셀을 꺼낸다.

채점은 ChipVerilog 공식 도구(`build/datasets/ChipVerilog/tools/formal_equivalence.py verify`)로 한다: iverilog 컴파일 게이트,
자체 검사 TB가 있으면 시뮬레이션, 없으면 Yosys 등가(유도/유한 깊이). 공식 판정 `status == "pass"`만 통과다.
정답 RTL(`Des/`)은 채점에만 쓰고 에이전트에게 주지 않는다.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from experiments.bench.common.modules import dedupe_modules  # noqa: F401  (시험과 pymtl 비교 스크립트가 여기서 가져온다)
from experiments.bench.common.schema import BenchItem, EvalSpec

# 이 저장소(experiments/bench/adapters)와 pymtl(src/rtl_factory/bench/adapters) 어디에 있든 위로 올라가며 데이터를 찾는다
CV_ROOT = next((d / "build" / "datasets" / "ChipVerilog" for d in Path(__file__).resolve().parents
                if (d / "build" / "datasets" / "ChipVerilog" / "tools").exists()), Path("build/datasets/ChipVerilog"))
TOOL = CV_ROOT / "tools" / "formal_equivalence.py"


def load_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def des_dir(original_id: str) -> Path:
    hits = [p for p in CV_ROOT.glob(f"Des/*/{original_id}") if p.is_dir()]
    if len(hits) != 1:
        raise FileNotFoundError(f"ChipVerilog Des 디렉터리를 찾지 못함: {original_id} ({hits})")
    return hits[0]


def reference_file(d: Path) -> Path:
    """공식 도구(discover_reference_file)와 같은 규칙: `<모듈>.v`, 없으면 디렉터리의 유일한 .v."""
    if (f := d / f"{d.name}.v").exists():
        return f
    vs = sorted(d.glob("*.v"))
    if len(vs) != 1:
        raise FileNotFoundError(f"정답 파일을 정하지 못함: {d}")
    return vs[0]


def to_item(rec: dict, src_root: Path) -> BenchItem:
    h, meta = rec["harness"], rec["metadata"]
    d = Path(src_root).resolve() / rec["id"]
    for rel, text in h["files"].items():
        if rel.startswith(("rtl/", "src/")):   # 정답(ref/)과 TB(tb/)는 꺼내지 않는다
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
    top = meta["top_module"]
    return BenchItem(
        benchmark="chipverilog",
        id=rec["id"],
        category=meta.get("tier", "standalone"),
        top=top,
        prompt_text=rec["input"],
        header_sv=meta.get("header_sv") or rec["context"].get(f"rtl/{top}.sv", ""),
        incdirs=[str(d / x) for x in h.get("include_dirs", [])],
        cells=[str(d / c) for c in h.get("cell_files", [])],
        reference_rtl=[str(reference_file(des_dir(meta["original_id"])))],
        eval=EvalSpec(kind="chipverilog"),
    )


def load_chipverilog(path: Path, src_root: Path) -> list[BenchItem]:
    return [to_item(r, src_root) for r in load_records(path)]


_CHECK_RE = re.compile(r"^\s*\d+\s*ps\s+(Answer is correct|Error! \S+ is incorrect)\s+([0-9a-fA-Fxz]+)", re.M)
_EXPECT_RE = re.compile(r"==\s*\d+'h([0-9a-fA-F_]+)")
_STIM_RE = re.compile(r"^\s*(\w+)\s*=\s*\d+'([bh])([0-9a-fA-F_]+)\s*;", re.M)


def tb_stimuli(tb_text: str) -> list[dict[str, str]]:
    """TB 원문의 입력 대입(`opa = 64'b...;`)을 검사 순서대로 묶는다. 가장 자주 대입되는 신호들(피연산자)만 보고
    clk, rst, enable처럼 몇 번만 대입되는 신호는 뺀다. 같은 이름이 다시 나오면 다음 검사 입력. 값은 16진수로."""
    found = _STIM_RE.findall(tb_text)
    if not found:
        return []
    counts: dict[str, int] = {}
    for name, _, _ in found:
        counts[name] = counts.get(name, 0) + 1
    top = max(counts.values())
    recs: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    for name, base, val in found:
        if counts[name] != top:
            continue
        if name in cur:
            recs.append(cur)
            cur = {}
        v = val.replace("_", "")
        cur[name] = format(int(v, 2), "x") if base == "b" else v.lower()
    if cur:
        recs.append(cur)
    return recs


def diagnose_sim(sim_log: str, tb_text: str = "") -> str:
    """자체 검사 TB의 출력("Answer is correct / Error! out is incorrect <값>")에서 Repair가 쓸 한 줄 진단을 만든다.
    값만 틀렸는지, 모든 검사 시점에 같은 값이 나오는지(파이프라인 깊이·enable 처리 의심), 기대값이 몇 검사 간격 어긋나 나오는지를 본다.
    TB 원문이 있으면 `== 64'h...` 기대값 목록과 대조한다. 형식이 맞지 않으면 빈 문자열."""
    checks = _CHECK_RE.findall(sim_log)
    if not checks:
        return ""
    actual = [v.lower().replace("_", "") for _, v in checks]
    n_ok = sum(1 for k, _ in checks if k.startswith("Answer"))
    parts = [f"자체 검사 {len(checks)}개 중 {n_ok}개 일치"]
    if len(actual) > 1 and len(set(actual)) == 1:
        parts.append("모든 검사 시점에 출력이 같은 값이다: 연속 입력이 순서대로 나오지 않으므로 파이프라인 깊이(지연)나 enable 처리를 먼저 의심할 것")
    expected = [v.lower().replace("_", "") for v in _EXPECT_RE.findall(tb_text)] if tb_text else []
    stim = tb_stimuli(tb_text) if tb_text else []
    if expected and len(expected) == len(actual) and n_ok < len(actual):
        # 틀린 검사마다 입력(있으면)·기대·실제를 적는다: Repair가 특수값(비정규, 오버플로) 같은 미정의 입력을 알아보게
        bad = [i for i, (k, _) in enumerate(checks) if not k.startswith("Answer")][:5]
        parts += [f"검사 {i + 1}: " + (f"입력 {stim[i]} " if len(stim) == len(actual) else "")
                  + f"기대 {expected[i]} 실제 {actual[i]}" for i in bad]
        best_k, best_hits = 0, 0
        for k in range(-(len(actual) - 1), len(actual)):
            hits = sum(1 for i, a in enumerate(actual) if 0 <= i - k < len(expected) and a == expected[i - k])
            if hits > best_hits:
                best_k, best_hits = k, hits
        if best_k and best_hits >= 2:
            # k>0: 검사 i에 기대값 i-k가 보임 = 출력이 늦음 = 후보 지연이 정답보다 길다
            parts.append(f"기대값 순서가 {abs(best_k)} 검사 간격 {'늦게' if best_k > 0 else '빠르게'} 나온다 ({best_hits}개 일치): "
                         f"지연이 정답보다 {'길다' if best_k > 0 else '짧다'}")
    return ". ".join(parts) + "."


def _verify(text: str, name: str, root: Path, timeout: int) -> tuple[dict | None, Path, str]:
    cdir = root / "result_root" / "rtlf" / name
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / f"{name}_t1.v").write_text(text, encoding="utf-8")
    p = subprocess.run([sys.executable, str(TOOL), "verify", str(cdir), "--result-root", str(root / "result_root"),
                        "--report-dir", str(root / "reports")], cwd=CV_ROOT, capture_output=True, text=True, timeout=timeout)
    (root / "verify.log").write_text(p.stdout + p.stderr, encoding="utf-8")
    rf = cdir / f"{name}_t1" / "result.json"
    return (json.loads(rf.read_text(encoding="utf-8")) if rf.exists() else None), cdir / f"{name}_t1", p.stdout + p.stderr


def evaluate(item: BenchItem, sv_files: list[Path], eval_dir: Path, timeout: int = 1800) -> dict[str, Any]:
    """후보 SV 파일들을 한 파일로 합쳐 공식 결과 트리(`<root>/rtlf/<모듈>/<모듈>_t1.v`)에 두고 공식 verify를 돌린다.
    같은 이름 module이 둘이면 처음 것(제공 셀)만 남긴다."""
    eval_dir = Path(eval_dir).resolve()
    des = Path(item.reference_rtl[0]).parent
    name = ("mips_" if des.parent.name == "mips_16" else "") + des.name
    # 방출 RTL의 SVA는 `ifndef SYNTHESIS 안에 있다. iverilog는 동시 assertion을 읽지 못하므로 RTLLM 채점(-DSYNTHESIS)처럼 끈다
    body, dropped = dedupe_modules("\n\n".join(Path(p).read_text(encoding="utf-8") for p in sv_files))
    text = "`define SYNTHESIS\n" + body
    r, adir, log = _verify(text, name, eval_dir, timeout)
    if not r:
        return {"ok": False, "kind": "chipverilog", "stage": "tool_error", "log_tail": log[-1000:]}
    ok = r.get("status") == "pass"
    # 등가 실패면 증명되지 않은 출력 이름을 남긴다 (Repair가 볼 단서. 공식 반례 요약은 배너뿐인 때가 많다)
    unproven = []
    if (eq := adir / "equivalence.log").exists():
        for m in re.finditer(r"Unproven \$equiv \S+: \\(\S+?)_gold(?: \[(\d+)\])?", eq.read_text(errors="replace")):
            if m.group(1) not in unproven:
                unproven.append(m.group(1))
    stage = {"compile_fail": "compile"}.get(r.get("status"), "sim")
    try:   # 공식 도구가 정답 쪽에서 찾지 못해 스텁으로 바꾼 하위 모듈 (JSON 문자열). 있으면 채점 기준이 정답이 아니다
        stubs = json.loads(r.get("generated_stub_modules") or "[]")
    except (TypeError, ValueError):
        stubs = []
    # 자체 검사 TB 실패면 값 목록만으로는 Repair가 지연 문제를 보지 못한다. 출력 패턴을 읽어 한 줄 진단을 앞에 붙인다
    diag = ""
    if not ok and (sl := adir / "sim.log").exists():
        tbs = sorted(p for p in des.glob("*.v") if re.search(r"tb|test", p.name, re.I))
        diag = diagnose_sim(sl.read_text(errors="replace"), tbs[0].read_text(errors="replace") if tbs else "")
    return {"ok": ok, "kind": "chipverilog", "status": r.get("status"), "flow": r.get("flow"), "proof_type": r.get("proof_type"),
            "stage": stage, "unproven_outputs": unproven, "deduped_modules": dropped, "diagnosis": diag, "stub_modules": stubs,
            "log_tail": "" if ok else (f'{r.get("status")}: ' + (diag + " " if diag else "")
                                       + (f"정답의 하위 모듈 {stubs}이(가) 데이터셋에 없어 공식 도구의 스텁으로 채점됨 (채점 결함). " if stubs else "")
                                       + (f"정답과 등가가 증명되지 않은 출력 {unproven[:20]} (사이클 단위 비교). " if unproven else "")
                                       + f'{r.get("reason", "")} {r.get("counterexample_summary", "")}')[:1500]}


def official_verdict(rec: dict, rtl_code: str, work_dir: Path) -> dict[str, Any]:
    """CVDP 레코드 하나와 아무 엔진의 생성 RTL 문자열을 공식 도구로 채점한다 (엔진 간 비교에서 모든 엔진에 같은 판정).
    CVDP 변환본이 덧붙인 `include 파일(예: defines.vh)은 공식 도구에 없으므로 레코드의 같은 이름 파일 내용으로 펼친다."""
    work_dir = Path(work_dir).resolve()
    files = {**rec.get("context", {}), **rec["harness"]["files"]}   # include 대상은 harness, 없으면 context에서 찾는다

    def inline(m: re.Match) -> str:
        name = m.group(1)
        return next((v for k, v in files.items() if k == name or k.endswith("/" + name)), m.group(0))

    if not rtl_code.strip():
        return {"ok": False, "kind": "chipverilog", "status": "no_rtl", "log_tail": "생성 RTL 없음"}
    work_dir.mkdir(parents=True, exist_ok=True)
    cand = work_dir / "cand.v"
    cand.write_text(re.sub(r'`include\s+"([^"]+)"', inline, rtl_code), encoding="utf-8")
    return evaluate(to_item(rec, work_dir / "src"), [cand], work_dir / "eval")
