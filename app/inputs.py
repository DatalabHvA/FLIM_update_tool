"""Read and check the input workbook (codes + readable names per platform).

Expected layout: one workbook with a sheet per platform. The sheet name decides
the platform ("Comtrade", "Destatis", "FRED"; case does not matter, "UN Comtrade"
also works). Column headers are matched loosely, see COLUMN_ALIASES.

    Comtrade:  Materiaal | Code | Omschrijving (optional)
    Destatis:  ID (optional) | Materiaal | Code | Tabel (optional)
    FRED:      ID (optional) | Materiaal | Code
    Eurostat:  ID (optional) | Materiaal | Code (NACE, e.g. C25) | Land (optional)
    CBS:       ID (optional) | Materiaal | Code (ProdCom/CPA, e.g. 237) | Prijs (optional)

Comtrade rows feed material_market_share.xlsx, the price sheets (CBS, Destatis,
FRED, Eurostat) feed prijzen.xlsx (ID = FLIM material id, written in row 1 of the price sheet).
"""
from __future__ import annotations

import io
import re
from dataclasses import asdict, dataclass, field
from typing import Optional

import openpyxl

PLATFORMS = ("comtrade", "cbs", "destatis", "fred", "eurostat")
PRICE_PLATFORMS = ("cbs", "destatis", "fred", "eurostat")
# CBS price types (column "Prijs"); "verbruik" = imports + domestic sales = Dutch market price
CBS_MARKETS = ("verbruik", "invoer", "binnenland", "buitenland", "afzet")
# GP 2019 code levels in Destatis table 61241-0004 (digits after "GP19", dashes ignored)
DESTATIS_DIGIT_LEVELS = (2, 3, 4, 5, 6, 9)
PLATFORM_LABELS = {"comtrade": "UN Comtrade", "cbs": "CBS", "destatis": "Destatis",
                   "fred": "FRED", "eurostat": "Eurostat"}

COLUMN_ALIASES = {
    "name": ("materiaal", "materiaal2", "naam", "name", "material", "materiaalnaam"),
    "code": ("code", "hs-code", "hs code", "hscode", "series id", "series_id",
             "reeks", "gp-code", "gp code"),
    "id": ("id", "flim id", "flim-id", "materiaal id"),
    "table": ("tabel", "table", "destatis tabel"),
    "geo": ("land", "geo", "country", "regio"),
    "market": ("prijs", "prijssoort", "markt", "price type"),
    "description": ("omschrijving", "naam code", "description", "beschrijving"),
}


@dataclass
class Row:
    platform: str
    code: str
    name: str
    id: Optional[str] = None
    table: Optional[str] = None
    geo: Optional[str] = None
    market: Optional[str] = None
    description: Optional[str] = None
    sheet_row: int = 0


@dataclass
class ParsedInput:
    rows: list = field(default_factory=list)
    errors: list = field(default_factory=list)      # block the run
    warnings: list = field(default_factory=list)    # shown, do not block

    def by_platform(self, platform):
        return [r for r in self.rows if r.platform == platform]

    def to_dict(self):
        return {
            "rows": [asdict(r) for r in self.rows],
            "errors": self.errors,
            "warnings": self.warnings,
            "counts": {p: len(self.by_platform(p)) for p in PLATFORMS},
        }


