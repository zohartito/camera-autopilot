"""Camera-free tests for Sony Camera Remote Command package review mapping."""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tools"))

import review_camera_remote_command_package


def _package_zip(root: Path, members: dict[str, str | bytes]) -> Path:
    """Create the regular ZIP fixture required by the hardened reviewer."""
    package = root / "Sony_Camera_Remote_Command.zip"
    with zipfile.ZipFile(package, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return package


def test_command_package_review_maps_registry_rows_from_local_package_text() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        package = _package_zip(root, {
            "README.txt": "Sony Camera Remote Command compatibility matrix: ILCE-7M2 supports PTP-IP over Wi-Fi and Wireless LAN.\n"
            "Live View command reference applies to supported Alpha bodies.\n",
            "commands.csv": "Command,Models,Notes\n"
            "Focus Area,ILCE-7M2,Set or read focus area / AFArea focus position\n"
            "Movie File Format,ILCE-7M2,File Format setting for movies\n"
            "Date/Time,ILCE-7M2,Clock sync command for Date Time Setup\n",
        })
        report, report_path = review_camera_remote_command_package.build_report(
            package,
            out_dir=root / "out",
            limit_per_file=20,
        )
        filtered, _ = review_camera_remote_command_package.build_report(
            package,
            out_dir=root / "out-filtered",
            limit_per_file=20,
            setting_ids=["camera.focus_area"],
        )
        repeated, _ = review_camera_remote_command_package.build_report(
            package,
            out_dir=root / "out-repeated",
            limit_per_file=20,
        )

    assert report_path.name.endswith("-command-package-review.json")
    assert report["package_hash_kind"] == "file_sha256"
    assert report["package_sha256"] == repeated["package_sha256"]
    assert len(report["package_sha256"]) == 64
    assert report["package_family"]["family"] == "camera_remote_command"
    assert report["package_family"]["ok_for_command_review"] is True
    assert report["policy"]["redistributes_package"] is False
    assert any("cameraremotecommand" in item["url"] for item in report["official_sources"])
    summary = report["registry_mapping_summary"]
    assert summary["camera_remote_command_rows"] >= 30
    assert summary["mapped_candidate_rows"] >= 3
    assert summary["model_scoped_candidate_rows"] >= 3
    assert summary["package_mentions_ilce_7m2"] is True

    mapped = {row["setting_id"]: row for row in report["registry_mapping"]}
    for setting_id in ("camera.focus_area", "camera.file_format", "setup.date_time_setup"):
        assert mapped[setting_id]["evidence_count"] >= 1, mapped[setting_id]
        assert mapped[setting_id]["model_scoped_evidence_count"] >= 1
        assert mapped[setting_id]["verdict"] == "candidate_model_scoped_evidence"
        assert mapped[setting_id]["manual_confirmation_required"] is True
    assert report["term_counts"]["ILCE-7M2"] >= 2
    assert report["compatibility_rows"]
    assert filtered["registry_mapping_summary"]["camera_remote_command_rows"] == 1
    assert filtered["registry_mapping_summary"]["filtered_setting_ids"] == ["camera.focus_area"]
    assert filtered["registry_mapping"][0]["setting_id"] == "camera.focus_area"
    assert filtered["registry_mapping"][0]["verdict"] == "candidate_model_scoped_evidence"
    print("  Camera Remote Command registry mapping: ok")


def test_command_package_review_rejects_sdk_archive_even_with_camera_terms() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        package = root / "CRSDK_v2.02.00.zip"
        with zipfile.ZipFile(package, "w") as zf:
            zf.writestr(
                "README.txt",
                "Camera Remote SDK compatibility notes mention ILCE-7M2.\n"
                "A paragraph also compares Sony Camera Remote Command, but this archive is still CRSDK.\n"
                "Focus Area and Live View appear in SDK docs but this is not Command evidence.\n",
            )
        out_dir = root / "out"
        try:
            review_camera_remote_command_package.build_report(
                package,
                out_dir=out_dir,
                limit_per_file=20,
            )
        except ValueError as exc:
            assert "Camera Remote SDK" in str(exc)
        else:
            raise AssertionError("SDK archive should not produce a Command package review")

    assert not list(out_dir.glob("*-command-package-review.json"))
    print("  Camera Remote Command reviewer SDK archive guard: ok")


def test_command_package_review_allows_command_package_that_mentions_sdk() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        package = _package_zip(root, {
            "README.txt": "Sony Camera Remote Command package for ILCE-7M2.\n"
            "This document compares Camera Remote SDK and Camera Remote Command distribution paths.\n"
            "Focus Area,ILCE-7M2,Set focus area over PTP command.\n",
        })
        report, _ = review_camera_remote_command_package.build_report(
            package,
            out_dir=root / "out",
            limit_per_file=20,
        )

    assert report["package_family"]["family"] == "camera_remote_command"
    assert report["package_family"]["ok_for_command_review"] is True
    assert report["package_family"]["sdk_marker_files"] == ["README.txt"]
    assert report["registry_mapping_summary"]["mapped_candidate_rows"] >= 1
    print("  Camera Remote Command reviewer command-over-sdk precedence: ok")


def test_command_package_review_does_not_scope_commands_from_global_model_mention() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        package = _package_zip(root, {
            "README.txt": "Sony Camera Remote Command package.\n"
            "Supported device: ILCE-7M2.\n"
            "Focus Area command is documented in a generic command section.\n",
        })
        report, _ = review_camera_remote_command_package.build_report(
            package,
            out_dir=root / "out",
            limit_per_file=20,
            setting_ids=["camera.focus_area"],
        )

    mapping = report["registry_mapping"][0]
    assert mapping["evidence_count"] >= 1
    assert mapping["model_scoped_evidence_count"] == 0
    assert mapping["verdict"] == "candidate_command_evidence_model_unscoped"
    assert report["registry_mapping_summary"]["model_scoped_candidate_rows"] == 0
    assert report["registry_mapping_summary"]["unscoped_candidate_rows"] == 1
    print("  Camera Remote Command reviewer model-scope guard: ok")


def test_command_package_review_rejects_unknown_package_identity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        package = root / "download.zip"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr("README.txt", "Generic camera tooling with Focus Area text.\n")
        try:
            review_camera_remote_command_package.build_report(
                package,
                out_dir=root / "out",
                limit_per_file=20,
            )
        except ValueError as exc:
            assert "identity marker" in str(exc)
        else:
            raise AssertionError("unknown archive should not produce a Command package review")

    assert not list((root / "out").glob("*-command-package-review.json"))
    print("  Camera Remote Command reviewer unknown-package guard: ok")


def test_command_package_review_maps_xlsx_compatibility_row() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        workbook = BytesIO()
        with zipfile.ZipFile(workbook, "w") as archive:
            archive.writestr(
                "xl/worksheets/sheet1.xml",
                """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1">
      <c r="A1" t="inlineStr"><is><t>Focus Area</t></is></c>
      <c r="B1" t="inlineStr"><is><t>ILCE-7M2</t></is></c>
      <c r="C1" t="inlineStr"><is><t>Supported</t></is></c>
    </row>
  </sheetData>
</worksheet>
""",
            )
        package = _package_zip(root, {
            "README.txt": "Sony Camera Remote Command package.\n",
            "CommandReference.xlsx": workbook.getvalue(),
        })
        report, _ = review_camera_remote_command_package.build_report(
            package,
            out_dir=root / "out",
            limit_per_file=20,
            setting_ids=["camera.focus_area"],
        )

    workbook_file = next(item for item in report["files"] if item["path"] == "CommandReference.xlsx")
    mapping = report["registry_mapping"][0]
    assert workbook_file["text_extraction"] == "xlsx_xml"
    assert workbook_file["text_extraction_error"] is None
    assert mapping["model_scoped_evidence_count"] == 1
    assert mapping["verdict"] == "candidate_model_scoped_evidence"
    assert mapping["manual_confirmation_required"] is True
    print("  Camera Remote Command reviewer XLSX mapping: ok")


def test_command_package_review_rejects_suspicious_zip_compression_ratio() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        package = root / "Sony_Camera_Remote_Command.zip"
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "README.txt",
                "Sony Camera Remote Command ILCE-7M2\n" + ("0" * 1024 * 1024),
            )
        try:
            review_camera_remote_command_package.build_report(
                package,
                out_dir=root / "out",
            )
        except ValueError as exc:
            assert "suspicious compression ratio" in str(exc)
        else:
            raise AssertionError("high-ratio ZIP member should be rejected")

    print("  Camera Remote Command reviewer compression-ratio guard: ok")


