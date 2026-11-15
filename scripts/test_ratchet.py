#!/usr/bin/env python3
"""Compare committed test data without checking out, importing or running head code.

The trusted base copy is run by test-ratchet.yml. Exit 0 means no supported
weakening was found; exit 1 means findings or an inspection error. This is a
structural ratchet, not a proof that test assertions remain meaningful.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from dataclasses import dataclass
import json
import math
from pathlib import PurePosixPath
import re
import subprocess


GUARDS = {"scripts/test_ratchet.py", ".github/workflows/test-ratchet.yml"}
GATES = {"scripts/test_safe.sh", "scripts/eval_gate.sh"}
EVALS = {"tests/eval_set.json", "tests/eval_set.learned.json"}
SECTIONS = ("deterministic", "model_graded", "watch")
SKIPS = {"pytest.skip", "pytest.xfail", "pytest.mark.skip",
         "pytest.mark.skipif", "pytest.mark.xfail"}


class InspectionError(Exception):
    """Data could not be inspected; never turn this into a passing result."""


@dataclass(frozen=True, order=True)
class Finding:
    kind: str
    path: str
    detail: str


def git(*args: str) -> bytes:
    result = subprocess.run(
        ["git", "--no-replace-objects", *args], capture_output=True, timeout=60,
    )
    if result.returncode:
        # Do not echo arbitrary git diagnostics or source blobs into Actions logs.
        raise InspectionError(f"git {args[0]} failed (exit {result.returncode})")
    return result.stdout


def commit(value: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", value):
        raise InspectionError("--base and --head must be full commit SHAs")
    return git("rev-parse", "--verify", f"{value}^{{commit}}").decode().strip()


class Tree:
    def __init__(self, revision: str):
        self.revision = revision
        self.entries = {}
        for record in git("ls-tree", "-rz", "--full-tree", revision).split(b"\0"):
            if record:
                metadata, path = record.split(b"\t", 1)
                mode, kind, oid = metadata.decode().split()
                self.entries[path.decode("utf-8")] = (mode, kind, oid)

    def read(self, path: str) -> bytes | None:
        if path not in self.entries:
            return None
        mode, kind, _ = self.entries[path]
        if mode not in {"100644", "100755"} or kind != "blob":
            raise InspectionError(f"{path}: expected a regular file")
        return git("show", f"{self.revision}:{path}")


def changed_paths(base: str, head: str) -> set[str]:
    fields = git("diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                 "--name-status", "-z", f"{base}...{head}", "--").split(b"\0")
    # --no-renames makes every record exactly status NUL path NUL.
    return {fields[i].decode("utf-8") for i in range(1, len(fields) - 1, 2)}


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{dotted(node.value)}.{node.attr}"
    return ""


def python_inventory(tree: Tree, findings: list[Finding]):
    tests = defaultdict(set)
    skips = Counter()
    for path in sorted(tree.entries):
        if not path.endswith(".py"):
            continue
        try:
            module = ast.parse(tree.read(path), filename=path)
        except (InspectionError, SyntaxError, ValueError, TypeError, RecursionError) as exc:
            findings.append(Finding("inspection_error", path, type(exc).__name__))
            continue
        aliases = {"pytest": "pytest"}
        for node in ast.walk(module):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "pytest":
                        aliases[alias.asname or "pytest"] = "pytest"
            elif isinstance(node, ast.ImportFrom) and node.module in {"pytest", "pytest.mark"}:
                for alias in node.names:
                    aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"

        def visit(node, scope=(), parent=None):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test_"):
                    tests[node.name].add(path)
                scope = (*scope, node.name)
            elif isinstance(node, ast.ClassDef):
                scope = (*scope, node.name)
            target = node.func if isinstance(node, ast.Call) else node
            name = dotted(target)
            root, _, tail = name.partition(".")
            name = aliases.get(root, root) + (f".{tail}" if tail else "")
            # Count each call/marker once, including bare marks and pytestmark lists.
            in_call = isinstance(parent, ast.Call) and parent.func is node
            if name in SKIPS and not in_call:
                skips[(path, ".".join(scope), ast.dump(node, include_attributes=False))] += 1
            for child in ast.iter_child_nodes(node):
                visit(child, scope, node)

        visit(module)
    return tests, skips


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def eval_data(tree: Tree, path: str):
    raw = tree.read(path)
    if raw is None:
        return {}, {}
    data = json.loads(raw, object_pairs_hook=unique_object)
    if not isinstance(data, dict):
        raise ValueError("eval must be an object")
    cases = {}
    for section in SECTIONS:
        rows = data.get(section, [])
        if not isinstance(rows, list):
            raise ValueError(f"{section} must be a list")
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                raise ValueError("eval case needs a nonempty id")
            key = (section, row["id"])
            if key in cases:
                raise ValueError("duplicate eval case id")
            cases[key] = row
    return data, cases


def bounds(value, clamp=False):
    if clamp:
        if not isinstance(value, dict) or set(value) != {"low", "high"}:
            raise ValueError("clamp_bounds needs low/high")
        value = [value["low"], value["high"]]
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("range needs two bounds")
    if any(type(n) not in (int, float) or not math.isfinite(n) for n in value):
        raise ValueError("range bounds must be finite numbers")
    if value[0] > value[1]:
        raise ValueError("range bounds are reversed")
    return value


def bucket(value):
    if not isinstance(value, list) or any(n is not None and not isinstance(n, str) for n in value):
        raise ValueError("wb_allowed must be a list of strings/null")
    return set(value)


def compare_evals(old: Tree, new: Tree, path: str, findings: list[Finding]):
    try:
        before, old_cases = eval_data(old, path)
        after, new_cases = eval_data(new, path)
        # Validate even newly added fields/cases. Malformed input cannot mean pass.
        for data, cases in ((before, old_cases), (after, new_cases)):
            if "clamp_bounds" in data:
                bounds(data["clamp_bounds"], clamp=True)
            for case in cases.values():
                if "brightness_range" in case:
                    bounds(case["brightness_range"])
                if "wb_allowed" in case:
                    bucket(case["wb_allowed"])
        for section, case_id in sorted(old_cases.keys() - new_cases.keys()):
            findings.append(Finding("eval_cases_dropped", path, f"{section}/{case_id}"))

        def compare_fields(left, right, label, fields):
            for field in fields:
                if field not in left:
                    continue
                loosened = field not in right
                if not loosened and field == "wb_allowed":
                    loosened = bool(bucket(right[field]) - bucket(left[field]))
                elif not loosened:
                    low, high = bounds(left[field], clamp=field == "clamp_bounds")
                    new_low, new_high = bounds(right[field], clamp=field == "clamp_bounds")
                    loosened = new_low < low or new_high > high
                if loosened:
                    findings.append(Finding("eval_range_widened", path, f"{label}: {field} loosened or removed"))

        compare_fields(before, after, "global", ("clamp_bounds",))
        for key in sorted(old_cases.keys() & new_cases.keys()):
            compare_fields(old_cases[key], new_cases[key], "/".join(key), ("brightness_range", "wb_allowed"))
    except (InspectionError, ValueError, TypeError, OverflowError, RecursionError) as exc:
        findings.append(Finding("inspection_error", path, f"eval data: {type(exc).__name__}"))


def gate_lines(tree: Tree, path: str) -> Counter:
    return Counter(line.strip() for line in (tree.read(path) or b"").decode("utf-8").splitlines()
                   if line.strip() and not line.lstrip().startswith("#"))


def inspect(base: str, head: str) -> list[Finding]:
    base, head = commit(base), commit(head)
    changes = changed_paths(base, head)
    # Match three-dot PR diff semantics, avoiding findings for base-only additions.
    ancestor = git("merge-base", base, head).decode().strip()
    old, new = Tree(ancestor), Tree(head)
    findings = []
    for path in sorted(GUARDS & changes):
        findings.append(Finding("guard_changed", path, "guard added, edited, deleted or mode changed"))
    for path in sorted(old.entries.keys() - new.entries.keys()):
        p = PurePosixPath(path)
        if path.startswith("tests/") or (p.parent == PurePosixPath("backend") and p.match("test_*.py")):
            findings.append(Finding("deleted_test_file", path, "file removed or renamed"))
    old_tests, old_skips = python_inventory(old, findings)
    new_tests, new_skips = python_inventory(new, findings)
    for name in sorted(old_tests.keys() - new_tests.keys()):
        findings.append(Finding("removed_test", ", ".join(sorted(old_tests[name])), name))
    for (path, scope, marker), count in sorted((new_skips - old_skips).items()):
        findings.append(Finding("new_skip", path, f"{scope or '<module>'}: {count} new/changed skip or xfail: {marker}"))
    for path in sorted(EVALS):
        compare_evals(old, new, path, findings)
    for path in sorted(GATES):
        try:
            for line, count in sorted((gate_lines(old, path) - gate_lines(new, path)).items()):
                findings.append(Finding("gate_command_removed", path, f"{count} removed: {line}"))
        except (InspectionError, UnicodeError) as exc:
            findings.append(Finding("inspection_error", path, type(exc).__name__))
    return sorted(set(findings))


def cell(value: str) -> str:
    # Escape control characters, line breaks and table separators from untrusted data.
    return json.dumps(value, ensure_ascii=True)[1:-1].replace("|", r"\u007c")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args()
    try:
        findings = inspect(args.base, args.head)
    except (InspectionError, OSError, UnicodeError, ValueError, RecursionError,
            subprocess.TimeoutExpired) as exc:
        findings = [Finding("inspection_error", "git trees", str(exc))]
    print("finding | path | detail")
    print("--- | --- | ---")
    for finding in findings:
        print(" | ".join(cell(v) for v in (finding.kind, finding.path, finding.detail)))
    if not findings:
        print("no findings")
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
