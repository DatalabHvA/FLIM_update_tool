"""Robust UN Comtrade import fetcher (replacement for get_NL_import).

What it fixes compared with a plain requests.get(...).json().get("data", []):

  * An error reply (rate limit, 5xx, bad key) is NOT turned into "no data".
    It is retried (rate limit / 5xx / network errors) or reported as failed.
  * "Valid answer, zero rows" (no trade) is kept apart from "request failed".
  * Retries happen inside the function, honouring Retry-After or the
    "Try again in N second(s)" text, so no outer retry loop is needed.
  * Batching: several HS codes in one request (cmdCode="A,B,C"). A batch that
    fails because of ONE bad code, or whose answer looks truncated, is split in
    halves until only the codes that fail on their own are left. Problems that
    splitting cannot fix (bad key, quota, rate limit that does not go away,
    service down) stop the run at once instead of hammering the API.

Usage in a notebook:

    from comtrade_imports import get_imports
    res = get_imports(["252310", "690721"], period="2024", api_key="...")
    res.data      # DataFrame: code, partnerISO, primaryValue
    res.no_data   # codes for which Comtrade answered with zero rows
    res.failed    # {code: error message} - re-run only these if needed
    res.requests  # number of HTTP requests sent (retries included)

The key can also be given through the environment variable COMTRADE_API_KEY.
Codes must be strings (a leading zero is lost if you pass the integer 901).

Items marked "assumption" below were NOT verified against the live API.
"""

import os
import re
import time
from typing import NamedTuple

import pandas as pd
import requests

URL = "https://comtradeapi.un.org/data/v1/get/C/A/HS"
RETRY_STATUS = (429, 500, 502, 503, 504)

# Indirection so tests can replace the clock.
_sleep = time.sleep
_now = time.monotonic


class ComtradeError(Exception):
    """A request failed and the request itself seems to be at fault."""


class ComtradeServiceError(ComtradeError):
    """The key, quota, rate limit or service is the problem, not the requested codes.

    Splitting the request cannot help, so the run stops when this is raised.
    """


class ImportResult(NamedTuple):
    data: pd.DataFrame
    no_data: list
    failed: dict
    requests: int = 0


def _json_or_none(resp):
    try:
        return resp.json()
    except ValueError:
        return None


