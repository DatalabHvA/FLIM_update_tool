"""End-to-end tests with the API calls replaced by fakes (no network, no keys needed)."""
import io
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
    assert up["counts"] == {"comtrade": 4, "destatis": 2, "fred": 2}
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
                        lambda *a, **k: "GP19M6")
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
