#!/usr/bin/env python3
"""Check that the project ledger never claims more than the receipts show.

Local check, run on the owner's Mac: the receipts cite files under the gitignored
``artifacts/`` directory, which exists only there, so hosted CI cannot run it.

It fails when:
- a ledger row marked Finished has no entry in any receipts doc's "Receipt index" (only the
  Evidence column counts, every index table wider than two columns must have an Evidence header,
  and every file in it is cited by repo path, not a bare name);
- a receipts doc or a Finished ledger row cites a repo path (``artifacts/…``,
  ``docs/…``, ``backend/…`` and so on) that does not exist;
- a receipts table row pairs a file with a SHA-256 that does not match the file;
- the ledger's status tally disagrees with the rows it lists.

Usage:
    python3 scripts/check_ledger_receipts.py
    python3 scripts/check_ledger_receipts.py --evidence-root ~/projects/camera-autopilot

``--evidence-root`` (or ``CAMERA_AUTOPILOT_EVIDENCE_ROOT``) is where ``artifacts/`` lives;
it defaults to this checkout. Point it at the main checkout when running from a worktree.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
STATUSES = ("Finished", "Built, needs proof", "Not finished", "Later", "Ambitious", "Dropped")
PATH_PREFIXES = ("artifacts/", "backend/", "deploy/", "docs/", "enclosure/", "mobile/", "scripts/",
                 "sonyremote/", "tests/", "tools/", "data/", "third_party/", ".handoff/", ".github/",
                 ".githooks/", ".claude/")
# Root-level files a receipt may cite by bare name. Listed explicitly, so a deleted file is still
# recognised as a citation (and reported missing) rather than silently ignored.
ROOT_FILES = ("AGENTS.md", "CLAUDE.md", "README.md", "LICENSE", "THIRD_PARTY_NOTICES.md", "run.sh",
              "conftest.py", "server.py", "requirements.txt", "requirements-test.txt",
              "requirements-test.lock")
PATH_RE = re.compile(r"`((?:%s)[^`\s]*|%s)`" % ("|".join(re.escape(p) for p in PATH_PREFIXES),
                                                "|".join(re.escape(f) for f in ROOT_FILES)))
SHA_RE = re.compile(r"[0-9a-f]{64}")
# Whole-header names of a hash column (so "Shadows" or "Shape" is not one).
HASH_HEADER_RE = re.compile(r"(sha[-_ ]?(1|224|256|384|512)?|sha256sum|hash(es)?|digest|checksum)")
# A Markdown link target: [text](target). Relative targets resolve against the linking document.
MD_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
# The same repo paths written as plain text or as a Markdown link target, not in backticks.
PLAIN_PATH_RE = re.compile(r"(?<![`\w/.\-])((?:%s)[\w./\-]*[\w/])" % "|".join(re.escape(p) for p in PATH_PREFIXES))
# An unquoted file-like token (name.ext, the extension holding a letter; a one-character stem needs a
# 2+ character extension, so "e.g." is not a file),
# scanned after removing backticked spans and URLs: e.g. proof.sqlite3, shot.heic, log.jsonl.
PLAIN_FILE_RE = re.compile(r"(?<![`\w/.:\-])([\w\-]{2,}(?:\.[\w\-]+)*\.[A-Za-z0-9]*[A-Za-z][A-Za-z0-9]*|[\w\-]\.(?=[A-Za-z0-9]{2,})[A-Za-z0-9]*[A-Za-z][A-Za-z0-9]*)(?![\w`/])")
# A backticked file name with no directory: in a receipt index every file is cited by repo path.
BARE_FILE_RE = re.compile(r"`([\w.\-]+\.[A-Za-z0-9]*[A-Za-z][A-Za-z0-9]*)`")
EMPTY_EVIDENCE = {"", "-", "—", "none", "n/a"}


def table_cells(line: str) -> list[str] | None:
    """Split a markdown table row into cells, or return None for non-rows and separators."""
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return None
    cells = [cell.strip() for cell in stripped[1:-1].split("|")]
    if all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells if cell):
        return None
    return cells


def normalise_id(raw: str) -> str:
    return raw.replace("`", "").replace("**", "").strip()


def parse_ledger(text: str) -> tuple[list[tuple[str, str]], dict[str, int], dict[str, list[int]], list[str]]:
    """Return (finished rows as (id, full line), row counts by status, tally entries per status,
    IDs used by more than one row)."""
    finished: list[tuple[str, str]] = []
    counts = {status: 0 for status in STATUSES}
    tally: dict[str, list[int]] = {status: [] for status in STATUSES}
    seen: set[str] = set()
    duplicates: list[str] = []
    for line in text.splitlines():
        cells = table_cells(line)
        if not cells or len(cells) < 2:
            continue
        status = cells[0].replace("**", "")
        if status not in STATUSES:
            continue
        if re.fullmatch(r"\d+", cells[1]) and len(cells) <= 3:
            tally[status].append(int(cells[1]))
            continue
        counts[status] += 1
        ledger_id = normalise_id(cells[1])
        if ledger_id in seen:
            duplicates.append(ledger_id)
        seen.add(ledger_id)
        if status == "Finished":
            finished.append((ledger_id, line))
    return finished, counts, tally, duplicates


def parse_receipt_index(text: str, root_names: frozenset[str] = frozenset(ROOT_FILES)) -> tuple[dict[str, str], list[str]]:
    """Map ledger ID -> evidence text from every '## Receipt index' section of a receipts doc,
    plus the problems found in those tables (a missing Evidence header, a bare file name)."""
    index: dict[str, str] = {}
    problems: list[str] = []
    in_index = False
    evidence_column: int | None = None
    for line in text.splitlines():
        if line.startswith("#"):
            in_index = bool(re.match(r"#+\s+Receipt index\b", line))
            evidence_column = None
            continue
        if not in_index:
            continue
        cells = table_cells(line)
        if not cells or len(cells) < 2:
            continue
        if evidence_column is None:  # the first row of each index table is its header
            headers = [cell.lower() for cell in cells]
            if "evidence" in headers:
                evidence_column = headers.index("evidence")
            elif len(cells) == 2:
                evidence_column = 1
            else:
                problems.append(f"a receipt index table with {len(cells)} columns has no 'Evidence' header: "
                                f"{line.strip()[:120]}")
                evidence_column = -1  # skip this table's rows; its Finished rows then count as missing
            continue
        if evidence_column < 0:
            continue
        ledger_id = normalise_id(cells[0])
        # Only the Evidence cell counts: a filled Kind or Body column is not a receipt.
        evidence = cells[evidence_column].strip() if evidence_column < len(cells) else ""
        problems.extend(f"receipt index row {ledger_id!r} cites {name!r} without its repo path"
                        for name in BARE_FILE_RE.findall(evidence) + PLAIN_FILE_RE.findall(re.sub(r"`[^`]*`|https?://\S+", " ", evidence))
                        if name not in root_names)
        # A slash-bearing token without spaces is a path (a quoted request line has spaces).
        problems.extend(f"receipt index row {ledger_id!r} cites {token!r} without its repo path"
                        for token in re.findall(r"`([^`\s]*/[^`\s]*)`", evidence)
                        if not PATH_RE.fullmatch(f"`{token}`"))
        if ledger_id and evidence.lower() not in EMPTY_EVIDENCE:  # a placeholder cell is no receipt
            index[ledger_id] = (index.get(ledger_id, "") + " " + evidence).strip()
    return index, problems


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve(rel: str, docs_root: Path, evidence_root: Path) -> Path:
    base = evidence_root if rel.startswith(("artifacts/", "backend/shots/")) else docs_root
    return base / rel.rstrip(".,;:")


def broken_links(doc: Path, text: str, docs_root: Path, evidence_root: Path) -> list[str]:
    """Relative Markdown link targets in `text` that do not exist, resolved against the linking doc."""
    problems = []
    for target in MD_LINK_RE.findall(text):
        target = target.split("#", 1)[0]
        if not target or re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I):  # empty anchor, http:, mailto:
            continue
        rel = Path(os.path.relpath((doc.parent / target).resolve(), docs_root.resolve())).as_posix()
        if rel == ".." or rel.startswith("../"):
            problems.append(f"{doc.name}: links to {target}, which is outside the repository")
            continue
        if not resolve(rel, docs_root, evidence_root).exists():
            problems.append(f"{doc.name}: links to {target}, which does not exist")
    return problems


def check(docs_root: Path, evidence_root: Path, ledger: Path | None = None) -> list[str]:
    problems: list[str] = []
    if ledger is None:
        ledgers = sorted((docs_root / "docs").glob("PROJECT_LEDGER_*.md"))
        if not ledgers:
            return ["no docs/PROJECT_LEDGER_*.md found"]
        ledger = ledgers[-1]
    receipts_docs = sorted((docs_root / "docs").glob("RECEIPTS_*.md"))
    if not receipts_docs:
        problems.append("no docs/RECEIPTS_*.md found")
    if not (evidence_root / "artifacts").is_dir():
        problems.append(f"{evidence_root / 'artifacts'} does not exist; run this on the Mac that holds "
                        "the receipts, or pass --evidence-root")

    ledger_text = ledger.read_text(encoding="utf-8")
    finished, counts, tally, duplicates = parse_ledger(ledger_text)
    totals = [int(n) for n in re.findall(r"\*\*Total: (\d+)\.\*\*", ledger_text)]
    if len(totals) != 1:
        problems.append(f"{ledger.name}: needs exactly one **Total: N.** declaration, found {len(totals)}")
    elif totals[0] != sum(counts.values()):
        problems.append(f"{ledger.name}: the declared total is {totals[0]} but {sum(counts.values())} rows are listed")
    problems.extend(f"{ledger.name}: ID {ledger_id!r} is used by more than one row" for ledger_id in duplicates)
    for status, claimed in tally.items():
        if len(claimed) != 1:
            problems.append(f"{ledger.name}: the tally needs exactly one {status!r} row, found {len(claimed)}")
        elif counts[status] != claimed[0]:
            problems.append(f"{ledger.name}: the tally says {claimed[0]} {status} but {counts[status]} rows are listed")

    index: dict[str, str] = {}
    cited: list[tuple[str, str]] = []
    # A deleted file on the explicit list is still recognised (and reported missing); any other
    # root file that disappears is then a bare name without a repo path, which also fails.
    root_names = frozenset(ROOT_FILES) | {entry.name for entry in docs_root.iterdir() if entry.is_file()}
    for doc in receipts_docs:
        text = doc.read_text(encoding="utf-8")
        doc_index, index_problems = parse_receipt_index(text, root_names)
        problems.extend(f"{doc.name}: {problem}" for problem in index_problems)
        problems.extend(broken_links(doc, text, docs_root, evidence_root))
        for ledger_id, evidence in doc_index.items():
            index[ledger_id] = (index.get(ledger_id, "") + " " + evidence).strip()
        sha_column: int | None = None
        in_table = False
        for line in text.splitlines():
            paths = PATH_RE.findall(line)
            cited.extend((doc.name, name) for name in BARE_FILE_RE.findall(line)
                         if name in root_names and name not in ROOT_FILES)
            cited.extend((doc.name, path) for path in set(paths) | set(PLAIN_PATH_RE.findall(line)))
            is_row = line.strip().startswith("|")
            cells = table_cells(line)
            if is_row and not in_table:  # the first row of a table is its header
                # Markdown emphasis or code formatting must not hide a hash header (**SHA-256**).
                headers = [re.sub(r"[*_`]", "", cell).strip().lower() for cell in cells or []]
                sha_column = headers.index("sha-256") if "sha-256" in headers else None
                if sha_column is None and any(HASH_HEADER_RE.fullmatch(cell.strip()) for cell in headers):
                    problems.append(f"{doc.name}: a hash column must be headed exactly 'SHA-256': "
                                    f"{line.strip()[:120]}")
                in_table = True
                continue
            in_table = is_row
            if not cells or sha_column is None:
                if SHA_RE.search(line.lower()):
                    problems.append(f"{doc.name}: a 64-digit digest outside a parsed SHA-256 table row: "
                                    f"{line.strip()[:120]}")
                continue
            # A table with a SHA-256 column: every row names one file and one well-formed digest.
            digest = cells[sha_column].strip() if sha_column < len(cells) else ""
            if len(paths) != 1 or not SHA_RE.fullmatch(digest):
                problems.append(f"{doc.name}: a SHA-256 row needs one cited file and one 64-digit lowercase "
                                f"hex digest: {line.strip()[:120]}")
                continue
            target = resolve(paths[0], docs_root, evidence_root)
            if target.exists() and not target.is_file():
                problems.append(f"{doc.name}: a SHA-256 row must name a regular file, not {paths[0]}")
            elif target.is_file() and sha256_of(target) != digest:
                problems.append(f"{doc.name}: SHA-256 mismatch for {paths[0]}")

    for ledger_id, line in finished:
        evidence = index.get(ledger_id, "")
        if evidence.lower() in EMPTY_EVIDENCE:
            problems.append(f"Finished row {ledger_id!r} has no entry in any receipts doc's Receipt index")
        cited.extend((ledger.name, path) for path in set(PATH_RE.findall(line)) | set(PLAIN_PATH_RE.findall(line)))
        problems.extend(broken_links(ledger, line, docs_root, evidence_root))
        for name in BARE_FILE_RE.findall(line):  # a backticked bare file name must be a root file
            if name in root_names:
                cited.append((ledger.name, name))
            else:
                problems.append(f"{ledger.name}: Finished row {ledger_id!r} cites {name!r} without its repo path")

    for source, rel in cited:
        if not resolve(rel, docs_root, evidence_root).exists():
            problems.append(f"{source}: cites {rel}, which does not exist")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--docs-root", type=Path, default=REPO, help="checkout whose docs/ is checked")
    parser.add_argument("--evidence-root", type=Path,
                        default=Path(os.environ.get("CAMERA_AUTOPILOT_EVIDENCE_ROOT", REPO)),
                        help="directory holding artifacts/ and backend/shots/ (default: this checkout)")
    parser.add_argument("--ledger", type=Path, help="ledger file (default: newest docs/PROJECT_LEDGER_*.md)")
    args = parser.parse_args(argv)
    problems = check(args.docs_root.expanduser(), args.evidence_root.expanduser(), args.ledger)
    if problems:
        for problem in problems:
            print(f"FAIL {problem}")
        print(f"{len(problems)} problem(s)")
        return 1
    print("OK: every Finished row has a receipt, every cited path exists, every listed hash matches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
