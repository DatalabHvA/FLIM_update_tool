"""Runs one update: fetch all codes, build the output files, record progress.

A Job holds everything the web page shows (per-code status, per-platform
progress, log lines, output files). It is filled from a background thread and
read by the HTTP handlers; all access goes through Job.lock.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
import traceback
import uuid
from typing import Optional

import pandas as pd

import comtrade_imports
import fetch_prices

from . import outputs
from .inputs import PLATFORM_LABELS, PLATFORMS, ParsedInput

STATUS_LABELS = {
    "pending": "wacht", "running": "bezig", "fetched": "opgehaald", "ok": "gelukt",
    "nodata": "geen data", "failed": "mislukt", "skipped": "overgeslagen",
}

CREDENTIALS = {
    "comtrade": (("COMTRADE_API_KEY",),),
    "destatis": (("DESTATIS_TOKEN",), ("DESTATIS_USER", "DESTATIS_PASSWORD")),
    "fred": (("FRED_API_KEY",),),
}


def credentials_present(platform: str) -> bool:
    """True if one of the accepted variable sets for the platform is complete."""
    return any(all(os.environ.get(v) for v in group) for group in CREDENTIALS[platform])


def credential_hint(platform: str) -> str:
    return " of ".join(" + ".join(g) for g in CREDENTIALS[platform])


class Cancelled(Exception):
    pass


class Job:
    def __init__(self, parsed: ParsedInput, settings: dict):
        self.id = uuid.uuid4().hex[:12]
        self.lock = threading.RLock()
        self.settings = settings
        self.state = "queued"           # queued, running, done, error
        self.error: Optional[str] = None
        self.created = time.time()
        self.started: Optional[float] = None
        self.finished: Optional[float] = None
        self.cancel_requested = False
        self.log_lines: list = []
        self.outputs: dict = {}         # key -> {filename, data, summary}
        self.items = []
        for r in parsed.rows:
            if r.platform not in settings["platforms"]:
                status, msg = "skipped", "platform niet geselecteerd"
            else:
                status, msg = "pending", ""
            self.items.append({
                "platform": r.platform, "platform_label": PLATFORM_LABELS[r.platform],
                "code": r.code, "name": r.name, "id": r.id, "table": r.table,
                "status": status, "status_label": STATUS_LABELS[status], "message": msg,
                "detail": "",
            })
        self.platform_state = {
            p: {"label": PLATFORM_LABELS[p], "state": "skipped" if p not in settings["platforms"]
                else ("pending" if self._items(p) else "empty"),
                "total": len(self._items(p)), "message": ""}
            for p in PLATFORMS}

    # ----------------------------------------------------------------- helpers
    def _items(self, platform, code=None, table=None):
        return [it for it in self.items if it["platform"] == platform
                and (code is None or it["code"] == code)
                and (table is None or (it["table"] or "") == table)]

    def log(self, msg: str, level: str = "info"):
        with self.lock:
            self.log_lines.append({"t": dt.datetime.now().strftime("%H:%M:%S"),
                                   "level": level, "msg": str(msg)})
        if self.cancel_requested:
            raise Cancelled()

    def set_status(self, platform, code, status, message=None, detail=None, table=None):
        with self.lock:
            for it in self._items(platform, code, table):
                it["status"] = status
                it["status_label"] = STATUS_LABELS[status]
                if message is not None:
                    it["message"] = message
                if detail is not None:
                    it["detail"] = detail

    def set_platform(self, platform, state, message=None):
        with self.lock:
            self.platform_state[platform]["state"] = state
            if message is not None:
                self.platform_state[platform]["message"] = message

    def snapshot(self, log_from: int = 0) -> dict:
        with self.lock:
            platforms = {}
            for p, ps in self.platform_state.items():
                its = self._items(p)
                counts = {s: 0 for s in STATUS_LABELS}
                for it in its:
                    counts[it["status"]] += 1
                done = counts["ok"] + counts["nodata"] + counts["failed"] + counts["fetched"]
                platforms[p] = dict(ps, counts=counts, done=done)
            return {
                "id": self.id, "state": self.state, "error": self.error,
                "settings": self.settings,
                "created": self.created, "started": self.started, "finished": self.finished,
                "elapsed": ((self.finished or time.time()) - self.started) if self.started else 0,
                "cancel_requested": self.cancel_requested,
                "platforms": platforms,
                "items": [{k: v for k, v in it.items()} for it in self.items],
                "log": self.log_lines[log_from:], "log_total": len(self.log_lines),
                "outputs": {k: {"filename": v["filename"], "summary": v["summary"]}
                            for k, v in self.outputs.items()},
            }


class _LogBridge(logging.Handler):
    """Sends warnings of the fetch_prices logger to the job log."""

    def __init__(self, job):
        super().__init__(level=logging.INFO)
        self.job = job

    def emit(self, record):
        try:
            self.job.log(record.getMessage(), "warn" if record.levelno >= logging.WARNING
                         else "info")
        except Cancelled:
            pass


def run_job(job: Job):
    bridge = _LogBridge(job)
    logger = logging.getLogger("fetch_prices")
    logger.addHandler(bridge)
    logger.setLevel(logging.INFO)
    with job.lock:
        job.state = "running"
        job.started = time.time()
    try:
        job.log("Start update (platforms: %s)" % ", ".join(
            PLATFORM_LABELS[p] for p in job.settings["platforms"]))
        imports = None
        price_cols = {}
        if "comtrade" in job.settings["platforms"] and job._items("comtrade"):
            imports = _run_comtrade(job)
        for p in ("destatis", "fred"):
            if p in job.settings["platforms"] and job._items(p):
                price_cols.update(_run_prices(job, p))
        _build_outputs(job, imports, price_cols)
        with job.lock:
            job.state = "done"
        job.log("Klaar.")
    except Cancelled:
        with job.lock:
            job.state = "error"
            job.error = "Afgebroken door gebruiker."
            for it in job.items:
                if it["status"] in ("pending", "running"):
                    it["status"], it["status_label"] = "skipped", STATUS_LABELS["skipped"]
                    it["message"] = "afgebroken"
            for ps in job.platform_state.values():
                if ps["state"] in ("pending", "running"):
                    ps["state"] = "error"
    except Exception as exc:  # noqa: BLE001 - shown to the user, run must not die silently
        with job.lock:
            job.state = "error"
            job.error = "Onverwachte fout: %s" % exc
            job.log_lines.append({"t": dt.datetime.now().strftime("%H:%M:%S"),
                                  "level": "error", "msg": traceback.format_exc()})
    finally:
        logger.removeHandler(bridge)
        with job.lock:
            job.finished = time.time()


# ---------------------------------------------------------------------- Comtrade
def _run_comtrade(job: Job):
    p = "comtrade"
    items = job._items(p)
    codes = list(dict.fromkeys(it["code"] for it in items))
    if not credentials_present(p):
        msg = "API-sleutel ontbreekt (%s)" % credential_hint(p)
        for c in codes:
            job.set_status(p, c, "failed", msg)
        job.set_platform(p, "error", msg)
        job.log("Comtrade: " + msg, "error")
        return None

    job.set_platform(p, "running")
    period = job.settings["comtrade_period"]
    job.log("Comtrade: %d codes, jaar %s, rapporteur %s"
            % (len(codes), period, job.settings["comtrade_reporter"]))

    def on_progress(done, total, failed):
        for c in codes[:done]:
            if c in failed:
                job.set_status(p, c, "failed", failed[c])
            else:
                job.set_status(p, c, "fetched", "")
        for c in codes[done:done + int(job.settings.get("comtrade_batch", 5))]:
            job.set_status(p, c, "running")
        if job.cancel_requested:
            raise Cancelled()

    try:
        res = comtrade_imports.get_imports(
            codes, period=period, reporter=job.settings["comtrade_reporter"],
            batch_size=int(job.settings.get("comtrade_batch", 5)),
            log=job.log, on_progress=on_progress)
    except comtrade_imports.ComtradeError as exc:
        for c in codes:
            job.set_status(p, c, "failed", str(exc))
        job.set_platform(p, "error", str(exc))
        job.log("Comtrade gestopt: %s" % exc, "error")
        return None

    counts = res.data.groupby("code").size().to_dict() if not res.data.empty else {}
    for c in codes:
        if c in res.failed:
            job.set_status(p, c, "failed", res.failed[c])
        elif c in counts:
            job.set_status(p, c, "ok", "%d partnerlanden" % counts[c])
        else:
            job.set_status(p, c, "nodata", "Comtrade gaf 0 rijen voor jaar %s" % period)
    n_bad = len(res.failed)
    job.set_platform(p, "done" if not n_bad else "partial",
                     "%d verzoeken, %d codes met data, %d zonder data, %d mislukt"
                     % (res.requests, len(counts), len(res.no_data), n_bad))
    return res.data


# ---------------------------------------------------------------------- Prices
def _run_prices(job: Job, p: str) -> dict:
    """Returns {(platform, code, table): series}."""
    items = job._items(p)
    out = {}
    if not credentials_present(p):
        msg = "inloggegevens ontbreken (%s)" % credential_hint(p)
        for it in items:
            job.set_status(p, it["code"], "failed", msg)
        job.set_platform(p, "error", msg)
        job.log("%s: %s" % (PLATFORM_LABELS[p], msg), "error")
        return out

    job.set_platform(p, "running")
    start = job.settings["prices_start"]
    end = job.settings.get("prices_end") or None
    if p == "destatis":
        default_table = job.settings.get("destatis_table") or fetch_prices.DESTATIS_DEFAULT_TABLE
        groups = {}
        for it in items:
            groups.setdefault(it["table"] or "", []).append(it["code"])
    else:
        groups = {"": [it["code"] for it in items]}

    failures = 0
    for table, codes in groups.items():
        codes = list(dict.fromkeys(codes))
        label = PLATFORM_LABELS[p] + (" tabel %s" % (table or default_table)
                                      if p == "destatis" else "")
        job.log("%s: %d codes vanaf %s" % (label, len(codes), start))

        def on_code(code, status, detail, _table=table):
            if status == "running":
                job.set_status(p, code, "running", "", table=_table if p == "destatis" else None)
            elif status == "ok":
                s = detail.dropna()
                msg = ("%d maanden, %s t/m %s" % (len(s), s.index.min().strftime("%Y-%m"),
                                                  s.index.max().strftime("%Y-%m"))
                       if len(s) else "geen waarden in de gevraagde periode")
                job.set_status(p, code, "ok" if len(s) else "nodata", msg,
                               table=_table if p == "destatis" else None)
            else:
                job.set_status(p, code, "failed", fetch_prices._short(detail, 500),
                               detail=str(detail),
                               table=_table if p == "destatis" else None)
            if job.cancel_requested:
                raise Cancelled()

        try:
            if p == "destatis":
                df = fetch_prices.get_destatis_prices(
                    codes, start, end, table=table or default_table,
                    log=job.log, on_code=on_code)
            else:
                df = fetch_prices.get_fred_prices(codes, start, end, log=job.log,
                                                  on_code=on_code)
        except (fetch_prices.DestatisError, fetch_prices.FredError, ConnectionError) as exc:
            failures += len(codes)
            for c in codes:
                job.set_status(p, c, "failed", fetch_prices._short(exc, 500), detail=str(exc),
                               table=table if p == "destatis" else None)
            job.log("%s gestopt: %s" % (label, exc), "error")
            continue
        failures += len(df.attrs.get("failed", {}))
        for c in df.columns:
            out[(p, c, table)] = df[c]

    job.set_platform(p, "done" if not failures else ("error" if not out else "partial"),
                     "%d reeksen opgehaald, %d mislukt" % (len(out), failures))
    return out


# ---------------------------------------------------------------------- outputs
def _build_outputs(job: Job, imports, price_cols: dict):
    # market shares
    if "comtrade" in job.settings["platforms"] and job._items("comtrade"):
        code_to_names = {}
        for it in job._items("comtrade"):
            if it["status"] == "ok":
                code_to_names.setdefault(it["code"], [])
                if it["name"] not in code_to_names[it["code"]]:
                    code_to_names[it["code"]].append(it["name"])
        shares = outputs.market_shares(imports, code_to_names)
        materials = shares["Materiaal2"].nunique() if not shares.empty else 0
        expected = len({it["name"] for it in job._items("comtrade")})
        if materials:
            job.outputs["market_share"] = {
                "filename": "material_market_share.xlsx",
                "data": outputs.market_share_xlsx(shares),
                "summary": "%d van %d materialen, %d regels" % (materials, expected, len(shares)),
                "complete": materials == expected,
            }
            job.log("material_market_share.xlsx: %d materialen, %d regels"
                    % (materials, len(shares)))
        else:
            job.log("Geen marktaandelen: geen enkele Comtrade-code gaf data.", "error")

    # prices
    price_items = [it for it in job.items if it["platform"] in ("destatis", "fred")
                   and it["platform"] in job.settings["platforms"]]
    if price_items:
        base = pd.Timestamp(job.settings["prices_start"]).to_period("M").to_timestamp()
        columns, sources = [], []
        for it in price_items:
            key = (it["platform"], it["code"], (it["table"] or "") if it["platform"] == "destatis"
                   else "")
            series = price_cols.get(key)
            src = {"id": it["id"], "materiaal": it["name"], "bron": it["platform_label"],
                   "code": it["code"], "tabel": it["table"] or (
                       (job.settings.get("destatis_table") or fetch_prices.DESTATIS_DEFAULT_TABLE)
                       if it["platform"] == "destatis" else None),
                   "status": it["status_label"], "melding": it["message"]}
            if series is not None and series.notna().any():
                norm, base_used, note = outputs.normalise(series, base)
                s = series.dropna()
                src.update({"eerste_maand": s.index.min().strftime("%Y-%m"),
                            "laatste_maand": s.index.max().strftime("%Y-%m"),
                            "aantal_maanden": len(s),
                            "basismaand": base_used.strftime("%Y-%m") if base_used is not None
                            else None})
                if note:
                    src["melding"] = note
                    job.set_status(it["platform"], it["code"], it["status"],
                                   (it["message"] + "; " if it["message"] else "") + note,
                                   table=it["table"] if it["platform"] == "destatis" else None)
                    job.log("%s %s: %s" % (it["platform_label"], it["code"], note), "warn")
                columns.append({"id": it["id"], "name": it["name"], "series": series,
                                "normalised": norm})
            sources.append(src)
        if columns:
            job.outputs["prijzen"] = {
                "filename": "prijzen.xlsx",
                "data": outputs.prices_xlsx(columns, base, sources),
                "summary": "%d van %d reeksen, genormaliseerd op %s"
                           % (len(columns), len(price_items), base.strftime("%Y-%m")),
                "complete": len(columns) == len(price_items),
            }
            job.log("prijzen.xlsx: %d van %d reeksen" % (len(columns), len(price_items)))
        else:
            job.log("Geen prijzen: geen enkele reeks is opgehaald.", "error")

    snap_items = job.snapshot()["items"]
    job.outputs["rapport"] = {
        "filename": "rapport.xlsx",
        "data": outputs.report_xlsx(snap_items, job.settings, list(job.log_lines)),
        "summary": "status per code, instellingen en log",
        "complete": True,
    }


# ---------------------------------------------------------------------- registry
class JobRegistry:
    """In-memory list of recent jobs; one job may run at a time."""

    def __init__(self, keep: int = 20):
        self.keep = keep
        self.jobs: dict = {}
        self.lock = threading.Lock()

    def running(self) -> Optional[Job]:
        with self.lock:
            for j in self.jobs.values():
                if j.state in ("queued", "running"):
                    return j
        return None

    def start(self, job: Job):
        with self.lock:
            self.jobs[job.id] = job
            while len(self.jobs) > self.keep:
                oldest = min(self.jobs.values(), key=lambda j: j.created)
                if oldest.state in ("queued", "running"):
                    break
                del self.jobs[oldest.id]
        threading.Thread(target=run_job, args=(job,), daemon=True, name="job-" + job.id).start()

    def get(self, job_id) -> Optional[Job]:
        with self.lock:
            return self.jobs.get(job_id)

    def list(self):
        with self.lock:
            return sorted(self.jobs.values(), key=lambda j: j.created, reverse=True)
