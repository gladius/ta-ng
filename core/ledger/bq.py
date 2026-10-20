"""BigQuery access for core, on the app's BQ_* settings (the same table the validations read). READ-ONLY: the app
creates nothing in the customer's dataset. A refresh runs one script in a BigQuery session, whose temporary tables
(BigQuery's own scratch space) the following queries read; the results land in the app's database.

    BQ_TABLE            project.dataset.table_or_view, one row per call
    BQ_REQUEST_COLUMN   the request body (messages, tools, model)
    BQ_RESPONSE_COLUMN  the chat completion (model, usage)
    BQ_ID_COLUMN        the call's id
    BQ_USECASE_COLUMN   the usecase
    BQ_TIME_COLUMN      the call's timestamp (partition column)
    BQ_LATENCY_COLUMN   the call's duration in ms

All are required, with no defaults: a guessed column silently reads the wrong data.
"""
import datetime

import credentials
from connectors.bigquery.source import _client

SETTINGS = {"table": "BQ_TABLE", "req": "BQ_REQUEST_COLUMN", "resp": "BQ_RESPONSE_COLUMN", "id": "BQ_ID_COLUMN",
            "usecase": "BQ_USECASE_COLUMN", "time": "BQ_TIME_COLUMN", "latency": "BQ_LATENCY_COLUMN"}


def skipped():
    """SKIP_USECASES: comma-separated LIKE patterns of usecases never analysed (e.g. `user-%` for developer traffic);
    empty: none. Used by every query that reads the log by usecase, and published to the router."""
    return [p.strip() for p in (credentials.get_config("SKIP_USECASES") or "").split(",") if p.strip()]


def only():
    """ONLY_USECASES: comma-separated LIKE patterns; set, only these usecases are read and listed (validating the
    auditor on two usecases before all). Empty: every usecase but the skipped ones."""
    return [p.strip() for p in (credentials.get_config("ONLY_USECASES") or "").split(",") if p.strip()]


def filters():
    """The query parameters of SKIP_FILTER."""
    return {"skip": skipped(), "only": only()}


# a usecase column is kept unless it matches a pattern of @skip, and, when @only is set, only if it matches one of
# @only; an empty list keeps everything; an untagged call (NULL usecase) is compared as ''
SKIP_FILTER = ("NOT EXISTS (SELECT 1 FROM UNNEST(@skip) p WHERE COALESCE(`{col}`, '') LIKE p)"
               " AND (COALESCE(ARRAY_LENGTH(@only), 0) = 0 OR EXISTS (SELECT 1 FROM UNNEST(@only) p WHERE COALESCE(`{col}`, '') LIKE p))")


def configured():
    return all(credentials.get_config(k) for k in SETTINGS.values())


def cfg():
    c = {k: credentials.get_config(v) for k, v in SETTINGS.items()}
    missing = [SETTINGS[k] for k, v in c.items() if not v]
    if missing:
        raise RuntimeError("BigQuery settings missing: " + ", ".join(missing))
    return {**c, "project": c["table"].split(".")[0]}


_clients = {}


def client():
    project = cfg()["project"]
    if project not in _clients:
        _clients[project] = _client(project)
    return _clients[project]


def _params(params):
    from google.cloud import bigquery

    def kind(v):
        return "INT64" if isinstance(v, int) else "FLOAT64" if isinstance(v, float) else \
            "DATE" if isinstance(v, datetime.date) else "STRING"
    def struct(d):
        return bigquery.StructQueryParameter(None, *[bigquery.ScalarQueryParameter(n, kind(x), x) for n, x in d.items()])
    out = []
    for k, v in (params or {}).items():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            out.append(bigquery.ArrayQueryParameter(k, "STRUCT", [struct(d) for d in v]))
        elif isinstance(v, list):
            out.append(bigquery.ArrayQueryParameter(k, kind(v[0]) if v else "STRING", v))
        else:
            out.append(bigquery.ScalarQueryParameter(k, kind(v), v))
    return out


_locations = {}


def location():
    """The log's dataset location (EU, US, ...): every job, the session included, must run there."""
    c = cfg()
    key = ".".join(c["table"].split(".")[:2])
    if key not in _locations:
        _locations[key] = client().get_dataset(key).location
    return _locations[key]


SLOW_S = 30             # a BigQuery job slower than this gets its own log line


def label(sql):
    """A job's name for the log: its first comment line, else its first statement's table ("hits", "placed", ...)."""
    lines = [l.strip() for l in (sql or "").strip().splitlines() if l.strip()]
    for l in lines:
        if l.startswith("--"):
            return l.lstrip("- ")[:60]
    words = (lines[0] if lines else "").replace("CREATE OR REPLACE TEMP TABLE ", "").replace("CREATE TEMP TABLE ", "") \
        .replace("INSERT INTO ", "").replace("UPDATE ", "").split()
    return " ".join(words[:2])[:60] if words else "?"


