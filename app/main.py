"""FLIM update tool: web front end.

Run locally:   uvicorn app.main:app --reload
Environment:   COMTRADE_API_KEY, DESTATIS_TOKEN (or DESTATIS_USER + DESTATIS_PASSWORD),
               FRED_API_KEY, optional APP_PASSWORD (+ APP_USERNAME) for basic auth.
Run with ONE worker process: jobs live in memory.
"""
from __future__ import annotations

import base64
import datetime as dt
import logging
import os
import re
import secrets
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import fetch_prices

from .inputs import PLATFORM_LABELS, PLATFORMS, parse_workbook
from .pipeline import Job, JobRegistry, credential_hint, credentials_present

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
EXAMPLE_INPUT = ROOT / "voorbeeld_invoer.xlsx"
MAX_UPLOAD = 5 * 1024 * 1024

log = logging.getLogger("flim_update")
app = FastAPI(title="FLIM update tool", docs_url=None, redoc_url=None)
registry = JobRegistry()
uploads: dict = {}   # input_id -> (created, ParsedInput, filename)


# ------------------------------------------------------------------ access control
@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """HTTP basic auth when APP_PASSWORD is set (health check stays open)."""
    password = os.environ.get("APP_PASSWORD")
    if not password or request.url.path == "/healthz":
        return await call_next(request)
    user = os.environ.get("APP_USERNAME", "hva")
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            given_user, _, given_pw = base64.b64decode(header[6:]).decode().partition(":")
        except Exception:  # noqa: BLE001
            given_user = given_pw = ""
        if (secrets.compare_digest(given_user.encode(), user.encode())
                and secrets.compare_digest(given_pw.encode(), password.encode())):
            return await call_next(request)
    return Response("Inloggen vereist", status_code=401,
                    headers={"WWW-Authenticate": 'Basic realm="FLIM update tool"'})


if not os.environ.get("APP_PASSWORD"):
    log.warning("APP_PASSWORD is not set: the tool is open to anyone who has the URL.")


# ------------------------------------------------------------------ pages / info
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/config")
def config():
    today = dt.date.today()
    return {
        "credentials": {p: {"label": PLATFORM_LABELS[p], "present": credentials_present(p),
                            "hint": credential_hint(p)} for p in PLATFORMS},
        "auth_enabled": bool(os.environ.get("APP_PASSWORD")),
        "defaults": {
            "comtrade_period": str(today.year - 2),
            "comtrade_reporter": "528",
            "prices_start": "2015-01",
            "prices_end": "",
            "destatis_table": fetch_prices.DESTATIS_DEFAULT_TABLE,
        },
    }


@app.get("/api/template")
def template():
    if not EXAMPLE_INPUT.exists():
        raise HTTPException(404, "voorbeeld_invoer.xlsx ontbreekt op de server")
    return FileResponse(EXAMPLE_INPUT, filename="voorbeeld_invoer.xlsx")


# ------------------------------------------------------------------ input
@app.post("/api/inputs")
async def upload_input(file: UploadFile = File(...)):
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Bestand is groter dan 5 MB.")
    parsed = parse_workbook(data)
    input_id = uuid.uuid4().hex[:12]
    now = time.time()
    for key in [k for k, v in uploads.items() if now - v[0] > 24 * 3600]:
        del uploads[key]
    uploads[input_id] = (now, parsed, file.filename)
    return dict(parsed.to_dict(), input_id=input_id, filename=file.filename)


# ------------------------------------------------------------------ jobs
class JobRequest(BaseModel):
    input_id: str
    platforms: list[str] = Field(default_factory=lambda: list(PLATFORMS))
    comtrade_period: str = "2024"
    comtrade_reporter: str = "528"
    prices_start: str = "2015-01"
    prices_end: str = ""
    destatis_table: str = ""


def _check_settings(req: JobRequest):
    problems = []
    if not re.fullmatch(r"\d{4}", req.comtrade_period.strip()):
        problems.append("Comtrade-jaar moet vier cijfers zijn, bv. 2024.")
    if not re.fullmatch(r"\d{1,3}", req.comtrade_reporter.strip()):
        problems.append("Rapporteur moet een landcode in cijfers zijn (528 = Nederland).")
    for label, value in (("Startmaand", req.prices_start), ("Eindmaand", req.prices_end)):
        if value.strip() and not re.fullmatch(r"\d{4}-\d{2}", value.strip()):
            problems.append("%s moet de vorm JJJJ-MM hebben." % label)
    if not req.prices_start.strip():
        problems.append("Startmaand is verplicht (ook de basismaand voor normalisatie).")
    bad = [p for p in req.platforms if p not in PLATFORMS]
    if bad or not req.platforms:
        problems.append("Kies minstens één platform.")
    return problems


@app.post("/api/jobs")
def start_job(req: JobRequest):
    entry = uploads.get(req.input_id)
    if entry is None:
        raise HTTPException(404, "Invoerbestand niet (meer) bekend; upload het opnieuw.")
    parsed = entry[1]
    if parsed.errors:
        raise HTTPException(400, "Het invoerbestand bevat fouten; los die eerst op.")
    problems = _check_settings(req)
    if problems:
        raise HTTPException(400, " ".join(problems))
    running = registry.running()
    if running:
        raise HTTPException(409, "Er loopt al een update (gestart om %s). Wacht tot die klaar is."
                            % dt.datetime.fromtimestamp(running.created).strftime("%H:%M"))
    settings = {
        "invoerbestand": entry[2],
        "platforms": list(dict.fromkeys(req.platforms)),
        "comtrade_period": req.comtrade_period.strip(),
        "comtrade_reporter": req.comtrade_reporter.strip(),
        "comtrade_batch": 5,
        "prices_start": req.prices_start.strip(),
        "prices_end": req.prices_end.strip(),
        "destatis_table": req.destatis_table.strip(),
    }
    job = Job(parsed, settings)
    registry.start(job)
    return {"job_id": job.id}


@app.get("/api/jobs")
def list_jobs():
    return [{"id": j.id, "state": j.state, "created": j.created,
             "file": j.settings.get("invoerbestand"),
             "outputs": list(j.outputs)} for j in registry.list()]


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, log_from: int = 0):
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(404, "Onbekende run (de server is misschien herstart).")
    return JSONResponse(job.snapshot(log_from))


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(404, "Onbekende run.")
    job.cancel_requested = True
    return {"ok": True}


@app.get("/api/jobs/{job_id}/files/{key}")
def download(job_id: str, key: str):
    job = registry.get(job_id)
    if job is None or key not in job.outputs:
        raise HTTPException(404, "Bestand niet gevonden.")
    out = job.outputs[key]
    return Response(
        out["data"],
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="%s"' % out["filename"]})