def _message(payload, resp):
    """Best-effort human readable text of an answer."""
    if isinstance(payload, dict):
        for key in ("message", "error", "Message"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
    return (resp.text or "")[:200]


def _is_rate_limit(text):
    return "rate limit" in (text or "").lower()


def _retry_wait(resp, text, attempt):
    """Seconds to wait before the next try."""
    header = resp.headers.get("Retry-After")
    if header is not None:
        try:
            return float(header) + 0.5
        except ValueError:
            pass
    match = re.search(r"try again in (\d+(?:\.\d+)?)\s*second", text or "", re.I)
    if match:
        return float(match.group(1)) + 0.5
    return float(min(2 ** attempt, 30))


class _Client:
    def __init__(self, api_key, min_interval, max_retries, max_wait, timeout,
                 session, verbose=False, log_fn=None):
        self.verbose = verbose
        self.log_fn = log_fn
        self.tag = ""
        self.api_key = api_key
        self.headers = {"Ocp-Apim-Subscription-Key": api_key}
        self.min_interval = min_interval
        self.max_retries = max_retries
        self.max_wait = max_wait
        self.timeout = timeout
        self.session = session if session is not None else requests.Session()
        self._last = None
        self.requests = 0

    def log(self, message):
        if self.log_fn is not None:
            self.log_fn(message)
        elif self.verbose:
            print(message, flush=True)

    def _throttle(self):
        if self._last is not None:
            gap = self.min_interval - (_now() - self._last)
            if gap > 0:
                _sleep(gap)
        self._last = _now()

    def get(self, params):
        """Return the parsed JSON of a successful answer.

        Raises ComtradeServiceError when the key, quota, rate limit or service is the
        problem, and ComtradeError when this particular request seems to be at fault
        (HTTP 400/404/..., HTTP 500 again after one retry, a 200 with an error text).
        """
        last = ""
        for attempt in range(self.max_retries + 1):
            self._throttle()
            self.requests += 1
            wait = None
            try:
                resp = self.session.get(URL, params=params, headers=self.headers,
                                        timeout=self.timeout)
            except requests.RequestException as exc:
                last = "network error: %s" % exc.__class__.__name__
                wait = float(min(2 ** attempt, 30))
            else:
                payload = _json_or_none(resp)
                text = _message(payload, resp).replace(self.api_key, "***")
                status = resp.status_code
                if status == 200 and not _is_rate_limit(text):
                    return self._check_ok(payload, text)
                if status in (401, 403):
                    raise ComtradeServiceError("HTTP %d: %s" % (status, text))
                if status == 500 and attempt >= 1:
                    # Assumption: a 500 that comes back after a retry is caused by
                    # this request (e.g. an input the server chokes on), so splitting
                    # may isolate it. 502/503/504 are treated as service problems.
                    raise ComtradeError("HTTP 500 after a retry: %s" % text)
                if status in RETRY_STATUS or _is_rate_limit(text):
                    last = "HTTP %d: %s" % (status, text)
                    wait = _retry_wait(resp, text, attempt)
                else:
                    raise ComtradeError("HTTP %d: %s" % (status, text))
            if attempt == self.max_retries:
                break
            if wait > self.max_wait:
                raise ComtradeServiceError(
                    "server asks to wait %.0f s (> max_wait=%.0f s): %s"
                    % (wait, self.max_wait, last))
            self.log("    %s -> waiting %.1f s (retry %d/%d)"
                     % (last, wait, attempt + 1, self.max_retries))
            _sleep(wait)
        raise ComtradeServiceError("giving up after %d attempts, last answer: %s"
                                   % (self.max_retries + 1, last))

    @staticmethod
    def _check_ok(payload, text):
        if not isinstance(payload, dict):
            raise ComtradeServiceError(
                "HTTP 200 but the body is not a JSON object: %s" % text)
        # Assumption: a successful answer has a "data" list (possibly empty)
        # and/or "count". Anything else is reported, not treated as "no data".
        if "data" not in payload and payload.get("count") != 0:
            raise ComtradeError("HTTP 200 without a 'data' key: %s" % text)
        error = payload.get("error")
        if isinstance(error, str) and error:
            raise ComtradeError("HTTP 200 with error text: %s" % error)
        return payload


class _State:
    """What a get_imports run has collected so far."""

    def __init__(self):
        self.rows = []
        self.failed = {}
        self.stop = None      # message once the run has been stopped


def _attempt(client, codes, base_params, max_records, st):
    """One request for `codes`: returns (kind, message).

    kind is "ok" (rows stored), "truncated", "bad" (this request seems at fault)
    or "stop" (key / quota / service problem).
    """
    params = dict(base_params, cmdCode=",".join(codes),
                  maxRecords=str(max_records))
    client.log("%s requesting %s" % (client.tag, _fmt_codes(codes)))
    started = _now()
    try:
        payload = client.get(params)
    except ComtradeServiceError as exc:
        client.log("    STOPPING after %.1f s: %s" % (_now() - started, exc))
        return "stop", str(exc)
    except ComtradeError as exc:
        client.log("    request failed after %.1f s: %s" % (_now() - started, exc))
        return "bad", str(exc)
    data = payload.get("data") or []
    count = payload.get("count")
    client.log("    answer: %d rows in %.1f s" % (len(data), _now() - started))
    if len(data) >= max_records or (isinstance(count, int) and count > len(data)):
        return "truncated", ("answer truncated at max_records=%d (count=%s)"
                             % (max_records, count))
    st.rows.extend(data)
    return "ok", ""


def _solve(client, codes, base_params, max_records, st, known_bad=False):
    """Fetch `codes` with as few requests as possible; True if all were fetched.

    A failing or truncated multi-code request is split in halves. If the first half
    then succeeds completely and the request had FAILED (not merely truncated), the
    failure must be in the second half, so that half is not requested as a whole but
    split straight away (saves one request per level). A code is only reported as
    failed after a request for that code alone failed, so a wrong inference can cost
    extra requests but never produces a wrong "failed".
    """
    if st.stop:
        for code in codes:
            st.failed[code] = "not attempted: " + st.stop
        return False
    bad = known_bad
    if known_bad and len(codes) > 1:
        client.log("    a failure must be in %s: splitting without testing it as a "
                   "whole" % _fmt_codes(codes))
    else:
        kind, message = _attempt(client, codes, base_params, max_records, st)
        if kind == "ok":
            return True
        if kind == "stop":
            st.stop = message
            for code in codes:
                st.failed[code] = message
            return False
        if len(codes) == 1:
            st.failed[codes[0]] = message + ("; raise max_records"
                                             if kind == "truncated" else "")
            client.log("    %s fails on its own" % codes[0])
            return False
        bad = kind == "bad"
        client.log("    splitting %d codes into %d + %d"
                   % (len(codes), len(codes) // 2, len(codes) - len(codes) // 2))
    mid = len(codes) // 2
    left_ok = _solve(client, codes[:mid], base_params, max_records, st)
    right_ok = _solve(client, codes[mid:], base_params, max_records, st,
                      known_bad=bad and left_ok)
    return left_ok and right_ok


def _base_params(period, reporter, flow):
    return {
        "period": str(period), "flowCode": flow, "reporterCode": str(reporter),
        "customsCode": "C00", "motCode": "0", "breakdownMode": "classic",
        "includeDesc": "true",
    }


def _fmt_codes(codes):
    if len(codes) <= 4:
        return ",".join(codes)
    return "%s ... (+%d more)" % (",".join(codes[:3]), len(codes) - 3)


def _as_code_list(codes):
    if isinstance(codes, str):
        codes = [codes]
    seen, out = set(), []
    for code in codes:
        code = str(code).strip()
        if code and code not in seen:
            seen.add(code)
            out.append(code)
    return out


def get_imports(codes, *, period="2024", reporter="528", flow="M",
                api_key=None, batch_size=5, max_records=10000,
                min_interval=1.0, max_retries=5, max_wait=120.0,
                timeout=60.0, session=None, verbose=True, log=None,
                on_progress=None):
    """Imports per partner country for a list of HS codes (annual, HS, C).

    reporter "528" = Netherlands, flow "M" = imports. Rows with partner W00
    (World total) are dropped, other rows are summed per (code, partner).

    batch_size: HS codes per request. 5 was checked live with probe_batching (same
    rows as single-code requests for 5 HS 4407/4408 codes, NL imports 2024); larger
    values are untested. 1 behaves like the old function.
    Failing batches are split in halves until only codes that fail on their own are
    left (res.failed); a batch whose answer looks truncated is split the same way
    (see probe_batching to check that detection against the real API). For one bad
    code in a batch of 5 this takes about 5 requests; a clean batch takes 1.
    Errors that splitting cannot fix (HTTP 401/403, rate limit or service errors
    that survive max_retries, a wait longer than max_wait) stop the run: the codes
    not yet tried are listed in res.failed as "not attempted: ...".
    max_records: 10000 was accepted by the API in that live check.
    min_interval: minimum seconds between requests (my guess, not documented).
    max_wait: if the server asks to wait longer than this (e.g. a daily quota),
    the run stops instead of sleeping.
    verbose: print one line per request (codes, rows, seconds), every retry
    wait, a rough time-left estimate and a final summary.
    log: optional callable(str) that receives those lines instead of print().
    on_progress: optional callable(done, total, failed) called after each batch;
    done = number of codes handled so far, failed = {code: message} so far.
    """
    codes = _as_code_list(codes)
    key = api_key or os.environ.get("COMTRADE_API_KEY")
    if not key:
        raise ComtradeError("No API key: pass api_key= or set COMTRADE_API_KEY.")
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")

    client = _Client(key, min_interval, max_retries, max_wait, timeout, session,
                     verbose, log)
    base_params = _base_params(period, reporter, flow)
    st = _State()
    if on_progress is not None:
        on_progress(0, len(codes), {})
    n_batches = -(-len(codes) // batch_size)
    start = _now()
    for n, i in enumerate(range(0, len(codes), batch_size), start=1):
        elapsed = _now() - start
        left = ""
        if n > 1:
            left = ", ~%.0f s left" % (elapsed / (n - 1) * (n_batches - n + 1))
        client.tag = "[%d/%d, %.0f s elapsed%s]" % (n, n_batches, elapsed, left)
        _solve(client, codes[i:i + batch_size], base_params, max_records, st)
        if on_progress is not None:
            on_progress(min(i + batch_size, len(codes)), len(codes), dict(st.failed))

    rows, failed = st.rows, st.failed
    columns = ["code", "partnerISO", "primaryValue"]
    if not rows:
        result = ImportResult(pd.DataFrame(columns=columns),
                              [c for c in codes if c not in failed], failed,
                              client.requests)
    else:
        df = pd.DataFrame(rows)
        needed = {"cmdCode", "partnerISO", "primaryValue"}
        if not needed <= set(df.columns):
            raise ComtradeError("Unexpected columns in the answer: %s"
                                % sorted(df.columns))
        df["cmdCode"] = df["cmdCode"].astype(str)
        returned = set(df["cmdCode"])
        df = df[df["partnerISO"] != "W00"]
        data = (df.groupby(["cmdCode", "partnerISO"], as_index=False)
                  ["primaryValue"].sum()
                  .rename(columns={"cmdCode": "code"})
                  .sort_values(["code", "primaryValue"],
                               ascending=[True, False], ignore_index=True))
        no_data = [c for c in codes if c not in returned and c not in failed]
        result = ImportResult(data[columns], no_data, failed, client.requests)
    client.log("done in %.0f s, %d requests: %d codes with data, %d without data, "
               "%d failed%s"
               % (_now() - start, client.requests, result.data["code"].nunique(),
                  len(result.no_data), len(result.failed),
                  " (run stopped early)" if st.stop else ""))
    return result


def _pairs(rows):
    """Comparable fingerprint of rows: sorted (partnerISO, primaryValue) pairs."""
    return sorted(((str(r.get("partnerISO")), str(r.get("primaryValue"))) for r in rows))


def probe_batching(codes, *, sizes=(2, 5), api_key=None, period="2024",
                   reporter="528", flow="M", max_records=10000, min_interval=1.0,
                   session=None):
    """Live experiment: does a batched request return the same rows as single requests?

    Fetches the first max(sizes) codes one by one, then once per batch size n the first n
    codes in ONE request (cmdCode="A,B,..."), and compares the answers code by code.
    Costs max(sizes) + len(sizes) API calls. Prints what it finds and returns a DataFrame
    with one row per batch size:

      rows_returned / rows_expected   rows in the batched answer / sum of the single answers
      count_field                     the "count" of the batched answer (None if absent)
      looks_truncated                 rows >= max_records, or count > rows
      same_as_single                  every code has identical (partnerISO, primaryValue) rows
      extra_codes                     cmdCode values in the answer that you did not ask for

    Batching is only worth using if same_as_single is True for the sizes you want.
    To see how the API reports truncation, run it once with a small max_records (e.g. 50):
    compare count_field with rows_returned.
    """
    codes = _as_code_list(codes)
    sizes = sorted({int(n) for n in sizes if 2 <= int(n) <= len(codes)})
    if not sizes:
        raise ValueError("Need at least 2 codes and a batch size of at least 2.")
    key = api_key or os.environ.get("COMTRADE_API_KEY")
    if not key:
        raise ComtradeError("No API key: pass api_key= or set COMTRADE_API_KEY.")
    client = _Client(key, min_interval, 5, 120.0, 60.0, session, True)
    base = _base_params(period, reporter, flow)
    used = codes[:max(sizes)]

    singles = {}
    for n, code in enumerate(used, start=1):
        client.log("[single %d/%d] %s" % (n, len(used), code))
        payload = client.get(dict(base, cmdCode=code, maxRecords=str(max_records)))
        singles[code] = payload.get("data") or []
        client.log("    %d rows, count=%s" % (len(singles[code]), payload.get("count")))
        if n == 1:
            client.log("    answer keys: %s" % sorted(payload))
            if singles[code]:
                client.log("    row keys: %s" % sorted(singles[code][0]))

    results = []
    for size in sizes:
        batch = used[:size]
        client.log("[batch of %d] %s" % (size, ",".join(batch)))
        started = _now()
        payload = client.get(dict(base, cmdCode=",".join(batch),
                                  maxRecords=str(max_records)))
        seconds = _now() - started
        data = payload.get("data") or []
        count = payload.get("count")
        returned = {str(r.get("cmdCode")) for r in data}
        per_code = {c: _pairs([r for r in data if str(r.get("cmdCode")) == c])
                    == _pairs(singles[c]) for c in batch}
        results.append({
            "batch_size": size, "codes": ",".join(batch),
            "rows_returned": len(data),
            "rows_expected": sum(len(singles[c]) for c in batch),
            "count_field": count,
            "looks_truncated": len(data) >= max_records
                               or (isinstance(count, int) and count > len(data)),
            "same_as_single": all(per_code.values()),
            "differs_for": [c for c, same in per_code.items() if not same],
            "extra_codes": sorted(returned - set(batch)),
            "seconds": round(seconds, 1),
        })
        last = results[-1]
        client.log("    %d rows (expected %d), count=%s, same as single: %s%s"
                   % (last["rows_returned"], last["rows_expected"], count,
                      last["same_as_single"],
                      ", TRUNCATED?" if last["looks_truncated"] else ""))
    return pd.DataFrame(results)
