"""End-to-end tests with the API calls replaced by fakes (no network, no keys needed)."""
import io
import json
import time

import openpyxl
import pandas as pd
import pytest
from fastapi.testclient import TestClient

import comtrade_imports
import fetch_prices
from app import main
from app.inputs import parse_workbook


def make_input(comtrade=(), destatis=(), fred=()):
    wb = openpyxl.Workbook()
    wb.active.title = "Uitleg"
    for title, headers, rows in (("Comtrade", ["Materiaal\xa0", "Code"], comtrade),
                                 ("Destatis", ["ID", "Materiaal", "Code"], destatis),
                                 ("FRED", ["ID", "Materiaal", "Code"], fred)):
        ws = wb.create_sheet(title)
        ws.append(headers)
        for r in rows:
            ws.append(list(r))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def monthly(code, start="2015-01", n=24, first=100.0):
    idx = pd.date_range(start, periods=n, freq="MS")
    return pd.Series([first + i for i in range(n)], index=idx, name=code)


@pytest.fixture
def client(monkeypatch):
    for k in ("COMTRADE_API_KEY", "FRED_API_KEY", "DESTATIS_TOKEN"):
        monkeypatch.setenv(k, "test-key")
    monkeypatch.delenv("APP_PASSWORD", raising=False)

    def fake_imports(codes, *, log=None, on_progress=None, **kw):
        on_progress(0, len(codes), {})
        rows, failed = [], {}
        for c in codes:
            if c == "9999":
                failed[c] = "HTTP 400: bad code"
            elif c == "1111":
                pass   # no data
            else:
                rows += [{"code": c, "partnerISO": "DEU", "primaryValue": 300.0},
                         {"code": c, "partnerISO": "BEL", "primaryValue": 100.0}]
            log("requesting %s" % c)
        on_progress(len(codes), len(codes), failed)
        data = pd.DataFrame(rows, columns=["code", "partnerISO", "primaryValue"])
        return comtrade_imports.ImportResult(data, ["1111"] if "1111" in codes else [],
                                             failed, len(codes))

    def fake_prices(kind):
        def fn(codes, start, end, *, log=None, on_code=None, **kw):
            cols, failed = [], {}
            for c in codes:
                on_code(c, "running", None)
                if c.endswith("BAD"):
                    failed[c] = "%s error for %s" % (kind, c)
                    on_code(c, "failed", failed[c])
                else:
                    s = monthly(c, start="2016-01" if c == "LATE" else "2015-01")
                    cols.append(s)
                    on_code(c, "ok", s)
            df = pd.concat(cols, axis=1) if cols else pd.DataFrame()
            df.attrs["failed"] = failed
            return df
        return fn

    monkeypatch.setattr(comtrade_imports, "get_imports", fake_imports)
    monkeypatch.setattr(fetch_prices, "get_destatis_prices", fake_prices("destatis"))
    monkeypatch.setattr(fetch_prices, "get_fred_prices", fake_prices("fred"))
    main.registry.jobs.clear()
    return TestClient(main.app)


def wait(client, job_id):
    for _ in range(100):
        snap = client.get("/api/jobs/%s" % job_id).json()
        if snap["state"] not in ("queued", "running"):
            return snap
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_parse_cleans_names_and_pads_codes():
    data = make_input(comtrade=[("Schuim\xa0algemeen\xa0", "3921\xa0"), ("Koffie", 901),
                                ("Leeg", None)])
    parsed = parse_workbook(data)
    assert not parsed.errors
    assert [(r.name, r.code) for r in parsed.rows] == [("Schuim algemeen", "3921"),
                                                      ("Koffie", "0901")]
    assert any("aangevuld" in w for w in parsed.warnings)
    assert any("geen code" in w for w in parsed.warnings)


def test_parse_rejects_unknown_layout():
    wb = openpyxl.Workbook()
    wb.active.append(["foo", "bar"])
    buf = io.BytesIO()
    wb.save(buf)
    parsed = parse_workbook(buf.getvalue())
    assert parsed.errors and "Comtrade" in parsed.errors[0]
    assert parse_workbook(b"not excel").errors