def test_command_package_review_rejects_package_symlinks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = _package_zip(root, {"README.txt": "Sony Camera Remote Command ILCE-7M2"})
        package = root / "package-link.zip"
        package.symlink_to(target)
        try:
            review_camera_remote_command_package.build_report(
                package,
                out_dir=root / "out",
            )
        except ValueError as exc:
            assert "regular ZIP" in str(exc)
        else:
            raise AssertionError("package symlink should be rejected")

    print("  Camera Remote Command reviewer package-symlink guard: ok")


def test_command_package_review_rejects_fifo_before_zip_open() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        package = Path(tmp) / "Sony_Camera_Remote_Command.zip"
        os.mkfifo(package)
        try:
            review_camera_remote_command_package.build_report(package, out_dir=Path(tmp) / "out")
        except ValueError as exc:
            assert "regular ZIP" in str(exc)
        else:
            raise AssertionError("FIFO should be rejected before ZIP inspection")
    print("  Camera Remote Command reviewer FIFO guard: ok")


def test_command_package_review_consumes_one_descriptor_snapshot_not_a_reopened_path() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        package = _package_zip(root, {
            "README.txt": "Sony Camera Remote Command package for ILCE-7M2. Focus Area\n",
        })
        replacement_root = root / "replacement"
        replacement_root.mkdir()
        replacement = _package_zip(replacement_root, {
            "README.txt": "wrong archive",
        })
        original_open = review_camera_remote_command_package.os.open

        def swap_after_open(path, flags):
            fd = original_open(path, flags)
            if Path(path) == package:
                package.unlink()
                package.symlink_to(replacement)
            return fd

        review_camera_remote_command_package.os.open = swap_after_open
        try:
            report, _ = review_camera_remote_command_package.build_report(package, out_dir=root / "out")
        finally:
            review_camera_remote_command_package.os.open = original_open

    assert report["package_family"]["ok_for_command_review"] is True
    assert report["package_sha256"]
    print("  Camera Remote Command reviewer descriptor snapshot guard: ok")


