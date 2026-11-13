#!/usr/bin/env python3
"""Create a compatibility evidence report from a local Sony Camera Remote Command package.

The Sony package is gated by Sony's terms. This tool does not download it and should not be used to
commit or redistribute the package. It records filenames, hashes, and small compatibility snippets from
a package already present on the user's machine.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import selectors
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Callable

from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import settings_registry

TEXT_SUFFIXES = {
    ".csv",
    ".htm",
    ".html",
    ".json",
    ".md",
    ".rst",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
OFFICE_SUFFIXES = {".docx", ".xlsx"}
PDF_SUFFIXES = {".pdf"}
MAX_PACKAGE_FILES = 10_000
MAX_PACKAGE_TOTAL_BYTES = 512 * 1024 * 1024
MAX_PACKAGE_FILE_BYTES = 128 * 1024 * 1024
MAX_PACKAGE_ARCHIVE_BYTES = MAX_PACKAGE_TOTAL_BYTES
MAX_PACKAGE_CENTRAL_DIRECTORY_BYTES = 32 * 1024 * 1024
MAX_OFFICE_XML_BYTES = 16 * 1024 * 1024
MAX_OFFICE_MEMBER_COUNT = 256
MAX_OFFICE_SHEET_COUNT = 128
MAX_OFFICE_HEADER_FOOTER_COUNT = 128
MAX_OFFICE_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_OFFICE_EXTRACTED_TEXT_BYTES = 8 * 1024 * 1024
MAX_OFFICE_CENTRAL_DIRECTORY_BYTES = 2 * 1024 * 1024
MAX_OFFICE_XML_ELEMENTS = 250_000
MAX_OFFICE_XML_DEPTH = 128
MAX_OFFICE_XML_PARSE_CHUNK_BYTES = 256
MAX_ZIP_COMPRESSION_RATIO = 200
MAX_PDFTEXT_STDOUT_BYTES = 8 * 1024 * 1024
MAX_PDFTEXT_STDERR_BYTES = 64 * 1024
PDFTEXT_TIMEOUT_SECONDS = 60.0

TERMS = (
    "ILCE-7M2",
    "PTP-IP",
    "Wi-Fi",
    "Wireless LAN",
    "Focus Area",
    "Focus Position",
    "AFArea",
    "Live View",
)

COMMAND_PACKAGE_PATTERNS = (
    re.compile(r"camera[\s_-]*remote[\s_-]*command", re.IGNORECASE),
    re.compile(r"remote[\s_-]*command", re.IGNORECASE),
)

SDK_PACKAGE_PATTERNS = (
    re.compile(r"crsdk", re.IGNORECASE),
    re.compile(r"camera[\s_-]*remote[\s_-]*sdk", re.IGNORECASE),
    re.compile(r"remote[\s_-]*sdk", re.IGNORECASE),
)

OFFICIAL_SOURCES = (
    {
        "label": "Sony Camera Remote Command",
        "url": "https://support.d-imaging.sony.co.jp/app/cameraremotecommand/en/index.html",
        "purpose": "Reference the current command documentation and supported-model list.",
    },
    {
        "label": "Sony Camera Remote Command download",
        "url": "https://pro.sony/ue_US/digital-imaging/camera-remote-command-download",
        "purpose": "Acquire the package through the approved local path; do not redistribute it.",
    },
)

GENERIC_WORDS = {
    "and",
    "area",
    "camera",
    "ctrl",
    "display",
    "file",
    "format",
    "mode",
    "set",
    "setup",
    "setting",
    "settings",
    "the",
    "with",
}

ROW_ALIASES = {
    "camera.focus_area": ("Focus Position", "AFArea", "AF Area", "Touch AF", "Focus Point"),
    "camera.file_format": ("Movie File Format", "FileFormat", "Video Format"),
    "camera.record_setting": ("Movie Record Setting", "Recording Setting"),
    "camera.dual_video_rec": ("Dual Video", "Dual Rec", "Proxy Recording"),
    "camera.exposure_step": ("Exposure Step", "ExposureStep", "Step EV"),
    "camera.creative_style": ("Creative Style", "CreativeStyle"),
    "camera.picture_profile": ("Picture Profile", "PictureProfile"),
    "camera.steadyshot": ("SteadyShot", "Image Stabilization", "Image Stabilisation"),
    "camera.audio_recording": ("Audio Recording", "AudioRec"),
    "camera.audio_rec_level": ("Audio Rec Level", "Audio Recording Level"),
    "camera.audio_out_timing": ("Audio Out Timing", "Audio Output Timing"),
    "camera.wind_noise_reduction": ("Wind Noise", "Wind Noise Reduction"),
    "custom.zebra": ("Zebra", "Zebra Display"),
    "custom.mf_assist": ("MF Assist", "Manual Focus Assist"),
    "custom.marker_display": ("Marker Display", "Movie Marker"),
    "custom.audio_level_display": ("Audio Level Display", "Audio Level"),
    "custom.peaking_level": ("Peaking Level", "Focus Peaking"),
    "custom.live_view_display": ("Live View Display", "Live View"),
    "custom.phase_detect_area": ("Phase Detect Area", "Phase Detection"),
    "custom.pre_af": ("Pre-AF", "Pre AF"),
    "custom.release_wo_lens": ("Release w/o Lens", "Release Without Lens"),
    "custom.af_with_shutter": ("AF w/ shutter", "AF With Shutter"),
    "custom.e_front_curtain_shut": ("Electronic Front Curtain", "e-Front Curtain"),
    "custom.exp_comp_set": ("Exposure Compensation Set", "Exp Comp Set"),
    "custom.aps_c_size_capture": ("APS-C Size Capture", "APS-C", "Crop"),
    "custom.lens_comp": ("Lens Compensation", "Lens Comp"),
    "wireless.airplane_mode": ("Airplane Mode", "Airplane"),
    "setup.tc_ub_settings": ("TC/UB", "Time Code", "User Bit"),
    "setup.remote_ctrl": ("Remote Ctrl", "Remote Control"),
    "setup.hdmi_settings": ("HDMI", "HDMI Settings"),
    "setup.date_time_setup": ("Date/Time", "Clock", "Date Time"),
    "setup.file_number": ("File Number", "FileNumber"),
    "setup.set_file_name": ("Set File Name", "File Name"),
}


def sha256_file(path: Path) -> str:
    """Compatibility helper: hash one descriptor-safe package snapshot."""
    return hashlib.sha256(_read_package_snapshot(path)).hexdigest()


def decode_text(data: bytes) -> str | None:
    for enc in ("utf-8", "utf-16", "utf-16-le", "cp932", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


@dataclass(frozen=True)
class ZipCentralDirectory:
    """Bounded facts parsed directly from ZIP metadata, before ``ZipFile`` allocation."""

    entries: int
    members: int
    expanded_bytes: int
    central_directory_bytes: int


def _read_exact(read_at: Callable[[int, int], bytes], offset: int, length: int, *, label: str) -> bytes:
    if offset < 0 or length < 0:
        raise ValueError(f"malformed ZIP {label}")
    data = read_at(offset, length)
    if len(data) != length:
        raise ValueError(f"malformed ZIP {label}")
    return data


def _zip64_member_fields(
    extra: bytes,
    *,
    need_uncompressed: bool,
    need_compressed: bool,
    need_local_offset: bool,
) -> tuple[int | None, int | None, int | None]:
    """Read ZIP64 size/offset fields in their spec-defined order, or fail closed."""
    cursor = 0
    while cursor < len(extra):
        if len(extra) - cursor < 4:
            raise ValueError("malformed ZIP central-directory extra field")
        field_id, field_size = struct.unpack_from("<HH", extra, cursor)
        cursor += 4
        if field_size > len(extra) - cursor:
            raise ValueError("malformed ZIP central-directory extra field")
        field = extra[cursor:cursor + field_size]
        cursor += field_size
        if field_id != 0x0001:
            continue
        position = 0

        def take_u64(required: bool) -> int | None:
            nonlocal position
            if not required:
                return None
            if len(field) - position < 8:
                raise ValueError("malformed ZIP64 member size")
            value = struct.unpack_from("<Q", field, position)[0]
            position += 8
            return value

        return (
            take_u64(need_uncompressed),
            take_u64(need_compressed),
            take_u64(need_local_offset),
        )
    raise ValueError("ZIP64 member is missing its ZIP64 extra field")


def _preflight_zip_central_directory(
    read_at: Callable[[int, int], bytes],
    size: int,
    *,
    max_entries: int,
    max_central_directory_bytes: int,
    max_expanded_bytes: int,
    max_member_bytes: int,
) -> ZipCentralDirectory:
    """Validate bounded ZIP metadata without letting ``ZipFile`` build an inventory first."""
    if size < 22:
        raise ValueError("malformed ZIP end-of-central-directory record")
    tail_length = min(size, 22 + 0xFFFF)
    tail_offset = size - tail_length
    tail = _read_exact(read_at, tail_offset, tail_length, label="end-of-central-directory record")
    eocd_offset = -1
    eocd: tuple[int, int, int, int, int, int] | None = None
    for cursor in range(tail_length - 22, -1, -1):
        if tail[cursor:cursor + 4] != b"PK\x05\x06":
            continue
        disk, central_disk, disk_entries, entries, central_size, central_offset, comment_size = struct.unpack_from(
            "<HHHHLLH", tail, cursor + 4
        )
        if cursor + 22 + comment_size == tail_length:
            eocd_offset = tail_offset + cursor
            eocd = (disk, central_disk, disk_entries, entries, central_size, central_offset)
            break
    if eocd is None:
        raise ValueError("malformed ZIP end-of-central-directory record")
    disk, central_disk, disk_entries, entries, central_size, central_offset = eocd
    if disk != 0 or central_disk != 0 or disk_entries != entries:
        raise ValueError("multi-disk ZIP archives are not allowed")

    zip64_record_offset: int | None = None
    if entries == 0xFFFF or central_size == 0xFFFFFFFF or central_offset == 0xFFFFFFFF:
        locator_offset = eocd_offset - 20
        locator = _read_exact(read_at, locator_offset, 20, label="ZIP64 locator")
        if locator[:4] != b"PK\x06\x07":
            raise ValueError("malformed ZIP64 locator")
        locator_disk, zip64_record_offset, locator_disks = struct.unpack_from("<LQL", locator, 4)
        if locator_disk != 0 or locator_disks != 1:
            raise ValueError("multi-disk ZIP64 archives are not allowed")
        header = _read_exact(read_at, zip64_record_offset, 56, label="ZIP64 end-of-central-directory record")
        if header[:4] != b"PK\x06\x06":
            raise ValueError("malformed ZIP64 end-of-central-directory record")
        (
            record_size,
            _made_by,
            _needed,
            zip64_disk,
            zip64_central_disk,
            zip64_disk_entries,
            zip64_entries,
            zip64_central_size,
            zip64_central_offset,
        ) = struct.unpack_from("<QHHLLQQQQ", header, 4)
        if (
            record_size < 44
            or zip64_record_offset + 12 + record_size > locator_offset
            or zip64_disk != 0
            or zip64_central_disk != 0
            or zip64_disk_entries != zip64_entries
        ):
            raise ValueError("malformed ZIP64 end-of-central-directory record")
        entries, central_size, central_offset = zip64_entries, zip64_central_size, zip64_central_offset

    central_end = central_offset + central_size
    central_boundary = zip64_record_offset if zip64_record_offset is not None else eocd_offset
    # Do not emulate ZipFile's concat-offset recovery.  An archive accepted by
    # this reviewer is one unambiguous ZIP layout: its central directory ends
    # immediately at EOCD (or ZIP64 EOCD), with no prefix, suffix, or hidden
    # concatenated archive influencing the declared member count.
    if central_end != central_boundary:
        raise ValueError("ambiguous ZIP central-directory boundary")
    if central_size > max_central_directory_bytes:
        raise ValueError("ZIP central directory exceeds byte budget")
    if entries > max_entries:
        raise ValueError("ZIP central directory exceeds entry-count budget")
    central = _read_exact(read_at, central_offset, central_size, label="central directory")
    cursor = 0
    members = 0
    expanded_bytes = 0
    local_offsets: list[int] = []
    for _ in range(entries):
        if len(central) - cursor < 46 or central[cursor:cursor + 4] != b"PK\x01\x02":
            raise ValueError("malformed ZIP central-directory entry")
        fields = struct.unpack_from("<6H3L5H2L", central, cursor + 4)
        flags = fields[2]
        compressed_size = fields[7]
        uncompressed_size = fields[8]
        filename_size, extra_size, comment_size = fields[9:12]
        disk_start = fields[12]
        local_offset = fields[15]
        record_size = 46 + filename_size + extra_size + comment_size
        if record_size > len(central) - cursor:
            raise ValueError("malformed ZIP central-directory entry")
        filename = central[cursor + 46:cursor + 46 + filename_size]
        extra_start = cursor + 46 + filename_size
        extra = central[extra_start:extra_start + extra_size]
        if not filename or b"\x00" in filename:
            raise ValueError("malformed ZIP member name")
        if flags & 0x41:
            raise ValueError("encrypted ZIP members are not allowed")
        if disk_start == 0xFFFF:
            raise ValueError("ZIP64 multi-disk members are not allowed")
        if disk_start != 0:
            raise ValueError("multi-disk ZIP members are not allowed")
        if (
            uncompressed_size == 0xFFFFFFFF
            or compressed_size == 0xFFFFFFFF
            or local_offset == 0xFFFFFFFF
        ):
            zip64_uncompressed, zip64_compressed, zip64_local_offset = _zip64_member_fields(
                extra,
                need_uncompressed=uncompressed_size == 0xFFFFFFFF,
                need_compressed=compressed_size == 0xFFFFFFFF,
                need_local_offset=local_offset == 0xFFFFFFFF,
            )
            if zip64_uncompressed is not None:
                uncompressed_size = zip64_uncompressed
            if zip64_compressed is not None:
                compressed_size = zip64_compressed
            if zip64_local_offset is not None:
                local_offset = zip64_local_offset
        if local_offset >= central_offset:
            raise ValueError("malformed ZIP local-header offset")
        if _read_exact(read_at, local_offset, 4, label="local-header signature") != b"PK\x03\x04":
            raise ValueError("ambiguous ZIP local-header layout")
        local_offsets.append(local_offset)
        if not filename.endswith(b"/"):
            members += 1
            if uncompressed_size > max_member_bytes:
                raise ValueError(f"ZIP member exceeds {max_member_bytes} bytes")
            if uncompressed_size and (
                compressed_size == 0
                or uncompressed_size / max(compressed_size, 1) > MAX_ZIP_COMPRESSION_RATIO
            ):
                raise ValueError("ZIP member has suspicious compression ratio")
            expanded_bytes += uncompressed_size
            if expanded_bytes > max_expanded_bytes:
                raise ValueError("ZIP archive exceeds expanded-byte budget")
        cursor += record_size
    if cursor != len(central):
        raise ValueError("malformed ZIP central-directory size")
    if entries:
        if min(local_offsets) != 0:
            raise ValueError("ambiguous prefixed ZIP layout")
    elif central_offset != 0:
        raise ValueError("ambiguous prefixed ZIP layout")
    return ZipCentralDirectory(entries, members, expanded_bytes, central_size)


def _preflight_zip_bytes(
    data: bytes,
    *,
    max_entries: int,
    max_central_directory_bytes: int,
    max_expanded_bytes: int,
    max_member_bytes: int,
) -> ZipCentralDirectory:
    return _preflight_zip_central_directory(
        lambda offset, length: data[offset:offset + length],
        len(data),
        max_entries=max_entries,
        max_central_directory_bytes=max_central_directory_bytes,
        max_expanded_bytes=max_expanded_bytes,
        max_member_bytes=max_member_bytes,
    )


class OfficeExtractionBudget:
    """Cumulative limits for all nested Office archives in one package review."""

    def __init__(
        self,
        *,
        max_members: int = MAX_OFFICE_MEMBER_COUNT,
        max_sheets: int = MAX_OFFICE_SHEET_COUNT,
        max_headers_footers: int = MAX_OFFICE_HEADER_FOOTER_COUNT,
        max_expanded_bytes: int = MAX_OFFICE_EXPANDED_BYTES,
        max_text_bytes: int = MAX_OFFICE_EXTRACTED_TEXT_BYTES,
        max_xml_elements: int = MAX_OFFICE_XML_ELEMENTS,
        max_xml_depth: int = MAX_OFFICE_XML_DEPTH,
    ):
        self.max_members = max_members
        self.max_sheets = max_sheets
        self.max_headers_footers = max_headers_footers
        self.max_expanded_bytes = max_expanded_bytes
        self.max_text_bytes = max_text_bytes
        self.max_xml_elements = max_xml_elements
        self.max_xml_depth = max_xml_depth
        self.members = 0
        self.sheets = 0
        self.headers_footers = 0
        self.expanded_bytes = 0
        self.text_bytes = 0
        self.xml_elements = 0

    def preflight_archive(self, data: bytes) -> ZipCentralDirectory:
        return _preflight_zip_bytes(
            data,
            max_entries=self.max_members - self.members,
            max_central_directory_bytes=MAX_OFFICE_CENTRAL_DIRECTORY_BYTES,
            max_expanded_bytes=self.max_expanded_bytes - self.expanded_bytes,
            max_member_bytes=MAX_OFFICE_XML_BYTES,
        )

    def accept_archive(self, infos: list[zipfile.ZipInfo], preflight: ZipCentralDirectory) -> None:
        members = [info for info in infos if not info.is_dir()]
        if len(infos) != preflight.entries or len(members) != preflight.members:
            raise ValueError("Office archive central directory changed while opening")
        if sum(info.file_size for info in members) != preflight.expanded_bytes:
            raise ValueError("Office archive central directory changed while opening")
        self.members += preflight.members
        self.expanded_bytes += preflight.expanded_bytes

    def consume_sheets(self, count: int) -> None:
        if count > self.max_sheets - self.sheets:
            raise ValueError("Office workbook exceeds cumulative sheet-count budget")
        self.sheets += count

    def consume_headers_footers(self, count: int) -> None:
        if count > self.max_headers_footers - self.headers_footers:
            raise ValueError("Office document exceeds cumulative header/footer-count budget")
        self.headers_footers += count

    def consume_xml_start(self, depth: int) -> None:
        if depth > self.max_xml_depth:
            raise ValueError("Office XML exceeds depth budget")
        if self.xml_elements >= self.max_xml_elements:
            raise ValueError("Office XML exceeds cumulative element budget")
        self.xml_elements += 1

    def add_text(self, value: str, *, separator: bool) -> str | None:
        cleaned = str(value).strip()
        if not cleaned:
            return None
        encoded = cleaned.encode("utf-8")
        cost = len(encoded) + (1 if separator else 0)
        if cost > self.max_text_bytes - self.text_bytes:
            raise ValueError("Office extraction exceeds cumulative text budget")
        self.text_bytes += cost
        return cleaned

    def text(self, values: list[str] | tuple[str, ...] | Any) -> str:
        parts: list[str] = []
        for value in values:
            cleaned = self.add_text(value, separator=bool(parts))
            if cleaned is not None:
                parts.append(cleaned)
        return " ".join(parts)

    def separator(self) -> None:
        if self.max_text_bytes - self.text_bytes < 1:
            raise ValueError("Office extraction exceeds cumulative text budget")
        self.text_bytes += 1


def _safe_zip_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    return archive.read(info)


class _BoundedXMLInput:
    """Keep iterparse from queueing a whole wide XML document before events run."""

    def __init__(self, data: bytes):
        self._stream = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = MAX_OFFICE_XML_PARSE_CHUNK_BYTES
        return self._stream.read(min(size, MAX_OFFICE_XML_PARSE_CHUNK_BYTES))


def _safe_xml_events(data: bytes, budget: OfficeExtractionBudget):
    if len(data) > MAX_OFFICE_XML_BYTES:
        raise ValueError(f"Office XML member exceeds {MAX_OFFICE_XML_BYTES} bytes")
    declaration_scan = data.lower().replace(b"\x00", b"")
    if b"<!doctype" in declaration_scan or b"<!entity" in declaration_scan:
        raise ValueError("Office XML DTD/entity declarations are not allowed")
    depth = 0
    parents: list[Any] = []
    for event, element in DefusedET.iterparse(_BoundedXMLInput(data), events=("start", "end")):
        if event == "start":
            depth += 1
            budget.consume_xml_start(depth)
            parents.append(element)
            yield event, element
        else:
            if not parents or parents[-1] is not element:
                raise ValueError("malformed Office XML event order")
            yield event, element
            parents.pop()
            if parents:
                # ElementTree otherwise keeps every cleared child in its
                # parent's child list until the root closes.  Remove it as
                # soon as callers have consumed its end event so a wide XML
                # document retains only the active ancestor path.
                parents[-1].remove(element)
            else:
                element.clear()
            depth -= 1
    if parents or depth:
        raise ValueError("malformed Office XML event order")


def _xml_text(data: bytes, budget: OfficeExtractionBudget) -> str:
    """Extract normalized XML text in mixed-content order without retaining a tree.

    ``iterparse`` does not guarantee ``element.text`` is complete at a start
    event.  Frames therefore defer their leading text until the first completed
    descendant (or their own end); opening all active frames then emits each
    parent's text before child content, followed by the completed child's tail.
    The one cumulative collector enforces the text budget in that document order.
    """
    parts: list[str] = []
    frames: list[dict[str, Any]] = []

    def append(value: str | None) -> None:
        cleaned = budget.add_text(value or "", separator=bool(parts))
        if cleaned is not None:
            parts.append(cleaned)

    for event, element in _safe_xml_events(data, budget):
        if event == "start":
            frames.append({"element": element, "opened": False})
            continue
        if not frames or frames[-1]["element"] is not element:
            raise ValueError("malformed Office XML frame order")
        # Opening ancestors from root through this node preserves: parent text,
        # child content, then child tail.  Each frame opens once only.
        for frame in frames:
            if not frame["opened"]:
                append(frame["element"].text)
                frame["opened"] = True
        append(element.tail)
        frames.pop()
    if frames:
        raise ValueError("malformed Office XML frame order")
    return " ".join(parts)


def _office_archive(data: bytes, budget: OfficeExtractionBudget) -> zipfile.ZipFile:
    # Count/size/encryption preflight happens before ZipFile can allocate one
    # ZipInfo object per central-directory entry.
    preflight = budget.preflight_archive(data)
    archive = zipfile.ZipFile(io.BytesIO(data))
    try:
        budget.accept_archive(archive.infolist(), preflight)
    except Exception:
        archive.close()
        raise
    return archive


def _extract_docx_text(data: bytes, budget: OfficeExtractionBudget) -> str:
    parts = []
    with _office_archive(data, budget) as archive:
        infos = sorted(
            (
                info
                for info in archive.infolist()
                if info.filename == "word/document.xml"
                or info.filename.startswith("word/header")
                or info.filename.startswith("word/footer")
            ),
            key=lambda info: info.filename,
        )
        budget.consume_headers_footers(sum(
            info.filename.startswith("word/header") or info.filename.startswith("word/footer")
            for info in infos
        ))
        for info in infos:
            part = _xml_text(_safe_zip_member(archive, info), budget)
            if part:
                if parts:
                    budget.separator()
                parts.append(part)
    return "\n".join(part for part in parts if part)


def _extract_xlsx_text(data: bytes, budget: OfficeExtractionBudget) -> str:
    with _office_archive(data, budget) as archive:
        sheet_infos = sorted(
            (
                info
                for info in archive.infolist()
                if info.filename.startswith("xl/worksheets/") and info.filename.endswith(".xml")
            ),
            key=lambda info: info.filename,
        )
        budget.consume_sheets(len(sheet_infos))
        # The sheet-cardinality guard must run before potentially enormous
        # sharedStrings.xml is inflated or parsed.
        shared_strings: list[str] = []
        shared_info = next((info for info in archive.infolist() if info.filename == "xl/sharedStrings.xml"), None)
        if shared_info is not None:
            fragments: list[str] = []
            for event, element in _safe_xml_events(_safe_zip_member(archive, shared_info), budget):
                if event != "end":
                    continue
                tag = element.tag.rsplit("}", 1)[-1]
                if tag == "t":
                    value = (element.text or "").strip()
                    if value:
                        fragments.append(value)
                elif tag == "si":
                    shared_strings.append(budget.text(fragments))
                    fragments = []

        lines = []
        for sheet_info in sheet_infos:
            current_row: list[str] | None = None
            cell_type: str | None = None
            cell_value = ""
            inline_fragments: list[str] = []
            for event, element in _safe_xml_events(_safe_zip_member(archive, sheet_info), budget):
                tag = element.tag.rsplit("}", 1)[-1]
                if event == "start" and tag == "row":
                    current_row = []
                    continue
                if event == "start" and tag == "c":
                    cell_type = element.attrib.get("t")
                    cell_value = ""
                    inline_fragments = []
                    continue
                if event != "end":
                    continue
                if tag == "v" and current_row is not None:
                    cell_value = (element.text or "").strip()
                elif tag == "t" and cell_type == "inlineStr":
                    value = (element.text or "").strip()
                    if value:
                        inline_fragments.append(value)
                elif tag == "c":
                    if current_row is None:
                        continue
                    if cell_type == "s" and cell_value.isdigit() and int(cell_value) < len(shared_strings):
                        value = shared_strings[int(cell_value)]
                    elif cell_type == "inlineStr":
                        value = " ".join(inline_fragments)
                    else:
                        value = cell_value
                    current_row.append(value)
                    cell_type = None
                    cell_value = ""
                    inline_fragments = []
                elif tag == "row":
                    if current_row and any(current_row):
                        line = budget.text((",".join(current_row),))
                        if lines:
                            budget.separator()
                        lines.append(line)
                    current_row = None
        return "\n".join(lines)


def _extract_pdf_text(data: bytes) -> tuple[str | None, str | None]:
    executable = shutil.which("pdftotext")
    if not executable:
        return None, "pdftotext executable not available"
    with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
        handle.write(data)
        handle.flush()
        proc = subprocess.Popen(
            [executable, "-layout", handle.name, "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        assert proc.stdout is not None and proc.stderr is not None
        selector = selectors.DefaultSelector()
        stdout = bytearray()
        stderr = bytearray()
        timed_out = False
        overflow = False
        try:
            selector.register(proc.stdout, selectors.EVENT_READ, stdout)
            selector.register(proc.stderr, selectors.EVENT_READ, stderr)
            deadline = time.monotonic() + PDFTEXT_TIMEOUT_SECONDS
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    target = key.data
                    cap = MAX_PDFTEXT_STDOUT_BYTES if target is stdout else MAX_PDFTEXT_STDERR_BYTES
                    if len(target) + len(chunk) > cap:
                        overflow = True
                        break
                    target.extend(chunk)
                if overflow:
                    break
        finally:
            selector.close()
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except OSError:
                    proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except OSError:
                        proc.kill()
                    proc.wait(timeout=2)
        if timed_out:
            return None, "pdftotext exceeded extraction deadline"
        if overflow:
            return None, "pdftotext output exceeds byte budget"
        if proc.returncode != 0:
            error = bytes(stderr).decode("utf-8", errors="replace").strip()
            return None, f"pdftotext exited {proc.returncode}: {error}"
        return bytes(stdout).decode("utf-8", errors="replace"), None


def extract_text(
    rel: str,
    data: bytes,
    *,
    office_budget: OfficeExtractionBudget | None = None,
) -> tuple[str | None, str, str | None]:
    suffix = Path(rel).suffix.lower()
    office_budget = office_budget or OfficeExtractionBudget()
    try:
        if suffix in TEXT_SUFFIXES or (b"\x00" not in data[:512] and suffix not in OFFICE_SUFFIXES | PDF_SUFFIXES):
            return decode_text(data), "plain_text", None
        if suffix == ".docx":
            return _extract_docx_text(data, office_budget), "docx_xml", None
        if suffix == ".xlsx":
            return _extract_xlsx_text(data, office_budget), "xlsx_xml", None
        if suffix == ".pdf":
            text, error = _extract_pdf_text(data)
            return text, "pdftotext" if text is not None else "pdf_unavailable", error
    except (
        DefusedET.ParseError,
        DefusedXmlException,
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
        zipfile.BadZipFile,
    ) as exc:
        return None, "extraction_error", f"{type(exc).__name__}: {exc}"
    return None, "binary_or_unsupported", None


def iter_package_files(package: bytes):
    count = 0
    total_bytes = 0
    preflight = _preflight_zip_bytes(
        package,
        max_entries=MAX_PACKAGE_FILES,
        max_central_directory_bytes=MAX_PACKAGE_CENTRAL_DIRECTORY_BYTES,
        max_expanded_bytes=MAX_PACKAGE_TOTAL_BYTES,
        max_member_bytes=MAX_PACKAGE_FILE_BYTES,
    )

    def validate(rel: str, size: int) -> None:
        nonlocal count, total_bytes
        count += 1
        total_bytes += size
        if count > MAX_PACKAGE_FILES:
            raise ValueError(f"package contains more than {MAX_PACKAGE_FILES} files")
        if size > MAX_PACKAGE_FILE_BYTES:
            raise ValueError(f"package member exceeds {MAX_PACKAGE_FILE_BYTES} bytes: {rel}")
        if total_bytes > MAX_PACKAGE_TOTAL_BYTES:
            raise ValueError(f"package exceeds {MAX_PACKAGE_TOTAL_BYTES} uncompressed bytes")

    try:
        with zipfile.ZipFile(io.BytesIO(package)) as zf:
            infos = zf.infolist()
            members = [info for info in infos if not info.is_dir()]
            if (
                len(infos) != preflight.entries
                or len(members) != preflight.members
                or sum(info.file_size for info in members) != preflight.expanded_bytes
            ):
                raise ValueError("package central directory changed while opening")
            for info in infos:
                if not info.is_dir():
                    validate(info.filename, info.file_size)
                    if info.file_size and (
                        info.compress_size == 0
                        or info.file_size / max(info.compress_size, 1) > MAX_ZIP_COMPRESSION_RATIO
                    ):
                        raise ValueError(f"package member has suspicious compression ratio: {info.filename}")
                    yield info.filename, zf.read(info)
    except zipfile.BadZipFile as exc:
        raise ValueError("package review accepts only a regular ZIP package") from exc


def _pread_exact(fd: int, offset: int, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        chunk = os.pread(fd, length - len(chunks), offset + len(chunks))
        if not chunk:
            break
        chunks.extend(chunk)
    return bytes(chunks)


def _read_package_snapshot(package: Path) -> bytes:
    """Open one regular archive descriptor and return its bounded immutable byte snapshot."""
    try:
        before = os.lstat(package)
    except FileNotFoundError:
        raise FileNotFoundError(f"package not found: {package}") from None
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError("package review accepts only a regular ZIP package")
    try:
        fd = os.open(package, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise ValueError("package review accepts only a regular ZIP package") from exc
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size > MAX_PACKAGE_ARCHIVE_BYTES
        ):
            raise ValueError("package review accepts only a bounded regular ZIP package")
        # Preflight the exact descriptor before reading its contents or allowing
        # ZipFile to allocate an unbounded central-directory inventory.
        _preflight_zip_central_directory(
            lambda offset, length: _pread_exact(fd, offset, length),
            opened.st_size,
            max_entries=MAX_PACKAGE_FILES,
            max_central_directory_bytes=MAX_PACKAGE_CENTRAL_DIRECTORY_BYTES,
            max_expanded_bytes=MAX_PACKAGE_TOTAL_BYTES,
            max_member_bytes=MAX_PACKAGE_FILE_BYTES,
        )
        data = bytearray()
        remaining = MAX_PACKAGE_ARCHIVE_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            data.extend(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        if (
            after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_size != opened.st_size
            or len(data) != opened.st_size
            or len(data) > MAX_PACKAGE_ARCHIVE_BYTES
        ):
            raise ValueError("package changed while being read")
        return bytes(data)
    finally:
        os.close(fd)


def clean_line(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def words(value: str) -> set[str]:
    return {
        word
        for word in re.split(r"[^a-z0-9]+", value.lower())
        if len(word) >= 3 and word not in GENERIC_WORDS
    }


def line_snippets(text: str, terms: tuple[str, ...], limit_per_file: int) -> list[dict]:
    snippets = []
    lines = text.splitlines()
    for idx, line in enumerate(lines, start=1):
        matched = [term for term in terms if term.lower() in line.lower()]
        if not matched:
            continue
        snippets.append({"line": idx, "terms": matched, "text": clean_line(line)[:500]})
        if len(snippets) >= limit_per_file:
            break
    return snippets


def _pattern_matches(value: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.search(value) for pattern in patterns)


def package_family(package: Path, files: list[dict[str, Any]]) -> dict[str, Any]:
    package_text = f"{package.name} {package}"
    command_name_matches = _pattern_matches(package_text, COMMAND_PACKAGE_PATTERNS)
    sdk_name_matches = _pattern_matches(package_text, SDK_PACKAGE_PATTERNS)
    command_markers = []
    sdk_markers = []
    for file_info in files:
        rel = file_info["path"]
        text = f"{rel}\n{file_info.get('_text') or ''}"
        if _pattern_matches(text, COMMAND_PACKAGE_PATTERNS):
            command_markers.append(rel)
        if _pattern_matches(text, SDK_PACKAGE_PATTERNS):
            sdk_markers.append(rel)

    command_matches = bool(command_name_matches or command_markers)
    sdk_matches = bool(sdk_name_matches or sdk_markers)
    if sdk_name_matches and not command_name_matches:
        family = "camera_remote_sdk"
        ok_for_command_review = False
        reject_reason = "package appears to be Sony Camera Remote SDK / CRSDK, not Camera Remote Command"
    elif command_matches:
        family = "camera_remote_command"
        ok_for_command_review = True
        reject_reason = None
    elif sdk_matches:
        family = "camera_remote_sdk"
        ok_for_command_review = False
        reject_reason = "package appears to be Sony Camera Remote SDK / CRSDK, not Camera Remote Command"
    else:
        family = "unknown"
        ok_for_command_review = False
        reject_reason = "package does not contain a verifiable Camera Remote Command identity marker"

    return {
        "family": family,
        "ok_for_command_review": ok_for_command_review,
        "reject_reason": reject_reason,
        "command_name_matches": command_name_matches,
        "sdk_name_matches": sdk_name_matches,
        "command_marker_files": command_markers[:20],
        "sdk_marker_files": sdk_markers[:20],
    }


def compatibility_snippets(text: str, rel: str, limit_per_file: int) -> list[dict[str, Any]]:
    snippets = []
    for idx, line in enumerate(text.splitlines(), start=1):
        clean = clean_line(line)
        if "ilce-7m2" not in clean.lower():
            continue
        snippets.append({"path": rel, "line": idx, "text": clean[:500]})
        if len(snippets) >= limit_per_file:
            break
    return snippets


def command_route_rows(registry: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    registry = registry or settings_registry.load_registry()
    return [row for row in registry["settings"] if "camera_remote_command" in row.get("routes", {})]


def row_terms(row: dict[str, Any]) -> list[str]:
    candidates = {
        row["label"],
        row["setting_id"],
        row["setting_id"].split(".", 1)[1].replace("_", " "),
    }
    candidates.update(ROW_ALIASES.get(row["setting_id"], ()))
    out = []
    seen = set()
    for candidate in candidates:
        candidate = clean_line(str(candidate))
        key = candidate.lower()
        if not candidate or key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return sorted(out, key=lambda item: (len(item), item.lower()), reverse=True)


def line_matches_row(line: str, row: dict[str, Any]) -> tuple[bool, list[str]]:
    lower = line.lower()
    compact_line = compact(line)
    matched = []
    for term in row_terms(row):
        term_lower = term.lower()
        term_compact = compact(term)
        if term_lower in lower or (len(term_compact) >= 6 and term_compact in compact_line):
            matched.append(term)
    if matched:
        return True, matched

    row_words = words(row["label"]) | words(row["setting_id"].split(".", 1)[1])
    overlap = sorted(row_words & words(line))
    if len(overlap) >= 2:
        return True, overlap
    return False, []


def map_registry_evidence(
    text_files: list[dict[str, Any]],
    *,
    registry: dict[str, Any] | None = None,
    setting_ids: set[str] | None = None,
    limit_per_setting: int = 8,
) -> list[dict[str, Any]]:
    rows = command_route_rows(registry)
    if setting_ids is not None:
        rows = [row for row in rows if row["setting_id"] in setting_ids]
    package_mentions_model = any("ilce-7m2" in (file_info.get("_text") or "").lower() for file_info in text_files)
    mapped = []
    for row in rows:
        evidence = []
        matched_terms = set()
        for file_info in text_files:
            text = file_info.get("_text") or ""
            if not text:
                continue
            for idx, line in enumerate(text.splitlines(), start=1):
                ok, terms = line_matches_row(line, row)
                if not ok:
                    continue
                matched_terms.update(terms)
                model_on_same_line = "ilce-7m2" in line.lower()
                evidence.append(
                    {
                        "path": file_info["path"],
                        "line": idx,
                        "matched_terms": terms,
                        "ilce_7m2_on_same_line": model_on_same_line,
                        "text": clean_line(line)[:500],
                    }
                )
                if len(evidence) >= limit_per_setting:
                    break
            if len(evidence) >= limit_per_setting:
                break
        model_scoped_evidence_count = sum(1 for item in evidence if item["ilce_7m2_on_same_line"])
        if model_scoped_evidence_count:
            verdict = "candidate_model_scoped_evidence"
        elif evidence and package_mentions_model:
            verdict = "candidate_command_evidence_model_unscoped"
        elif evidence:
            verdict = "candidate_command_evidence_no_model"
        else:
            verdict = "needs_manual_review"
        mapped.append(
            {
                "setting_id": row["setting_id"],
                "label": row["label"],
                "registry_status": row["status"],
                "route_status": row["routes"]["camera_remote_command"].get("status"),
                "matched_terms": sorted(matched_terms),
                "evidence": evidence,
                "evidence_count": len(evidence),
                "model_scoped_evidence_count": model_scoped_evidence_count,
                "package_mentions_ilce_7m2": package_mentions_model,
                "verdict": verdict,
                "manual_confirmation_required": True,
                "next_action": row["next_action"],
            }
        )
    return mapped


def build_report(
    package: Path,
    *,
    out_dir: Path,
    limit_per_file: int = 20,
    setting_ids: list[str] | None = None,
    registry: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    package = package.expanduser()
    snapshot = _read_package_snapshot(package)
    # The snapshot is the only package content this review consumes. Keep a lexical display path
    # so a replacement/symlink after the descriptor read cannot influence later review steps.
    package = package.absolute()
    registry = registry or settings_registry.load_registry()
    selected_setting_ids = None
    if setting_ids:
        selected_setting_ids = set()
        for setting_id in setting_ids:
            row = settings_registry.get_setting(setting_id, registry)
            if "camera_remote_command" not in row.get("routes", {}):
                raise ValueError(f"{setting_id} has no camera_remote_command route")
            selected_setting_ids.add(setting_id)

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    report_path = out_dir / f"{stamp}-command-package-review.json"

    files = []
    term_counts = {term: 0 for term in TERMS}
    compatibility_rows = []
    office_budget = OfficeExtractionBudget()
    for rel, data in iter_package_files(snapshot):
        text, extraction_method, extraction_error = extract_text(rel, data, office_budget=office_budget)
        snippets = line_snippets(text, TERMS, limit_per_file) if text else []
        if text:
            compatibility_rows.extend(compatibility_snippets(text, rel, limit_per_file))
        for snippet in snippets:
            for term in snippet["terms"]:
                term_counts[term] += 1
        files.append(
            {
                "path": rel,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "text_like": text is not None,
                "text_extraction": extraction_method,
                "text_extraction_error": extraction_error,
                "snippets": snippets,
                "_text": text or "",
            }
        )

    family = package_family(package, files)
    if not family["ok_for_command_review"]:
        raise ValueError(family["reject_reason"])

    registry_mapping = map_registry_evidence(
        files,
        registry=registry,
        setting_ids=selected_setting_ids,
    )
    files_for_report = [{key: value for key, value in file_info.items() if key != "_text"} for file_info in files]
    package_hash = hashlib.sha256(snapshot).hexdigest()
    report = {
        "timestamp": stamp,
        "package": str(package),
        "package_kind": "zip_file",
        "package_sha256": package_hash,
        "package_hash_kind": "file_sha256",
        "package_family": family,
        "official_sources": list(OFFICIAL_SOURCES),
        "policy": {
            "downloads_package": False,
            "copies_package": False,
            "redistributes_package": False,
            "records_hashes_and_short_snippets_only": True,
        },
        "terms": list(TERMS),
        "term_counts": term_counts,
        "files": files_for_report,
        "compatibility_rows": compatibility_rows[: limit_per_file * 4],
        "registry_mapping_summary": {
            "camera_remote_command_rows": len(registry_mapping),
            "mapped_candidate_rows": sum(1 for row in registry_mapping if row["evidence_count"] > 0),
            "model_scoped_candidate_rows": sum(
                1 for row in registry_mapping if row["model_scoped_evidence_count"] > 0
            ),
            "unscoped_candidate_rows": sum(
                1
                for row in registry_mapping
                if row["evidence_count"] > 0 and row["model_scoped_evidence_count"] == 0
            ),
            "package_mentions_ilce_7m2": term_counts["ILCE-7M2"] > 0,
            "filtered_setting_ids": sorted(selected_setting_ids or []),
        },
        "registry_mapping": registry_mapping,
        "next_review": [
            "Find README interface compatibility row for ILCE-7M2.",
            "Treat only model_scoped_candidate_rows as automated ILCE-7M2 candidates; manually inspect matrix layouts that separate model headers from command rows.",
            "Confirm each candidate command is supported on ILCE-7M2, not merely mentioned elsewhere in the package.",
            "For candidate rows, run behavior proof before changing registry status to proven_set.",
            "Do not commit the Sony package itself unless the license explicitly allows redistribution.",
        ],
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report, report_path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", help="local regular ZIP package")
    parser.add_argument("--out-dir", default="artifacts/falsification/r4_camera_remote_command")
    parser.add_argument("--limit-per-file", type=int, default=20)
    parser.add_argument("--setting", action="append", default=[], help="limit registry mapping to one setting_id; repeatable")
    args = parser.parse_args(argv)

    try:
        report, report_path = build_report(
            Path(args.package),
            out_dir=Path(args.out_dir),
            limit_per_file=args.limit_per_file,
            setting_ids=args.setting,
        )
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "report": str(report_path),
                "term_counts": report["term_counts"],
                "registry_mapping_summary": report["registry_mapping_summary"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
