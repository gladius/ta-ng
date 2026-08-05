"""Sample support agent — generates REAL recorded traces to test the downgrade audit on AUTHENTIC outputs.

Not part of the product: a stand-in enterprise agent we audit. It makes real model calls (llm_client) over a
diverse ticket set and writes a contract fixture to connectors/recorded/fixtures/, so the recorded connector +
CLI + audit consume it like any other agent. It exercises every path we care about, on purpose:

  triage         structured (FORCED tool call)  -> deterministic tool-decision check; downgrade sonnet->haiku
  <cat>_handler  free-text, BIG system prompt, per-category SUBGRAPH (graph_path)   -> the free-text judge path
  tech_handler   free-text WITH extended THINKING                                    -> thinking-matched replay
  responder      free-text, drafts the customer reply                                -> the free-text judge path

Diversity is deliberate: short/long/garbled/multi-issue/ambiguous tickets across 4 domains, so the judge meets
real variance (not a happy path). Baselines run on an expensive tier so a downgrade is a real candidate.

Run:  python -m sample_agents.support_agent              # full generate (real API spend)
      python -m sample_agents.support_agent --dry 2      # 2 tickets only, cheap smoke test
"""
import sys
import json
import os
from concurrent.futures import ThreadPoolExecutor

from app.services import llm_client
from contract import build_trace

_FIXTURES = os.path.join(os.path.dirname(__file__), "..", "connectors", "recorded", "fixtures")
AGENT = "acme-support"

# expensive baselines -> each is a real downgrade candidate (sonnet->haiku, opus->sonnet)
MODELS = {"triage": "claude-sonnet-5", "billing": "claude-opus-4-8", "technical": "claude-sonnet-5",
          "account": "claude-sonnet-5", "shipping": "claude-sonnet-5", "responder": "claude-sonnet-5"}
THINKING = {"technical"}                                    # this handler reasons with extended thinking

TRIAGE_SYS = (
    "You are the triage router for Acme's customer-support agent. Read the ticket and classify it. "
    "Category is one of exactly: billing, technical, account, shipping. Urgency is one of: low, normal, high. "
    "Rules: a double charge, failed payment, or refund dispute is billing. A broken feature, error, crash, or "
    "'not working' is technical. Login, password, profile, or plan changes are account. Delivery, tracking, "
    "returns, or damaged items are shipping. Anything threatening churn, legal action, or reporting an outage "
    "affecting work is high urgency. You MUST call set_category exactly once.")

SET_CATEGORY_TOOL = {
    "name": "set_category",
    "description": "Record the triage decision for a ticket.",
    "input_schema": {"type": "object",
                     "properties": {"category": {"type": "string", "enum": ["billing", "technical", "account", "shipping"]},
                                    "urgency": {"type": "string", "enum": ["low", "normal", "high"]}},
                     "required": ["category", "urgency"]}}

HANDBOOKS = {
    "billing": (
        "You are Acme's billing resolution specialist. Resolve the customer's billing issue and write a short "
        "internal resolution note (3-5 sentences) stating the diagnosis and the exact action to take.\n"
        "POLICY:\n"
        "- Double charges: confirm both charges share the same order id and amount; if so, refund the duplicate "
        "to the original payment method; refunds post in 5-10 business days.\n"
        "- Failed payment then retry that succeeded: no action, the first failure never settled; reassure only.\n"
        "- Subscription renewal disputes: pro-rate a refund only if cancellation was requested BEFORE the renewal "
        "date; otherwise the charge stands but offer a one-time 20% goodwill credit if the customer is at-risk.\n"
        "- Chargebacks already filed: do NOT also refund (that double-credits); note 'await bank resolution'.\n"
        "- Never promise a refund amount you have not confirmed against the order. State the amount only if the "
        "ticket gives the order id and amount.\n"
        "Include the order id in your note whenever the ticket provides one."),
    "technical": (
        "You are Acme's technical support engineer. Diagnose the customer's problem and write a short internal "
        "resolution note (3-5 sentences): the most likely cause and the concrete next step.\n"
        "TROUBLESHOOTING GUIDE:\n"
        "- 'Export fails / spins forever': known issue on datasets over 50k rows; workaround is a filtered export; "
        "a fix ships next release. Tell the customer the workaround.\n"
        "- 'Login loops / redirects': stale auth cookie; instruct a hard refresh + clear site data; if SSO, the "
        "identity provider clock may be skewed -> escalate to platform.\n"
        "- 'Webhook not firing': check the endpoint returns 2xx within 5s; retries stop after 3 failures; a 4xx "
        "means the payload was rejected -> it is the customer's endpoint, not ours.\n"
        "- 'Data looks wrong / missing': never claim data loss; first confirm the timezone + filter the view; most "
        "'missing' data is a filter or timezone artifact.\n"
        "- Only escalate to engineering when the guide says so; otherwise give the workaround."),
    "account": (
        "You are Acme's account specialist. Resolve the account/access issue and write a short internal resolution "
        "note (3-5 sentences).\n"
        "POLICY:\n"
        "- Password reset: send the reset link to the ACCOUNT email on file only; never to a new email in the "
        "ticket (that is an account-takeover risk) -> if they ask to change the email, require identity verification.\n"
        "- Plan downgrade: takes effect at the end of the current billing period, not immediately; seats above the "
        "new plan's limit must be removed first.\n"
        "- Plan upgrade: effective immediately, pro-rated.\n"
        "- 'Locked out after failed logins': unlock after verifying identity; lockout auto-clears in 30 minutes.\n"
        "- Never reveal whether a specific email has an account (privacy)."),
    "shipping": (
        "You are Acme's shipping and returns specialist. Resolve the delivery/returns issue and write a short "
        "internal resolution note (3-5 sentences).\n"
        "POLICY:\n"
        "- Lost in transit: if tracking has not moved in 7+ days, file a carrier claim and ship a replacement; do "
        "not make the customer wait on the claim.\n"
        "- Damaged on arrival: send a prepaid return label and a replacement; no need to return if the item is "
        "under $25 (waste of freight) -> just replace.\n"
        "- Wrong item shipped: replacement + prepaid label; expedite the reshipment.\n"
        "- Return window is 30 days from delivery; outside that, deny unless the item is defective.\n"
        "- Always include the tracking number in your note when the ticket provides one."),
}