def test_command_package_review_defuses_office_xml_entities() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        workbook = BytesIO()
        with zipfile.ZipFile(workbook, "w") as archive:
            archive.writestr(
                "xl/worksheets/sheet1.xml",
                """<?xml version="1.0"?>
<!DOCTYPE worksheet [<!ENTITY camera "ILCE-7M2">]>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData><row><c t="inlineStr"><is><t>&camera;</t></is></c></row></sheetData>
</worksheet>
""",
            )
        package = _package_zip(root, {
            "README.txt": "Sony Camera Remote Command package for ILCE-7M2.\n",
            "CommandReference.xlsx": workbook.getvalue(),
        })
        report, _ = review_camera_remote_command_package.build_report(
            package,
            out_dir=root / "out",
            setting_ids=["camera.focus_area"],
        )

    workbook_file = next(item for item in report["files"] if item["path"] == "CommandReference.xlsx")
    assert workbook_file["text_extraction"] == "extraction_error"
    assert "DTD/entity declarations are not allowed" in workbook_file["text_extraction_error"]
    assert report["registry_mapping"][0]["evidence_count"] == 0
    print("  Camera Remote Command reviewer defused Office XML guard: ok")


def _office_zip(members: dict[str, str]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, value in members.items():
            archive.writestr(name, value)
    return buffer.getvalue()


def test_command_package_review_bounds_nested_office_counts_expansion_and_text() -> None:
    worksheet = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData><row><c t="inlineStr"><is><t>ILCE-7M2</t></is></c></row></sheetData></worksheet>'
    )
    two_sheets = _office_zip({"xl/worksheets/sheet1.xml": worksheet, "xl/worksheets/sheet2.xml": worksheet})
    budget = review_camera_remote_command_package.OfficeExtractionBudget(
        max_members=8, max_sheets=1, max_headers_footers=8, max_expanded_bytes=4096, max_text_bytes=4096
    )
    text, method, error = review_camera_remote_command_package.extract_text(
        "two-sheets.xlsx", two_sheets, office_budget=budget
    )
    assert text is None and method == "extraction_error" and "sheet-count" in error

    docx = _office_zip({
        "word/document.xml": "<document><p>body</p></document>",
        "word/header1.xml": "<header><p>one</p></header>",
        "word/header2.xml": "<header><p>two</p></header>",
    })
    budget = review_camera_remote_command_package.OfficeExtractionBudget(
        max_members=8, max_sheets=8, max_headers_footers=1, max_expanded_bytes=4096, max_text_bytes=4096
    )
    text, method, error = review_camera_remote_command_package.extract_text("headers.docx", docx, office_budget=budget)
    assert text is None and method == "extraction_error" and "header/footer-count" in error

    expanded = _office_zip({"xl/worksheets/sheet1.xml": worksheet, "xl/styles.xml": worksheet})
    budget = review_camera_remote_command_package.OfficeExtractionBudget(
        max_members=8, max_sheets=8, max_headers_footers=8, max_expanded_bytes=len(worksheet) + 1,
        max_text_bytes=4096,
    )
    text, method, error = review_camera_remote_command_package.extract_text("expanded.xlsx", expanded, office_budget=budget)
    assert text is None and method == "extraction_error" and "expanded-byte" in error

    first = _office_zip({"word/document.xml": "<document><p>abc</p></document>"})
    second = _office_zip({"word/document.xml": "<document><p>def</p></document>"})
    budget = review_camera_remote_command_package.OfficeExtractionBudget(
        max_members=8, max_sheets=8, max_headers_footers=8, max_expanded_bytes=4096, max_text_bytes=5
    )
    assert review_camera_remote_command_package.extract_text("first.docx", first, office_budget=budget)[0] == "abc"
    text, method, error = review_camera_remote_command_package.extract_text("second.docx", second, office_budget=budget)
    assert text is None and method == "extraction_error" and "text budget" in error
    print("  Camera Remote Command reviewer nested Office budgets: ok")


