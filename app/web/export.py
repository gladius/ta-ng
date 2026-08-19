"""Render a report/profile page as a SELF-CONTAINED, portable HTML string: one file, no external CSS/JS, no app nav
or action chrome, markdown preview forced on with the toggle hidden. Used by the report + profile download ZIPs so
the saved doc looks like the page, opens anywhere offline, and prints to PDF straight from the browser. The template
does the stripping via an `export` flag; here we just inline the two static assets and stamp the generated time.
Zero new dependencies."""
import os
import re
import base64
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_CSS = os.path.join(_HERE, "static", "app.css")
_MDJS = os.path.join(_HERE, "static", "md.js")
_FONTS = os.path.join(_HERE, "static", "fonts")


def _read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _inline_fonts(css):
    """Replace `url(/static/fonts/x.woff2)` with a base64 data: URI so the exported file needs no font server —
    truly self-contained (exact fonts offline, not a system fallback). ~110KB once; fine for a shareable doc."""
    def repl(m):
        try:
            with open(os.path.join(_FONTS, os.path.basename(m.group(1))), "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode("ascii")
            return "url(data:font/woff2;base64,%s)" % b64
        except OSError:
            return m.group(0)                                   # font missing -> leave the ref, browser falls back
    return re.sub(r"url\(/static/fonts/([^)]+\.woff2)\)", repl, css)


def render(templates, name, ctx):
    """Render template `name` with export=True and every asset inlined -> a self-contained HTML string.
    The templates don't use `request`/`url_for`, so we render straight off the Jinja env (no Request needed)."""
    c = dict(ctx)
    c["export"] = True
    c["inline_css"] = _inline_fonts(_read(_CSS))
    c["inline_md"] = _read(_MDJS)
    c["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return templates.env.get_template(name).render(c)