def clean(value) -> str:
    """Cell value -> trimmed text; non-breaking spaces become normal spaces."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    text = str(value).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _platform_of(sheet_name: str) -> Optional[str]:
    low = sheet_name.lower()
    for p in PLATFORMS:
        if p in low:
            return p
    return None


def _header_map(header_cells) -> dict:
    """{field: column index} for the recognised headers."""
    found = {}
    for idx, cell in enumerate(header_cells):
        text = clean(cell).lower()
        for fld, aliases in COLUMN_ALIASES.items():
            if fld not in found and text in aliases:
                found[fld] = idx
    return found


def _normalise_code(platform, raw, cell_value, where, warnings):
    code = raw.replace(" ", "")
    if platform == "comtrade":
        if not code.isdigit():
            warnings.append("%s: HS-code %r bevat andere tekens dan cijfers." % (where, raw))
        elif isinstance(cell_value, (int, float)) and len(code) % 2 == 1:
            # Excel dropped the leading zero of e.g. 0901 -> 901
            code = "0" + code
            warnings.append("%s: HS-code %s stond als getal in Excel; aangevuld tot %s."
                            % (where, raw, code))
        elif len(code) not in (2, 4, 6):
            warnings.append("%s: HS-code %s heeft %d cijfers (verwacht 2, 4 of 6)."
                            % (where, code, len(code)))
    elif platform == "destatis":
        code = code.upper()
        digits = len(re.sub(r"\D", "", code[4:])) if code.startswith("GP19") else 0
        if not re.fullmatch(r"GP19-[\d-]*\d", code):
            warnings.append("%s: Destatis-code %r lijkt niet op GP19-<cijfers>." % (where, raw))
        elif digits not in DESTATIS_DIGIT_LEVELS:
            warnings.append("%s: Destatis-code %s heeft %d cijfers; Destatis kent codes van %s "
                            "cijfers." % (where, code, digits,
                                          ", ".join(map(str, DESTATIS_DIGIT_LEVELS))))
    elif platform == "cbs":
        if not re.fullmatch(r"\d{2,8}", code):
            warnings.append("%s: CBS-code %r lijkt niet op een ProdCom/CPA-code (2-8 cijfers, "
                            "bv. 237 of 2331)." % (where, raw))
        elif isinstance(cell_value, (int, float)) and len(code) in (3, 7):
            warnings.append("%s: CBS-code %s stond als getal in Excel; controleer of er geen "
                            "voorloopnul (bv. 0811) is weggevallen." % (where, code))
    elif platform == "eurostat":
        code = code.upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9_-]*", code):
            warnings.append("%s: Eurostat-code %r lijkt niet op een NACE-code (bv. C25)."
                            % (where, raw))
    elif platform == "fred":
        code = code.upper()
        if not re.fullmatch(r"[A-Z0-9_]+", code):
            warnings.append("%s: FRED-reeks %r bevat onverwachte tekens." % (where, raw))
    return code


def _market(platform, value, where, warnings):
    """CBS price type from column 'Prijs' (None = default 'verbruik')."""
    if platform != "cbs" or not value:
        return None
    low = value.lower().replace("prijzen", "").replace("prijs", "").strip()
    for name in CBS_MARKETS:
        if low.startswith(name):
            return name
    warnings.append("%s: prijssoort %r onbekend; 'verbruik' gebruikt (kies uit %s)."
                    % (where, value, ", ".join(CBS_MARKETS)))
    return None


def parse_workbook(data: bytes) -> ParsedInput:
    out = ParsedInput()
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001 - any parse problem is a user error here
        out.errors.append("Kan het bestand niet lezen als Excel (.xlsx): %s" % exc)
        return out

    recognised = []
    for ws in wb.worksheets:
        platform = _platform_of(ws.title)
        if platform is None:
            continue
        recognised.append(ws.title)
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            out.warnings.append("Blad '%s' is leeg." % ws.title)
            continue
        cols = _header_map(rows[0])
        missing = [f for f in ("name", "code") if f not in cols]
        if missing:
            out.errors.append(
                "Blad '%s': kolom(men) %s niet gevonden in de eerste rij. Gevonden koppen: %s"
                % (ws.title, " en ".join({"name": "'Materiaal'", "code": "'Code'"}[m]
                                         for m in missing),
                   [clean(c) for c in rows[0] if clean(c)]))
            continue

        def get(row, fld):
            i = cols.get(fld)
            return row[i] if i is not None and i < len(row) else None

        for n, row in enumerate(rows[1:], start=2):
            name, code_raw = clean(get(row, "name")), clean(get(row, "code"))
            if not name and not code_raw:
                continue
            where = "%s rij %d" % (ws.title, n)
            if not code_raw:
                out.warnings.append("%s: '%s' heeft geen code en wordt overgeslagen."
                                    % (where, name))
                continue
            if not name:
                out.warnings.append("%s: code %s heeft geen naam; de code wordt als naam "
                                    "gebruikt." % (where, code_raw))
                name = code_raw
            code = _normalise_code(platform, code_raw, get(row, "code"), where, out.warnings)
            out.rows.append(Row(
                platform=platform, code=code, name=name,
                id=clean(get(row, "id")) or None,
                table=clean(get(row, "table")) or None,
                geo=clean(get(row, "geo")).upper() or None,
                market=_market(platform, clean(get(row, "market")), where, out.warnings),
                description=clean(get(row, "description")) or None,
                sheet_row=n))
    sheet_names = wb.sheetnames
    wb.close()

    if not recognised:
        out.errors.append(
            "Geen bladen gevonden met de naam Comtrade, CBS, Destatis, FRED of Eurostat. Gebruik het "
            "voorbeeldbestand of hernoem de bladen (bladen in dit bestand: %s)."
            % ", ".join(sheet_names))
    elif not out.rows and not out.errors:
        out.errors.append("Er staan geen codes in het bestand.")
    _check_duplicates(out)
    return out


def _check_duplicates(out: ParsedInput):
    users = {}
    for r in out.rows:
        key = (r.platform, r.code, r.table or "", r.geo or "", r.market or "")
        users.setdefault(key, []).append(r.name)
    for (platform, code, *_), names in users.items():
        if len(names) > 1:
            out.warnings.append(
                "%s: code %s wordt door %d materialen gebruikt (%s); zij krijgen dezelfde reeks."
                % (PLATFORM_LABELS[platform], code, len(names), ", ".join(names)))

    names = {}
    for r in out.by_platform("comtrade"):
        names.setdefault(r.name, []).append(r.code)
    for name, codes in names.items():
        if len(set(codes)) > 1:
            out.warnings.append(
                "Comtrade: '%s' heeft meerdere codes (%s). De importen worden opgeteld tot "
                "een marktaandeel voor dit materiaal. Bedoel je aparte materialen, geef ze "
                "dan een andere naam." % (name, ", ".join(codes)))

    ids = {}
    for r in [r for r in out.rows if r.platform in PRICE_PLATFORMS]:
        if r.id:
            ids.setdefault(r.id, []).append(r.name)
    for id_, used in ids.items():
        if len(used) > 1:
            out.warnings.append("Prijzen: ID %s wordt gebruikt door meerdere reeksen (%s)."
                                % (id_, ", ".join(used)))
    missing_id = [r.name for r in [r for r in out.rows if r.platform in PRICE_PLATFORMS]
                  if not r.id]
    if missing_id:
        out.warnings.append("Prijzen: %d reeks(en) zonder FLIM-ID (%s). Rij 1 van "
                            "prijzen.xlsx blijft daar leeg."
                            % (len(missing_id), ", ".join(missing_id[:5])
                               + (" ..." if len(missing_id) > 5 else "")))
