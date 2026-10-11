"""BigQuery access for core, on the app's BQ_* settings (the same table the validations read).

    BQ_TABLE            project.dataset.table_or_view, one row per call
    BQ_REQUEST_COLUMN   the request body (messages, tools, model)
    BQ_RESPONSE_COLUMN  the chat completion (model, usage)
    BQ_ID_COLUMN        the call's id
    BQ_AGENT_COLUMN     the usecase
    BQ_TIME_COLUMN      the call's timestamp (partition column)
Derived tables land next to BQ_TABLE in BQ_WORK_DATASET (default: the same dataset).
"""
import json

import credentials
from connectors.bigquery.source import _client


def cfg():
    table = credentials.get_config("BQ_TABLE")
    if not table:
        raise RuntimeError("BQ_TABLE is not set")
    project, dataset, _ = table.split(".")
    return {"table": table, "project": project, "dataset": credentials.get_config("BQ_WORK_DATASET") or dataset,
            "req": credentials.get_config("BQ_REQUEST_COLUMN", "request_payload"),
            "resp": credentials.get_config("BQ_RESPONSE_COLUMN", "response_payload"),
            "id": credentials.get_config("BQ_ID_COLUMN", "request_id"),
            "usecase": credentials.get_config("BQ_AGENT_COLUMN", "vegas_usecase_name"),
            "time": credentials.get_config("BQ_TIME_COLUMN", "created_timestamp_utc")}


_clients = {}


def client():
    project = cfg()["project"]
    if project not in _clients:
        _clients[project] = _client(project)
    return _clients[project]


def query(sql, params=None, timeout=600):
    from google.cloud import bigquery
    import datetime
    def kind(v):
        return "INT64" if isinstance(v, int) else "DATE" if isinstance(v, datetime.date) else "STRING"
    qp = [bigquery.ArrayQueryParameter(k, kind(v[0]) if v else "STRING", v) if isinstance(v, list)
          else bigquery.ScalarQueryParameter(k, kind(v), v) for k, v in (params or {}).items()]
    from google.auth.exceptions import RefreshError
    try:
        return list(client().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=qp)).result(timeout=timeout))
    except RefreshError:                      # the dev machine's gcloud token expired mid-run: a fresh client, once
        _clients.pop(cfg()["project"], None)
        return list(client().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=qp)).result(timeout=timeout))


def work(name):
    """Fully qualified name of a derived table."""
    c = cfg()
    return "`%s.%s.%s`" % (c["project"], c["dataset"], name)


def load_rows(name, rows, schema, replace=False):
    """Load dict rows into a derived table (created if missing)."""
    from google.cloud import bigquery
    c = client()
    job = c.load_table_from_json(rows, work(name).strip("`"), job_config=bigquery.LoadJobConfig(
        schema=[bigquery.SchemaField(n, t, mode=m) for n, t, m in schema],
        write_disposition="WRITE_TRUNCATE" if replace else "WRITE_APPEND"))
    job.result()
    return job.output_rows


def system_text(first_message):
    """The text of a logged message whose content is a string or a list of parts."""
    m = json.loads(first_message) if isinstance(first_message, str) else (first_message or {})
    c = m.get("content")
    if isinstance(c, list):
        return "\n".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c if isinstance(c, str) else ""
