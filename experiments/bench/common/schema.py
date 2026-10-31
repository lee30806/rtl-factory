"""벤치마크 평가 프레임워크 공통 데이터 구조 (BENCHMARK_PLAN 4.1절, 6절)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class EvalSpec:
    kind: str = "iverilog"                              # iverilog | verilator | cvdp_docker | yosys_lec
    tb_files: list[str] = field(default_factory=list)
    pass_pattern: str = "Your Design Passed|PASS"
    top_tb: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EvalSpec:
        return cls(
            kind=d.get("kind", "iverilog"),
            tb_files=list(d.get("tb_files", [])),
            pass_pattern=d.get("pass_pattern", "Your Design Passed|PASS"),
            top_tb=d.get("top_tb"),
        )


@dataclass
class BenchItem:
    benchmark: str                                      # rtllm | chipverilog | realbench | cvdp | rtl_benchls
    id: str
    category: str                                       # sequential | standalone | multi_module | cross_module | cid003 | cid005 | round_trip
    top: str
    prompt_text: str
    header_sv: str                                      # 포트·파라미터만 선언된 빈 모듈 텍스트 또는 헤더 파일 경로
    params: dict[str, str] = field(default_factory=dict)
    incdirs: list[str] = field(default_factory=list)
    defines: dict[str, str] = field(default_factory=dict)
    cells: list[str] = field(default_factory=list)
    reference_rtl: list[str] = field(default_factory=list)   # B1~B4에서는 에이전트 작업 디렉터리에 절대 노출하지 않음
    eval: EvalSpec = field(default_factory=EvalSpec)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BenchItem:
        ev = d.get("eval", {})
        return cls(
            benchmark=d["benchmark"],
            id=d["id"],
            category=d.get("category", "sequential"),
            top=d["top"],
            prompt_text=d["prompt_text"],
            header_sv=d["header_sv"],
            params=dict(d.get("params", {})),
            incdirs=list(d.get("incdirs", [])),
            defines=dict(d.get("defines", {})),
            cells=list(d.get("cells", [])),
            reference_rtl=list(d.get("reference_rtl", [])),
            eval=ev if isinstance(ev, EvalSpec) else EvalSpec.from_dict(ev),
        )


@dataclass
class BenchResult:
    benchmark: str
    id: str
    category: str
    top: str
    spec_gate_ok: bool
    spec_errors: list[str] = field(default_factory=list)
    pipeline_status: str = "not_run"
    chosen: dict[str, int] | None = None
    agent_runs: int = 0
    minutes: float = 0.0
    auto_approvals: list[dict[str, Any]] = field(default_factory=list)
    review_codes: list[str] = field(default_factory=list)
    candidates: dict[str, dict[str, Any]] = field(default_factory=dict)
    bench_pass_chosen: bool = False
    bench_pass_any: bool = False
    ppa_chosen: dict[str, Any] | None = None
    # 외부 TB 보정 (위 필드는 첫 실행 결과 그대로): 회차별 계기, 수정 사유, 다시 돌린 결과
    ext_fix: list[dict[str, Any]] = field(default_factory=list)
    bench_pass_chosen_final: bool = False                # 마지막 실행에서 선택된 후보의 외부 TB 통과
    bench_pass_any_final: bool = False                   # 어느 실행이든 외부 TB를 통과한 후보가 있음
    sim_model: bool = False                              # 시뮬레이션 모델 경로로 처리함 (합성, PPA 없음)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
