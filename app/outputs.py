"""Turn fetched data into the Excel files the FLIM tool reads."""
from __future__ import annotations

import io

import openpyxl
import pandas as pd
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

BOLD = Font(bold=True)
HEADER_FILL = PatternFill("solid", fgColor="DDEBF7")


def market_shares(imports: pd.DataFrame, code_to_names: dict) -> pd.DataFrame:
    """Import value per partner -> share per material.

    imports: columns code, partnerISO, primaryValue (from comtrade_imports.get_imports).
    code_to_names: {code: [material name, ...]}. Codes with the same material name are
    summed before the share is taken, so the shares of one material add up to 1.
    Returns the layout of material_market_share.xlsx: Materiaal2, partnerISO, market_share.
    """
    columns = ["Materiaal2", "partnerISO", "market_share"]
    if imports is None or imports.empty:
        return pd.DataFrame(columns=columns)
    parts = []
    for code, names in code_to_names.items():
        sub = imports[imports["code"] == code]
        for name in names:
            parts.append(sub.assign(Materiaal2=name))
    if not parts:
        return pd.DataFrame(columns=columns)
    df = pd.concat(parts, ignore_index=True)
    df = df.groupby(["Materiaal2", "partnerISO"], as_index=False, sort=False)["primaryValue"].sum()
    df = df[df["primaryValue"] > 0]
    totals = df.groupby("Materiaal2")["primaryValue"].transform("sum")
    df["market_share"] = df["primaryValue"] / totals
    order = list(dict.fromkeys(n for names in code_to_names.values() for n in names))
    df["_order"] = df["Materiaal2"].map({n: i for i, n in enumerate(order)})
    df = df.sort_values(["_order", "market_share"], ascending=[True, False])
    return df[columns].reset_index(drop=True)


def market_share_xlsx(shares: pd.DataFrame) -> bytes:
    """Same layout as the existing file: unnamed index column + 3 columns, 'Sheet1'."""
    buf = io.BytesIO()
    shares.to_excel(buf, sheet_name="Sheet1", index=True)
    return buf.getvalue()


def normalise(series: pd.Series, base_month: pd.Timestamp):
    """series / value in base_month. Returns (normalised series, base used, note)."""
    s = series.dropna()
    if s.empty:
        return series * float("nan"), None, "geen waarden"
    if base_month in s.index and s.loc[base_month] != 0:
        return series / s.loc[base_month], base_month, ""
    first = s.index.min()
    return (series / s.loc[first], first,
            "basismaand %s ontbreekt; genormaliseerd op %s"
            % (base_month.strftime("%Y-%m"), first.strftime("%Y-%m")))


def prices_xlsx(columns: list, base_month: pd.Timestamp, sources: list) -> bytes:
    """Build prijzen.xlsx.

    columns: list of dicts {id, name, series (raw, monthly index), normalised}.
    sources: list of dicts for the 'Bronnen' sheet (one per input row, incl. failures).
    Sheet 'Datasheet': row 1 = ID, row 2 = name, column A = month, values relative to
    the base month (base month = 1), like the existing FLIM price sheet.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Datasheet"
    _write_matrix(ws, columns, "normalised")
    _write_matrix(wb.create_sheet("Ruwe indices"), columns, "series")

    src = wb.create_sheet("Bronnen")
    headers = ["ID", "Materiaal", "Bron", "Code", "Tabel", "Status", "Eerste maand",
               "Laatste maand", "Aantal maanden", "Basismaand", "Melding"]
    src.append(headers)
    for s in sources:   # keys: header in lower case, spaces -> "_"
        src.append([s.get(h.lower().replace(" ", "_")) for h in headers])
    _style_header(src, 1)
    src.append([])
    src.append(["Genormaliseerd op %s (= 1)." % base_month.strftime("%Y-%m")])
    _autowidth(src)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _write_matrix(ws, columns, key):
    ws.cell(1, 1, "ID")
    ws.cell(2, 1, "Jaar")
    index = pd.DatetimeIndex([])
    for col in columns:
        index = index.union(col[key].dropna().index)
    for j, col in enumerate(columns, start=2):
        id_ = col["id"]
        ws.cell(1, j, int(id_) if id_ and str(id_).isdigit() else id_)
        ws.cell(2, j, col["name"])
    for i, month in enumerate(index, start=3):
        c = ws.cell(i, 1, month.to_pydatetime())
        c.number_format = "yyyy-mm"
        for j, col in enumerate(columns, start=2):
            value = col[key].get(month)
            if value is not None and pd.notna(value):
                ws.cell(i, j, float(value))
    _style_header(ws, 1)
    _style_header(ws, 2)
    ws.freeze_panes = "B3"
    ws.column_dimensions["A"].width = 10
    for j in range(2, len(columns) + 2):
        ws.column_dimensions[get_column_letter(j)].width = 18


def report_xlsx(items: list, settings: dict, log_lines: list) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Rapport"
    headers = ["Platform", "Code", "Materiaal", "ID", "Status", "Melding"]
    ws.append(headers)
    for it in items:
        ws.append([it["platform_label"], it["code"], it["name"], it.get("id"),
                   it["status_label"], it.get("message") or ""])
    _style_header(ws, 1)
    _autowidth(ws, max_width=90)

    st = wb.create_sheet("Instellingen")
    for k, v in settings.items():
        st.append([k, str(v)])
    _autowidth(st)

    lg = wb.create_sheet("Log")
    lg.append(["Tijd", "Niveau", "Bericht"])
    for line in log_lines:
        lg.append([line["t"], line["level"], line["msg"]])
    _style_header(lg, 1)
    lg.column_dimensions["C"].width = 140

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _style_header(ws, row):
    for cell in ws[row]:
        if cell.value is not None:
            cell.font = BOLD
            cell.fill = HEADER_FILL


def _autowidth(ws, max_width=60):
    for col in ws.columns:
        width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
        ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 8), max_width)