def test_full_run(client):
    data = make_input(
        comtrade=[("Hout", "4407"), ("Hout", "4412"), ("Steen", "9999"), ("Niks", "1111")],
        destatis=[(5, "Hout index", "GP19-1"), (6, "Kapot", "GP19-BAD")],
        fred=[(24, "Textiel", "PCU1"), (25, "Laat", "LATE")])
    up = client.post("/api/inputs", files={"file": ("in.xlsx", data)}).json()
    assert up["counts"] == {"comtrade": 4, "cbs": 0, "destatis": 2, "fred": 2, "eurostat": 0}
    r = client.post("/api/jobs", json={"input_id": up["input_id"], "prices_start": "2015-01"})
    assert r.status_code == 200, r.text
    snap = wait(client, r.json()["job_id"])
    assert snap["state"] == "done", snap["error"]

    status = {(i["platform"], i["code"]): i["status"] for i in snap["items"]}
    assert status[("comtrade", "4407")] == "ok"
    assert status[("comtrade", "9999")] == "failed"
    assert status[("comtrade", "1111")] == "nodata"
    assert status[("destatis", "GP19-BAD")] == "failed"
    assert snap["platforms"]["comtrade"]["state"] == "partial"
    assert set(snap["outputs"]) == {"market_share", "prijzen", "rapport"}

    # market share: Hout = 4407 + 4412 summed -> DEU 0.75, BEL 0.25
    ms = pd.read_excel(io.BytesIO(client.get(
        "/api/jobs/%s/files/market_share" % snap["id"]).content), index_col=0)
    assert list(ms.columns) == ["Materiaal2", "partnerISO", "market_share"]
    hout = ms[ms["Materiaal2"] == "Hout"].set_index("partnerISO")["market_share"]
    assert hout["DEU"] == pytest.approx(0.75) and hout["BEL"] == pytest.approx(0.25)
    assert set(ms["Materiaal2"]) == {"Hout"}

    # prices: row 1 ids, row 2 names, normalised to 2015-01
    wb = openpyxl.load_workbook(io.BytesIO(client.get(
        "/api/jobs/%s/files/prijzen" % snap["id"]).content))
    ws = wb["Datasheet"]
    assert [c.value for c in ws[1]][:4] == ["ID", 5, 24, 25]
    assert [c.value for c in ws[2]][:4] == ["Jaar", "Hout index", "Textiel", "Laat"]
    assert ws["B3"].value == pytest.approx(1.0)
    assert ws["B4"].value == pytest.approx(101 / 100)
    assert {"Ruwe indices", "Bronnen"} <= set(wb.sheetnames)
    late = [i for i in snap["items"] if i["code"] == "LATE"][0]
    assert "basismaand" in late["message"]


def test_missing_key_is_reported(client, monkeypatch):
    monkeypatch.delenv("FRED_API_KEY")
    data = make_input(fred=[(1, "A", "PCU1")])
    up = client.post("/api/inputs", files={"file": ("in.xlsx", data)}).json()
    job = client.post("/api/jobs", json={"input_id": up["input_id"]}).json()["job_id"]
    snap = wait(client, job)
    assert snap["items"][0]["status"] == "failed"
    assert "FRED_API_KEY" in snap["items"][0]["message"]
    assert snap["platforms"]["fred"]["state"] == "error"
    assert "prijzen" not in snap["outputs"]


def test_bad_settings_and_auth(client, monkeypatch):
    up = client.post("/api/inputs", files={"file": ("in.xlsx", make_input(
        comtrade=[("A", "4407")]))}).json()
    r = client.post("/api/jobs", json={"input_id": up["input_id"], "comtrade_period": "24"})
    assert r.status_code == 400
    monkeypatch.setenv("APP_PASSWORD", "geheim")
    assert client.get("/api/config").status_code == 401
    assert client.get("/api/config", auth=("hva", "geheim")).status_code == 200
    assert client.get("/healthz").status_code == 200


