"""BigQuery access for core, on the app's BQ_* settings (the same table the validations read). READ-ONLY: the app
creates nothing in the customer's dataset. A refresh runs one script in a BigQuery session, whose temporary tables
(BigQuery's own scratch space) the following queries read; the results land in the app's database.

    BQ_TABLE            project.dataset.table_or_view, one row per call
    BQ_REQUEST_COLUMN   the request body (messages, tools, model)
    BQ_RESPONSE_COLUMN  the chat completion (model, usage)
    BQ_ID_COLUMN        the call's id
    BQ_AGENT_COLUMN     the usecase
    BQ_TIME_COLUMN      the call's timestamp (partition column)
"""
import datetime

import credentials
from connectors.bigquery.source import _client


def cfg():
    table = credentials.get_config("BQ_TABLE")
    if not table:
        raise RuntimeError("BQ_TABLE is not set")
    project = table.split(".")[0]
    return {"table": table, "project": project,
            "req": credentials.get_config("BQ_REQUEST_COLUMN", "request_payload"),
            "resp": credentials.get_config("BQ_RESPONSE_COLUMN", "response_payload"),
            "id": credentials.get_config("BQ_ID_COLUMN", "request_id"),
            "usecase": credentials.get_config("BQ_AGENT_COLUMN", "vegas_usecase_name"),
            "time": credentials.get_config("BQ_TIME_COLUMN", "created_timestamp_utc"),
            "latency": credentials.get_config("BQ_LATENCY_COLUMN", "duration_ms")}      # ms per call, when the log has it


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

    def query(self, sql, params=None, timeout=1800):
        from google.cloud import bigquery
        from google.auth.exceptions import RefreshError
        cfg_ = bigquery.QueryJobConfig(query_parameters=_params(params),
                                       connection_properties=[bigquery.ConnectionProperty("session_id", self.id)])
        try:
            return list(client().query(sql, job_config=cfg_, location=location()).result(timeout=timeout))
        except RefreshError:                  # the dev machine's gcloud token expired mid-run: a fresh client, once
            _clients.pop(cfg()["project"], None)
            return list(client().query(sql, job_config=cfg_, location=location()).result(timeout=timeout))

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
