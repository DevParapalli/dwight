import csv
import itertools
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import openpyxl

SUPPORTED_SUFFIXES = {".csv", ".xlsx"}


def iter_raw_rows(path: Path) -> Iterator[list]:
    """One streaming pass over a source file's raw rows. Re-reads from disk on
    each call rather than caching in memory -- simple, and cheap at the file
    sizes this pipeline deals with (profiling makes one pass, cleaning another)."""
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(newline="") as f:
            yield from csv.reader(f)
    elif suffix == ".xlsx":
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb.active
            for row in ws.iter_rows(values_only=True):
                yield list(row)
        finally:
            wb.close()
    else:
        raise ValueError(f"unsupported source file type: {suffix!r}")


def detect_header_row(preview_rows: list[list], max_scan: int = 10) -> int:
    """The header is the first row (within the first max_scan) with more than
    one non-empty cell. Junk banner rows above a real header are single-cell;
    a real header spans every column."""
    for i, row in enumerate(preview_rows[:max_scan]):
        non_empty = sum(1 for c in row if c not in (None, ""))
        if non_empty > 1:
            return i
    return 0


@dataclass
class SourceTable:
    path: Path
    filename: str
    source_type: str
    columns: list[str]
    header_row_index: int

    def rows(self) -> Iterator[dict]:
        for i, row in enumerate(iter_raw_rows(self.path)):
            if i <= self.header_row_index:
                continue
            yield dict(zip(self.columns, row))


def load_source_table(path: Path) -> SourceTable:
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise ValueError(f"unsupported source file type: {path.suffix!r}")

    raw = iter_raw_rows(path)
    preview = list(itertools.islice(raw, 10))
    raw.close()

    header_idx = detect_header_row(preview)
    columns = [str(c).strip() if c is not None else "" for c in preview[header_idx]]
    source_type = "csv" if path.suffix.lower() == ".csv" else "xlsx"
    return SourceTable(
        path=path, filename=path.name, source_type=source_type,
        columns=columns, header_row_index=header_idx,
    )
