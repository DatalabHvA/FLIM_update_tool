#!/usr/bin/env python
"""
fetch_prices.py - download monthly price index series by code from

  1) Destatis GENESIS-Online (REST web service), e.g. GP19-237012
  2) FRED (St. Louis Fed API),                    e.g. PCU327991327991
  3) Eurostat (dissemination API, no key),        e.g. C25 (NACE Rev. 2, dataset sts_inppd_m)
  4) CBS StatLine (OData, no key),                e.g. 237 (ProdCom/CPA; consumption prices NL)

and return them as ONE table: rows = months, columns = the codes you asked for.

Library use
-----------
    from fetch_prices import get_prices, get_destatis_prices, get_fred_prices

    df = get_prices(
        destatis_codes=["GP19-237012"],
        fred_codes=["PCU327991327991"],
        start="2015-01",
    )

Command line
------------
    python fetch_prices.py --destatis GP19-237012 --fred PCU327991327991 \
        --start 2015-01 --out prices.csv

Credentials (read from environment variables, never hard-code them)
-------------------------------------------------------------------
    DESTATIS_TOKEN                      personal API token of your GENESIS-Online account
      or DESTATIS_USER + DESTATIS_PASSWORD
    FRED_API_KEY                        free FRED API key

Large Destatis requests
-----------------------
GENESIS refuses requests that are too big for direct download (status code 98). The
usual fix is a narrower request (check the variable name with describe_destatis_table).
If a background job is really needed, pass allow_job=True (--allow-job); according to
the GENESIS documentation jobs need DESTATIS_USER + DESTATIS_PASSWORD, a token is not
accepted for them.

NOTE: both services deliver price INDICES (not absolute prices), and the two
indices have different base periods, so their levels are not directly comparable.

STATUS: written from the public API documentation / client libraries. Token authentication
and the status-98 answer of data/tablefile have been seen against the live Destatis service
(reported by the user); everything else is tested only against synthetic responses. Run
with --save-raw once and check the output before relying on it.
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import sys
import time
import warnings
import zipfile
from pathlib import Path
from typing import Iterable, Optional, Union

import pandas as pd
import requests

logger = logging.getLogger("fetch_prices")

# Base URL as given in the official GENESIS web service documentation (REST, v2020 path).
DESTATIS_BASE_URL = "https://genesis.destatis.de/genesisWS/rest/2020/"
# Table "Erzeugerpreisindex gewerblicher Produkte" by GP 2019 code. 61241-0004 has the 2-, 3-,
# 4-, 5-, 6- and selected 9-digit codes (one variable per level); 61241-0006 has fewer codes.
DESTATIS_DEFAULT_TABLE = "61241-0004"
# Measure that was selected in your chart link ("content": "PRE001$QMU").
DESTATIS_DEFAULT_MEASURE = "PRE001"
FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"
# Eurostat dissemination API (JSON-stat 2.0, no key needed). Default dataset: producer prices
# in industry, domestic market, monthly; codes are NACE Rev. 2 codes such as C25 or C237.
EUROSTAT_DATA_URL = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/"
EUROSTAT_DEFAULT_DATASET = "sts_inppd_m"
EUROSTAT_DEFAULT_GEO = "EU27_2020"
EUROSTAT_DEFAULT_UNIT = "I21"          # index 2021 = 100
EUROSTAT_DEFAULT_S_ADJ = "NSA"         # not seasonally / calendar adjusted
# CBS StatLine open data (OData v3, no key). Producer price tables per ProdCom/CPA code with
# five price types; "verbruik" = consumption prices = import prices + domestic sales prices,
# i.e. what users in the Netherlands pay, wherever the product was made. Newest table first:
# 85770NED (2021=100, 2018-now) is chained to 83935NED (2015=100, 2012-2023) on the overlap.
CBS_ODATA_URL = "https://opendata.cbs.nl/ODataApi/odata/"
CBS_TABLES = ("85770NED", "83935NED")
CBS_DEFAULT_MARKET = "verbruik"
CBS_MARKETS = {               # name in the input file -> start of the CBS category title
    "verbruik": "Verbruiksprijzen",
    "invoer": "Invoerprijzen",
    "binnenland": "Afzetprijzen binnenland",
    "buitenland": "Afzetprijzen buitenland",
    "afzet": "Totaal afzetprijzen",
}

_RETRY_STATUS = (429, 500, 502, 503, 504)
# Tokens GENESIS uses instead of a number (same list as the pystatis client).
_DESTATIS_NA = {"", "...", ".", "-", "/", "x"}

CodeList = Union[str, Iterable[str]]


class DestatisError(RuntimeError):
    """Problem while requesting or parsing data from GENESIS-Online."""


class FredError(RuntimeError):
    """Problem while requesting or parsing data from FRED."""


class EurostatError(RuntimeError):
    """Problem while requesting or parsing data from Eurostat."""


class CbsError(RuntimeError):
    """Problem while requesting or parsing data from CBS StatLine."""


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _as_list(codes: CodeList) -> list:
    if codes is None:
        return []
    if isinstance(codes, str):
        return [codes]
    return list(codes)


def _month_start(value) -> Optional[pd.Timestamp]:
    """'2015-01', '2015-01-17' or a date -> Timestamp of the first day of that month."""
    if value is None:
        return None
    return pd.Timestamp(value).to_period("M").to_timestamp()


def _short(message: str, limit: int = 300) -> str:
    """First line of a message, cut to `limit` characters (for status lines)."""
    line = str(message).strip().splitlines()[0] if str(message).strip() else ""
    return line if len(line) <= limit else line[:limit - 3] + "..."


def _describe(series: pd.Series, start_ts, end_ts) -> str:
    """One-phrase summary of a fetched series inside the requested period."""
    s = series
    if start_ts is not None:
        s = s[s.index >= start_ts]
    if end_ts is not None:
        s = s[s.index <= end_ts]
    if s.empty:
        return "no observations in the requested period"
    text = "%d month%s, %s to %s" % (len(s), "" if len(s) == 1 else "s",
                                      s.index.min().strftime("%Y-%m"),
                                      s.index.max().strftime("%Y-%m"))
    missing = int(s.isna().sum())
    return text + (", %d without value" % missing if missing else "")


def _run_codes(source: str, codes: list, fetch_one, *, verbose: bool, on_error: str,
               errors: tuple, start_ts=None, end_ts=None, log=None, on_code=None):
    """Call fetch_one(code) -> pd.Series for every code and report progress.

    One status line before and one after each code (verbose=True), then a summary.
    on_error="collect": a code that raises one of `errors` is recorded in `failed` and the
    next code is tried; on_error="raise": the exception propagates at once (old behaviour).
    If codes failed and verbose=False a RuntimeWarning names them, so nothing fails silently.
    log: optional callable(str) that receives the status lines instead of print().
    on_code: optional callable(code, status, detail) with status "running" (detail None),
    "ok" (detail = the series) or "failed" (detail = the error message).
    Returns (list of series, {failed code: message}).
    """
    if on_error not in ("collect", "raise"):
        raise ValueError("on_error must be 'collect' or 'raise', not %r" % (on_error,))

    def say(message):
        if log is not None:
            log(message)
        elif verbose:
            print(message, flush=True)

    def notify(code, status, detail):
        if on_code is not None:
            on_code(code, status, detail)

    columns, failed = [], {}
    started = time.monotonic()
    total = len(codes)
    for i, code in enumerate(codes, start=1):
        elapsed = time.monotonic() - started
        left = "" if i == 1 else ", ~%.0f s left" % (elapsed / (i - 1) * (total - i + 1))
        say("[%d/%d, %.0f s elapsed%s] %s %s" % (i, total, elapsed, left, source, code))
        t0 = time.monotonic()
        notify(code, "running", None)
        try:
            series = fetch_one(code)
        except errors as exc:
            if on_error == "raise":
                raise
            failed[code] = str(exc)
            say("    FAILED after %.1f s: %s" % (time.monotonic() - t0, _short(exc)))
            notify(code, "failed", str(exc))
            continue
        columns.append(series)
        say("    ok: %s, %.1f s" % (_describe(series, start_ts, end_ts), time.monotonic() - t0))
        notify(code, "ok", series)
    if total:
        say("%s: %d of %d codes ok%s" % (source, len(columns), total,
                                         "; failed: " + ", ".join(failed) if failed else ""))
    if failed and not verbose and log is None:
        warnings.warn("%s: %d of %d codes failed: %s" % (
            source, len(failed), total,
            "; ".join("%s (%s)" % (c, _short(m, 120)) for c, m in failed.items())),
            RuntimeWarning, stacklevel=3)
    return columns, failed


def _redact(text: str, *secrets: Optional[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _request(method: str, url: str, *, secrets: tuple = (), attempts: int = 3,
             backoff: float = 2.0, **kwargs) -> requests.Response:
    """requests.request with a few retries on network errors / 429 / 5xx.

    Secrets (API keys) are removed from any error message that is raised.
    """
    last = "unknown error"
    for attempt in range(attempts):
        try:
            resp = requests.request(method, url, **kwargs)
            if resp.status_code not in _RETRY_STATUS:
                return resp
            last = "HTTP %d" % resp.status_code
        except (requests.ConnectionError, requests.Timeout) as exc:
            last = "%s: %s" % (type(exc).__name__, exc)
        if attempt < attempts - 1:
            time.sleep(backoff * (2 ** attempt))
    raise ConnectionError(_redact("Request to %s failed after %d attempts (%s)"
                                  % (url, attempts, last), *secrets))


def _try_json(text: str) -> Optional[dict]:
    """Parse text as a JSON object, or return None if it is not one."""
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        return None
    try:
        obj = json.loads(stripped)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


# --------------------------------------------------------------------------- #
# Destatis GENESIS-Online
# --------------------------------------------------------------------------- #
def _destatis_headers(token: Optional[str] = None, username: Optional[str] = None,
                      password: Optional[str] = None) -> dict:
    """Credentials go into HTTP header fields (see GENESIS web service docs, 2.2.2).

    A personal API token replaces the user name and needs no password (docs 2.1.3).
    """
    token = token or os.environ.get("DESTATIS_TOKEN")
    username = username or os.environ.get("DESTATIS_USER")
    password = password or os.environ.get("DESTATIS_PASSWORD")
    if token:
        return {"username": token}
    if username and password:
        return {"username": username, "password": password}
    raise DestatisError(
        "No Destatis credentials found. Set the environment variable DESTATIS_TOKEN "
        "(token from the 'Webservice (API)' dialog of your GENESIS-Online account) "
        "or DESTATIS_USER and DESTATIS_PASSWORD.")


def _base_url(base_url: Optional[str]) -> str:
    return (base_url or os.environ.get("DESTATIS_BASE_URL") or DESTATIS_BASE_URL).rstrip("/")


def _post_destatis(path: str, params: dict, headers: dict, base_url: str) -> str:
    """POST to a GENESIS method (e.g. 'data/tablefile') and return the decoded answer text."""
    secrets = tuple(headers.values())
    resp = _request("POST", base_url.rstrip("/") + "/" + path, headers=headers, data=params,
                    timeout=(30, 300), secrets=secrets)
    content = resp.content
    if content[:2] == b"PK":                      # zipped result file
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            content = archive.read(archive.namelist()[0])
    text = content.decode("utf-8-sig", errors="replace")
    if resp.status_code >= 400:
        raise DestatisError(_redact("HTTP %d from %s: %s" % (resp.status_code, path, text[:500]),
                                    *secrets))
    return text


def _status_of(text: str) -> Optional[dict]:
    """A CSV answer starts with a header line; a JSON answer is a status message."""
    obj = _try_json(text)
    if obj is None:
        return None
    status = obj.get("Status")
    return status if isinstance(status, dict) else {"Content": text[:500]}


def check_destatis_login(token=None, username=None, password=None, base_url=None) -> str:
    """Call helloworld/logincheck and return the raw answer (to test your credentials)."""
    headers = _destatis_headers(token, username, password)
    secrets = tuple(headers.values())
    resp = _request("POST", _base_url(base_url) + "/helloworld/logincheck", headers=headers,
                    timeout=(30, 60), secrets=secrets)
    return "HTTP %d: %s" % (resp.status_code, _redact(resp.text[:500], *secrets))


def find_in_metadata(obj, pattern: str) -> list:
    """All strings (values and keys) anywhere inside a JSON structure that match a regex.

    Structure-agnostic helper for describe_destatis_table, e.g.
    find_in_metadata(meta, r"^GP19M") lists the classification variable names.
    """
    regex = re.compile(pattern)
    found = set()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(key, str) and regex.search(key):
                    found.add(key)
                walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)
        elif isinstance(node, str) and regex.search(node):
            found.add(node)

    walk(obj)
    return sorted(found)


def describe_destatis_table(table: str = DESTATIS_DEFAULT_TABLE, language: str = "en", *,
                            token: Optional[str] = None, username: Optional[str] = None,
                            password: Optional[str] = None, base_url: Optional[str] = None,
                            raw_dir: Optional[Union[str, Path]] = None) -> dict:
    """Structure of a GENESIS table (variables and their values) via metadata/table.

    Returns the parsed JSON answer; use find_in_metadata(meta, r"^GP19M") to list the
    variable names. The request parameters (name, area, language) are my reading of the
    client libraries, not confirmed against the documentation.
    """
    headers = _destatis_headers(token, username, password)
    return _fetch_metadata(table, language, headers, _base_url(base_url), raw_dir=raw_dir)


_META_CACHE: dict = {}


def _fetch_metadata(table: str, language: str, headers: dict, base_url: str,
                    raw_dir: Optional[Union[str, Path]] = None) -> dict:
    text = _post_destatis("metadata/table", {"name": table, "area": "all", "language": language},
                          headers, base_url)
    if raw_dir:
        Path(raw_dir).mkdir(parents=True, exist_ok=True)
        (Path(raw_dir) / ("destatis_metadata_%s.json" % table)).write_text(text, encoding="utf-8")
    obj = _try_json(text)
    if obj is None:
        raise DestatisError("metadata/table did not return JSON: %r" % text[:300])
    _META_CACHE[(base_url, table)] = obj
    return obj


# Variable names of the GP 2019 classification in GENESIS, e.g. GP19M2 ... GP19M6 for the
# 2- to 6-digit codes. The trailing number is taken as the number of digits of the codes
# the variable holds (assumption based on GP19M6 / GP19M3; the 9-digit variable of table
# 61241-0004 is expected to end in 9 as well - check the error message if it does not).
_GP_VARIABLE = r"^GP19[A-Z]+\d+$"


def gp_digits(code: str) -> Optional[int]:
    """Number of digits after 'GP19' (dashes ignored): GP19-0811 -> 4, GP19-233110002 -> 9."""
    match = re.match(r"^GP19(.*)$", str(code).strip(), re.I)
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return len(digits) or None


def _gp_variables_by_digits(candidates: list) -> dict:
    by_digits = {}
    for name in candidates:
        by_digits.setdefault(int(re.search(r"(\d+)$", name).group(1)), []).append(name)
    return by_digits


# Digit levels of the GP 2019 codes in table 61241-0004, used when the variable names have to
# be derived from the default variable.
DESTATIS_DIGIT_LEVELS_GUESS = (2, 3, 4, 5, 6, 9)
_STAT_VARS_CACHE: dict = {}


def _statistic_gp_variables(table: str, language: str, headers: dict, base_url: str) -> list:
    """GP19 variable names of the statistic behind a table (61241-0004 -> statistic 61241),
    via catalogue/variables2statistic. Returns [] when the catalogue cannot be read."""
    statistic = table.split("-")[0]
    key = (base_url, statistic)
    if key not in _STAT_VARS_CACHE:
        try:
            text = _post_destatis("catalogue/variables2statistic",
                                  {"name": statistic, "selection": "GP19*", "area": "all",
                                   "pagelength": "100", "language": language},
                                  headers, base_url)
            obj = _try_json(text) or {}
            found = find_in_metadata(obj.get("List") or [], _GP_VARIABLE)
        except DestatisError as exc:
            logger.warning("Could not list the variables of statistic %s: %s", statistic, exc)
            found = []
        _STAT_VARS_CACHE[key] = found
    return _STAT_VARS_CACHE[key]


def _resolve_classifying_variable(table: str, requested: Optional[str], language: str,
                                  headers: dict, base_url: str):
    """Return a function code -> GENESIS variable that holds that GP code.

    With 'requested' that variable is used for every code (checked against the table
    metadata when it can be read). Without it, the GP19 variables come from the table
    metadata plus the variables of its statistic (catalogue/variables2statistic): the table
    metadata of 61241-0004 only names its default GP19M2, the other levels are the
    alternatives behind "Select another variable". Per code the variable whose name ends in
    the code's number of digits (gp_digits) is used. If the catalogue cannot be read, the
    other levels are derived from the default name (GP19M2 -> GP19M3, ...). A code that
    matches no variable raises DestatisError for that code only. Results are cached.
    """
    meta = _META_CACHE.get((base_url, table))
    if meta is None:
        try:
            meta = _fetch_metadata(table, language, headers, base_url)
        except DestatisError as exc:
            if requested:
                logger.warning("Could not read the metadata of table %s (%s); using "
                               "classifying_variable=%s without checking it.", table, exc,
                               requested)
                return lambda code: requested
            raise DestatisError("Could not read the metadata of table %s to find the GP variable "
                                "(%s). Pass classifying_variable= explicitly." % (table, exc))
    candidates = find_in_metadata(meta, _GP_VARIABLE)
    defaults = list(candidates)                 # the variable(s) the table itself shows
    if requested:
        if find_in_metadata(meta, "^%s$" % re.escape(requested)):
            return lambda code: requested
        if candidates:
            raise DestatisError("Variable %r is not in the metadata of table %s; the GP19 "
                                "variables found there are: %s." % (requested, table, candidates))
        logger.warning("Cannot confirm variable %s in the metadata of table %s; continuing.",
                       requested, table)
        return lambda code: requested
    if not candidates:
        raise DestatisError("No GP19<letters><number> variable found in the metadata of table %s. "
                            "Pass classifying_variable= explicitly." % table)
    # The table metadata only lists the variable shown by default (61241-0004: GP19M2). The
    # other levels ("Select another variable" in GENESIS) are variables of the statistic,
    # so ask the catalogue for those as well.
    extra = _statistic_gp_variables(table, language, headers, base_url)
    candidates = sorted(set(candidates) | set(extra))
    by_digits = _gp_variables_by_digits(candidates)
    if not extra and len(by_digits) == 1:
        # Catalogue not readable: derive the other levels from the default name
        # (GP19M2 -> GP19M3, GP19M4, ...). A wrong guess fails per code with that name.
        base = candidates[0]
        logger.info("Table %s: only variable %s known; other levels derived from its name",
                    table, base)
        for n in DESTATIS_DIGIT_LEVELS_GUESS:
            by_digits.setdefault(n, [re.sub(r"\d+$", str(n), base)])
    logger.info("Table %s: GP19 variables per number of digits: %s", table,
                ", ".join("%d: %s" % (n, "/".join(v)) for n, v in sorted(by_digits.items())))

    def pick(code):
        n = gp_digits(code)
        if n is None:
            raise DestatisError("Code %s does not look like GP19-<digits>; cannot choose a "
                                "variable of table %s." % (code, table))
        names = by_digits.get(n, [])
        if len(names) > 1:
            # prefer the family of the table's own variable (GP19M2 -> GP19M...)
            prefixes = {re.sub(r"\d+$", "", d) for d in defaults}
            names = [v for v in names if re.sub(r"\d+$", "", v) in prefixes] or names
        if len(names) == 1:
            return names[0]
        if not names:
            raise DestatisError("Code %s has %d digits, but table %s has no GP19 variable for "
                                "%d-digit codes (variables found: %s)."
                                % (code, n, table, n, ", ".join(candidates)))
        raise DestatisError("Code %s has %d digits and table %s has several matching variables "
                            "(%s). Pass classifying_variable= explicitly."
                            % (code, n, table, ", ".join(names)))
    return pick


def _run_destatis_job(code: str, params: dict, headers: dict, base_url: str,
                      job_timeout: float, poll_interval: float) -> str:
    """Background job as done by the pystatis client: tablefile with job=true, poll
    catalogue/results until the job shows up, then fetch it with data/resultfile."""
    if "password" not in headers:
        raise DestatisError("Background jobs need DESTATIS_USER and DESTATIS_PASSWORD; according "
                            "to the GENESIS documentation an API token cannot be used for jobs.")
    text = _post_destatis("data/tablefile", dict(params, job="true"), headers, base_url)
    status = _status_of(text)
    if status is None:
        return text                                   # data came back directly after all
    match = re.search(r"(?<=:\s).*_\d+", str(status.get("Content", "")))
    if not match:
        raise DestatisError("Could not find a job id in the GENESIS answer for %s: %s"
                            % (code, str(status)[:500]))
    job_id = match.group(0).strip()
    logger.info("Destatis job %s started for %s", job_id, code)

    deadline = time.monotonic() + job_timeout
    while True:
        poll = _try_json(_post_destatis(
            "catalogue/results", {"selection": job_id, "area": "user", "pagelength": "1"},
            headers, base_url))
        if poll and poll.get("List"):
            break
        if time.monotonic() >= deadline:
            raise DestatisError("Job %s is not finished after %.0f s. It keeps running on the "
                                "server and its result will appear in your GENESIS account."
                                % (job_id, job_timeout))
        time.sleep(poll_interval)

    result_params = dict(params)
    result_params["name"] = job_id
    text = _post_destatis("data/resultfile", result_params, headers, base_url)
    status = _status_of(text)
    if status is not None:
        raise DestatisError("GENESIS returned a status message instead of the job result for "
                            "%s: %s" % (job_id, str(status)[:500]))
    return text


def _download_destatis_ffcsv(code: str, *, table: str, classifying_variable: str,
                             start_year: Optional[int], end_year: Optional[int],
                             language: str, headers: dict, base_url: str,
                             allow_job: bool = False, job_timeout: float = 900.0,
                             poll_interval: float = 5.0) -> str:
    params = {
        "name": table,
        "area": "all",
        "compress": "false",
        "format": "ffcsv",
        "language": language,
        "classifyingvariable1": classifying_variable,
        "classifyingkey1": code,
    }
    if start_year:
        params["startyear"] = str(start_year)
    if end_year:
        params["endyear"] = str(end_year)
    secrets = tuple(headers.values())

    text = _post_destatis("data/tablefile", params, headers, base_url)
    status = _status_of(text)
    if status is None:
        return text
    if status.get("Code") == 98:
        if allow_job:
            return _run_destatis_job(code, params, headers, base_url, job_timeout, poll_interval)
        raise DestatisError(_redact(
            "GENESIS says the request is too big for direct download (status 98) for table %s, "
            "%s = %s, years %s-%s: %s\nA single code over a few years should be small. Possible "
            "reasons: %r is not a value of that variable in this table (check the code and its "
            "digit level), or the server estimates the size before filtering. If a background "
            "job is really needed, pass allow_job=True (needs DESTATIS_USER and "
            "DESTATIS_PASSWORD, a token cannot create jobs)."
            % (table, classifying_variable, code, start_year or "first", end_year or "last",
               status.get("Content", ""), code), *secrets))
    raise DestatisError(_redact("GENESIS returned a status message instead of data for "
                                "%s: %s" % (code, str(status)[:500]), *secrets))


def parse_destatis_ffcsv(text: str, *, code: str, classifying_variable: str,
                         value_variable: Optional[str] = DESTATIS_DEFAULT_MEASURE,
                         decimal: str = ".") -> tuple:
    """Turn a GENESIS 'ffcsv' answer into (pd.Series indexed by month start, metadata dict).

    Expected layout (as used by the pystatis client): ';'-separated, one row per
    observation, columns time, N_variable_code / N_variable_attribute_code pairs,
    value_variable_label, value, value_unit, value_q (and probably value_variable_code).
    The rows of the requested code are found by the code's value in any variable column;
    classifying_variable is only used to break a tie. Anything unexpected raises a
    DestatisError that lists what was found, instead of guessing.
    """
    lines = text.splitlines()
    if len(lines) < 2:
        raise DestatisError("Empty answer for %s: %r" % (code, text[:200]))
    # Same row filter as pystatis: keep header + rows starting with digits (statistic code).
    body = [lines[0]] + [ln for ln in lines[1:] if ln[:4].isdigit()]
    df = pd.read_csv(io.StringIO("\n".join(body)), sep=";", dtype=str, keep_default_na=False)
    if df.empty:
        raise DestatisError("No data rows in the answer for %s." % code)

    lower = {c.lower(): c for c in df.columns}
    time_col, value_col = lower.get("time"), lower.get("value")
    if time_col is None or value_col is None:
        raise DestatisError("Unexpected ffcsv layout, need columns 'time' and 'value' but got: %s. "
                            "Re-run with raw_dir / --save-raw and inspect the file."
                            % list(df.columns))

    # --- choose the measure (e.g. index level vs. % change) ------------------------------
    key_cols = [lower[k] for k in ("value_variable_code", "value_variable_label") if k in lower]
    meta = {}
    if key_cols:
        if value_variable:
            needle = value_variable.lower()
            mask = pd.Series(False, index=df.index)
            for col in key_cols:
                mask |= df[col].str.lower().str.contains(needle, regex=False)
            if not mask.any():
                raise DestatisError(
                    "value_variable=%r matches none of the measures in the answer: %s. Pass part "
                    "of the code or label you want." % (
                        value_variable, df[key_cols].drop_duplicates().to_dict("records")))
            df = df[mask]
        options = df[key_cols].drop_duplicates()
        if len(options) > 1:
            raise DestatisError("Several measures remain for %s: %s. Narrow it down with "
                                "value_variable=." % (code, options.to_dict("records")))
        meta.update(options.iloc[0].to_dict())
    if "value_unit" in lower:
        units = sorted(set(df[lower["value_unit"]]) - {""})
        meta["unit"] = units[0] if len(units) == 1 else units

    # --- keep only the requested code ------------------------------------------------------
    # The code is located by its VALUE: GENESIS labels it with a level-specific variable
    # (seen: a request filtered via GP19M6 for GP19-237 came back under GP19M3), so the
    # variable name of the request is only a tie-breaker, not a requirement.
    var_nums = sorted(int(m.group(1)) for c in df.columns
                      for m in [re.fullmatch(r"(\d+)_variable_code", c)] if m)
    attr_cols = {n: "%d_variable_attribute_code" % n for n in var_nums
                 if "%d_variable_attribute_code" % n in df.columns}
    matches = [n for n, col in attr_cols.items() if (df[col] == code).any()]
    if not matches:
        present = sorted({v for n in var_nums for v in df["%d_variable_code" % n].unique()})
        values = sorted({v for col in attr_cols.values() for v in df[col].unique()
                         if str(v).upper().startswith("GP19")})
        hint = ""
        if classifying_variable not in present:
            hint = (" Requested variable %s, but Destatis answered with %s: the variable "
                    "name is probably wrong for this table." % (classifying_variable, present))
        elif values:
            hint = " GP codes in the answer: %s." % ", ".join(values[:10])
        raise DestatisError("Code %s is not a value of any variable in the answer (variables "
                            "present: %s).%s" % (code, present, hint))
    code_num = next((n for n in matches
                     if (df["%d_variable_code" % n] == classifying_variable).any()), matches[0])
    df = df[df[attr_cols[code_num]] == code]
    answer_variable = df["%d_variable_code" % code_num].iloc[0]
    meta["answer_variable"] = answer_variable
    if answer_variable != classifying_variable:
        logger.info("Code %s is delivered under variable %s (request used %s).",
                 code, answer_variable, classifying_variable)

    # --- month of each observation -------------------------------------------------------
    stamp = pd.to_datetime(df[time_col], errors="coerce")
    month_num = next((n for n in var_nums if (df["%d_variable_code" % n] == "MONAT").all()), None)
    if month_num is not None:
        # time = year, month comes from a MONAT variable with codes MONAT01..MONAT12
        month = pd.to_numeric(df["%d_variable_attribute_code" % month_num]
                              .str.extract(r"^MONAT(\d{2})$")[0], errors="coerce")
        keep = stamp.notna() & month.between(1, 12)
        month_start = pd.to_datetime(pd.DataFrame(
            {"year": stamp[keep].dt.year, "month": month[keep].astype(int), "day": 1}))
    else:
        # time column already carries the month
        keep = stamp.notna()
        month_start = stamp[keep].dt.to_period("M").dt.to_timestamp()
    df = df[keep]
    if df.empty:
        raise DestatisError("No rows with a usable month for %s (time column example: %r)."
                            % (code, text.splitlines()[1][:200]))

    # --- values ----------------------------------------------------------------------------
    raw = df[value_col].str.strip()
    if decimal == ",":
        raw = raw.str.replace(",", ".", regex=False)
    values = pd.to_numeric(raw, errors="coerce")
    unparsed = values.isna() & ~df[value_col].str.strip().isin(_DESTATIS_NA)
    if unparsed.any():
        raise DestatisError("Could not read %d value(s) as numbers for %s, e.g. %s (decimal=%r)."
                            % (unparsed.sum(), code, df.loc[unparsed, value_col].head(3).tolist(),
                               decimal))

    series = pd.Series(values.to_numpy(), index=pd.DatetimeIndex(month_start.to_numpy()), name=code)
    if series.index.duplicated().any():
        varying = [c for c in df.columns if c.endswith("_attribute_code") and df[c].nunique() > 1]
        raise DestatisError("More than one value per month for %s; these variables still vary: %s. "
                            "Narrow the request." % (code, varying))
    return series.sort_index(), meta


def get_destatis_prices(codes: CodeList, start="2015-01", end=None, *,
                        table: str = DESTATIS_DEFAULT_TABLE,
                        value_variable: Optional[str] = DESTATIS_DEFAULT_MEASURE,
                        classifying_variable: Optional[str] = None,
                        language: str = "en",
                        token: Optional[str] = None, username: Optional[str] = None,
                        password: Optional[str] = None, base_url: Optional[str] = None,
                        raw_dir: Optional[Union[str, Path]] = None,
                        allow_job: bool = False, job_timeout: float = 900.0,
                        verbose: bool = True, on_error: str = "collect",
                        log=None, on_code=None) -> pd.DataFrame:
    """Monthly values for a list of GP 2019 codes (e.g. ['GP19-237012']) from GENESIS-Online.

    Returns a DataFrame: index = first day of each month ('month'), one column per code.
    Per-code metadata (measure label, unit) is stored in df.attrs['destatis_metadata'].
    verbose=True prints a status line per code. on_error="collect" (default) keeps going when
    a code fails; df.attrs['completed'] / df.attrs['failed'] ({code: message}) tell which
    codes made it. on_error="raise" stops at the first failing code (full traceback).
    classifying_variable=None reads the variable that holds the GP code from the table
    metadata: one GP19 variable is used for all codes; with several (table 61241-0004) each
    code gets the variable for its number of digits (GP19-0811 -> 4-digit variable,
    GP19-233110002 -> 9-digit variable). A given name is checked against the metadata
    before any data is requested and used for every code.
    allow_job=True lets the function start a GENESIS background job when the server answers
    'too big' (status 98); this needs username/password, not a token.
    """
    codes = _as_list(codes)
    headers = _destatis_headers(token, username, password)
    base = _base_url(base_url)
    start_ts, end_ts = _month_start(start), _month_start(end)
    decimal = "," if language == "de" else "."
    raw_path = Path(raw_dir) if raw_dir else None
    if raw_path:
        raw_path.mkdir(parents=True, exist_ok=True)

    variable_for = _resolve_classifying_variable(table, classifying_variable, language, headers,
                                                 base)
    metadata = {}

    def fetch_one(code):
        variable = variable_for(code)
        logger.info("Destatis %s: table %s, %s = %s", code, table, variable, code)
        text = _download_destatis_ffcsv(
            code, table=table, classifying_variable=variable,
            start_year=start_ts.year if start_ts is not None else None,
            end_year=end_ts.year if end_ts is not None else None,
            language=language, headers=headers, base_url=base,
            allow_job=allow_job, job_timeout=job_timeout)
        if raw_path:
            (raw_path / ("destatis_%s_%s.csv" % (table, code))).write_text(text, encoding="utf-8")
        series, meta = parse_destatis_ffcsv(text, code=code, classifying_variable=variable,
                                            value_variable=value_variable, decimal=decimal)
        metadata[code] = meta
        return series

    columns, failed = _run_codes("Destatis", codes, fetch_one, verbose=verbose, on_error=on_error,
                                 errors=(DestatisError, ConnectionError),
                                 start_ts=start_ts, end_ts=end_ts, log=log, on_code=on_code)
    df = _assemble(columns, start_ts, end_ts)
    df.attrs["destatis_metadata"] = metadata
    df.attrs["completed"] = [c for c in codes if c not in failed]
    df.attrs["failed"] = failed
    return df


# --------------------------------------------------------------------------- #
# FRED
# --------------------------------------------------------------------------- #
def _fred_series(code: str, key: str, start_ts, end_ts, frequency, aggregation_method) -> pd.Series:
    """One FRED series as monthly pd.Series (empty if FRED has no observations)."""
    params = {"series_id": code, "api_key": key, "file_type": "json",
              "observation_start": start_ts.strftime("%Y-%m-%d")}
    if end_ts is not None:
        params["observation_end"] = (end_ts + pd.offsets.MonthEnd(0)).strftime("%Y-%m-%d")
    if frequency:
        params["frequency"] = frequency
        params["aggregation_method"] = aggregation_method
    logger.info("FRED %s", code)
    resp = _request("GET", FRED_OBSERVATIONS_URL, params=params, timeout=(10, 60),
                    secrets=(key,))
    if resp.status_code != 200:
        raise FredError(_redact("series %s: HTTP %d: %s" % (code, resp.status_code,
                                                            resp.text[:300]), key))
    try:
        payload = resp.json()
    except ValueError:
        raise FredError("series %s: answer is not JSON: %r" % (code, resp.text[:200]))
    if "observations" not in payload:
        raise FredError("series %s: no 'observations' in answer (keys: %s)"
                        % (code, sorted(payload)))
    obs = pd.DataFrame(payload["observations"], columns=["date", "value"])
    if obs.empty:
        logger.warning("FRED %s returned no observations for the requested period", code)
        return pd.Series(dtype=float, name=code, index=pd.DatetimeIndex([], name="month"))
    values = pd.to_numeric(obs["value"], errors="coerce")
    unparsed = values.isna() & (obs["value"] != ".")   # "." = missing value in FRED
    if unparsed.any():
        raise FredError("series %s: cannot read values as numbers, e.g. %s"
                        % (code, obs.loc[unparsed, "value"].head(3).tolist()))
    index = pd.DatetimeIndex(pd.to_datetime(obs["date"]).dt.to_period("M").dt.to_timestamp())
    series = pd.Series(values.to_numpy(), index=index, name=code)
    if series.index.duplicated().any():
        raise FredError("series %s: more than one observation per month; try "
                        "frequency='m'." % code)
    return series.sort_index()


def get_fred_prices(codes: CodeList, start="2015-01", end=None, *,
                    api_key: Optional[str] = None, frequency: Optional[str] = "m",
                    aggregation_method: str = "avg", verbose: bool = True,
                    on_error: str = "collect", log=None, on_code=None) -> pd.DataFrame:
    """Monthly values for a list of FRED series ids (e.g. ['PCU327991327991']).

    Uses fred/series/observations. frequency='m' aggregates to monthly if a series is
    finer than monthly (FRED returns an error if it is coarser); frequency=None leaves
    the series as published. Returns index = first day of each month, one column per id.
    verbose / on_error / df.attrs['completed'] / df.attrs['failed']: see get_destatis_prices.
    """
    codes = _as_list(codes)
    key = api_key or os.environ.get("FRED_API_KEY")
    if not key:
        raise FredError("No FRED API key found. Set the environment variable FRED_API_KEY "
                        "(a free key can be requested from your FRED account).")
    start_ts, end_ts = _month_start(start), _month_start(end)

    columns, failed = _run_codes(
        "FRED", codes,
        lambda code: _fred_series(code, key, start_ts, end_ts, frequency, aggregation_method),
        verbose=verbose, on_error=on_error, errors=(FredError, ConnectionError),
        start_ts=start_ts, end_ts=end_ts, log=log, on_code=on_code)
    df = _assemble(columns, start_ts, end_ts)
    df.attrs["completed"] = [c for c in codes if c not in failed]
    df.attrs["failed"] = failed
    return df


# --------------------------------------------------------------------------- #
# Eurostat
# --------------------------------------------------------------------------- #
def _eurostat_error_text(payload, resp) -> str:
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, list):
        error = "; ".join(str(e.get("label") or e) if isinstance(e, dict) else str(e)
                          for e in error)
    elif isinstance(error, dict):
        error = error.get("label") or error.get("message") or str(error)
    return str(error or resp.text[:300])


def _eurostat_series(code: str, dataset: str, geo: str, unit: str, s_adj: str,
                     start_ts, end_ts) -> pd.Series:
    """One Eurostat series as monthly pd.Series (code = NACE Rev. 2 code, e.g. C25)."""
    params = {"format": "JSON", "lang": "EN", "nace_r2": code, "geo": geo, "unit": unit,
              "s_adj": s_adj}
    if start_ts is not None:
        params["sinceTimePeriod"] = start_ts.strftime("%Y-%m")
    if end_ts is not None:
        params["untilTimePeriod"] = end_ts.strftime("%Y-%m")
    where = "%s (dataset %s, geo %s, unit %s)" % (code, dataset, geo, unit)
    logger.info("Eurostat %s", where)
    resp = _request("GET", EUROSTAT_DATA_URL + dataset, params=params, timeout=(10, 120))
    try:
        payload = resp.json()
    except ValueError:
        raise EurostatError("%s: HTTP %d, answer is not JSON: %r"
                            % (where, resp.status_code, resp.text[:200]))
    if resp.status_code != 200 or (isinstance(payload, dict) and payload.get("error")):
        raise EurostatError("%s: HTTP %d: %s" % (where, resp.status_code,
                                                 _eurostat_error_text(payload, resp)))
    ids, sizes = payload.get("id"), payload.get("size")
    if not ids or not sizes or "time" not in ids:
        raise EurostatError("%s: unexpected answer (keys: %s)" % (where, sorted(payload)))
    empty = [d for d, s in zip(ids, sizes) if s == 0]
    if empty:
        # Eurostat answers HTTP 200 with an empty dimension for an unknown code or country.
        raise EurostatError("%s: Eurostat does not know this value of %s. Check the NACE code "
                            "and the country." % (where, " / ".join(empty)))
    several = {d: s for d, s in zip(ids, sizes) if d != "time" and s != 1}
    if several:
        raise EurostatError("%s: the answer holds more than one series (%s). Narrow the request."
                            % (where, ", ".join("%s: %d values" % kv for kv in several.items())))
    values = payload.get("value") or {}
    if not values:
        # Eurostat answers HTTP 200 without values for an unknown code or country.
        raise EurostatError("%s: Eurostat has no values for this code and country in the "
                            "requested period. Check the NACE code and the country." % where)
    pos = ids.index("time")
    stride = 1
    for size in sizes[pos + 1:]:
        stride *= size
    time_index = payload["dimension"]["time"]["category"]["index"]
    if isinstance(time_index, list):
        time_index = {label: i for i, label in enumerate(time_index)}
    months, numbers = [], []
    for label, i in time_index.items():
        try:
            month = pd.Period(label, freq="M").to_timestamp()
        except (ValueError, TypeError):
            raise EurostatError("%s: time period %r is not a month; only monthly datasets are "
                                "supported." % (where, label))
        flat = i * stride
        value = values[flat] if isinstance(values, list) and flat < len(values) \
            else (values.get(str(flat)) if isinstance(values, dict) else None)
        months.append(month)
        numbers.append(float(value) if value is not None else float("nan"))
    series = pd.Series(numbers, index=pd.DatetimeIndex(months), name=code).sort_index()
    if series.index.duplicated().any():
        raise EurostatError("%s: more than one value per month." % where)
    return series


def get_eurostat_prices(codes: CodeList, start="2015-01", end=None, *,
                        dataset: str = EUROSTAT_DEFAULT_DATASET,
                        geo: str = EUROSTAT_DEFAULT_GEO, unit: str = EUROSTAT_DEFAULT_UNIT,
                        s_adj: str = EUROSTAT_DEFAULT_S_ADJ, verbose: bool = True,
                        on_error: str = "collect", log=None, on_code=None) -> pd.DataFrame:
    """Monthly values for a list of NACE Rev. 2 codes (e.g. ['C25', 'C237']) from Eurostat.

    Default: dataset sts_inppd_m (producer prices in industry, domestic market), EU27_2020,
    index 2021=100, unadjusted. No API key needed. Returns index = first day of each month,
    one column per code. verbose / on_error / log / on_code / df.attrs['completed'] /
    df.attrs['failed']: see get_destatis_prices.
    """
    codes = _as_list(codes)
    start_ts, end_ts = _month_start(start), _month_start(end)
    columns, failed = _run_codes(
        "Eurostat", codes,
        lambda code: _eurostat_series(code, dataset, geo, unit, s_adj, start_ts, end_ts),
        verbose=verbose, on_error=on_error, errors=(EurostatError, ConnectionError),
        start_ts=start_ts, end_ts=end_ts, log=log, on_code=on_code)
    df = _assemble(columns, start_ts, end_ts)
    df.attrs["completed"] = [c for c in codes if c not in failed]
    df.attrs["failed"] = failed
    return df


# --------------------------------------------------------------------------- #
# CBS StatLine
# --------------------------------------------------------------------------- #
_CBS_STRUCTURE: dict = {}


def _cbs_get(table: str, path: str) -> list:
    resp = _request("GET", CBS_ODATA_URL + table + "/" + path, timeout=(10, 120))
    if resp.status_code != 200:
        raise CbsError("CBS table %s: HTTP %d for %s: %s"
                       % (table, resp.status_code, path.split("?")[0], resp.text[:200]))
    try:
        return resp.json()["value"]
    except (ValueError, KeyError):
        raise CbsError("CBS table %s: answer for %s is not OData JSON: %r"
                       % (table, path.split("?")[0], resp.text[:200]))


def _cbs_structure(table: str) -> dict:
    """Dimension names, product codes and price types of a CBS PPI table (cached).

    The two PPI tables name their dimensions differently, so they are recognised by
    content: the product dimension has titles like '2331 Keramische tegels ...', the
    price-type dimension has 'Verbruiksprijzen' etc.
    """
    if table in _CBS_STRUCTURE:
        return _CBS_STRUCTURE[table]
    props = _cbs_get(table, "DataProperties?$format=json")
    dims = [p["Key"] for p in props if p.get("Type") == "Dimension"]
    topics = [p["Key"] for p in props if p.get("Type") == "Topic"]
    time_dim = next((p["Key"] for p in props if p.get("Type") == "TimeDimension"), None)
    if not dims or not topics or not time_dim:
        raise CbsError("CBS table %s has an unexpected layout (properties: %s)"
                       % (table, [p.get("Key") for p in props]))
    product_dim = market_dim = None
    products, markets = {}, {}
    for dim in dims:
        cats = _cbs_get(table, dim + "?$format=json")
        titles = [str(c.get("Title", "")) for c in cats]
        if any(t.startswith(tuple(CBS_MARKETS.values())) for t in titles):
            market_dim = dim
            for name, prefix in CBS_MARKETS.items():
                key = next((c["Key"] for c in cats if str(c["Title"]).startswith(prefix)), None)
                if key:
                    markets[name] = key
        else:
            product_dim = dim
            for c in cats:
                code = str(c.get("Title", "")).split(" ", 1)[0]
                products.setdefault(code, (c["Key"], str(c["Title"]).strip()))
    if not product_dim or not market_dim:
        raise CbsError("CBS table %s: cannot find the product and price-type dimensions (%s)"
                       % (table, dims))
    structure = {"product_dim": product_dim, "market_dim": market_dim, "time_dim": time_dim,
                 "topic": topics[0], "products": products, "markets": markets}
    _CBS_STRUCTURE[table] = structure
    return structure


def _cbs_table_series(table: str, code: str, market: str) -> Optional[pd.Series]:
    """Monthly index of one code in one CBS table; None if the table lacks the code."""
    st = _cbs_structure(table)
    if code not in st["products"]:
        return None
    if market not in st["markets"]:
        raise CbsError("CBS table %s has no price type %r (available: %s)"
                       % (table, market, ", ".join(st["markets"])))
    product_key, title = st["products"][code]
    flt = "%s eq '%s' and %s eq '%s' and substringof('MM', %s)" % (
        st["product_dim"], product_key, st["market_dim"], st["markets"][market], st["time_dim"])
    rows = _cbs_get(table, "TypedDataSet?$format=json&$select=%s,%s&$filter=%s" % (
        st["time_dim"], st["topic"], requests.utils.quote(flt)))
    months, values = [], []
    for row in rows:
        match = re.fullmatch(r"(\d{4})MM(\d{2})", str(row.get(st["time_dim"], "")).strip())
        if match and row.get(st["topic"]) is not None:
            months.append(pd.Timestamp(int(match.group(1)), int(match.group(2)), 1))
            values.append(float(row[st["topic"]]))
    series = pd.Series(values, index=pd.DatetimeIndex(months), name=code, dtype=float).sort_index()
    series.attrs["title"] = title
    return series


def _chain(new: pd.Series, old: pd.Series, months: int = 12):
    """Extend `new` backwards with `old`, rescaled on their first `months` common months.

    Returns (series, note) or (new, None) if there is nothing to add or no overlap.
    """
    new, old = new.dropna(), old.dropna()
    if new.empty:
        return old, None
    earlier = old[old.index < new.index.min()]
    common = new.index.intersection(old.index)[:months]
    if earlier.empty or len(common) == 0:
        return new, None
    factor = new.loc[common].mean() / old.loc[common].mean()
    out = pd.concat([earlier * factor, new]).sort_index()
    note = ("%s t/m %s aangevuld uit oudere CBS-tabel (gekoppeld op %s t/m %s)"
            % (earlier.index.min().strftime("%Y-%m"), earlier.index.max().strftime("%Y-%m"),
               common.min().strftime("%Y-%m"), common.max().strftime("%Y-%m")))
    return out, note


def _cbs_series(code: str, market: str, tables, start_ts, end_ts) -> pd.Series:
    """One CBS series, newest table first, older tables chained in front of it."""
    code = str(code).strip()
    found, notes, combined, title = [], [], None, None
    for table in tables:
        series = _cbs_table_series(table, code, market)
        if series is None:
            continue
        found.append(table)
        title = title or series.attrs.get("title")
        if series.dropna().empty:
            continue
        if combined is None:
            combined = series
        elif start_ts is None or combined.dropna().index.min() > start_ts:
            older = series if start_ts is None else series[series.index >= start_ts]
            combined, note = _chain(combined, older)
            if note:
                notes.append(note)
    if not found:
        raise CbsError("Code %s staat niet in CBS-tabel %s. Gebruik een ProdCom/CPA-code zoals "
                       "CBS die schrijft, bv. 237 of 2331." % (code, " / ".join(tables)))
    if combined is None or combined.dropna().empty:
        raise CbsError("CBS heeft geen %s voor %s (%s) in tabel %s."
                       % (CBS_MARKETS.get(market, market).lower(), code, title, " / ".join(found)))
    combined = combined.copy()
    combined.name = code
    combined.attrs["title"] = title
    combined.attrs["note"] = "; ".join(notes)
    return combined


def get_cbs_prices(codes: CodeList, start="2015-01", end=None, *,
                   market: str = CBS_DEFAULT_MARKET, tables=CBS_TABLES,
                   verbose: bool = True, on_error: str = "collect", log=None,
                   on_code=None) -> pd.DataFrame:
    """Monthly CBS producer price indices for ProdCom/CPA codes (e.g. ['237', '2331']).

    market: 'verbruik' (default: consumption prices = imports + domestic sales, the price
    level on the Dutch market), 'invoer', 'binnenland', 'buitenland' or 'afzet'.
    The newest table comes first; when it starts after `start`, older tables are chained
    in front of it on their overlapping months (series.attrs['note'] says which months).
    No key needed. verbose / on_error / log / on_code: see get_destatis_prices.
    """
    codes = _as_list(codes)
    if market not in CBS_MARKETS:
        raise CbsError("Onbekende CBS-prijssoort %r; kies uit %s"
                       % (market, ", ".join(CBS_MARKETS)))
    start_ts, end_ts = _month_start(start), _month_start(end)
    columns, failed = _run_codes(
        "CBS", codes, lambda code: _cbs_series(code, market, tables, start_ts, end_ts),
        verbose=verbose, on_error=on_error, errors=(CbsError, ConnectionError),
        start_ts=start_ts, end_ts=end_ts, log=log, on_code=on_code)
    df = _assemble(columns, start_ts, end_ts)
    df.attrs["completed"] = [c for c in codes if c not in failed]
    df.attrs["failed"] = failed
    df.attrs["notes"] = {s.name: s.attrs.get("note", "") for s in columns}
    return df


# --------------------------------------------------------------------------- #
# combined
# --------------------------------------------------------------------------- #
def _assemble(columns: list, start_ts, end_ts) -> pd.DataFrame:
    if not columns:
        return pd.DataFrame(index=pd.DatetimeIndex([], name="month"))
    df = pd.concat(columns, axis=1).sort_index()
    df.index.name = "month"
    if start_ts is not None:
        df = df[df.index >= start_ts]
    if end_ts is not None:
        df = df[df.index <= end_ts]
    return df


def get_prices(destatis_codes: CodeList = (), fred_codes: CodeList = (), start="2015-01",
               end=None, destatis_kwargs: Optional[dict] = None,
               fred_kwargs: Optional[dict] = None, *, eurostat_codes: CodeList = (),
               eurostat_kwargs: Optional[dict] = None, cbs_codes: CodeList = (),
               cbs_kwargs: Optional[dict] = None, verbose: bool = True,
               on_error: str = "collect") -> pd.DataFrame:
    """Fetch Destatis, FRED, Eurostat and CBS codes in one call; one column per code, one row per month.

    df.attrs['completed'] lists the codes that were fetched, df.attrs['failed'] maps the
    failed codes to their error message (see get_destatis_prices for verbose / on_error).
    """
    frames = []
    common = {"verbose": verbose, "on_error": on_error}
    if _as_list(destatis_codes):
        frames.append(get_destatis_prices(destatis_codes, start, end,
                                          **dict(common, **(destatis_kwargs or {}))))
    if _as_list(fred_codes):
        frames.append(get_fred_prices(fred_codes, start, end,
                                      **dict(common, **(fred_kwargs or {}))))
    if _as_list(eurostat_codes):
        frames.append(get_eurostat_prices(eurostat_codes, start, end,
                                          **dict(common, **(eurostat_kwargs or {}))))
    if _as_list(cbs_codes):
        frames.append(get_cbs_prices(cbs_codes, start, end,
                                     **dict(common, **(cbs_kwargs or {}))))
    if not frames:
        raise ValueError("Give at least one Destatis, FRED, Eurostat or CBS code.")
    out = pd.concat(frames, axis=1).sort_index()
    out.index.name = "month"
    failed, completed = {}, []
    for frame in frames:
        out.attrs.update(frame.attrs)
        failed.update(frame.attrs.get("failed", {}))
        completed.extend(frame.attrs.get("completed", []))
    out.attrs["failed"], out.attrs["completed"] = failed, completed
    return out


# --------------------------------------------------------------------------- #
# command line
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Download monthly price index series from "
                                            "Destatis GENESIS-Online, FRED, Eurostat and CBS by code.")
    p.add_argument("--destatis", nargs="*", default=[], metavar="CODE",
                   help="GP 2019 codes, e.g. GP19-237012")
    p.add_argument("--fred", nargs="*", default=[], metavar="ID",
                   help="FRED series ids, e.g. PCU327991327991")
    p.add_argument("--eurostat", nargs="*", default=[], metavar="NACE",
                   help="NACE Rev. 2 codes for Eurostat, e.g. C25 C237")
    p.add_argument("--geo", default=EUROSTAT_DEFAULT_GEO,
                   help="Eurostat country, default EU27_2020 (e.g. NL, DE)")
    p.add_argument("--cbs", nargs="*", default=[], metavar="CODE",
                   help="CBS ProdCom/CPA codes, e.g. 237 2331 (consumption prices in NL)")
    p.add_argument("--cbs-market", default=CBS_DEFAULT_MARKET, choices=sorted(CBS_MARKETS),
                   help="CBS price type, default verbruik (imports + domestic sales)")
    p.add_argument("--start", default="2015-01", help="first month, default 2015-01")
    p.add_argument("--end", default=None, help="last month (default: all available)")
    p.add_argument("--table", default=DESTATIS_DEFAULT_TABLE, help="GENESIS table code")
    p.add_argument("--value-variable", default=DESTATIS_DEFAULT_MEASURE,
                   help="measure to keep (part of its code or label), default PRE001")
    p.add_argument("--classifying-variable", default=None,
                   help="GENESIS variable holding the code (default: read from the table metadata)")
    p.add_argument("--language", default="en", choices=["de", "en"])
    p.add_argument("--save-raw", default=None, metavar="DIR",
                   help="also store the raw GENESIS answers in this folder")
    p.add_argument("--allow-job", action="store_true",
                   help="start a GENESIS background job if the request is too big "
                        "(needs DESTATIS_USER and DESTATIS_PASSWORD)")
    p.add_argument("--check-login", action="store_true",
                   help="only test the Destatis credentials and exit")
    p.add_argument("--describe-table", action="store_true",
                   help="print the GP19M... variable names of --table and exit")
    p.add_argument("--out", default=None, help="write the table to this CSV file")
    p.add_argument("-v", "--verbose", action="store_true", help="also log every HTTP step")
    p.add_argument("--quiet", action="store_true", help="no per-code status lines")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")
    try:
        if args.check_login:
            print(check_destatis_login())
            return 0
        if args.describe_table:
            meta = describe_destatis_table(args.table, args.language, raw_dir=args.save_raw)
            names = find_in_metadata(meta, r"^GP19M")
            print("Variables matching ^GP19M in table %s: %s" % (args.table, names or "none found"))
            if not names:
                print("Top-level keys of the answer: %s (use --save-raw DIR to keep the full JSON)"
                      % sorted(meta))
            return 0
        df = get_prices(
            args.destatis, args.fred, args.start, args.end,
            destatis_kwargs={"table": args.table, "value_variable": args.value_variable,
                             "classifying_variable": args.classifying_variable,
                             "language": args.language, "raw_dir": args.save_raw,
                             "allow_job": args.allow_job},
            eurostat_codes=args.eurostat, eurostat_kwargs={"geo": args.geo},
            cbs_codes=args.cbs, cbs_kwargs={"market": args.cbs_market},
            verbose=not args.quiet,
        )
    except (DestatisError, FredError, EurostatError, CbsError, ValueError,
            ConnectionError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    print(df.to_string(float_format=lambda v: "%.2f" % v))
    if args.out:
        df.to_csv(args.out, date_format="%Y-%m")
        print("Saved %s" % args.out)
    failed = df.attrs.get("failed", {})
    if failed:
        print("FAILED codes (%d): %s" % (len(failed), ", ".join(failed)), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
