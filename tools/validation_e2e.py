"""Real end-to-end check of model audits of sent requests (paid: about $1 with the default gpt-5.4-mini judge).

    E2E_DATABASE_URL=postgresql+psycopg://... .venv/bin/python tools/validation_e2e.py [--reset]

Runs TWO app instances (the web app plus 1 in-process worker each, as one worker per Cloud Run deployment) on the
given database, through the gateway in .env. Job A: 25 full-body requests x 2 candidates. Job B: 3 requests sent by
request_id only, looked up in BigQuery (E2E_REQUEST_IDS=id1,id2,id3, else 3 picked from BQ_TABLE; skipped without
BigQuery). Mid-run instance 1 crashes (SIGKILL: its requests must go stale, then be taken over) and is restarted;
later instance 2 is stopped as in a redeploy (SIGTERM: it must hand its requests back at once) and is restarted.
Everything is then checked against the database itself, and a PASS/FAIL line is printed per check.

--reset drops the four validation_* tables first (never point it at a database whose jobs you want to keep).
Ports 8101-8104 and 8199 must be free. Logs: .data/e2e_logs/."""
import collections
import csv
import io
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sqlalchemy as sa

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
DB_URL = os.environ.get("E2E_DATABASE_URL") or sys.exit("set E2E_DATABASE_URL (a Postgres URL) first")
KEY = secrets.token_hex(16)
CB_PORT = 8199
PY = sys.executable
ENV = dict(os.environ, DATABASE_URL=DB_URL, VALIDATION_API_KEY=KEY, VALIDATION_INPROCESS_WORKERS="1",
           AUDIT_DEBUG="", PYTHONUNBUFFERED="1")
LOGS = os.path.join(REPO, ".data", "e2e_logs")
os.makedirs(LOGS, exist_ok=True)
JUDGE = os.environ.get("E2E_JUDGE", "gpt-5.4-mini")
T0 = time.time()
failures = []


def say(msg):
    print("%5.0fs  %s" % (time.time() - T0, msg), flush=True)


def check(ok, what):
    print("   %s  %s" % ("PASS" if ok else "FAIL", what), flush=True)
    if not ok:
        failures.append(what)


# ---- the batch: enterprise-agent style requests (tool calls, decisions, extraction, free text) -------------------
TRIAGE_SYS = ("You are Acme's support triage agent. Always open exactly one ticket with create_ticket. Priority: urgent = "
              "outage or many users blocked with a deadline; high = money wrong or many users affected; medium = one "
              "user blocked; low = a question.")
TRIAGE_TOOLS = [{"type": "function", "function": {
    "name": "create_ticket", "description": "Open a support ticket for the right team.",
    "parameters": {"type": "object", "required": ["team", "priority", "summary"], "properties": {
        "team": {"type": "string", "enum": ["billing", "technical", "account", "shipping"]},
        "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"]},
        "summary": {"type": "string"}}}}}]
TRIAGE = ["Our whole team can't log in since this morning, SSO returns error 500. Board demo in 2 hours.",
          "I was charged twice for invoice INV-2291 ($480 each). Please fix.",
          "Package for order 77812 shows delivered but nothing arrived.",
          "How do I change the email on my account? Not urgent.",
          "API latency went from 200ms to 9s for all our EU calls in the last hour."]
REFUND_SYS = ("You are a refunds agent. Policy: orders under $100 that arrived damaged or never arrived are refunded with "
              "issue_refund. Anything $100 or more, or any other reason, goes to escalate with a one-line reason.")
REFUND_TOOLS = [
    {"type": "function", "function": {"name": "issue_refund", "description": "Refund an order.",
     "parameters": {"type": "object", "required": ["order_id", "amount"], "properties": {
         "order_id": {"type": "string"}, "amount": {"type": "number"}}}}},
    {"type": "function", "function": {"name": "escalate", "description": "Send to a human.",
     "parameters": {"type": "object", "required": ["order_id", "reason"], "properties": {
         "order_id": {"type": "string"}, "reason": {"type": "string"}}}}}]
REFUND = ["Order A-311, $42.50, the mug arrived shattered.",
          "Order A-312, $260, the laptop stand never arrived.",
          "Order A-313, $18, I just don't like the colour.",
          "Order A-314, $99.99, box was empty when it arrived.",
          "Order A-315, $75, arrived damaged, and I'd also like a discount on my next order."]