class Session:
    """A BigQuery session: temp tables made by one query stay readable by the next, for 24 h or until closed. Every
    job it runs is timed into `totals` (jobs, seconds, bytes read, slot ms); one slower than SLOW_S is logged to the
    running task with `day` as its prefix."""
    def __init__(self, session_id=None):
        """A new session, or an existing one by id (its temp tables still there if it is under 24 h old)."""
        from google.cloud import bigquery
        self.totals = {"jobs": 0, "s": 0.0, "bytes": 0, "slot_ms": 0}
        self.day = ""
        if session_id:
            self.id = session_id
            return
        job = client().query("SELECT 1", job_config=bigquery.QueryJobConfig(create_session=True), location=location())
        job.result()
        self.id = job.session_info.session_id

    def has(self, table):
        """Does this temp table exist in the session (False when the session expired or the script never got there)."""
        try:
            self.query("SELECT 1 FROM %s LIMIT 0" % table, timeout=120)
            return True
        except Exception:
            return False

    def _cfg(self, params):
        from google.cloud import bigquery
        return bigquery.QueryJobConfig(query_parameters=_params(params), maximum_bytes_billed=max_bytes(),
                                       connection_properties=[bigquery.ConnectionProperty("session_id", self.id)])

    def _record(self, job, sql, t0):
        import time
        sec = time.time() - t0
        b, slot = int(getattr(job, "total_bytes_processed", 0) or 0), int(getattr(job, "slot_millis", 0) or 0)
        self.totals["jobs"] += 1
        self.totals["s"] += sec
        self.totals["bytes"] += b
        self.totals["slot_ms"] += slot
        if sec >= SLOW_S:
            from core import tasks
            tasks.note("%sBigQuery %s: %d s, %.1f GB read, %d slot-s" % (self.day + ": " if self.day else "", label(sql), sec, b / 1e9, slot / 1000))

    def query(self, sql, params=None, timeout=1800):
        """One statement (or a short script) in the session, timed. Reads over BQ_MAX_TB (default 2 TB) are refused
        before they run."""
        import time
        from google.auth.exceptions import RefreshError
        t0 = time.time()
        try:
            job = client().query(sql, job_config=self._cfg(params), location=location())
            rows = list(job.result(timeout=timeout))
        except RefreshError:                  # the dev machine's gcloud token expired mid-run: a fresh client, once
            _clients.pop(cfg()["project"], None)
            job = client().query(sql, job_config=self._cfg(params), location=location())
            rows = list(job.result(timeout=timeout))
        self._record(job, sql, t0)
        return rows

    def script(self, sql, params=None, timeout=4 * 3600, progress=None, every=15):
        """A multi-statement script, polled every `every` seconds; `progress(text)` gets the statement running (its
        first comment line, e.g. "-- 1. the log, read once") so a page can show how far it is."""
        import time
        job = client().query(sql, job_config=self._cfg(params), location=location())
        t0 = time.time()
        said, said_at = None, 0.0               # progress only when the statement changes, else every 5 minutes
        while not job.done():
            if progress:
                try:
                    kids = sorted(client().list_jobs(parent_job=job.job_id, max_results=200), key=lambda k: k.created)
                    running = [k for k in kids if k.state == "RUNNING"]
                    head = ((running or kids)[-1].query if kids else "").strip().splitlines()
                    now = next((l.lstrip("- ").strip() for l in head if l.startswith("--")), head[0][:40] if head else "starting")
                    if now != said or time.time() - said_at >= 300:
                        progress("%s (%d statements done, %d min)" % (now, len(kids) - len(running), (time.time() - t0) / 60))
                        said, said_at = now, time.time()
                except Exception:
                    pass
            if time.time() - t0 > timeout:
                client().cancel_job(job.job_id, location=location())
                raise TimeoutError("the day script ran over %d h" % (timeout // 3600))
            time.sleep(every)
        job.result()
        self._record(job, sql, t0)
        try:
            kids = sorted(client().list_jobs(parent_job=job.job_id, max_results=500), key=lambda k: -((k.ended - k.started).total_seconds() if k.ended and k.started else 0))
            self.timing = "BigQuery %d min; slowest: %s" % ((time.time() - t0) / 60, ", ".join(
                "%s %d s" % ((k.query or "").strip().splitlines()[0].replace("CREATE OR REPLACE TEMP TABLE ", "").replace("CREATE TEMP TABLE ", "")[:24],
                            (k.ended - k.started).total_seconds()) for k in kids[:3]))
        except Exception:
            self.timing = "BigQuery %d min" % ((time.time() - t0) / 60)
        return job

    def close(self):
        try:
            self.query("CALL BQ.ABORT_SESSION()")
        except Exception:
            pass


def max_bytes():
    """BQ_MAX_TB (default 2): a query that would read more is refused before it runs."""
    return int(float(credentials.get_config("BQ_MAX_TB") or 2) * 1e12)


def query(sql, params=None, timeout=600):
    """One query outside a session, under the byte cap."""
    from google.cloud import bigquery
    return list(client().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=_params(params), maximum_bytes_billed=max_bytes()),
                               location=location()).result(timeout=timeout))