def test_real_price_functions_with_log_hooks(monkeypatch):
    """The real get_destatis_prices / get_fred_prices with log= and on_code= (HTTP faked)."""
    monkeypatch.setenv("DESTATIS_TOKEN", "t")
    monkeypatch.setenv("FRED_API_KEY", "k")
    monkeypatch.setattr(fetch_prices, "_resolve_classifying_variable",
                        lambda *a, **k: (lambda code: ("GP19M6", code)))
    monkeypatch.setattr(fetch_prices, "_download_destatis_ffcsv", lambda code, **k: "csv")
    monkeypatch.setattr(fetch_prices, "parse_destatis_ffcsv",
                        lambda text, *, code, **k: (monthly(code), {}))

    class FakeResp:
        status_code = 200

        def json(self):
            return {"observations": [{"date": "2015-01-01", "value": "100"},
                                     {"date": "2015-02-01", "value": "101"}]}

    monkeypatch.setattr(fetch_prices, "_request", lambda *a, **k: FakeResp())

    lines, events = [], []
    on_code = lambda code, status, detail: events.append((code, status))  # noqa: E731
    df = fetch_prices.get_destatis_prices(["GP19-1"], log=lines.append, on_code=on_code)
    assert list(df.columns) == ["GP19-1"] and not df.attrs["failed"]
    df = fetch_prices.get_fred_prices(["PCU1"], log=lines.append, on_code=on_code)
    assert list(df.columns) == ["PCU1"] and not df.attrs["failed"]
    assert events == [("GP19-1", "running"), ("GP19-1", "ok"),
                      ("PCU1", "running"), ("PCU1", "ok")]
    assert any("GP19-1" in line for line in lines)


# Structure of statistic 61241 as GENESIS returns it (checked live): the table metadata only
# names GP19M2, the statistic has one variable per level and the 9-digit level is GP19N1.
DESTATIS_VARIABLES = [("GP19M2", "GP2019 (2-digit codes): Industrial products"),
                      ("GP19M3", "GP2019 (3-digit codes): Industrial products"),
                      ("GP19M4", "GP2019 (4-digit codes): Industrial products"),
                      ("GP19M6", "GP2019 (6-digit codes): Industrial products"),
                      ("GP19N1", "GP2019 (selected 9-digit codes): Industr. Products"),
                      ("GP19N2", "GP2019 (special items): Industrial products")]
DESTATIS_VALUES = {"GP19M2": ["GP19-23"], "GP19M3": ["GP19-237"],
                   "GP19M4": ["GP19-0811", "GP19-0501"], "GP19M6": ["GP19-237011", "GP19-237012"],
                   "GP19N1": ["GP19-233110002", "GP19--052010000", "GP19-2312121001"],
                   "GP19N2": ["GP19-05-01"]}


def _run_destatis_with(monkeypatch, post, codes):
    """get_destatis_prices with fake metadata and a fake _post_destatis (catalogue calls).
    Returns ({input code: (variable, code sent)}, result DataFrame)."""
    monkeypatch.setenv("DESTATIS_TOKEN", "t")
    for cache in (fetch_prices._META_CACHE, fetch_prices._STAT_VARS_CACHE,
                  fetch_prices._VALUES_CACHE):
        cache.clear()
    meta = {"Object": {"Structure": {"Rows": [{"Code": "GP19M2"}]}}}
    monkeypatch.setattr(fetch_prices, "_fetch_metadata", lambda *a, **k: meta)
    monkeypatch.setattr(fetch_prices, "_post_destatis", post)
    used, sent = {}, []

    def fake_download(code, *, classifying_variable, **k):
        sent.append((classifying_variable, code))
        return "csv"

    def fake_parse(text, *, code, **k):
        return monthly(code), {}

    monkeypatch.setattr(fetch_prices, "_download_destatis_ffcsv", fake_download)
    monkeypatch.setattr(fetch_prices, "parse_destatis_ffcsv", fake_parse)
    df = fetch_prices.get_destatis_prices(codes, table="61241-0004", log=lambda m: None)
    ok = [c for c in codes if c not in df.attrs["failed"]]
    used = dict(zip(ok, sent))
    for cache in (fetch_prices._META_CACHE, fetch_prices._STAT_VARS_CACHE,
                  fetch_prices._VALUES_CACHE):
        cache.clear()
    return used, df


def _fake_catalogue(calls=None):
    def post(path, params, headers, base_url):
        if calls is not None:
            calls.append((path, params["name"]))
        if path == "catalogue/variables2statistic":
            assert params["name"] == "61241"
            return json.dumps({"List": [{"Code": c, "Content": d}
                                        for c, d in DESTATIS_VARIABLES]})
        if path == "catalogue/values2variable":
            return json.dumps({"List": [{"Code": c} for c in DESTATIS_VALUES[params["name"]]]})
        raise AssertionError(path)
    return post


