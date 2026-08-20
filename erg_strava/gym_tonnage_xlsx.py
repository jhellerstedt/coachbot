"""Parse tonnage.xlsx-style gym spreadsheets (stdlib only — no openpyxl)."""

from __future__ import annotations

import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

_XLSX_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

# Spreadsheet column keys → display names used in gym plans.
DEFAULT_TONNAGE_XLSX_EXERCISES: Tuple[Tuple[str, str], ...] = (
    ("hex", "Hex-bar deadlift"),
    ("lats", "Lat pull-down"),
    ("squat", "Back squat"),
    ("bench", "Bench press"),
    ("bulg", "Bulgarian split squat"),
    ("arms", "Arms"),
)


@dataclass(frozen=True)
class TonnageXlsxSession:
    session_date: date
    total_tonnage_kg: float
    hex_max_kg: float
    exercise_tonnage_kg: Dict[str, float]
    body_weight_kg: Optional[float] = None

    def exercises_for_import(
        self,
        labels: Sequence[Tuple[str, str]] = DEFAULT_TONNAGE_XLSX_EXERCISES,
    ) -> List[Tuple[str, float, Optional[float]]]:
        out: List[Tuple[str, float, Optional[float]]] = []
        for key, name in labels:
            tonnage = self.exercise_tonnage_kg.get(key)
            if tonnage is None or tonnage <= 0:
                continue
            max_w = self.hex_max_kg if key == "hex" else None
            out.append((name, float(tonnage), max_w))
        return out


def excel_serial_to_date(serial: float) -> date:
    return date(1899, 12, 30) + timedelta(days=int(float(serial)))


def _read_xlsx_rows(path: Path) -> List[List[str]]:
    with zipfile.ZipFile(path) as zf:
        shared: List[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root:
                shared.append(
                    "".join(
                        (node.text or "")
                        for node in si.iter()
                        if node.tag.endswith("}t")
                    )
                )
        sheet = ET.fromstring(zf.read("xl/worksheets/sheet1.xml"))
        rows: List[List[str]] = []
        for row in sheet.findall(".//m:sheetData/m:row", _XLSX_NS):
            vals: List[str] = []
            for cell in row.findall("m:c", _XLSX_NS):
                ref_type = cell.get("t")
                value = cell.find("m:v", _XLSX_NS)
                if value is None or value.text is None:
                    continue
                raw = value.text
                if ref_type == "s":
                    raw = shared[int(raw)]
                vals.append(raw)
            if vals:
                rows.append(vals)
        return rows


def _parse_data_row(vals: Sequence[str]) -> TonnageXlsxSession:
    if len(vals) == 10:
        body_weight_kg = float(vals[1])
        total_tonnage_kg = float(vals[2])
        hex_max_kg = float(vals[3])
        tonnage_vals = vals[4:10]
    elif len(vals) == 9:
        body_weight_kg = None
        total_tonnage_kg = float(vals[1])
        hex_max_kg = float(vals[2])
        tonnage_vals = vals[3:9]
    else:
        raise ValueError(
            f"Expected 9 or 10 columns after date, got {len(vals)}: {list(vals)}"
        )

    keys = [key for key, _ in DEFAULT_TONNAGE_XLSX_EXERCISES]
    if len(tonnage_vals) != len(keys):
        raise ValueError(
            f"Expected {len(keys)} exercise tonnage columns, got {len(tonnage_vals)}"
        )

    exercise_tonnage_kg = {
        key: float(value) for key, value in zip(keys, tonnage_vals)
    }
    return TonnageXlsxSession(
        session_date=excel_serial_to_date(vals[0]),
        body_weight_kg=body_weight_kg,
        total_tonnage_kg=total_tonnage_kg,
        hex_max_kg=hex_max_kg,
        exercise_tonnage_kg=exercise_tonnage_kg,
    )


def parse_tonnage_xlsx(path: Path) -> List[TonnageXlsxSession]:
    """
    Parse a tonnage workbook.

    Columns (with optional body-weight row):
    date | [body_weight] | total | hex_max | hex_tonnage | lats | box_squat |
    bench | bulgarians | arms
    """
    rows = _read_xlsx_rows(path)
    if not rows:
        return []
    header = [cell.strip().lower() for cell in rows[0]]
    if header[0] != "date":
        raise ValueError(f"First column must be 'date', got {rows[0]!r}")

    sessions: List[TonnageXlsxSession] = []
    for vals in rows[1:]:
        if not vals or not str(vals[0]).strip():
            continue
        sessions.append(_parse_data_row(vals))
    sessions.sort(key=lambda s: s.session_date)
    return sessions