def test_command_package_review_preflights_zip_and_sheet_cardinality_before_allocation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        package = _package_zip(Path(tmp), {"one.txt": "one", "two.txt": "two"})
        original_limit = review_camera_remote_command_package.MAX_PACKAGE_FILES
        review_camera_remote_command_package.MAX_PACKAGE_FILES = 1
        try:
            try:
                review_camera_remote_command_package._read_package_snapshot(package)
            except ValueError as exc:
                assert "entry-count" in str(exc)
            else:
                raise AssertionError("regular-FD central-directory preflight accepted too many entries")
        finally:
            review_camera_remote_command_package.MAX_PACKAGE_FILES = original_limit

    encrypted = bytearray(_office_zip({"word/document.xml": "<document/>"}))
    central = encrypted.index(b"PK\x01\x02")
    encrypted[central + 8] |= 0x01  # General-purpose bit flag: traditional encryption.
    try:
        review_camera_remote_command_package._preflight_zip_bytes(
            bytes(encrypted), max_entries=8, max_central_directory_bytes=4096,
            max_expanded_bytes=4096, max_member_bytes=4096,
        )
    except ValueError as exc:
        assert "encrypted" in str(exc)
    else:
        raise AssertionError("encrypted ZIP central directory was accepted")

    prefixed = bytearray(b"JUNK" + _office_zip({"word/document.xml": "<document/>"}))
    eocd_offset = prefixed.rfind(b"PK\x05\x06")
    central_offset = struct.unpack_from("<L", prefixed, eocd_offset + 16)[0]
    # Make the central directory and local offset self-consistent after a
    # prefix. ZipFile accepts this prefixed layout; our preflight must reject
    # it before its constructor can materialize an inventory.
    struct.pack_into("<L", prefixed, eocd_offset + 16, central_offset + 4)
    original_zipfile = review_camera_remote_command_package.zipfile.ZipFile
    struct.pack_into("<L", prefixed, central_offset + 4 + 42, 4)
    with original_zipfile(BytesIO(prefixed)) as archive:
        assert archive.read("word/document.xml") == b"<document/>"
    zipfile_opened: list[bool] = []

    class MustNotOpenAmbiguousZip:
        def __init__(self, *_args, **_kwargs):
            zipfile_opened.append(True)
            raise AssertionError("ZipFile ran before ambiguous-layout rejection")

    review_camera_remote_command_package.zipfile.ZipFile = MustNotOpenAmbiguousZip
    try:
        budget = review_camera_remote_command_package.OfficeExtractionBudget(
            max_members=8, max_sheets=8, max_headers_footers=8, max_expanded_bytes=4096, max_text_bytes=4096
        )
        text, method, error = review_camera_remote_command_package.extract_text(
            "prefixed.docx", bytes(prefixed), office_budget=budget
        )
    finally:
        review_camera_remote_command_package.zipfile.ZipFile = original_zipfile
    assert text is None and method == "extraction_error" and "ambiguous" in error
    assert not zipfile_opened

    docx = _office_zip({
        "word/document.xml": "<document><p>body</p></document>",
        "word/header1.xml": "<header><p>header</p></header>",
    })
    original_zipfile = review_camera_remote_command_package.zipfile.ZipFile
    zipfile_opened = []

    class MustNotOpenZipFile:
        def __init__(self, *_args, **_kwargs):
            zipfile_opened.append(True)
            raise AssertionError("ZipFile ran before central-directory count rejection")

    review_camera_remote_command_package.zipfile.ZipFile = MustNotOpenZipFile
    try:
        budget = review_camera_remote_command_package.OfficeExtractionBudget(
            max_members=1, max_sheets=8, max_headers_footers=8, max_expanded_bytes=4096, max_text_bytes=4096
        )
        text, method, error = review_camera_remote_command_package.extract_text("too-many.docx", docx, office_budget=budget)
    finally:
        review_camera_remote_command_package.zipfile.ZipFile = original_zipfile
    assert text is None and method == "extraction_error" and "entry-count" in error
    assert not zipfile_opened

    worksheet = "<worksheet><sheetData><row><c t=\"s\"><v>0</v></c></row></sheetData></worksheet>"
    workbook = _office_zip({
        "xl/sharedStrings.xml": "<sst><si><t>must not inflate</t></si></sst>",
        "xl/worksheets/sheet1.xml": worksheet,
        "xl/worksheets/sheet2.xml": worksheet,
    })
    original_read = review_camera_remote_command_package._safe_zip_member
    read_members: list[str] = []

    def guard_shared_strings(archive, info):
        read_members.append(info.filename)
        if info.filename == "xl/sharedStrings.xml":
            raise AssertionError("sharedStrings was inflated before sheet-count rejection")
        return original_read(archive, info)

    review_camera_remote_command_package._safe_zip_member = guard_shared_strings
    try:
        budget = review_camera_remote_command_package.OfficeExtractionBudget(
            max_members=8, max_sheets=1, max_headers_footers=8, max_expanded_bytes=4096, max_text_bytes=4096
        )
        text, method, error = review_camera_remote_command_package.extract_text("too-many-sheets.xlsx", workbook, office_budget=budget)
    finally:
        review_camera_remote_command_package._safe_zip_member = original_read
    assert text is None and method == "extraction_error" and "sheet-count" in error
    assert "xl/sharedStrings.xml" not in read_members
    print("  Camera Remote Command reviewer preflight allocation order: ok")


