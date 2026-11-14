"""SV 텍스트의 module 블록 다루기: 이름 찾기, 중복 정의 제거, 제공 셀과 겹치는 정의 제거 (ChipVerilog·RealBench·CVDP 채점 공통)."""
from __future__ import annotations

import re
from pathlib import Path

MODULE_BLOCK_RE = re.compile(r"^\s*module\s+([A-Za-z_]\w*)\b.*?\bendmodule\b", re.M | re.S)
MODULE_NAME_RE = re.compile(r"^\s*module\s+([A-Za-z_]\w*)\b", re.M)


def module_names(text: str) -> list[str]:
    return MODULE_NAME_RE.findall(text)


def modules_in_files(files: list[Path]) -> set[str]:
    names: set[str] = set()
    for f in files:
        names.update(module_names(Path(f).read_text(encoding="utf-8", errors="replace")))
    return names


def dedupe_modules(text: str) -> tuple[str, list[str]]:
    """같은 이름의 module 정의가 둘 이상이면 처음 것만 남긴다 (파일 목록은 제공 셀이 먼저 온다)."""
    seen: set[str] = set()
    dropped: list[str] = []

    def keep(m: re.Match) -> str:
        if m.group(1) in seen:
            dropped.append(m.group(1))
            return f"// [rtlf] 중복 정의 제거: {m.group(1)} (제공 셀을 씀)"
        seen.add(m.group(1))
        return m.group(0)

    return MODULE_BLOCK_RE.sub(keep, text), dropped


def drop_modules(text: str, names: set[str]) -> tuple[str, list[str]]:
    """채점 환경이 따로 컴파일하는 모듈(제공 셀, 테스트벤치 쪽 라이브러리)과 이름이 같은 정의를 후보 텍스트에서 뺀다."""
    dropped: list[str] = []

    def keep(m: re.Match) -> str:
        if m.group(1) in names:
            dropped.append(m.group(1))
            return f"// [rtlf] 채점 환경이 제공하는 모듈과 겹쳐 제거: {m.group(1)}"
        return m.group(0)

    return MODULE_BLOCK_RE.sub(keep, text), dropped


def merge_candidate(sv_files: list[Path], provided: set[str] | None = None) -> tuple[str, list[str]]:
    """후보 SV 파일들을 한 텍스트로 합친다: 중복 정의는 처음 것만, 채점 환경이 제공하는 모듈은 뺀다."""
    text, dropped = dedupe_modules("\n\n".join(Path(p).read_text(encoding="utf-8", errors="replace") for p in sv_files))
    if provided:
        text, d2 = drop_modules(text, provided)
        dropped += d2
    return text, dropped
