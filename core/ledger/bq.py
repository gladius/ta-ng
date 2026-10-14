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


class Session:
    """A BigQuery session: temp tables made by one query stay readable by the next, for 24 h or until closed."""
    def __init__(self):
        from google.cloud import bigquery
        job = client().query("SELECT 1", job_config=bigquery.QueryJobConfig(create_session=True), location=location())
        job.result()
        self.id = job.session_info.session_id

    def _cfg(self, params):
        from google.cloud import bigquery
        cap = float(credentials.get_config("BQ_MAX_TB") or 2) * 1e12
        return bigquery.QueryJobConfig(query_parameters=_params(params), maximum_bytes_billed=int(cap),
                                       connection_properties=[bigquery.ConnectionProperty("session_id", self.id)])

    def query(self, sql, params=None, timeout=1800):
        """One statement in the session. Reads over BQ_MAX_TB (default 2 TB) are refused before they run."""
        from google.auth.exceptions import RefreshError
        try:
            return list(client().query(sql, job_config=self._cfg(params), location=location()).result(timeout=timeout))
        except RefreshError:                  # the dev machine's gcloud token expired mid-run: a fresh client, once
            _clients.pop(cfg()["project"], None)
            return list(client().query(sql, job_config=self._cfg(params), location=location()).result(timeout=timeout))

    def script(self, sql, params=None, timeout=4 * 3600, progress=None, every=15):
        """A multi-statement script, polled every `every` seconds; `progress(text)` gets the statement running (its
        first comment line, e.g. "-- 1. the log, read once") so a page can show how far it is."""
        import time
        job = client().query(sql, job_config=self._cfg(params), location=location())
        t0 = time.time()
        while not job.done():
            if progress:
                try:
                    kids = sorted(client().list_jobs(parent_job=job.job_id, max_results=200), key=lambda k: k.created)
                    running = [k for k in kids if k.state == "RUNNING"]
                    head = ((running or kids)[-1].query if kids else "").strip().splitlines()
                    progress("%s (%d statements done, %d min)" % (next((l.lstrip("- ").strip() for l in head if l.startswith("--")),
                                                                     head[0][:40] if head else "starting"), len(kids) - len(running), (time.time() - t0) / 60))
                except Exception:
                    pass
            if time.time() - t0 > timeout:
                client().cancel_job(job.job_id, location=location())
                raise TimeoutError("the day script ran over %d h" % (timeout // 3600))
            time.sleep(every)
        job.result()
        return job

    def close(self):
        try:
            self.query("CALL BQ.ABORT_SESSION()")
        except Exception:
            pass


def query(sql, params=None, timeout=600):
    """One query outside a session."""
    from google.cloud import bigquery
    return list(client().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=_params(params)), location=location())
                .result(timeout=timeout))