def test_command_package_review_streams_office_xml_with_element_and_depth_budgets() -> None:
    docx = _office_zip({"word/document.xml": "<document><p><t>ILCE-7M2</t></p></document>"})
    original_fromstring = review_camera_remote_command_package.DefusedET.fromstring
    fromstring_calls: list[bool] = []

    def no_full_tree(*_args, **_kwargs):
        fromstring_calls.append(True)
        raise AssertionError("full XML tree construction is forbidden")

    review_camera_remote_command_package.DefusedET.fromstring = no_full_tree
    try:
        budget = review_camera_remote_command_package.OfficeExtractionBudget(
            max_members=8, max_sheets=8, max_headers_footers=8, max_expanded_bytes=4096,
            max_text_bytes=4096, max_xml_elements=2, max_xml_depth=8,
        )
        text, method, error = review_camera_remote_command_package.extract_text("element-budget.docx", docx, office_budget=budget)
    finally:
        review_camera_remote_command_package.DefusedET.fromstring = original_fromstring
    assert text is None and method == "extraction_error" and "element budget" in error
    assert not fromstring_calls

    budget = review_camera_remote_command_package.OfficeExtractionBudget(
        max_members=8, max_sheets=8, max_headers_footers=8, max_expanded_bytes=4096,
        max_text_bytes=4096, max_xml_elements=8, max_xml_depth=2,
    )
    text, method, error = review_camera_remote_command_package.extract_text("depth-budget.docx", docx, office_budget=budget)
    assert text is None and method == "extraction_error" and "depth budget" in error

    mixed = b"<document><p>Hello <b>secure</b> world</p></document>"
    budget = review_camera_remote_command_package.OfficeExtractionBudget(
        max_members=8, max_sheets=8, max_headers_footers=8, max_expanded_bytes=4096,
        max_text_bytes=4096, max_xml_elements=32, max_xml_depth=8,
    )
    assert review_camera_remote_command_package._xml_text(mixed, budget) == "Hello secure world"
    nested_mixed = b"<document><p>A <b>B <i>C</i> D</b> E</p><p>F</p></document>"
    budget = review_camera_remote_command_package.OfficeExtractionBudget(
        max_members=8, max_sheets=8, max_headers_footers=8, max_expanded_bytes=4096,
        max_text_bytes=4096, max_xml_elements=32, max_xml_depth=8,
    )
    assert review_camera_remote_command_package._xml_text(nested_mixed, budget) == "A B C D E F"

    # This executes against the installed defusedxml parser in the canonical
    # environment.  At most the active child is retained under the root; old
    # cleared children are removed before the next sibling arrives.
    assert review_camera_remote_command_package.DefusedET.__name__.startswith("defusedxml.")
    wide_xml = ("<document>" + "<p>ILCE-7M2</p>" * 1_001 + "</document>").encode("utf-8")
    budget = review_camera_remote_command_package.OfficeExtractionBudget(
        max_members=8, max_sheets=8, max_headers_footers=8, max_expanded_bytes=4096,
        max_text_bytes=64 * 1024, max_xml_elements=2_000, max_xml_depth=8,
    )
    root = None
    retained_width = 0
    for _event, element in review_camera_remote_command_package._safe_xml_events(wide_xml, budget):
        if root is None:
            root = element
        retained_width = max(retained_width, len(root))
    # The parser can queue one bounded source chunk before yielding, but cannot
    # retain the 1,001 completed siblings beneath the root.
    assert retained_width <= 32, retained_width
    print("  Camera Remote Command reviewer streaming XML budgets: ok")


