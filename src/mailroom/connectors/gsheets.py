"""Google Sheets recap.

One row per triaged email, appended to a single tab. The header row is written
once and then reused, so the sheet stays filter/pivot friendly.
"""
from __future__ import annotations

from typing import Any, Sequence

from . import google_auth


def _svc():
    return google_auth.service("sheets", "v4")


def ensure_tab(spreadsheet_id: str, tab: str, header: Sequence[str]) -> None:
    svc = _svc()
    meta = svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    titles = {s["properties"]["title"] for s in meta.get("sheets", [])}
    if tab not in titles:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": tab}}}]},
        ).execute()
    first = svc.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id, range=f"{tab}!A1:Z1"
    ).execute().get("values", [])
    if not first:
        svc.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id, range=f"{tab}!A1",
            valueInputOption="RAW", body={"values": [list(header)]},
        ).execute()


def append_rows(spreadsheet_id: str, tab: str, rows: list[list[Any]]) -> int:
    if not rows:
        return 0
    resp = _svc().spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{tab}!A1",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()
    return resp.get("updates", {}).get("updatedRows", 0)