def test_destatis_codes_looked_up_in_value_lists(monkeypatch):
    """Each code gets the variable that holds it; '-' vs '--' does not matter; the column
    keeps the code as given; unknown codes fail before any data is requested."""
    calls = []
    codes = ["GP19-23", "GP19-237", "GP19-0811", "GP19-237011", "GP19-233110002",
             "GP19-052010000", "gp19-2312121001", "GP19-237013"]
    used, df = _run_destatis_with(monkeypatch, _fake_catalogue(calls), codes)
    assert used == {"GP19-23": ("GP19M2", "GP19-23"), "GP19-237": ("GP19M3", "GP19-237"),
                    "GP19-0811": ("GP19M4", "GP19-0811"),
                    "GP19-237011": ("GP19M6", "GP19-237011"),
                    "GP19-233110002": ("GP19N1", "GP19-233110002"),
                    "GP19-052010000": ("GP19N1", "GP19--052010000"),
                    "gp19-2312121001": ("GP19N1", "GP19-2312121001")}
    assert "GP19-052010000" in df.columns            # input spelling kept in the output
    assert list(df.attrs["failed"]) == ["GP19-237013"]
    msg = df.attrs["failed"]["GP19-237013"]
    assert "does not exist" in msg and "GP19-237011" in msg and "GP19-237012" in msg
    # catalogue read once per run: 1 statistic + 6 variables
    assert len(calls) == 7


def test_destatis_dash_ambiguity_prefers_digit_level(monkeypatch):
    """GP19-0501 (4-digit level) and special item GP19-05-01 look the same without dashes."""
    used, df = _run_destatis_with(monkeypatch, _fake_catalogue(), ["GP19-0501", "GP19-05-01"])
    assert used == {"GP19-0501": ("GP19M4", "GP19-0501"),
                    "GP19-05-01": ("GP19N2", "GP19-05-01")}


def test_destatis_levels_by_digits_without_catalogue(monkeypatch):
    def post(*a, **k):
        raise fetch_prices.DestatisError("HTTP 404")

    used, df = _run_destatis_with(monkeypatch, post, [
        "GP19-23", "GP19-237", "GP19-0811", "GP19-237011", "GP19-233110002", "GP19-2312121001"])
    assert {c: v for c, (v, _) in used.items()} == {
        "GP19-23": "GP19M2", "GP19-237": "GP19M3", "GP19-0811": "GP19M4",
        "GP19-237011": "GP19M6", "GP19-233110002": "GP19N1", "GP19-2312121001": "GP19N1"}


def test_gp_digits():
    assert fetch_prices.gp_digits("GP19-0811") == 4
    assert fetch_prices.gp_digits("gp19-233110002") == 9
    assert fetch_prices.gp_digits("GP19-23--31") == 4
    assert fetch_prices.gp_digits("PCU1") is None


class _EurostatResp:
    def __init__(self, payload, status=200):
        self.payload, self.status_code, self.text = payload, status, str(payload)

    def json(self):
        return self.payload


def _jsonstat(code, geo, values, months=("2015-01", "2015-02", "2015-03")):
    """Minimal JSON-stat answer: one series, time as the last dimension."""
    ids = ["freq", "nace_r2", "unit", "geo", "time"]
    sizes = [1, 1 if code else 0, 1, 1, len(months)]
    cat = lambda keys: {"category": {"index": {k: i for i, k in enumerate(keys)}}}  # noqa: E731
    return {"id": ids, "size": sizes, "value": values,
            "dimension": {"freq": cat(["M"]), "nace_r2": cat([code] if code else []),
                          "unit": cat(["I21"]), "geo": cat([geo]), "time": cat(months)}}


def test_eurostat_parsing(monkeypatch):
    calls = []

    def fake_request(method, url, params=None, **kw):
        calls.append((url, dict(params)))
        if params["nace_r2"] == "C999":
            return _EurostatResp(_jsonstat(None, params["geo"], {}))
        # value 2015-02 missing (Eurostat leaves gaps out of the value dict)
        return _EurostatResp(_jsonstat(params["nace_r2"], params["geo"], {"0": 100.0, "2": 102.0}))

    monkeypatch.setattr(fetch_prices, "_request", fake_request)
    df = fetch_prices.get_eurostat_prices(["C25", "C999"], start="2015-01", geo="NL",
                                          log=lambda m: None)
    assert list(df.columns) == ["C25"]
    assert df["C25"].iloc[0] == 100.0 and pd.isna(df["C25"].iloc[1]) and df["C25"].iloc[2] == 102.0
    assert "does not know" in df.attrs["failed"]["C999"]
    url, params = calls[0]
    assert url.endswith("/sts_inppd_m")
    assert params["geo"] == "NL" and params["unit"] == "I21" and params["sinceTimePeriod"] == "2015-01"