INTENT_SYS = ("Classify the customer message into exactly one label: billing, cancel, bug, how_to, sales. Reply with the "
              "label only.")
INTENT = [("Cancel my subscription at the end of this month.", "cancel"),
          ("The export button does nothing in Firefox.", "bug"),
          ("Can I get a quote for 300 seats?", "sales"),
          ("Why was I billed $12 extra this month?", "billing"),
          ("Where do I find the API keys page?", "how_to")]
EXTRACT_SYS = ('Extract the fields as JSON: {"vendor": str, "invoice_number": str, "total": number, "currency": str, '
               '"due_date": "YYYY-MM-DD"}. Reply with JSON only.')
EXTRACT = ["Invoice 4471 from Northwind Traders. Total due: EUR 1,240.00 by 15 November 2026.",
           "Globex Corp bill #GX-0098, amount USD 87.5, payment due 2026-12-01.",
           "Initech invoice no. 22-AB, £3,000 total, please pay before Jan 3rd 2027.",
           "From: Umbrella Ltd. Ref INV/7/2026. Grand total 560.10 CHF. Due on 30.10.2026.",
           "Stark Industries invoice SI-1. Total $12,999. Net 30 from 2026-10-01."]
HR_SYS = ("You are an HR assistant for a company with this policy: 25 days paid leave per year, up to 5 unused days "
          "carry over to the next year and expire on March 31, sick leave is separate and unlimited with a doctor's "
          "note after 3 days. Answer in at most two sentences.")
HR = ["I have 8 days left at year end, what happens to them?",
      "Does being sick for 2 days use my vacation days?",
      "Can I use my carried-over days in June?",
      "How many vacation days do I get per year?",
      "I was sick for 4 days, do I need anything?"]


def items():
    out = []
    def req(system, user, tools=None, max_tokens=300):
        r = {"model": "gpt-5.4", "max_tokens": max_tokens,
             "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if tools:
            r.update(tools=tools, tool_choice="required")
        return r
    cands = [{"label": "mini", "model": "gpt-5.4-mini"}, {"label": "nano", "model": "gpt-5.4-nano"}]
    for i, u in enumerate(TRIAGE):                    # ids with a slash, a space, '#', '?' and non-ASCII on purpose
        out.append({"id": "triage/T-%d" % (1001 + i), "request": req(TRIAGE_SYS, u, TRIAGE_TOOLS), "candidates": cands})
    for i, u in enumerate(REFUND):
        out.append({"id": "refund #%d" % (2041 + i), "request": req(REFUND_SYS, u, REFUND_TOOLS), "candidates": cands})
    for i, (u, label) in enumerate(INTENT):          # these carry the original's recorded answer, as LiteLLM logs it
        out.append({"id": "intent-%d" % (i + 1), "request": req(INTENT_SYS, u, max_tokens=20), "candidates": cands,
                    "response": {"id": "r%d" % i, "model": "gpt-5.4", "choices": [
                        {"message": {"role": "assistant", "content": label}}],
                        "usage": {"prompt_tokens": 60, "completion_tokens": 2}}})
    for i, u in enumerate(EXTRACT):
        out.append({"id": "extract/inv-%d" % (i + 1), "request": req(EXTRACT_SYS, u), "candidates": cands})
    for i, u in enumerate(HR):
        out.append({"id": "hr/pto ü?%d" % (i + 1), "request": req(HR_SYS, u), "candidates": cands})
    return out


# ---- plumbing ----------------------------------------------------------------------------------------------------
def http(path, body=None, headers=None, key=True, port=None):
    h = dict(headers or {})
    if key:
        h["X-API-Key"] = KEY
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port or up[0], path), data=data, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            ctype = r.headers.get("Content-Type", "")
            return r.status, dict(r.headers), (json.loads(raw) if "json" in ctype and raw else raw.decode())
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


callbacks = []


