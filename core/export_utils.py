"""Reusable export helpers for the warehouse Digital Twin dashboards."""
from __future__ import annotations

from io import BytesIO
import pandas as pd


def workbook_bytes(sheets: dict[str, pd.DataFrame]) -> bytes:
    """Build one Excel workbook containing every supplied output table."""
    out = BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        used = set()
        for raw_name, df in sheets.items():
            if df is None:
                continue
            name = str(raw_name)[:31] or "Output"
            base = name
            i = 1
            while name in used:
                suffix = f"_{i}"
                name = (base[: 31 - len(suffix)] + suffix)
                i += 1
            used.add(name)
            if isinstance(df, pd.Series):
                df = df.to_frame()
            elif not isinstance(df, pd.DataFrame):
                df = pd.DataFrame(df)
            df.to_excel(writer, sheet_name=name, index=False)
            ws = writer.book[name]
            ws.freeze_panes = "A2"
            for column_cells in ws.columns:
                max_len = min(max(len(str(cell.value)) if cell.value is not None else 0 for cell in column_cells) + 2, 45)
                ws.column_dimensions[column_cells[0].column_letter].width = max_len
    return out.getvalue()