def test_command_package_review_bounds_pdftotext_stdout_without_spawning() -> None:
    class FakeProcess:
        def __init__(self):
            stdout_read, stdout_write = os.pipe()
            stderr_read, stderr_write = os.pipe()
            os.write(stdout_write, b"x" * 9)
            os.close(stdout_write)
            os.close(stderr_write)
            self.stdout = os.fdopen(stdout_read, "rb")
            self.stderr = os.fdopen(stderr_read, "rb")
            self.returncode = 0
            self.pid = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def terminate(self):
            return None

        def kill(self):
            return None

    original_which = review_camera_remote_command_package.shutil.which
    original_popen = review_camera_remote_command_package.subprocess.Popen
    original_limit = review_camera_remote_command_package.MAX_PDFTEXT_STDOUT_BYTES
    process: FakeProcess | None = None

    def fake_popen(*_args, **_kwargs):
        nonlocal process
        process = FakeProcess()
        return process

    review_camera_remote_command_package.shutil.which = lambda _name: "pdftotext"
    review_camera_remote_command_package.subprocess.Popen = fake_popen
    review_camera_remote_command_package.MAX_PDFTEXT_STDOUT_BYTES = 8
    try:
        text, error = review_camera_remote_command_package._extract_pdf_text(b"%PDF-1.4")
    finally:
        review_camera_remote_command_package.shutil.which = original_which
        review_camera_remote_command_package.subprocess.Popen = original_popen
        review_camera_remote_command_package.MAX_PDFTEXT_STDOUT_BYTES = original_limit
        if process is not None:
            process.stdout.close()
            process.stderr.close()
    assert text is None and error == "pdftotext output exceeds byte budget"
    print("  Camera Remote Command reviewer pdftotext stdout budget: ok")


if __name__ == "__main__":
    test_command_package_review_maps_registry_rows_from_local_package_text()
    test_command_package_review_rejects_sdk_archive_even_with_camera_terms()
    test_command_package_review_allows_command_package_that_mentions_sdk()
    test_command_package_review_does_not_scope_commands_from_global_model_mention()
    test_command_package_review_rejects_unknown_package_identity()
    test_command_package_review_maps_xlsx_compatibility_row()
    test_command_package_review_rejects_suspicious_zip_compression_ratio()
    test_command_package_review_rejects_package_symlinks()
    test_command_package_review_rejects_fifo_before_zip_open()
    test_command_package_review_consumes_one_descriptor_snapshot_not_a_reopened_path()
    test_command_package_review_defuses_office_xml_entities()
    test_command_package_review_bounds_nested_office_counts_expansion_and_text()
    test_command_package_review_preflights_zip_and_sheet_cardinality_before_allocation()
    test_command_package_review_streams_office_xml_with_element_and_depth_budgets()
    test_command_package_review_bounds_pdftotext_stdout_without_spawning()
    print("camera_remote_command_review tests: PASS")