class CB(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        callbacks.append((self.path, time.time() - T0, body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


procs = {}                  # instance name -> (process, log, port)
up = []                     # ports of the instances serving now


def spawn(name, port):
    """Start one app instance: the web app plus its 2 in-process workers, on the shared Postgres."""
    log = open(os.path.join(LOGS, name + ".log"), "w")
    p = subprocess.Popen([PY, "-m", "uvicorn", "app.web.server:app", "--host", "127.0.0.1", "--port", str(port)],
                         cwd=REPO, env=ENV, stdout=log, stderr=subprocess.STDOUT)
    procs[name] = (p, log, port)
    for _ in range(120):
        try:
            if http("/api/audit/models", port=port)[0] == 200:
                up.append(port)
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("%s never came up" % name)


def down(name, sig):
    p, _, port = procs[name]
    p.send_signal(sig)
    up.remove(port)


def stop_all():
    for name, (p, log, _) in procs.items():
        if p.poll() is None:
            p.terminate()
            try:
                p.wait(20)
            except subprocess.TimeoutExpired:
                p.kill()
        log.close()


# ---- run ---------------------------------------------------------------------------------------------------------
def logged_ids():
    """3 request_ids for job B, or [] without BigQuery."""
    if os.environ.get("E2E_REQUEST_IDS"):
        return os.environ["E2E_REQUEST_IDS"].split(",")
    from connectors.bigquery import source
    if not source.SOURCE.configured():
        return []
    table, q, r = source._cfg()
    rows = source._client(table.split(".")[0]).query(
        "SELECT `{i}` FROM `{t}` WHERE `{q}` IS NOT NULL AND `{q}` != '[]' AND `{r}` IS NOT NULL LIMIT 3".format(
            i=os.environ.get("BQ_ID_COLUMN") or "request_id", t=table, q=q, r=r)).result(timeout=120)
    return [row[0] for row in rows]


def main():
    eng = sa.create_engine(DB_URL, pool_pre_ping=True)
    if "--reset" in sys.argv:
        with eng.begin() as c:
            for t in ("validation_callbacks", "validation_cells", "validation_items", "validation_jobs"):
                c.execute(sa.text("DROP TABLE IF EXISTS %s" % t))
        say("dropped the validation tables (--reset)")
    srv = ThreadingHTTPServer(("127.0.0.1", CB_PORT), CB)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    names = {}              # worker-id prefix (one per process) -> instance name
    pending = []            # instances started but not yet seen holding work

    def holders():
        """{process id prefix: [seqs it holds unfinished]} across all jobs; also names newly seen processes."""
        with eng.connect() as c:
            rows = c.execute(sa.text("select worker, seq from validation_items where claim is not null and "
                                     "result is null")).all()
        out = collections.defaultdict(list)
        for w, seq in rows:
            pre = w.rsplit("-t", 1)[0]
            out[pre].append(seq)
            if pre not in names:
                names[pre] = pending.pop(0) if pending else "a restarted instance"
        return out

    def held_by(name):
        return sorted(s for pre, seqs in holders().items() if names.get(pre) == name for s in seqs)

    def wait_seen(name):
        pending.append(name)
        for _ in range(60):
            holders()
            if name in names.values():
                return
            time.sleep(1)
        raise RuntimeError("%s never claimed work" % name)

    A = items()
    rids = logged_ids()
    if rids:
        B = [{"request_id": i, "candidates": [{"label": "cheap", "model": "gpt-5.4-nano"}]} for i in rids]
        say("job B: %d requests by request_id from BigQuery" % len(B))
    else:
        B = [dict(it, id="b-" + it["id"], candidates=[{"label": "cheap", "model": "gpt-5.4-nano"}]) for it in A[15:18]]
        say("job B: no BigQuery configured, so full-body requests (the request_id path is NOT tested)")
    spawn("i1", 8101)
    s, _, ja = http("/api/audit/models", {"requests": A, "judge_model": JUDGE, "max_budget_usd": 2.0,
                                         "callback_url": "http://127.0.0.1:%d/cb/A" % CB_PORT}, port=8101)
    check(s == 202, "job A accepted by instance 1 (%d requests x 2 candidates)" % len(A))
    a = ja["audits"][0]["id"]
    wait_seen("i1")
    say("instance 1 up on :8101 with 2 workers; job %s submitted to it" % a)
    spawn("i2", 8102)
    s, _, jb = http("/api/audit/models", {"requests": B, "judge_model": JUDGE, "max_budget_usd": 1.0,
                                         "callback_url": "http://127.0.0.1:%d/cb/B" % CB_PORT}, port=8102)
    check(s == 202, "job B accepted by instance 2 (%d requests x 1 candidate)" % len(B))
    b = jb["audits"][0]["id"]
    wait_seen("i2")
    say("instance 2 up on :8102 with 2 workers; job %s submitted to it" % b)

    etag, n200, n304, killed, termed, last, most = None, 0, collections.Counter(), None, None, None, 0
    t_term = handed_back = exited = None
    i = 0
    while True:
        port = up[i % len(up)]                       # alternate instances: they must agree, ETag included
        i += 1
        s, h, j = http("/api/audit/models/" + a, headers={"If-None-Match": etag} if etag else None, port=port)
        if s == 304:
            n304[port] += 1
        else:
            n200 += 1
            etag = h.get("etag") or h.get("ETag")
            most = max(most, j["workers"])
            line = "A %s %d/%d, %d worker(s), spend $%.3f" % (j["status"], j["items_done"], j["items_total"],
                                                             j["workers"], j["spend_usd"])
            if line != last:
                say(line)
                last = line
        if killed is None and time.time() - T0 > 30:
            h1 = held_by("i1")
            if h1:
                down("i1", signal.SIGKILL)
                killed = h1
                say("CRASH: SIGKILLed instance 1 while it held requests %s (no hand-back: they must go stale, then "
                    "be taken over)" % h1)
                spawn("i1-restarted", 8103)
                pending.append("i1-restarted")
                say("instance 1 restarted on :8103")
        if termed is None and killed and time.time() - T0 > 70:
            h2 = held_by("i2")
            if h2:
                down("i2", signal.SIGTERM)
                termed, t_term = h2, time.time()
                say("REDEPLOY: SIGTERMed instance 2 while it held requests %s (must hand them back at once)" % h2)
                spawn("i2-restarted", 8104)
                pending.append("i2-restarted")
                say("instance 2 restarted on :8104")
        if termed and handed_back is None and not held_by("i2"):
            handed_back = time.time() - t_term
            say("instance 2's requests were free again %.1fs after SIGTERM" % handed_back)
        if termed and exited is None and procs["i2"][0].poll() is not None:
            exited = time.time() - t_term
            say("instance 2's process exited %.1fs after SIGTERM" % exited)
        if s == 200 and j["status"] not in ("queued", "running"):
            break
        if time.time() - T0 > 1200:
            check(False, "job A finished within 20 minutes")
            break
        time.sleep(3)
    for _ in range(200):
        if http("/api/audit/models/" + b)[2]["status"] not in ("queued", "running"):
            break
        time.sleep(3)
    for _ in range(20):
        if len(callbacks) >= 2:
            break
        time.sleep(1)
    time.sleep(3)
    say("both jobs ended")
    for job in (a, b):
        j = http("/api/audit/models/" + job)[2]
        if j["status"] != "done":                    # e.g. out of credit: say why instead of failing every check
            check(False, "%s ended %s after %d/%d: %s" % (job, j["status"], j["items_done"], j["items_total"],
                                                           j["stop_reason"] or j["error"]))
            print("\n%d CHECK(S) FAILED; fix the cause, then run again" % len(failures), flush=True)
            return

    # ---- checks --------------------------------------------------------------------------------------------------
    print("\n== Two instances, crash and redeploy", flush=True)
    check(most >= 2, "both instances worked at once: up to %d workers on job A" % most)
    check(killed is not None, "instance 1 crashed while holding requests %s" % (killed,))
    check(termed is not None, "instance 2 was stopped while holding requests %s" % (termed,))
    check(handed_back is not None and handed_back < 10,
          "instance 2 handed its requests back on SIGTERM (%s s)" % (handed_back and round(handed_back, 1)))
    print("   instance 2's process took %s s to exit after SIGTERM" % (exited and round(exited, 1)), flush=True)
    check(len(n304) >= 2, "'304 unchanged' came from %d different instances %s (ETag agrees across them)"
          % (len(n304), dict(n304)))
    with eng.connect() as c:
        for job, its, ncand in ((a, A, 2), (b, B, 1)):
            j = c.execute(sa.text("select * from validation_jobs where id=:j"), {"j": job}).mappings().one()
            rows = c.execute(sa.text("select * from validation_items where job_id=:j order by seq"),
                             {"j": job}).mappings().all()
            res = [json.loads(r["result"]) if r["result"] else None for r in rows]
            check(j["status"] == "done" and j["items_done"] == len(its) == j["items_total"],
                  "%s: done, %d/%d requests" % (job, j["items_done"], j["items_total"]))
            check(all(res) and [r["id"] for r in res] == [it.get("id") or it["request_id"] for it in its],
                  "%s: every request has exactly its own result, in order" % job)
            check(all(r["claim"] is None for r in rows), "%s: no claims left behind" % job)
            total = round(sum(r["spend_usd"] for r in res), 6)
            check(abs(j["spend_usd"] - total) < 1e-6,
                  "%s: job spend $%.4f = sum of its results $%.4f (nothing recorded twice)" % (job, j["spend_usd"], total))
            if job == a and killed and termed:
                who = {r["seq"]: names.get(r["worker"].rsplit("-t", 1)[0], "a restarted instance") for r in rows}
                check(all(who[q] != "i1" for q in killed),
                      "crashed instance 1's requests were redone by %s" % sorted({who[q] for q in killed}))
                check(all(who[q] != "i2" for q in termed),
                      "stopped instance 2's requests were redone by %s" % sorted({who[q] for q in termed}))
                print("   who recorded job A's requests: %s" % dict(collections.Counter(who.values())), flush=True)
            cells = c.execute(sa.text("select * from validation_cells where job_id=:j"), {"j": job}).mappings().all()
            check(len(cells) == len(its) * (ncand + 1), "%s: %d grid cells = requests x (candidates + original)"
                  % (job, len(cells)))
            byk = {(x["seq"], x["label"]): x for x in cells}
            bad = []
            for seq, r in zip([x["seq"] for x in rows], res):
                o = byk[(seq, "original")]
                if (o["kept"], o["runs"]) != (r["reference"]["agree"], r["reference"]["of"]):
                    bad.append((seq, "original"))
                for cnd in r["candidates"]:
                    x = byk[(seq, cnd["label"])]
                    served = collections.Counter(u["model"] for u in cnd["runs"] if u.get("model") and not u.get("error"))
                    if (x["verdict"], x["kept"], x["runs"]) != (cnd["verdict"], cnd["kept"], cnd["of"]) or \
                            (served and x["served_model"] != served.most_common(1)[0][0]):
                        bad.append((seq, cnd["label"]))
            check(not bad, "%s: every grid cell matches its full result %s" % (job, bad or ""))
            if job == a:
                resA = res

    print("\n== Status API and polling", flush=True)
    s, h, st = http("/api/audit/models/" + a)
    check(s == 200 and "results" not in st and st["results_url"] == "/api/audit/models/%s/results" % a,
          "status is light and points at the results")
    want = {}
    for r in resA:
        for cnd in r["candidates"]:
            want.setdefault(cnd["label"], collections.Counter())[cnd["verdict"]] += 1
    got = {x["label"]: {k: x[k] for k in ("within", "outside", "unverified")} for x in st["summary"] if x["label"] != "original"}
    check(all(got[l] == {k: want[l].get(k, 0) for k in ("within", "outside", "unverified")} for l in want),
          "summary counts match the results: %s" % got)
    s2, _, _ = http("/api/audit/models/" + a, headers={"If-None-Match": h.get("etag") or h.get("ETag")})
    check(s2 == 304, "a repeat poll of a finished job gets 304")
    check(sum(n304.values()) > 0, "polling during the run: %d full replies, %d '304 unchanged'"
          % (n200, sum(n304.values())))

    print("\n== Results API: paging, filters, search, one request", flush=True)
    seen, p = [], 1
    while True:
        s, _, pg = http("/api/audit/models/%s/results?per_page=7&page=%d" % (a, p))
        seen += [r["id"] for r in pg["results"]]
        if p * 7 >= pg["total"]:
            break
        p += 1
    check(seen == [it["id"] for it in A], "%d pages of 7 return all %d requests once, in order" % (p, len(seen)))
    def ids(qs):
        s, _, pg = http("/api/audit/models/%s/results?per_page=200&%s" % (a, qs))
        return [r["id"] for r in pg["results"]], pg["total"]
    prob = [r["id"] for r in resA if any(c["verdict"] != "within" for c in r["candidates"])]
    split = [r["id"] for r in resA if len({c["verdict"] for c in r["candidates"]}) > 1]
    nano_out = [r["id"] for r in resA if any(c["label"] == "nano" and c["verdict"] == "outside" for c in r["candidates"])]
    check(ids("show=problems") == (prob, len(prob)), "'needs a look' filter: %d requests" % len(prob))
    check(ids("show=split") == (split, len(split)), "'candidates disagree' filter: %d requests" % len(split))
    check(ids("label=nano&verdict=outside") == (nano_out, len(nano_out)), "nano outside: %d requests" % len(nano_out))
    check(ids("q=REFUND")[0] == [it["id"] for it in A if "refund" in it["id"]], "search 'REFUND' finds the 5 refund requests")
    check(ids("q=%25")[0] == [], "search for '%' matches nothing (no wildcard leak)")
    odd = [it["id"] for it in A if any(ch in it["id"] for ch in "/ #?ü")]
    ok = all(http("/api/audit/models/%s/results/%s" % (a, urllib.parse.quote(i, safe="")))[2].get("id") == i for i in odd)
    check(ok, "one-request endpoint works for all %d ids with / space # ? ü" % len(odd))

    print("\n== Status and downloads", flush=True)
    check(all(http("/api/audit/models/%s" % a, port=p)[0] == 200 for p in up), "job status serves from every live instance")
    check(len(http("/api/audit/models")[2]) >= 2, "job list returns the jobs")
    req_ok = [http("/api/audit/models/%s/results/%s" % (a, urllib.parse.quote(it["id"], safe="")))[0] for it in A]
    check(all(x == 200 for x in req_ok), "every one of the %d requests opens on its own" % len(A))
    s, _, csv_txt = http("/api/audit/models/%s/results.csv" % a)
    check(s == 200 and len(list(csv.reader(io.StringIO(csv_txt)))) == 1 + len(A) * 2, "CSV: header + %d rows" % (len(A) * 2))
    s, _, js = http("/api/audit/models/%s/results.json" % a)
    js = json.loads(js) if isinstance(js, str) else js
    check(s == 200 and len(js["results"]) == len(A), "JSON download holds all %d results" % len(A))

    print("\n== Callbacks", flush=True)
    by = collections.Counter(p for p, _, _ in callbacks)
    check(by == {"/cb/A": 1, "/cb/B": 1}, "exactly one callback per job: %s" % dict(by))
    for path, t, body in callbacks:
        check(body["status"] == "done" and body["summary"] and body["results_url"].endswith("/results"),
              "%s at %.0fs: status %s, summary, results_url" % (path, t, body["status"]))

    print("\n== Worker logs", flush=True)
    tb = {n: open(os.path.join(LOGS, n + ".log")).read().count("Traceback") for n in procs}
    check(not any(tb.values()), "no tracebacks in web or worker logs %s" % tb)

    print("\n== What the run found (judge %s)" % JUDGE, flush=True)
    for x in st["summary"]:
        print("   %-8s %-22s within %s outside %s unverified %s  fit %s  latency %s ms  cost/call $%s  cost vs original %s%%"
              % (x["label"], ",".join(x["models"]), x.get("within", "-"), x.get("outside", "-"), x.get("unverified", "-"),
                 x.get("avg_fit_rate", x.get("avg_consistency")), x["avg_latency_ms"] and round(x["avg_latency_ms"]),
                 x["avg_cost_usd"], x.get("cost_vs_original_pct", "-")), flush=True)
    print("   routing breakdown: %s" % (st["routing"] or "none (each candidate answered by one model)"), flush=True)
    for r in resA:
        print("   %-18s orig %s/%s  %s" % (r["id"], r["reference"]["agree"], r["reference"]["of"], "  ".join(
            "%s %s %d/%d" % (c["label"], c["verdict"], c["kept"], c["of"]) for c in r["candidates"])), flush=True)
    with eng.connect() as c:
        spent = c.execute(sa.text("select sum(spend_usd) from validation_jobs where id in (:a, :b)"), {"a": a, "b": b}).scalar()
    print("\n%s  jobs %s, %s  recorded spend $%.4f  (work thrown away by the killed/stopped workers is not in it)"
          % ("ALL CHECKS PASSED" if not failures else "%d CHECK(S) FAILED: %s" % (len(failures), failures), a, b, spent),
          flush=True)


try:
    main()
finally:
    stop_all()
    print("test processes stopped; logs in %s" % LOGS, flush=True)
