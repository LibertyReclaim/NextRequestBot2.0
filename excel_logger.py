"""Excel IO helpers for loading municipalities and writing tracking results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List

import pandas as pd


REQUIRED_COLUMNS = ["municipality", "type", "portal_url"]
TRACKING_COLUMNS = [
    "status",
    "request_number",
    "request number",
    "submitted_at",
    "failed",
    "records_received",
    "notes",
    "screenshot_path",
]


@dataclass
class MunicipalityRow:
    index: int
    municipality: str
    state: str
    portal_url: str


class ExcelLogger:
    """Reads input workbook and writes tracked statuses back to the same workbook."""

    def __init__(self, input_path: str) -> None:
        self.input_path = input_path

        # Read everything as strings to avoid float64 write crashes later.
        self.df = pd.read_excel(input_path, dtype=str)
        self._ensure_required_and_tracking_columns()
        self._normalize_text_columns()

        # Explicitly keep status as string dtype.
        self.df["status"] = self.df["status"].astype(str)

    def _ensure_required_and_tracking_columns(self) -> None:
        for col in REQUIRED_COLUMNS + TRACKING_COLUMNS:
            if col not in self.df.columns:
                self.df[col] = ""

        # Keep both request number styles in sync.
        if self.df["request_number"].eq("").all() and not self.df["request number"].eq("").all():
            self.df["request_number"] = self.df["request number"]
        elif self.df["request number"].eq("").all() and not self.df["request_number"].eq("").all():
            self.df["request number"] = self.df["request_number"]

    def _normalize_text_columns(self) -> None:
        # Convert all columns to string-safe values and remove NaN-like text.
        for col in self.df.columns:
            self.df[col] = self.df[col].fillna("").astype(str)
            self.df[col] = self.df[col].replace({"nan": "", "NaN": "", "None": ""})

    @staticmethod
    def _clean_text(value) -> str:
        if value is None:
            return ""
        if isinstance(value, float) and pd.isna(value):
            return ""
        text = str(value)
        return "" if text.lower() in {"nan", "none"} else text

    def iter_rows(self) -> List[MunicipalityRow]:
        rows: List[MunicipalityRow] = []
        for idx, row in self.df.iterrows():
            portal_url = self._clean_text(row.get("portal_url", "")).strip()
            if not portal_url:
                continue
            rows.append(
                MunicipalityRow(
                    index=idx,
                    municipality=self._clean_text(row.get("municipality", "")).strip(),
                    state=self._clean_text(row.get("state", "")).strip(),
                    portal_url=portal_url,
                )
            )
        return rows

    def log_result(self, row_index: int, result: Dict) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()

        status = self._clean_text(result.get("status", ""))
        request_number = self._clean_text(result.get("request_number", ""))

        # Force string writes so dtype coercion never fails.
        self.df.at[row_index, "status"] = status
        self.df.at[row_index, "request_number"] = request_number
        self.df.at[row_index, "request number"] = request_number
        self.df.at[row_index, "submitted_at"] = self._clean_text(result.get("submitted_at", now_iso))
        self.df.at[row_index, "failed"] = self._clean_text(result.get("failed", ""))
        self.df.at[row_index, "records_received"] = self._clean_text(result.get("records_received", ""))
        self.df.at[row_index, "notes"] = self._clean_text(result.get("notes", ""))
        self.df.at[row_index, "screenshot_path"] = self._clean_text(result.get("screenshot_path", ""))

    def save(self) -> None:
        # Write back to the same input workbook as requested.
        self.df.to_excel(self.input_path, index=False)
