"""P11 R1: 원본 RTL 소스를 읽어 컴파일 단위 하나로 elaborate한다.

- 파일 목록은 순서가 있다. `-f filelist.f`의 줄(파일, `+incdir+`, `-I`, `+define+`, `-D`, 주석 `//`, `#`)도 받는다.
- 헤더(`.vh`, `.svh`, `.h`, `.inc`, 또는 다른 파일이 `` `include``하는 파일)는 따로 컴파일하지 않고 include 경로로만 찾는다.
- 나머지 파일은 받은 순서대로 컴파일 단위 하나로 읽는다 (상용 시뮬레이터처럼 앞 파일의 매크로가 뒤 파일에 보임).
  정의보다 먼저 쓰인 매크로는 정의한 파일을 알려 주고 거부한다.
- Verilog 파일(`.v`, 또는 lang="verilog")은 먼저 그대로 읽고, 오류가 나면 `` `begin_keywords "1364-2005" ``로 감싸 다시 읽는다.
  SV 예약어(`logic` 등)를 이름으로 쓴 원본도 읽힌다. 감싼 글자는 같은 줄 맨 앞에 붙여 줄 번호가 바뀌지 않는다. 감싼 파일은
  `wrapped`에 남겨 R4가 원본을 시뮬레이션할 때도 같게 감싼다.
- 원문 글자는 `text(node)`로 되찾는다 (매크로를 풀기 전의 글자).
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

import pyslang

HEADER_EXT = {".vh", ".svh", ".h", ".inc", ".vinc", ".svi"}
VERILOG_EXT = {".v", ".vlog"}
KW_OPEN = '`begin_keywords "1364-2005" '
KW_CLOSE = "\n`end_keywords\n"
DEFINE_RE = re.compile(r"^[ \t]*`define[ \t]+([A-Za-z_][A-Za-z0-9_$]*)(\([^)]*\))?[ \t]*((?:[^\n\\]|\\\n|\\.)*)", re.M)
INCLUDE_RE = re.compile(r'`include[ \t]+"([^"]+)"')
NUM_RE = re.compile(r"^(?:\d[\d_]*|\d*\s*'[sS]?[bBoOdDhH]\s*[0-9a-fA-F_xXzZ?]+)$")
CONST_EXPR_RE = re.compile(r"^[\s\d_'sSbBoOdDhHa-fA-FxXzZ?()+\-*/%<>&|^~`A-Za-z0-9_$]+$")


class SourceError(Exception):
    """R1이 읽기 전에 거부하는 경우 (파일 없음, 파일 순서 오류, 문법 오류)."""

    def __init__(self, items: list[dict]):
        super().__init__("; ".join(i["message"] for i in items))
        self.items = items


@dataclass
class Inputs:
    files: list[Path]
    top: str
    params: dict[str, str] = field(default_factory=dict)
    incdirs: list[Path] = field(default_factory=list)
    defines: dict[str, str] = field(default_factory=dict)
    lang: str | None = None          # None이면 확장자로 정함. "verilog" | "sv"

    def to_dict(self) -> dict:
        return {"files": [str(f) for f in self.files], "top": self.top, "params": self.params,
                "incdirs": [str(d) for d in self.incdirs], "defines": self.defines, "lang": self.lang}


def read_filelist(path: Path) -> tuple[list[Path], list[Path], dict[str, str]]:
    """`-f` 파일 목록 → (파일, include 경로, define). 상대 경로는 목록 파일의 폴더 기준."""
    base = path.parent
    files, incs, defs = [], [], {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = re.sub(r"//.*|#.*", "", line).strip()
        if not line:
            continue
        for tok in shlex.split(line):
            if tok.startswith("+incdir+"):
                incs += [base / p for p in tok[len("+incdir+"):].split("+") if p]
            elif tok.startswith("+define+"):
                for d in tok[len("+define+"):].split("+"):
                    if d:
                        k, _, v = d.partition("=")
                        defs[k] = v or "1"
            elif tok.startswith("-I"):
                incs.append(base / tok[2:])
            elif tok.startswith("-D"):
                k, _, v = tok[2:].partition("=")
                defs[k] = v or "1"
            elif tok.startswith("-f"):
                f2, i2, d2 = read_filelist(base / tok[2:].strip())
                files += f2
                incs += i2
                defs.update(d2)
            else:
                files.append(base / tok)
    return files, incs, defs


def _resolve_include(name: str, frm: Path, incdirs: list[Path]) -> Path | None:
    for d in (frm.parent, *incdirs):
        p = (d / name)
        if p.is_file():
            return p.resolve()
    return None


@dataclass
class Macro:
    name: str
    value: str
    file: str
    line: int
    args: bool = False

    @property
    def numeric(self) -> bool:
        """숫자 상수 매크로인지 (인자 없음, 값이 리터럴이나 리터럴과 다른 숫자 매크로의 산술식)."""
        return not self.args and bool(self.value) and bool(CONST_EXPR_RE.match(self.value)) and not re.search(r"\$|\b(begin|end|if|assign)\b", self.value)


class Design:
    """컴파일한 원본. sm으로 원문 위치를 되찾는다."""

    def __init__(self, inp: Inputs):
        self.inp = inp
        self.files = [Path(f).resolve() for f in inp.files]
        missing = [f for f in self.files if not f.is_file()]
        if missing:
            raise SourceError([{"kind": "missing_file", "message": f"파일이 없음: {f}"} for f in missing])
        self.incdirs = [Path(d).resolve() for d in inp.incdirs]
        self.headers, self.units = self._classify()
        self.macros: dict[str, Macro] = {}
        self._scan_macros()
        # Verilog 파일은 먼저 그대로 읽고(상용 툴처럼 SV 문법으로), 오류가 나면 1364-2005 키워드로 감싸 다시 읽는다.
        # 감싸면 SV 예약어를 이름으로 쓴 원본이 읽히지만, `$error` 같은 SV 시스템 태스크는 Verilator가 거부하므로 필요할 때만 감싼다.
        self.wrapped: set[str] = set()
        self._compile(wrap=False)
        if any(d.isError() for d in self.diags) and any(self.is_verilog(f) for f in self.units):
            self._compile(wrap=True)
        self._check_errors()
        tops = [t for t in self.comp.getRoot().topInstances if t.name == inp.top]
        if not tops:
            raise SourceError([{"kind": "no_top", "message": f"top 모듈이 없음: {inp.top}"}])
        self.top = tops[0]

    def _compile(self, wrap: bool) -> None:
        inp = self.inp
        self.sm = pyslang.SourceManager()
        self.sm.setDisableProximatePaths(True)
        for d in self.incdirs:
            self.sm.addUserDirectories(str(d))
        self.wrapped = set()
        bufs = []
        for f in self.units:
            text = f.read_text(encoding="utf-8", errors="replace")
            if wrap and self.is_verilog(f):
                text = KW_OPEN + text + KW_CLOSE
                self.wrapped.add(str(f))
            bufs.append(self.sm.assignText(str(f), text))
        bag = pyslang.Bag()
        po = pyslang.PreprocessorOptions()
        po.predefines = [f"{k}={v}" for k, v in inp.defines.items()]
        po.additionalIncludePaths = [str(d) for d in self.incdirs]
        co = pyslang.CompilationOptions()
        co.topModules = {inp.top}
        co.paramOverrides = [f"{k}={v}" for k, v in inp.params.items()]
        bag.preprocessorOptions, bag.compilationOptions = po, co
        self.tree = pyslang.SyntaxTree.fromBuffers(bufs, self.sm, bag)
        self.comp = pyslang.Compilation(bag)
        self.comp.addSyntaxTree(self.tree)
        self.diags = list(self.comp.getAllDiagnostics())
        self._bytes = {}

    # -- 파일 --
    def is_verilog(self, f: Path) -> bool:
        if self.inp.lang:
            return self.inp.lang == "verilog"
        return f.suffix.lower() in VERILOG_EXT

    def _classify(self) -> tuple[list[Path], list[Path]]:
        included: set[Path] = set()
        for f in self.files:
            for name in INCLUDE_RE.findall(f.read_text(encoding="utf-8", errors="replace")):
                p = _resolve_include(name, f, self.incdirs)
                if p:
                    included.add(p)
        headers = [f for f in self.files if f.suffix.lower() in HEADER_EXT or f in included]
        # 목록에 없는 헤더도 include 경로에서 찾으므로, 그 폴더를 include 경로 뒤에 더한다
        for h in headers:
            if h.parent not in self.incdirs:
                self.incdirs.append(h.parent)
        return headers, [f for f in self.files if f not in headers]

    def _scan_macros(self) -> None:
        seen: set[Path] = set()

        def scan(f: Path):
            if f in seen:
                return
            seen.add(f)
            text = f.read_text(encoding="utf-8", errors="replace")
            for m in DEFINE_RE.finditer(text):
                value = re.sub(r"\\\n", " ", m.group(3)).split("//")[0].strip()
                line = text.count("\n", 0, m.start()) + 1
                self.macros.setdefault(m.group(1), Macro(m.group(1), value, str(f), line, bool(m.group(2))))
            for name in INCLUDE_RE.findall(text):
                p = _resolve_include(name, f, self.incdirs)
                if p:
                    scan(p)

        for k, v in self.inp.defines.items():
            self.macros[k] = Macro(k, v, "-D", 0)
        for f in self.units:
            scan(f)

    def _check_errors(self) -> None:
        items = []
        for d in self.diags:
            if not d.isError():
                continue
            code = str(d.code).removeprefix("DiagCode(").removesuffix(")")
            file, line = self.where(d.location)
            item = {"kind": "compile", "code": code, "file": file, "line": line, "message": f"{file}:{line} {code}"}
            if code == "UnknownDirective":
                name = self._directive_name(d.location)
                if name and name in self.macros and self.macros[name].file != "-D":
                    item = {"kind": "macro_order", "code": code, "file": file, "line": line, "macro": name,
                            "message": f"{file}:{line} 매크로 `{name} 사용이 정의({self.macros[name].file}:{self.macros[name].line})보다 앞섬. "
                                       f"파일 목록에서 {Path(self.macros[name].file).name} 파일을 {Path(file).name} 앞으로 옮기세요"}
                elif name:
                    item["message"] = f"{file}:{line} 정의되지 않은 매크로 `{name}"
            if item not in items:
                items.append(item)
        if items:
            raise SourceError(items)

    def _directive_name(self, loc) -> str | None:
        try:
            text = self._slice(loc.buffer, loc.offset, loc.offset + 80)
        except Exception:
            return None
        m = re.match(r"`([A-Za-z_][A-Za-z0-9_$]*)", text)
        return m.group(1) if m else None

    # -- 위치와 원문 --
    def where(self, loc) -> tuple[str, int]:
        try:
            loc = self.sm.getFullyOriginalLoc(loc)
            return self.sm.getFileName(loc), self.sm.getLineNumber(loc)
        except Exception:
            return "?", 0

    def text(self, node) -> str:
        """구문 노드의 원문 (매크로를 풀기 전). 앞뒤 공백을 지운다."""
        if node is None:
            return ""
        r = self.sm.getFullyOriginalRange(node.sourceRange)
        return self._slice(r.start.buffer, r.start.offset, r.end.offset).strip()

    def _slice(self, buf, a: int, b: int) -> str:
        """버퍼의 바이트 위치 [a, b) 글자. pyslang 위치는 바이트 단위라 한글 주석이 있으면 문자열 인덱스와 다르다."""
        key = buf.id if hasattr(buf, "id") else buf
        cache = self.__dict__.setdefault("_bytes", {})
        if key not in cache:
            cache[key] = self.sm.getSourceText(buf).encode("utf-8")
        return cache[key][a:b].decode("utf-8", errors="replace")

    def warnings(self) -> list[dict]:
        out = []
        for d in self.diags:
            if d.isError():
                continue
            code = str(d.code).removeprefix("DiagCode(").removesuffix(")")
            file, line = self.where(d.location)
            out.append({"code": code, "file": file, "line": line})
        return out

    def module_source(self, defn) -> dict:
        """모듈 정의의 원문(파일, 시작·끝 줄, 글자). 감싼 키워드 글자는 지운다."""
        syn = defn.syntax
        r = self.sm.getFullyOriginalRange(syn.sourceRange)
        text = self._slice(r.start.buffer, r.start.offset, r.end.offset)
        file = self.sm.getFileName(r.start)
        return {"file": file, "line": self.sm.getLineNumber(r.start), "end_line": self.sm.getLineNumber(r.end),
                "text": text.replace(KW_OPEN, "")}

    def headers_text(self) -> dict[str, str]:
        return {str(h): h.read_text(encoding="utf-8", errors="replace") for h in self.headers}


def rel(path: str | Path, base: Path) -> str:
    try:
        return os.path.relpath(path, base)
    except ValueError:
        return str(path)
