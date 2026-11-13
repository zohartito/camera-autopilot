"""The ledger/receipts checker, on a fake tree (the real receipts live only on the owner's Mac)."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("check_ledger_receipts", REPO / "scripts" / "check_ledger_receipts.py")
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)

PHOTO = b"not really a jpeg"
PHOTO_SHA = hashlib.sha256(PHOTO).hexdigest()


FULL_TALLY = ("| Built, needs proof | 0 | fakes |\n| Not finished | 0 | open |\n"
              "| Ambitious | 0 | stretch |\n| Dropped | 0 | no |\n")


def _tree(tmp_path: Path, *, finished_count: int = 2, index_rows: str | None = None,
          photo_sha: str = PHOTO_SHA, write_photo: bool = True, other_tally: str = FULL_TALLY,
          total: int = 3) -> Path:
    (tmp_path / "docs").mkdir(parents=True)
    (tmp_path / "artifacts" / "run1").mkdir(parents=True)
    if write_photo:
        (tmp_path / "artifacts" / "run1" / "a.jpg").write_bytes(PHOTO)
    (tmp_path / "LICENSE").write_text("proprietary\n", encoding="utf-8")
    (tmp_path / "docs" / "PROJECT_LEDGER_2026-01-01.md").write_text(
        "| Status | Count | Meaning |\n|---|---|---|\n"
        f"| Finished | {finished_count} | proven |\n| Later | 1 | waits |\n{other_tally}\n**Total: {total}.**\n\n"
        "| Status | ID | Item | Note |\n|---|---|---|---|\n"
        "| Finished | CAP #1 | One tap | live |\n"
        "| Finished | `Rules` | Standing rules | decided |\n"
        "| Later | CAP #18 | Live view | board |\n",
        encoding="utf-8")
    if index_rows is None:
        index_rows = ("| CAP #1 | R1: `artifacts/run1/a.jpg` |\n"
                      "| Rules | decision in `docs/PROJECT_LEDGER_2026-01-01.md` and `LICENSE` |\n")
    (tmp_path / "docs" / "RECEIPTS_2026-01-02.md").write_text(
        "# Receipts\n\n## R1\n\n| File | SHA-256 |\n|---|---|\n"
        f"| `artifacts/run1/a.jpg` | {photo_sha} |\n\n"
        "## Receipt index\n\n| Ledger ID | Evidence |\n|---|---|\n" + index_rows,
        encoding="utf-8")
    return tmp_path


def test_a_consistent_tree_passes(tmp_path):
    root = _tree(tmp_path)
    assert checker.check(root, root) == []
    assert checker.main(["--docs-root", str(root), "--evidence-root", str(root)]) == 0


def test_a_finished_row_without_a_receipt_fails(tmp_path):
    root = _tree(tmp_path, index_rows="| CAP #1 | R1: `artifacts/run1/a.jpg` |\n")
    problems = checker.check(root, root)
    assert problems == ["Finished row 'Rules' has no entry in any receipts doc's Receipt index"]


def test_an_empty_evidence_cell_is_not_a_receipt(tmp_path):
    root = _tree(tmp_path, index_rows="| CAP #1 | R1: `artifacts/run1/a.jpg` |\n| Rules | — |\n")
    assert any("'Rules'" in problem for problem in checker.check(root, root))


def test_only_the_evidence_column_counts_as_a_receipt(tmp_path):
    root = _tree(tmp_path)
    doc = root / "docs" / "RECEIPTS_2026-01-02.md"
    text = doc.read_text(encoding="utf-8").split("## Receipt index")[0]
    doc.write_text(text + "## Receipt index\n\n| Ledger ID | Kind | Evidence | Body, date |\n|---|---|---|---|\n"
                   "| CAP #1 | live | R1: `artifacts/run1/a.jpg` | A7 III |\n| Rules | decision | — | n/a |\n",
                   encoding="utf-8")
    assert checker.check(root, root) == ["Finished row 'Rules' has no entry in any receipts doc's Receipt index"]


def _replace_index(root, table):
    doc = root / "docs" / "RECEIPTS_2026-01-02.md"
    doc.write_text(doc.read_text(encoding="utf-8").split("## Receipt index")[0] + "## Receipt index\n\n" + table,
                   encoding="utf-8")


def test_a_wide_index_without_an_evidence_header_fails(tmp_path):
    root = _tree(tmp_path)
    _replace_index(root, "| Ledger ID | Kind | Proof | Body |\n|---|---|---|---|\n"
                         "| CAP #1 | live | `artifacts/run1/a.jpg` | A7 III |\n| Rules | decision | `LICENSE` | n/a |\n")
    problems = checker.check(root, root)
    assert any("has no 'Evidence' header" in problem for problem in problems)
    assert any("'CAP #1' has no entry" in problem for problem in problems)


def test_a_bare_file_name_in_the_index_fails_but_a_root_file_does_not(tmp_path):
    root = _tree(tmp_path)
    _replace_index(root, "| Ledger ID | Evidence |\n|---|---|\n"
                         "| CAP #1 | `artifacts/run1/a.jpg`, `b.jpg` |\n| Rules | `LICENSE` |\n")
    assert checker.check(root, root) == [
        "RECEIPTS_2026-01-02.md: receipt index row 'CAP #1' cites 'b.jpg' without its repo path"]


def test_a_bare_directory_in_the_index_fails_but_a_quoted_request_does_not(tmp_path):
    root = _tree(tmp_path)
    _replace_index(root, "| Ledger ID | Evidence |\n|---|---|\n"
                         "| CAP #1 | `artifacts/run1/a.jpg`, `-02/`, `POST /shoot 200` |\n| Rules | `LICENSE` |\n")
    assert checker.check(root, root) == [
        "RECEIPTS_2026-01-02.md: receipt index row 'CAP #1' cites '-02/' without its repo path"]


def test_a_cited_artifact_that_is_missing_fails(tmp_path):
    root = _tree(tmp_path, write_photo=False)
    problems = checker.check(root, root)
    assert any("cites artifacts/run1/a.jpg, which does not exist" in problem for problem in problems)


def test_a_wrong_hash_fails(tmp_path):
    root = _tree(tmp_path, photo_sha="0" * 64)
    assert checker.check(root, root) == ["RECEIPTS_2026-01-02.md: SHA-256 mismatch for artifacts/run1/a.jpg"]


def test_a_truncated_uppercase_or_missing_hash_fails(tmp_path):
    for case, bad in enumerate((PHOTO_SHA[:40], PHOTO_SHA.upper(), "—")):
        root = _tree(tmp_path / f"case{case}", photo_sha=bad)
        problems = checker.check(root, root)
        assert len(problems) == 1 and "needs one cited file and one 64-digit lowercase hex digest" in problems[0]


def test_a_hash_row_naming_a_directory_fails(tmp_path):
    root = _tree(tmp_path)
    doc = root / "docs" / "RECEIPTS_2026-01-02.md"
    doc.write_text(doc.read_text(encoding="utf-8").replace("`artifacts/run1/a.jpg` | " + PHOTO_SHA,
                                                           "`artifacts/run1/` | " + PHOTO_SHA), encoding="utf-8")
    assert checker.check(root, root) == [
        "RECEIPTS_2026-01-02.md: a SHA-256 row must name a regular file, not artifacts/run1/"]


def test_a_misspelled_hash_header_fails(tmp_path):
    root = _tree(tmp_path)
    doc = root / "docs" / "RECEIPTS_2026-01-02.md"
    doc.write_text(doc.read_text(encoding="utf-8").replace("| File | SHA-256 |", "| File | SHA256 |"),
                   encoding="utf-8")
    problems = checker.check(root, root)
    assert any("a hash column must be headed exactly 'SHA-256'" in problem for problem in problems)
    assert any("a 64-digit digest outside a parsed SHA-256 table row" in problem for problem in problems)


def test_a_duplicated_ledger_id_fails(tmp_path):
    root = _tree(tmp_path)
    ledger = root / "docs" / "PROJECT_LEDGER_2026-01-01.md"
    ledger.write_text(ledger.read_text(encoding="utf-8").replace("| Finished | `Rules` |", "| Finished | CAP #1 |"),
                      encoding="utf-8")
    assert "PROJECT_LEDGER_2026-01-01.md: ID 'CAP #1' is used by more than one row" in checker.check(root, root)


def test_a_plain_or_linked_path_in_a_finished_row_is_checked(tmp_path):
    root = _tree(tmp_path)
    ledger = root / "docs" / "PROJECT_LEDGER_2026-01-01.md"
    ledger.write_text(ledger.read_text(encoding="utf-8").replace(
        "| Finished | CAP #1 | One tap | live |",
        "| Finished | CAP #1 | One tap | tools/backup.py and [notes](docs/notes.md) |"), encoding="utf-8")
    problems = checker.check(root, root)
    assert "PROJECT_LEDGER_2026-01-01.md: cites tools/backup.py, which does not exist" in problems
    assert "PROJECT_LEDGER_2026-01-01.md: cites docs/notes.md, which does not exist" in problems


def test_a_relative_markdown_link_to_a_missing_file_fails(tmp_path):
    root = _tree(tmp_path)
    (root / ".handoff").mkdir()
    (root / ".handoff" / "decisions.md").write_text("decided\n", encoding="utf-8")
    _replace_index(root, "| Ledger ID | Evidence |\n|---|---|\n"
                         "| CAP #1 | `artifacts/run1/a.jpg` |\n"
                         "| Rules | [decision](../.handoff/decisions.md), [gone](../.handoff/missing.md), "
                         "[web](https://example.com) |\n")
    assert checker.check(root, root) == ["RECEIPTS_2026-01-02.md: links to ../.handoff/missing.md, which does not exist"]


def test_a_shadows_column_is_not_a_hash_header(tmp_path):
    root = _tree(tmp_path)
    doc = root / "docs" / "RECEIPTS_2026-01-02.md"
    doc.write_text(doc.read_text(encoding="utf-8").replace(
        "## Receipt index", "## Metrics\n\n| Frame | Shadows | Shape |\n|---|---|---|\n| 1 | 0.2 | wide |\n\n## Receipt index"),
        encoding="utf-8")
    assert checker.check(root, root) == []


def test_a_tracked_root_file_can_be_cited_by_name_until_it_is_deleted(tmp_path):
    root = _tree(tmp_path)
    (root / "smoke_test.py").write_text("print('hi')\n", encoding="utf-8")
    _replace_index(root, "| Ledger ID | Evidence |\n|---|---|\n"
                         "| CAP #1 | `artifacts/run1/a.jpg`, `smoke_test.py` |\n| Rules | `LICENSE` |\n")
    assert checker.check(root, root) == []
    (root / "smoke_test.py").unlink()
    assert checker.check(root, root) == [
        "RECEIPTS_2026-01-02.md: receipt index row 'CAP #1' cites 'smoke_test.py' without its repo path"]


def test_the_declared_total_must_match_the_rows(tmp_path):
    root = _tree(tmp_path, total=4)
    assert checker.check(root, root) == [
        "PROJECT_LEDGER_2026-01-01.md: the declared total is 4 but 3 rows are listed"]


def test_an_emphasised_sha_header_still_verifies_hashes(tmp_path):
    root = _tree(tmp_path, photo_sha="0" * 64)
    doc = root / "docs" / "RECEIPTS_2026-01-02.md"
    doc.write_text(doc.read_text(encoding="utf-8").replace("| File | SHA-256 |", "| **File** | **SHA-256** |"),
                   encoding="utf-8")
    assert checker.check(root, root) == ["RECEIPTS_2026-01-02.md: SHA-256 mismatch for artifacts/run1/a.jpg"]


def test_a_finished_row_citing_a_deleted_root_file_fails(tmp_path):
    root = _tree(tmp_path)
    (root / "smoke_test.py").write_text("print('hi')\n", encoding="utf-8")
    ledger = root / "docs" / "PROJECT_LEDGER_2026-01-01.md"
    ledger.write_text(ledger.read_text(encoding="utf-8").replace(
        "| Finished | CAP #1 | One tap | live |", "| Finished | CAP #1 | One tap | `smoke_test.py` |"), encoding="utf-8")
    assert checker.check(root, root) == []
    (root / "smoke_test.py").unlink()
    assert checker.check(root, root) == [
        "PROJECT_LEDGER_2026-01-01.md: Finished row 'CAP #1' cites 'smoke_test.py' without its repo path"]


def test_placeholders_repeated_across_receipts_docs_are_still_empty(tmp_path):
    root = _tree(tmp_path)
    _replace_index(root, "| Ledger ID | Evidence |\n|---|---|\n| CAP #1 | — |\n| Rules | `LICENSE` |\n")
    (root / "docs" / "RECEIPTS_2026-01-03.md").write_text(
        "# More\n\n## Receipt index\n\n| Ledger ID | Evidence |\n|---|---|\n| CAP #1 | — |\n", encoding="utf-8")
    assert "Finished row 'CAP #1' has no entry in any receipts doc's Receipt index" in checker.check(root, root)


def test_a_bare_file_with_a_long_extension_fails(tmp_path):
    root = _tree(tmp_path)
    _replace_index(root, "| Ledger ID | Evidence |\n|---|---|\n"
                         "| CAP #1 | `artifacts/run1/a.jpg`, `proof.sqlite3` |\n| Rules | `LICENSE` |\n")
    assert checker.check(root, root) == [
        "RECEIPTS_2026-01-02.md: receipt index row 'CAP #1' cites 'proof.sqlite3' without its repo path"]


def test_an_unquoted_bare_file_fails_but_a_quoted_request_line_does_not(tmp_path):
    root = _tree(tmp_path)
    _replace_index(root, "| Ledger ID | Evidence |\n|---|---|\n"
                         "| CAP #1 | `artifacts/run1/a.jpg` (see proof.sqlite3), `GET /x?image=shot_1.jpg 200` |\n"
                         "| Rules | `LICENSE` |\n")
    assert checker.check(root, root) == [
        "RECEIPTS_2026-01-02.md: receipt index row 'CAP #1' cites 'proof.sqlite3' without its repo path"]


def test_bare_files_of_any_extension_fail_but_timestamps_and_urls_do_not(tmp_path):
    root = _tree(tmp_path)
    _replace_index(root, "| Ledger ID | Evidence |\n|---|---|\n"
                         "| CAP #1 | `artifacts/run1/a.jpg`, proof.heic, log.jsonl, 2026-09-23T01:09:06.836Z, "
                         "https://example.com/x.html, e.g. 0.24 |\n| Rules | `LICENSE` |\n")
    assert checker.check(root, root) == [
        "RECEIPTS_2026-01-02.md: receipt index row 'CAP #1' cites 'proof.heic' without its repo path",
        "RECEIPTS_2026-01-02.md: receipt index row 'CAP #1' cites 'log.jsonl' without its repo path"]


def test_a_digest_outside_a_parsed_sha_table_fails(tmp_path):
    root = _tree(tmp_path, photo_sha="0" * 64)
    doc = root / "docs" / "RECEIPTS_2026-01-02.md"
    doc.write_text(doc.read_text(encoding="utf-8").replace("| File | SHA-256 |\n|---|---|\n| `artifacts/run1/a.jpg` | ",
                                                           "File | SHA-256\n---|---\n`artifacts/run1/a.jpg` | "),
                   encoding="utf-8")
    problems = checker.check(root, root)
    assert len(problems) == 1 and "a 64-digit digest outside a parsed SHA-256 table row" in problems[0]


def test_a_one_character_bare_file_fails_but_e_g_does_not(tmp_path):
    root = _tree(tmp_path)
    _replace_index(root, "| Ledger ID | Evidence |\n|---|---|\n"
                         "| CAP #1 | `artifacts/run1/a.jpg`, x.jpg, e.g. this |\n| Rules | `LICENSE` |\n")
    assert checker.check(root, root) == [
        "RECEIPTS_2026-01-02.md: receipt index row 'CAP #1' cites 'x.jpg' without its repo path"]


def test_a_link_outside_the_repository_fails_even_if_the_file_exists(tmp_path):
    root = _tree(tmp_path / "repo")
    (tmp_path / "outside.jpg").write_bytes(b"x")
    _replace_index(root, "| Ledger ID | Evidence |\n|---|---|\n"
                         "| CAP #1 | [proof](../../outside.jpg) |\n| Rules | `LICENSE` |\n")
    assert checker.check(root, root) == [
        "RECEIPTS_2026-01-02.md: links to ../../outside.jpg, which is outside the repository"]


def test_a_stale_tally_fails(tmp_path):
    root = _tree(tmp_path, finished_count=3)
    assert checker.check(root, root) == [
        "PROJECT_LEDGER_2026-01-01.md: the tally says 3 Finished but 2 rows are listed"]


def test_a_missing_tally_row_fails(tmp_path):
    root = _tree(tmp_path, other_tally=FULL_TALLY.replace("| Dropped | 0 | no |\n", ""))
    assert checker.check(root, root) == [
        "PROJECT_LEDGER_2026-01-01.md: the tally needs exactly one 'Dropped' row, found 0"]


def test_a_duplicated_tally_row_fails(tmp_path):
    root = _tree(tmp_path, other_tally=FULL_TALLY + "| Dropped | 0 | again |\n")
    assert checker.check(root, root) == [
        "PROJECT_LEDGER_2026-01-01.md: the tally needs exactly one 'Dropped' row, found 2"]


def test_a_cited_root_level_file_that_is_missing_fails(tmp_path):
    root = _tree(tmp_path)
    (root / "LICENSE").unlink()
    assert checker.check(root, root) == ["RECEIPTS_2026-01-02.md: cites LICENSE, which does not exist"]


def test_missing_artifacts_dir_is_reported_not_silently_passed(tmp_path):
    root = _tree(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    problems = checker.check(root, elsewhere)
    assert any("does not exist; run this on the Mac" in problem for problem in problems)


def test_evidence_root_is_separate_from_the_docs_checkout(tmp_path):
    evidence = _tree(tmp_path / "main")
    docs = tmp_path / "worktree"
    (docs / "docs").mkdir(parents=True)
    for doc in (evidence / "docs").iterdir():
        (docs / "docs" / doc.name).write_text(doc.read_text(encoding="utf-8"), encoding="utf-8")
    (docs / "LICENSE").write_text("proprietary\n", encoding="utf-8")
    assert checker.check(docs, evidence) == []