RESPONDER_SYS = (
    "You are Acme's customer-facing support writer. Given an internal resolution note, write the reply we send "
    "to the customer. Be warm, concise (3-6 sentences), and specific. Preserve every commitment and every "
    "concrete detail from the note EXACTLY — the action taken, any amounts, order/tracking ids, and timeframes. "
    "Never invent a detail the note does not contain. Do not include internal-only phrasing or escalation notes.")

# --- diverse ticket set: 4 domains, varied length + phrasing + edge cases -------------------------------------
TICKETS = [
    # billing
    "I was charged twice for order 88213 this morning, both for $49.99. Please refund the duplicate.",
    "my subscription renewed yesterday for $120 but I cancelled last week, I want my money back",
    "Payment failed then I tried again and it went through. Did I get charged twice? Order 5567.",
    "You people charged my card AGAIN for order 90210, $75, after I already disputed it with my bank. Fix this now or I'm calling my lawyer.",
    "hi, quick q — the renewal for my Pro plan, can I get it prorated? I meant to cancel but forgot, order 4471",
    "Double billed. 33019. $18.00 each. today.",
    "I see two charges but different amounts ($40 and $12) on order 7781, is that normal?",
    "refund status? you said 5-10 days for order 6620 and it's been 3 days",
    "Charged for something I never bought. No order number, my email is on the account.",
    "The invoice for order 2245 shows tax I shouldn't be paying, I'm tax-exempt, please correct and refund the difference.",
    "cancelled before renewal on the 3rd, renewal hit on the 5th, order 9988, want the refund",
    "why is there a $1 charge on my card from acme? order unknown",
    # technical
    "The CSV export just spins forever and never downloads. I have about 80k rows.",
    "I keep getting redirected to the login page over and over, can't get in. We use SSO with Okta.",
    "our webhook stopped firing last night, nothing is coming through to our endpoint",
    "half my data is missing from the dashboard since this morning!! this is affecting our whole team, we can't work",
    "Export button does nothing. Small dataset, maybe 200 rows.",
    "app crashes when I click Reports. Chrome, latest.",
    "The numbers in my weekly report look totally wrong, way lower than they should be.",
    "webhook returns to us but we get a 4xx from your side sometimes? or is it us?",
    "everything is broken",
    "Getting 'session expired' every 2 minutes even though I just logged in.",
    "Integration test: our endpoint is slow, sometimes 8-9 seconds, and your webhooks seem to stop after a few tries. What's the retry policy?",
    "The graph shows no data for last week but I know we had activity. Timezone is set to UTC.",
    # account
    "I can't log in, forgot my password. Can you send a reset to my other email newone@example.com?",
    "please downgrade me from Team to Pro",
    "locked out after too many tries, help",
    "I want to upgrade to Enterprise today, how fast does it take effect?",
    "change the email on my account to a new one please",
    "Do you have an account for jane.doe@corp.com? I need to know if she's registered.",
    "downgrade to the free plan, I have 12 seats currently and the free plan allows 3",
    "reset password. account email is on file.",
    "I think someone else is logging into my account, I see activity I didn't do.",
    "how do I add two-factor auth to my profile",
    "upgrade me and also I was double charged last month for $60 on order 1201",  # multi-issue (billing bleed)
    "cant login. thats it.",
    # shipping
    "My package hasn't moved in tracking for 9 days. Tracking 1Z999AA10123456784.",
    "item arrived smashed, the screen is cracked. It was a $300 monitor.",
    "you sent me the wrong item, I ordered a keyboard and got a mouse, order 4410",
    "I want to return something I bought 45 days ago, changed my mind",
    "damaged on arrival, it's a $12 phone case, tracking 9400111899223",
    "where is my order??",
    "return window question: delivered 20 days ago, item is defective, can I still return?",
    "package says delivered but I never got it, tracking 1Z888BB20456789012",
    "wrong size shipped, need the right one fast, order 3390",
    "The box was crushed but the item inside seems fine, do I need to do anything?",
    "lost package, no movement 10 days, tracking TRK555, this is for a client deadline tomorrow",  # high urgency
    "",  # edge case: empty ticket
]