def test_eurostat_in_app_grouped_by_country(client, monkeypatch):
    seen = []

    def fake_eurostat(codes, start, end, *, geo, log=None, on_code=None, **kw):
        seen.append((geo, list(codes)))
        cols = []
        for c in codes:
            on_code(c, "running", None)
            s = monthly(c, first=50.0 if geo == "DE" else 100.0)
            cols.append(s)
            on_code(c, "ok", s)
        df = pd.concat(cols, axis=1)
        df.attrs["failed"] = {}
        return df

    monkeypatch.setattr(fetch_prices, "get_eurostat_prices", fake_eurostat)
    wb = openpyxl.load_workbook(io.BytesIO(make_input(fred=[(1, "A", "PCU1")])))
    ws = wb.create_sheet("Eurostat")
    ws.append(["ID", "Materiaal", "Code", "Land"])
    ws.append([84, "Steen", "c237", None])
    ws.append([86, "Cement", "C2351", "de"])
    ws.append([87, "Cement NL", "C2351", "NL"])
    buf = io.BytesIO()
    wb.save(buf)
    up = client.post("/api/inputs", files={"file": ("in.xlsx", buf.getvalue())}).json()
    assert up["counts"]["eurostat"] == 3 and not up["errors"]
    job = client.post("/api/jobs", json={"input_id": up["input_id"]}).json()["job_id"]
    snap = wait(client, job)
    assert snap["state"] == "done", snap["error"]
    assert sorted(seen) == [("DE", ["C2351"]), ("EU27_2020", ["C237"]), ("NL", ["C2351"])]
    eu = {i["name"]: i["status"] for i in snap["items"] if i["platform"] == "eurostat"}
    assert eu == {"Steen": "ok", "Cement": "ok", "Cement NL": "ok"}
    wb = openpyxl.load_workbook(io.BytesIO(client.get(
        "/api/jobs/%s/files/prijzen" % snap["id"]).content))
    assert [c.value for c in wb["Datasheet"][1]] == ["ID", 1, 84, 86, 87]
    bronnen = [r for r in wb["Bronnen"].iter_rows(min_row=2, values_only=True) if r[0]]
    assert ("sts_inppd_m, DE") in [r[4] for r in bronnen]


def test_cbs_chains_old_table_in_front(monkeypatch):
    """New table from 2018 at level 100, old table from 2015 at level 50: old part doubled."""
    new = pd.Series([100.0, 102.0, 104.0],
                    index=pd.date_range("2018-01", periods=3, freq="MS"))
    old = pd.Series([40.0, 45.0, 50.0, 51.0, 52.0],
                    index=pd.to_datetime(["2015-01-01", "2016-01-01", "2018-01-01",
                                          "2018-02-01", "2018-03-01"]))
    tables = {"NEW": {"237": new}, "OLD": {"237": old, "0811": None}}
    monkeypatch.setattr(fetch_prices, "_cbs_table_series",
                        lambda table, code, market: tables[table].get(code))
    df = fetch_prices.get_cbs_prices(["237", "0811", "9999"], start="2015-01",
                                     tables=("NEW", "OLD"), log=lambda m: None)
    s = df["237"].dropna()
    factor = (100 + 102 + 104) / (50 + 51 + 52)
    assert s.loc["2015-01-01"] == pytest.approx(40 * factor)
    assert s.loc["2018-01-01"] == 100.0           # the newest table wins where it has data
    assert "2015-01 t/m 2016-01" in df.attrs["notes"]["237"]
    assert set(df.attrs["failed"]) == {"0811", "9999"}
    assert "staat niet in CBS-tabel" in df.attrs["failed"]["9999"]


def test_cbs_price_type_column():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "CBS"
    ws.append(["ID", "Materiaal", "Prijs", "Code"])
    ws.append([1, "A", "Invoerprijzen", "237"])
    ws.append([2, "B", None, "2331"])
    ws.append([3, "C", "onzin", "2351"])
    buf = io.BytesIO()
    wb.save(buf)
    parsed = parse_workbook(buf.getvalue())
    assert [r.market for r in parsed.rows] == ["invoer", None, None]
    assert any("onzin" in w for w in parsed.warnings)


def test_manual_page_linked_from_main_page(client):
    page = client.get("/handleiding")
    assert page.status_code == 200 and "Handleiding FLIM update tool" in page.text
    assert 'href="/handleiding"' in client.get("/").text
