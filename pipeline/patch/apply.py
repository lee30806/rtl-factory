"""Repair의 patch 목록을 IR에 적용한다. 쓰기 범위와 동결 범위를 툴이 강제한다.

patch 하나는 `{op, target, field, value}`이다. 노드는 배열 인덱스가 아니라 이름과 `id`로 가리킨다.

    {"op": "set",    "target": "blocks/k_cnt", "field": "body", "value": "..."}
    {"op": "set",    "target": "blocks/k_fsm/transitions/tr3", "field": "cond", "value": "..."}
    {"op": "add",    "target": "signals", "value": {"name": "x", "kind": "wire", "type": "logic"}}
    {"op": "add",    "target": "blocks/k_fsm/transitions", "value": {"id": "tr4", ...}}
    {"op": "remove", "target": "signals/x"}

- 쓸 수 있는 곳: `module`의 L2(`types`, `signals`), L3(`blocks`, `functions`, `assertions`), 연결(`connections`).
  `top`과 `block_top`은 전부 동결 범위라 쓸 수 없다. `external`도 아무것도 쓸 수 없다.
- lint waiver나 검사를 끄는 주석, 전처리 지시어(`` `ifdef ``, `` `line ``, `` `define `` 등)가 들어간 값은 거부한다.
- 하나라도 거부되면 아무것도 적용하지 않는다.

    python3 -m pipeline.patch.apply <ir.json> <patches.json> [--library ir...] [-o out.json]

`-o` 없이 실행하면 적용 결과를 Validator로 검사만 한다 (Repair의 patch 사전 실행).
`-o`를 주면 Validator까지 통과한 경우에만 결과를 쓴다.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from pipeline.ir.model import Module, load_library

KEY = {"types": "name", "signals": "name", "blocks": "id", "connections": "id", "functions": "name", "assertions": "id",
       "ports": "name", "params": "name", "domains": "name", "instances": "name", "transitions": "id"}
WRITABLE = {"module": {"types", "signals", "blocks", "connections", "functions", "assertions"},
            "top": set(), "block_top": set(), "external": set()}
_WAIVER = re.compile(r"lint_off|spyglass\s*(disable|waive)|translate_off|synthesis\s+off|\bwaive", re.I)
# `ifdef VERILATOR로 검사를 피하거나 `line으로 위치를 바꾸거나 `define으로 다른 파일에 새는 것을 막는다.
_DIRECTIVE = re.compile(r"`\s*[A-Za-z_]")


class PatchError(Exception):
    pass


@dataclass
class PatchResult:
    ok: bool
    ir: dict                                   # 성공하면 새 IR, 실패하면 원래 IR
    rejected: list[dict] = field(default_factory=list)  # {"index", "patch", "reason"}


def writable(role: str, node: str) -> bool:
    """`node`(예: `blocks/k_cnt/body`)를 Repair가 고칠 수 있는지. Classifier도 같은 기준을 쓴다."""
    return node.split("/", 1)[0] in WRITABLE.get(role, set())


def ir_hash(raw: dict) -> str:
    """진동 감지용 IR 해시."""
    return hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _parse(target: str) -> list[str]:
    parts = target.split("/")
    if not parts[0] or any(not p for p in parts):
        raise PatchError(f"target 형식이 틀림: {target!r}")
    return parts


def _find(coll: list, section: str, key: str) -> dict | None:
    return next((n for n in coll if isinstance(n, dict) and n.get(KEY[section]) == key), None)


def _resolve(raw: dict, parts: list[str]) -> tuple[list, str, str | None]:
    """target 경로를 (컬렉션, 섹션 이름, 그 안의 키 또는 None)으로 푼다."""
    if parts[0] not in KEY or parts[0] == "transitions":
        raise PatchError(f"알 수 없는 섹션: {parts[0]}")
    coll, section = raw.setdefault(parts[0], []), parts[0]
    rest = parts[1:]
    if len(rest) >= 2 and section == "blocks" and rest[1] == "transitions":
        blk = _find(coll, "blocks", rest[0])
        if blk is None or blk.get("kind") != "fsm":
            raise PatchError(f"fsm 블록이 없음: blocks/{rest[0]}")
        coll, section, rest = blk.setdefault("transitions", []), "transitions", rest[2:]
    if len(rest) > 1:
        raise PatchError(f"target이 너무 깊음: {'/'.join(parts)}")
    return coll, section, (rest[0] if rest else None)


def _has_waiver(v) -> bool:
    if isinstance(v, str):
        return bool(_WAIVER.search(v))
    if isinstance(v, dict):
        return any("waive" in str(k).lower() or _has_waiver(x) for k, x in v.items())
    if isinstance(v, list):
        return any(_has_waiver(x) for x in v)
    return False


def _has_directive(v) -> bool:
    if isinstance(v, str):
        return bool(_DIRECTIVE.search(v))
    if isinstance(v, dict):
        return any(_has_directive(x) for x in v.values())
    if isinstance(v, list):
        return any(_has_directive(x) for x in v)
    return False


def _apply_one(raw: dict, p: dict) -> None:
    if not isinstance(p, dict) or p.get("op") not in ("set", "add", "remove") or not isinstance(p.get("target"), str):
        raise PatchError("patch는 {op: set|add|remove, target, field, value} 형식이어야 함")
    role = raw.get("role", "")
    if not writable(role, p["target"]):
        raise PatchError(f"쓰기 범위 밖: {p['target']} (role {role}에서 고칠 수 있는 곳: "
                         f"{', '.join(sorted(WRITABLE.get(role, ()))) or '없음'})")
    if _has_waiver(p.get("value")) or "waive" in str(p.get("field", "")).lower():
        raise PatchError("lint waiver나 검사를 끄는 내용은 추가할 수 없음")
    if _has_directive(p.get("value")):
        raise PatchError("전처리 지시어(`ifdef, `line, `define 등)는 쓸 수 없음")
    coll, section, key = _resolve(raw, _parse(p["target"]))
    op = p["op"]
    if op == "add":
        if key is not None:
            raise PatchError("add의 target은 컬렉션이어야 함 (예: signals, blocks/<id>/transitions)")
        v = p.get("value")
        if not isinstance(v, dict) or not isinstance(v.get(KEY[section]), str):
            raise PatchError(f"add할 노드에 {KEY[section]}가 없음")
        if _find(coll, section, v[KEY[section]]) is not None:
            raise PatchError(f"이미 있음: {p['target']}/{v[KEY[section]]}")
        coll.append(copy.deepcopy(v))
        return
    if key is None:
        raise PatchError(f"{op}의 target은 노드여야 함 (예: {section}/<{KEY[section]}>)")
    node = _find(coll, section, key)
    if node is None:
        raise PatchError(f"노드가 없음: {p['target']}")
    if op == "remove":
        coll.remove(node)
        return
    f = p.get("field")
    if not isinstance(f, str) or not f:
        raise PatchError("set에는 field가 필요함")
    if f == KEY[section]:
        raise PatchError(f"{f}는 노드의 이름이라 바꿀 수 없음. remove 후 add로 바꿈")
    if section == "blocks" and f == "transitions":
        raise PatchError("전이는 blocks/<id>/transitions/<id> 단위로 고침")
    node[f] = copy.deepcopy(p.get("value"))


def apply(raw: dict, patches: list[dict]) -> PatchResult:
    """patch 목록을 순서대로 적용한다. 하나라도 거부되면 원래 IR을 그대로 돌려준다."""
    from pipeline.validator.structure import frozen_scope

    new = copy.deepcopy(raw)
    rejected = []
    if not isinstance(patches, list):
        return PatchResult(False, raw, [{"index": None, "patch": patches, "reason": "patch 목록은 배열이어야 함"}])
    for i, p in enumerate(patches):
        try:
            _apply_one(new, p)
        except PatchError as e:
            rejected.append({"index": i, "patch": p, "reason": str(e)})
    if not rejected:
        # 범위 규칙과 별개로 동결 범위 해시도 비교한다.
        try:
            before, after = frozen_scope({"m": Module(raw)}), frozen_scope({"m": Module(new)})
        except (KeyError, TypeError, AttributeError):
            before = after = None  # 형식이 깨진 IR은 Validator V1이 잡는다
        if before != after:
            changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
            rejected.append({"index": None, "patch": None, "reason": f"동결 범위가 바뀜: {', '.join(changed)}"})
    if rejected:
        return PatchResult(False, raw, rejected)
    return PatchResult(True, new)


def main(argv: list[str] | None = None) -> int:
    from pipeline.validator.checks import errors, validate

    ap = argparse.ArgumentParser(description="IR patch 적용")
    ap.add_argument("ir")
    ap.add_argument("patches")
    ap.add_argument("--library", nargs="*", default=[], help="인스턴스가 참조하는 모듈의 IR")
    ap.add_argument("-o", "--out", help="결과 IR을 쓸 파일. 없으면 검사만 함")
    args = ap.parse_args(argv)
    raw = json.loads(Path(args.ir).read_text(encoding="utf-8"))
    res = apply(raw, json.loads(Path(args.patches).read_text(encoding="utf-8")))
    out = {"ok": res.ok, "rejected": res.rejected, "errors": [], "ir_hash": ir_hash(res.ir)}
    if res.ok:
        issues = validate(Module(res.ir), load_library(args.library))
        out["errors"] = [str(i) for i in errors(issues)]
        out["ok"] = not out["errors"]
        if args.out and out["ok"]:
            Path(args.out).write_text(json.dumps(res.ir, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