def _call(model, system, user, tools=None, force_tool=None, thinking=False):
    """One real model call. Returns (text, tool_calls, usage). Thinking forces temp!=0 per Anthropic."""
    kw = {"model": model, "max_tokens": 1200, "system": system,
          "messages": [{"role": "user", "content": user or "(empty ticket)"}]}
    if thinking:
        kw["thinking"] = {"type": "adaptive"}
        kw["output_config"] = {"effort": "low"}
        kw["max_tokens"] = 1600
    # temperature intentionally NOT sent (newest models reject it; not a reliability lever — see audit.replay)
    if tools:
        kw["tools"] = tools
        if force_tool:
            kw["tool_choice"] = {"type": "tool", "name": force_tool}
    r = llm_client.complete(**kw)
    text, calls = [], []
    for b in r.content:
        if b.type == "text":
            text.append(b.text)
        elif b.type == "tool_use":
            calls.append({"name": b.name, "args": b.input})
    u = getattr(r, "usage", None)
    usage = {"input_tokens": getattr(u, "input_tokens", 0) or 0,
             "output_tokens": getattr(u, "output_tokens", 0) or 0, "cached_tokens": 0}
    return "\n".join(text).strip(), calls, usage


def _trace(idx, node, graph_path, model, system, user, output, usage, tools_defined=(), tools_called=(), thinking=False):
    t = build_trace(trace_id="acme-%03d-%s" % (idx, node), agent_id=AGENT, node_id=node, graph_path=graph_path,
                    model=model, task_type="support",
                    input_messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                    output=output, usage=usage, tools_defined=tools_defined, tools_called=tools_called,
                    thinking_enabled=thinking, source="sample_agents.support_agent")
    return t


def run_ticket(idx, ticket):
    """triage -> route to the category handler (subgraph) -> responder. Returns the list of contract traces."""
    traces = []
    # 1) triage — real classification, forced tool call
    _, calls, u = _call(MODELS["triage"], TRIAGE_SYS, ticket, tools=[SET_CATEGORY_TOOL], force_tool="set_category")
    cat = (calls[0]["args"].get("category") if calls else None) or "technical"
    # store the FULL recorded behavior (the tool call WITH args) as output, so the audit's reference is complete
    triage_out = "\n".join("[calls %s(%s)]" % (c["name"], json.dumps(c["args"], sort_keys=True)) for c in calls)
    traces.append(_trace(idx, "triage", "", MODELS["triage"], TRIAGE_SYS, ticket,
                         output=triage_out, usage=u, tools_defined=[[SET_CATEGORY_TOOL["name"], json.dumps(SET_CATEGORY_TOOL)]],
                         tools_called=[c["name"] for c in calls]))
    # 2) category handler — free-text, big system prompt, per-category subgraph (technical uses thinking)
    hsys = HANDBOOKS[cat]
    think = cat in THINKING
    note, _, u2 = _call(MODELS[cat], hsys, ticket, thinking=think)
    traces.append(_trace(idx, "%s_handler" % cat, cat, MODELS[cat], hsys, ticket,
                         output=note, usage=u2, thinking=think))
    # 3) responder — free-text reply from the note
    ruser = "TICKET:\n%s\n\nINTERNAL RESOLUTION NOTE:\n%s" % (ticket or "(empty)", note)
    reply, _, u3 = _call(MODELS["responder"], RESPONDER_SYS, ruser)
    traces.append(_trace(idx, "responder", "", MODELS["responder"], RESPONDER_SYS, ruser, output=reply, usage=u3))
    return traces


def generate(tickets, out_name=AGENT):
    with ThreadPoolExecutor(max_workers=6) as ex:                      # tickets are independent
        batches = list(ex.map(lambda it: run_ticket(it[0], it[1]), list(enumerate(tickets))))
    traces = [t for batch in batches for t in batch]
    os.makedirs(_FIXTURES, exist_ok=True)
    path = os.path.join(_FIXTURES, out_name + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"agent": out_name, "workspace": "recorded", "traces": traces}, f, indent=2)
    tin = sum(t["usage"]["input_tokens"] for t in traces)
    tout = sum(t["usage"]["output_tokens"] for t in traces)
    print("wrote %d traces from %d tickets -> %s" % (len(traces), len(tickets), path))
    print("real tokens: %d in / %d out" % (tin, tout))
    from collections import Counter
    per = Counter(t["node_id"] for t in traces)
    print("per node:", dict(per))
    return path


if __name__ == "__main__":
    n = None
    if len(sys.argv) >= 3 and sys.argv[1] == "--dry":
        n = int(sys.argv[2])
    tickets = TICKETS[:n] if n else TICKETS
    name = AGENT + ("-dry" if n else "")
    generate(tickets, out_name=name)
