#!/usr/bin/env python3
"""Push the call-sites' model choices to the gateway's pin store (trust_router/deploy, `./lt pins`).

    .venv/bin/python tools/push_pins.py [usecase ...]            # dry run: prints the `./lt pins set` commands
    .venv/bin/python tools/push_pins.py --apply [usecase ...]    # runs them and records the choice on the call-site

A call-site is pushed when it has a choice (store.set_choice: a person's or the router's) whose model still has a valid
passing verdict, or -- with --from-audits -- when no choice is set but audits.knowledge finds a cheapest passing model
(the choice is then recorded with --apply). The pin carries the current version's phrases and need as the fingerprint,
expires with the verdict, allows tools only with --allow-tools, and caps the prompt at the largest sampled prompt.
"""
import argparse
import datetime
import json
import shlex
import subprocess
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LT = os.path.expanduser("~/Workspace/trust_router/deploy/lt")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("usecases", nargs="*", help="only these usecases (default: all)")
    ap.add_argument("--apply", action="store_true", help="run `./lt pins set` on the gateway and record the choice")
    ap.add_argument("--from-audits", action="store_true", help="also sites without a choice, taking the cheapest passing model")
    ap.add_argument("--canary", type=float, default=0.01)
    ap.add_argument("--allow-tools", action="store_true")
    ap.add_argument("--by", default=os.getenv("USER", "push_pins"))
    a = ap.parse_args(argv)

    from core.identity import store
    from modules.models import audits
    from sqlalchemy import select
    from app.services import db

    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    sites = [s for s in store.all_sites() if not a.usecases or (s["usecase"] or "") in a.usecases]
    cmds = []
    for s in sites:
        if not s.get("versions"):
            continue
        k = audits.knowledge(s["site"], s)
        choice = s.get("choice") or {}
        model = choice.get("model") or (a.from_audits and k["cheapest"] and k["cheapest"]["model"])
        if not model:
            continue
        v = k["candidates"].get(model)
        if not v or not v["valid"] or v["verdict"] != "passes":
            print(f"skip {s['usecase']}/{s['site']} ({s.get('name')}): {model} has no valid passing verdict", file=sys.stderr)
            continue
        ver = s["versions"][-1]
        with db.ready().connect() as c:
            row = c.execute(select(audits.audits.c.samples).where(audits.audits.c.id == v["audit"])).first()
        samples = (json.loads(row[0]) if row and row[0] else {}).get(s["site"], [])
        max_prompt = max((x.get("prompt_tokens") or 0 for x in samples), default=0) or None
        ttl_days = max(0.0, (datetime.datetime.fromisoformat(v["valid_until"]) - now).total_seconds() / 86400)
        cmd = [LT, "pins", "set", s["usecase"] or "", s["site"], model, "--canary", str(a.canary), "--ttl-days", f"{ttl_days:.2f}",
               "--phrases", "|".join(ver["phrases"]), "--need", str(ver["need"]), "--reason", f"audit {v['audit']}: passes {v.get('requests')} requests, cost {v.get('cost_vs_original_pct')}%"]
        if max_prompt:
            cmd += ["--max-prompt-tokens", str(int(max_prompt * 1.5))]
        if a.allow_tools:
            cmd.append("--allow-tools")
        cmds.append((s, cmd, {"model": model, "audit": v["audit"], "guard": {"max_prompt_tokens": max_prompt and int(max_prompt * 1.5), "allow_tools": a.allow_tools},
                              "until": v["valid_until"], "by": a.by, "at": now.isoformat(timespec="seconds")}))
    for s, cmd, choice in cmds:
        print(" ".join(shlex.quote(x) for x in cmd))
        if a.apply:
            subprocess.run(cmd, check=True)
            store.set_choice(s["site"], choice)
    print(f"{len(cmds)} pin(s) {'pushed' if a.apply else 'to push (dry run; --apply to push)'}", file=sys.stderr)


if __name__ == "__main__":
    main()
