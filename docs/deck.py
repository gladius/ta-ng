"""Token Auditor deck — generated, not hand-drawn.

    python docs/deck.py          # rebuilds docs/token-auditor-deck.pptx in place

Every box, arrow and label is a native PowerPoint shape, so the deck stays editable by anyone —
but edit THIS file, not the .pptx, or the next rebuild overwrites your change.

  1  what it is                    definition, the gap it closes, scope boundary
  2  for agent teams               the finding card that lands on a developer's desk
  3  cache-prefix expansion        interleaved static/per-call lines -> one prefix, dual proof
  4  model-tier downgrade          reference-set fit, majority-voted, vs the original's own self-consistency
  5  input compression             the same engine, a new transform, and the refine loop
  6  production coverage           which prompt shape each lever needs, and where Cortex lands
"""
import os

from pptx import Presentation
from pptx.util import Inches, Pt
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE, MSO_CONNECTOR
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.oxml.ns import qn

W, H = 13.333, 7.5

INK    = RGBColor(0x10, 0x1B, 0x1A)
MUTED  = RGBColor(0x55, 0x66, 0x64)
ACCENT = RGBColor(0x0B, 0x6B, 0x62)
BRICK  = RGBColor(0x9E, 0x40, 0x30)
PAPER  = RGBColor(0xF9, 0xFB, 0xFA)

KIND = {  # fill, line, text
    "det":  ("F3F7F6", "93A8A5", "101B1A"),
    "live": ("DCE9E6", "5E8B84", "0C2320"),
    "ai":   ("E6E1F0", "6B5E8A", "1E1830"),
    "win":  ("0B6B62", "0B6B62", "FFFFFF"),
    "zero": ("F0E5E2", "9E4030", "3A1710"),
    "abs":  ("F2EBDA", "8A6A22", "2E2408"),
}
SERIF, SANS, MONO = "Georgia", "Segoe UI", "Consolas"


def rgb(h):
    return RGBColor.from_string(h)


def textbox(slide, x, y, w, h, runs, align=PP_ALIGN.LEFT, anchor=MSO_ANCHOR.TOP, spacing=None):
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = anchor
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    for i, (txt, size, bold, color, font) in enumerate(runs):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        if spacing:
            p.space_after = Pt(spacing)
        r = p.add_run()
        r.text, r.font.size, r.font.bold = txt, Pt(size), bold
        r.font.color.rgb, r.font.name = color, font
    return tb


def _fill_text(sh, text, kind, size, bold_first, font=None, align=PP_ALIGN.CENTER):
    _, _, txt = KIND[kind]
    tf = sh.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    tf.margin_left = tf.margin_right = Inches(0.08)
    tf.margin_top = tf.margin_bottom = Inches(0.03)
    for i, ln in enumerate(text.split("\n")):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = align
        r = p.add_run()
        r.text = ln
        r.font.size = Pt(size)
        r.font.bold = bold_first and i == 0
        r.font.color.rgb = rgb(txt)
        r.font.name = font or SANS


def shape(slide, kind, mso, x, y, w, h, text="", size=11, bold_first=False, radius=None,
          font=None, align=PP_ALIGN.CENTER):
    fill, line, _ = KIND[kind]
    sh = slide.shapes.add_shape(mso, Inches(x), Inches(y), Inches(w), Inches(h))
    if radius is not None:
        sh.adjustments[0] = radius
    sh.fill.solid()
    sh.fill.fore_color.rgb = rgb(fill)
    sh.line.color.rgb = rgb(line)
    sh.line.width = Pt(1.0)
    sh.shadow.inherit = False
    if text:
        _fill_text(sh, text, kind, size, bold_first, font, align)
    return sh


def box(slide, x, y, w, h, text, kind="det", size=11, bold_first=False):
    return shape(slide, kind, MSO_SHAPE.ROUNDED_RECTANGLE, x, y, w, h, text, size, bold_first, radius=0.08)


def block(slide, x, y, w, h, text, kind="det", size=10.5, bold_first=False, align=PP_ALIGN.CENTER):
    """A square-cornered slab — reads as a region of bytes in a request, not a process step."""
    return shape(slide, kind, MSO_SHAPE.RECTANGLE, x, y, w, h, text, size, bold_first, align=align)


def diamond(slide, x, y, w, h, text, size=10):
    return shape(slide, "det", MSO_SHAPE.DIAMOND, x, y, w, h, text, size)


def _arrowhead(conn):
    """python-pptx exposes no arrowhead API — append <a:tailEnd> to the line's <a:ln>."""
    ln = conn.line._get_or_add_ln()
    for old in ln.findall(qn("a:tailEnd")):
        ln.remove(old)
    ln.append(ln.makeelement(qn("a:tailEnd"), {"type": "triangle", "w": "med", "len": "med"}))


def arrow(slide, x1, y1, x2, y2, color="5E706E", width=1.25, head=True):
    c = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2))
    c.line.color.rgb = rgb(color)
    c.line.width = Pt(width)
    if head:                     # intermediate legs of a hand-drawn elbow carry no head
        _arrowhead(c)
    return c


def edge_label(slide, x, y, text, color=None, w=0.9):
    return textbox(slide, x, y, w, 0.22, [(text, 9, False, color or MUTED, MONO)], align=PP_ALIGN.CENTER)


def new_slide(prs, kicker, title, standfirst):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    bg = slide.background.fill
    bg.solid()
    bg.fore_color.rgb = PAPER
    rule = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.75), Inches(0.55), Inches(0.55), Pt(2.5))
    rule.fill.solid(); rule.fill.fore_color.rgb = ACCENT
    rule.line.fill.background(); rule.shadow.inherit = False
    textbox(slide, 0.75, 0.75, 8, 0.3, [(kicker, 10, False, ACCENT, MONO)])
    textbox(slide, 0.75, 1.05, 10.5, 0.55, [(title, 27, False, INK, SERIF)])
    textbox(slide, 0.75, 1.68, 11.3, 0.34, [(standfirst, 12, False, MUTED, SANS)])
    return slide


def caption(slide, x, y, w, text, size=10, color=None, align=PP_ALIGN.LEFT):
    return textbox(slide, x, y, w, 0.5, [(text, size, False, color or MUTED, SANS)], align=align)


def label(slide, x, y, w, text, color=None, align=PP_ALIGN.LEFT):
    return textbox(slide, x, y, w, 0.24, [(text, 9.5, False, color or MUTED, MONO)], align=align)


# ═════════════════════════════════════════════════════════════════════════════
# SLIDE 1 — model-tier downgrade
# ═════════════════════════════════════════════════════════════════════════════
def slide_downgrade(prs):
    s = new_slide(prs, "LEVER TWO", "Model-tier downgrade, end to end",
                  "The cheaper model is measured against your current model's OWN inconsistency — "
                  "not against perfection, and not against a written rubric.")

    R1, R2, BH = 2.40, 4.40, 1.05          # top row = cheaper path · bottom row = original / reference-set path
    IN_Y = 3.40

    box(s, 0.70, IN_Y, 1.85, BH, "5 genuinely different\nreal recorded inputs", "det")

    # top path — the CHEAPER model, judged against the reference set (majority-voted)
    box(s, 2.95, R1, 2.05, BH, "Run the CHEAPER\nmodel · 5×", "live")
    box(s, 5.30, R1, 2.30, BH, "Judge each vs the\nreference set · 3× vote", "ai")
    box(s, 10.85, R1, 1.85, BH, "SAFE\ndollars booked", "win", bold_first=True)

    # bottom path — the ORIGINAL's own 5 outputs ARE the reference set; each vs the other 4 = its self-consistency
    box(s, 2.95, R2, 2.05, BH, "Run the ORIGINAL 5×\n= the reference set", "live")
    box(s, 5.30, R2, 2.30, BH, "Each vs the other 4\n· 3× vote", "ai")
    box(s, 10.85, R2, 1.85, BH, "NOT-SAFE\nzero", "zero", bold_first=True)

    # the two rates meet at ONE arithmetic comparison — no rubric, no all-or-nothing
    DX, DY, DW, DH = 8.05, 3.02, 2.30, 1.66
    diamond(s, DX, DY, DW, DH, "cheaper n/5 ≥\noriginal n/5 − 1?")

    arrow(s, 2.55, IN_Y + 0.25, 2.95, R1 + BH / 2)                 # input -> both rows
    arrow(s, 2.55, IN_Y + BH - 0.25, 2.95, R2 + BH / 2)
    arrow(s, 5.00, R1 + BH / 2, 5.30, R1 + BH / 2)                 # each run -> its judge
    arrow(s, 5.00, R2 + BH / 2, 5.30, R2 + BH / 2)
    arrow(s, 7.60, R1 + BH / 2, DX + 0.35, DY + 0.52)             # judges -> the comparison
    arrow(s, 7.60, R2 + BH / 2, DX + 0.35, DY + DH - 0.52)
    edge_label(s, 7.62, R1 + BH / 2 - 0.36, "cheaper n/5", ACCENT)
    edge_label(s, 7.62, R2 + BH / 2 + 0.16, "original n/5", MUTED)
    arrow(s, DX + DW - 0.35, DY + 0.52, 10.85, R1 + BH / 2)       # verdict
    arrow(s, DX + DW - 0.35, DY + DH - 0.52, 10.85, R2 + BH / 2)
    edge_label(s, 10.18, R1 + BH / 2 - 0.34, "yes", ACCENT)
    edge_label(s, 10.18, R2 + BH / 2 + 0.12, "no", BRICK)

    LEG = [("det", "Deterministic · free"), ("live", "Live provider call"), ("ai", "AI judgement · voted"),
           ("win", "Proven · counted"), ("zero", "Zero · shown anyway")]
    lx = 0.75
    for kind, text in LEG:
        fill, line, _ = KIND[kind]
        sw = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(lx), Inches(6.42), Inches(0.15), Inches(0.15))
        sw.fill.solid(); sw.fill.fore_color.rgb = rgb(fill)
        sw.line.color.rgb = rgb(line); sw.line.width = Pt(0.75); sw.shadow.inherit = False
        caption(s, lx + 0.24, 6.40, 2.1, text, 9.5)
        lx += 2.42

    caption(s, 0.75, 6.92, 11.9,
            "The original's own 5 outputs ARE the acceptable range — the cheaper only has to fit it as often as the "
            "original fits itself. Every judgement is a majority vote, so one stray judge call can't flip the verdict.")


# ═════════════════════════════════════════════════════════════════════════════
# SLIDE 2 — cache-prefix expansion
# ═════════════════════════════════════════════════════════════════════════════
def slide_cache(prs):
    s = new_slide(prs, "LEVER ONE", "Cache-prefix expansion",
                  "Real prompts scatter fixed rules through per-call content, across both turns. "
                  "Recovering the prefix is a rewrite, not a swap.")

    ROW_H, ROW_GAP, TAGW = 0.30, 0.055, 0.24
    LX, LW = 0.75, 5.05
    TXT_X = LX + TAGW + 0.10

    def line_row(y, tag, text):
        kind = "det" if tag == "S" else "zero"
        fill, line_c, _ = KIND[kind]
        t = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(LX), Inches(y), Inches(TAGW), Inches(ROW_H))
        t.fill.solid(); t.fill.fore_color.rgb = rgb(line_c)
        t.line.fill.background(); t.shadow.inherit = False
        _fill_text(t, tag, "win", 8.5, False, MONO)
        r = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(TXT_X), Inches(y),
                               Inches(LW - TAGW - 0.10), Inches(ROW_H))
        r.fill.solid(); r.fill.fore_color.rgb = rgb(fill)
        r.line.color.rgb = rgb(line_c); r.line.width = Pt(0.75); r.shadow.inherit = False
        _fill_text(r, text, kind, 9, False, SANS, PP_ALIGN.LEFT)

    label(s, LX, 2.42, LW, "TODAY · ONE CALL-SITE'S PROMPT, LINE BY LINE")

    y = 2.86
    label(s, TXT_X, y - 0.24, 2.0, "SYSTEM", MUTED)
    for tag, text in [("D", "Session 4f2a-91c7 · 2026-08-09 14:22 UTC"),
                      ("S", "You are a claims triage assistant."),
                      ("S", "Never disclose internal policy limits."),
                      ("D", "Retrieved context: 3 policy documents"),
                      ("S", "Classify as: approve / review / deny")]:
        line_row(y, tag, text)
        y += ROW_H + ROW_GAP

    y += 0.26
    label(s, TXT_X, y - 0.24, 2.0, "USER", MUTED)
    for tag, text in [("D", "Customer Priya M. · policy #88213"),
                      ("S", "Always cite the clause you relied on."),
                      ("D", "“My claim was rejected — why?”")]:
        line_row(y, tag, text)
        y += ROW_H + ROW_GAP

    caption(s, LX, y + 0.06, LW, "Byte-comparison tags every line across the sampled calls. "
                                 "Fixed content is scattered — and some of it sits in the user turn.", 9.5)

    ar = s.shapes.add_shape(MSO_SHAPE.RIGHT_ARROW, Inches(6.05), Inches(4.10), Inches(0.62), Inches(0.48))
    ar.fill.solid(); ar.fill.fore_color.rgb = ACCENT
    ar.line.fill.background(); ar.shadow.inherit = False

    # ── right: the recovered prefix + the verbatim tail ─────────────────────
    RX, RW = 7.05, 5.50
    label(s, RX, 2.42, RW, "AFTER · THE MODEL WRITES ONE CONTIGUOUS PREFIX", ACCENT)

    block(s, RX, 2.86, RW, 1.34,
          "You are a claims triage assistant.\n"
          "Never disclose internal policy limits.\n"
          "Classify as: approve / review / deny\n"
          "Always cite the clause you relied on.   (hoisted out of the user turn)",
          "win", size=9.5, align=PP_ALIGN.LEFT)
    caption(s, RX, 4.28, RW, "CACHED — byte-identical every call, billed at about a tenth of the rate.",
            9.5, ACCENT)

    block(s, RX, 4.66, RW, 1.34,
          "Session 4f2a-91c7 · 2026-08-09 14:22 UTC\n"
          "Retrieved context: 3 policy documents\n"
          "Customer Priya M. · policy #88213\n"
          "“My claim was rejected — why?”",
          "zero", size=9.5, align=PP_ALIGN.LEFT)
    caption(s, RX, 6.08, RW, "PER-CALL — moved below the prefix, verbatim. Never regenerated.", 9.5, BRICK)

    # ── the three guards on the rewrite ─────────────────────────────────────
    gx, gw = 0.75, 3.85
    for head, body in [
            ("Byte-comparison holds the veto",
             "A line may only be hoisted if comparison independently proves it fixed. The model cannot "
             "promote a line that varies."),
            ("The model rewrites, minimally",
             "It may reword for coherence once per-call content sits below — “the context above” "
             "becomes “below” — but may not change any rule."),
            ("Entangled content is left alone",
             "Where fixed rules are tangled with per-call instructions, it keeps a smaller, correct prefix "
             "rather than a larger wrong one.")]:
        textbox(s, gx, 6.72, gw, 0.6,
                [(head, 9.5, True, INK, SANS), (body, 9, False, MUTED, SANS)], spacing=1)
        gx += 4.03


# ═════════════════════════════════════════════════════════════════════════════
# SLIDE — input compression (mirrors the downgrade geometry deliberately)
# ═════════════════════════════════════════════════════════════════════════════
def slide_compress(prs):
    s = new_slide(prs, "LEVER THREE", "Input compression, end to end",
                  "The same proof engine, a new transform — and a loop that backs off rather than "
                  "giving up when the first cut is too deep.")

    MY, MH, MID = 2.55, 1.15, 3.125
    BY, BH, BID = 4.45, 1.15, 5.025
    DX, DW = 7.75, 2.10
    DCX = DX + DW / 2
    TX, TW = 10.30, 2.40

    box(s, 0.70, MY, 2.05, MH, "One call-site's\nsystem prompt", "det")
    box(s, 3.05, MY, 2.05, MH, "COMPRESS · abstractive\naim: half the tokens", "ai")
    box(s, 5.40, MY, 2.05, MH, "Judge vs the recorded\nproduction output", "live")
    diamond(s, DX, MY - 0.15, DW, MH + 0.30, "Held on every\nsingle re-run?")
    box(s, TX, MY, TW, MH, "SAFE\ndollars booked", "win", bold_first=True)

    FX, FW = 4.90, 2.30
    FCX = FX + FW / 2
    box(s, FX, BY, FW, BH, "BACK OFF · surgical\nsteered by the named\ndrift reason", "ai")
    diamond(s, DX, BY - 0.15, DW, BH + 0.30, "Safe on the\ngentler cut?")
    box(s, TX, BY, TW, BH, "NOT-SAFE\nnothing booked", "zero", bold_first=True)

    arrow(s, 2.75, MID, 3.05, MID)
    arrow(s, 5.10, MID, 5.40, MID)
    arrow(s, 7.45, MID, DX, MID)
    arrow(s, DX + DW, MID, TX, MID)
    edge_label(s, TX - 0.675, MID - 0.30, "yes", ACCENT)

    arrow(s, DCX, MY + MH + 0.15, DCX, 4.12, head=False)
    arrow(s, DCX, 4.12, FCX, 4.12, head=False)
    arrow(s, FCX, 4.12, FCX, BY)
    edge_label(s, DCX - 0.96, MY + MH + 0.24, "no", BRICK)

    arrow(s, FX + FW, BID, DX, BID)
    arrow(s, DX + DW, BID, TX, BID)
    edge_label(s, TX - 0.675, BID - 0.30, "no", BRICK)

    arrow(s, DCX, BY - 0.15, TX + 0.90, MY + MH)
    edge_label(s, 9.85, 4.00, "yes", ACCENT)

    # the three guards, in the order of authority the engine applies them
    bar = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.75), Inches(6.30), Inches(0.55), Pt(2.5))
    bar.fill.solid(); bar.fill.fore_color.rgb = ACCENT
    bar.line.fill.background(); bar.shadow.inherit = False
    label(s, 0.75, 6.46, 6.0, "THREE GUARDS, IN ORDER OF AUTHORITY", ACCENT)

    gx, gw = 0.75, 3.85
    for head, body in [
            ("The compressor is preservation-first",
             "Every rule, threshold, code, tool name and heading stays verbatim. Only non-rule prose — "
             "rationale, examples — may be reworded."),
            ("A coverage gate, before any spend",
             "The rewrite is thrown away unless it is genuinely shorter and every locked literal "
             "survives word for word."),
            ("The proof has the last word",
             "A cut that changes behaviour is discarded and its drift reason steers the gentler retry. "
             "Dollars only on a unanimous SAFE.")]:
        textbox(s, gx, 6.76, gw, 0.55,
                [(head, 10, True, INK, SANS), (body, 9, False, MUTED, SANS)], spacing=1)
        gx += 4.03


# ═════════════════════════════════════════════════════════════════════════════
# SLIDE 3 — production coverage: Cortex
# ═════════════════════════════════════════════════════════════════════════════
def slide_cortex(prs):
    s = new_slide(prs, "PRODUCTION COVERAGE", "Cortex's shape defeats two of the three levers",
                  "Not a limit of the engine. Each lever needs a particular prompt shape, and our only "
                  "production agent has neither of the two that suit caching or downgrade.")

    # ── left: the coverage map — which shape each lever needs ───────────────
    LX, LW = 0.75, 1.72          # lever column
    NX, NW = 2.63, 2.46          # what it needs
    CX, CW = 5.29, 2.46          # where Cortex lands
    ROWH, PITCH, TOP = 0.92, 1.04, 3.06

    label(s, LX, 2.52, 7.0, "WHICH SHAPE EACH LEVER NEEDS")
    label(s, NX, 2.80, NW, "NEEDS", MUTED)
    label(s, CX, 2.80, CW, "CORTEX SUPERVISOR", MUTED)

    def lever_row(i, name, status, needs, verdict, kind):
        y = TOP + i * PITCH
        t = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(LX), Inches(y), Inches(LW), Inches(ROWH))
        t.fill.solid(); t.fill.fore_color.rgb = ACCENT if kind != "abs" else rgb("8A6A22")
        t.line.fill.background(); t.shadow.inherit = False
        _fill_text(t, "%s\n%s" % (name, status), "win", 9.5, True, SANS)
        n = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(NX), Inches(y), Inches(NW), Inches(ROWH))
        n.fill.solid(); n.fill.fore_color.rgb = rgb(KIND["det"][0])
        n.line.color.rgb = rgb(KIND["det"][1]); n.line.width = Pt(0.75); n.shadow.inherit = False
        _fill_text(n, needs, "det", 9.5, False, SANS, PP_ALIGN.LEFT)
        c = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(CX), Inches(y), Inches(CW), Inches(ROWH))
        c.fill.solid(); c.fill.fore_color.rgb = rgb(KIND[kind][0])
        c.line.color.rgb = rgb(KIND[kind][1]); c.line.width = Pt(1.0); c.shadow.inherit = False
        _fill_text(c, verdict, kind, 9.5, False, SANS, PP_ALIGN.LEFT)

    lever_row(0, "CACHING", "shipped",
              "A large prefix that repeats byte-for-byte across calls",
              "RAG-assembled. The static that survives must clear 1,024–4,096 tokens to cache at all.",
              "zero")
    lever_row(1, "DOWNGRADE", "shipped",
              "One model, bounded work, at a single call-site",
              "Routed across tiers, so the detector skips it by design.",
              "zero")
    lever_row(2, "COMPRESSION", "shipped",
              "A large prompt that changes on every call",
              "The lever that matches this shape. Whether it reaches Cortex is not yet measured.",
              "abs")

    dv = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(8.12), Inches(2.52), Pt(0.75), Inches(3.68))
    dv.fill.solid(); dv.fill.fore_color.rgb = rgb("D2DBDA")
    dv.line.fill.background(); dv.shadow.inherit = False

    # ── right: the production surface, both call-sites ──────────────────────
    RX, RW = 8.52, 4.06
    label(s, RX, 2.52, RW, "THE PRODUCTION SURFACE", BRICK)
    box(s, RX, 3.06, RW, 1.16, "supervisor\nrouted + RAG-assembled\nboth shipped levers out",
        "zero", size=9.5, bold_first=True)
    box(s, RX, 4.38, RW, 1.16, "create ticket\nsmall prompt\nlimited ceiling either way",
        "det", size=9.5, bold_first=True)
    caption(s, RX, 5.70, RW,
            "Two call-sites carry a prompt and a token count. The larger has no surface for the levers "
            "we ship today.", 9.5)

    # ── bottom: where that leaves us ────────────────────────────────────────
    bar = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.75), Inches(6.44), Inches(0.55), Pt(2.5))
    bar.fill.solid(); bar.fill.fore_color.rgb = ACCENT
    bar.line.fill.background(); bar.shadow.inherit = False
    label(s, 0.75, 6.60, 5.0, "WHERE THAT LEAVES US", ACCENT)

    gx, gw = 0.75, 5.82
    for head, body in [
            ("Compression is the lever to point at Cortex next",
             "It is shipped, it targets exactly this shape, and it has a second mode that rewrites "
             "retrieved documents without dropping any. It has not been run here yet."),
            ("Non-production agents carry the validation today",
             "Apex and the others are where the levers are exercised. Their traffic is not production "
             "traffic and we will not present it as such.")]:
        textbox(s, gx, 6.88, gw, 0.50,
                [(head, 10, True, INK, SANS), (body, 9, False, MUTED, SANS)], spacing=1)
        gx += 6.01


# ═════════════════════════════════════════════════════════════════════════════
# SLIDE 0 — what it is
# ═════════════════════════════════════════════════════════════════════════════
def slide_intro(prs):
    s = new_slide(prs, "WHAT IT IS", "An auditor for agent token spend",
                  "It reads your agents' recorded traffic, finds spend that can be removed, writes the "
                  "change itself, and proves the agent still behaves the same before claiming a saving.")

    LX, LW = 0.75, 6.62
    label(s, LX, 2.52, LW, "THE GAP IT CLOSES", BRICK)
    textbox(s, LX, 2.86, LW, 1.3,
            [("Cost dashboards report what you spent. They cannot tell you what was waste — because a "
              "cost is only waste if you can remove it without breaking the agent, and that is a "
              "behavioural question, not an accounting one.", 11, False, INK, SANS),
             ("So the finding lands on an engineer's desk as “this looks expensive”, and nothing "
              "happens. Nobody edits a working production prompt on the strength of an estimate.",
              11, False, MUTED, SANS)], spacing=8)

    # the principle, as a callout
    cy, ch = 4.62, 1.42
    cb = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(LX), Inches(cy), Inches(LW), Inches(ch))
    cb.fill.solid(); cb.fill.fore_color.rgb = rgb("F1F5F4")
    cb.line.fill.background(); cb.shadow.inherit = False
    ac = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(LX), Inches(cy), Pt(2.5), Inches(ch))
    ac.fill.solid(); ac.fill.fore_color.rgb = ACCENT
    ac.line.fill.background(); ac.shadow.inherit = False
    textbox(s, LX + 0.32, cy + 0.22, LW - 0.62, 1.0,
            [("Don't judge what you can't prove.", 17, False, INK, SERIF),
             ("Every dollar on the report survived a live experiment against the real provider, using "
              "your own recorded traffic. Anything it cannot prove is reported as zero.",
              10, False, MUTED, SANS)], spacing=7)

    dv = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(7.68), Inches(2.52), Pt(0.75), Inches(3.68))
    dv.fill.solid(); dv.fill.fore_color.rgb = rgb("D2DBDA")
    dv.line.fill.background(); dv.shadow.inherit = False

    # ── right: the four stages ──────────────────────────────────────────────
    RX, RW = 8.08, 4.50
    label(s, RX, 2.52, RW, "WHAT IT DOES", ACCENT)
    y = 2.90
    for n, head, body in [
            ("01", "Observe", "Recorded traffic from your tracing platform. Read-only."),
            ("02", "Detect", "Byte-compare and price every call-site. Free, deterministic, seconds."),
            ("03", "Produce", "An AI writes the candidate fix — a reordered prompt, a cheaper model."),
            ("04", "Prove", "Re-run it live on the real provider and try to break it.")]:
        rule = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(RX), Inches(y), Inches(0.30), Pt(2))
        rule.fill.solid(); rule.fill.fore_color.rgb = ACCENT
        rule.line.fill.background(); rule.shadow.inherit = False
        textbox(s, RX, y + 0.10, 0.42, 0.22, [(n, 9, False, ACCENT, MONO)])
        textbox(s, RX + 0.52, y + 0.06, RW - 0.52, 0.8,
                [(head, 11, True, INK, SANS), (body, 9.5, False, MUTED, SANS)], spacing=2)
        y += 0.86

    # ── bottom: scope ───────────────────────────────────────────────────────
    bar = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.75), Inches(6.44), Inches(0.55), Pt(2.5))
    bar.fill.solid(); bar.fill.fore_color.rgb = rgb("9E4030")
    bar.line.fill.background(); bar.shadow.inherit = False
    label(s, 0.75, 6.60, 5.0, "WHAT IT IS NOT", BRICK)

    gx, gw = 0.75, 3.85
    for head, body in [
            ("Not a dashboard",
             "It produces changes you can apply, not charts you have to interpret."),
            ("Not in the request path",
             "It reads traces out of band and never sits between an agent and its provider."),
            ("Not an auto-deployer",
             "The output is a proposed change with its evidence. A human decides and applies it.")]:
        textbox(s, gx, 6.88, gw, 0.50,
                [(head, 10, True, INK, SANS), (body, 9, False, MUTED, SANS)], spacing=1)
        gx += 4.03


# ═════════════════════════════════════════════════════════════════════════════
# SLIDE — for the teams who build the agents
# ═════════════════════════════════════════════════════════════════════════════
def slide_for_builders(prs):
    s = new_slide(prs, "FOR AGENT TEAMS", "A change you can apply, with the evidence attached",
                  "Nothing to instrument, nothing deployed on your behalf. You get the fix, the proof "
                  "behind it, and an honest no when there isn't one.")

    LX, LW = 0.75, 6.60
    label(s, LX, 2.52, LW, "WHAT LANDS ON YOUR DESK")

    hb = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(LX), Inches(2.86), Inches(LW), Inches(0.52))
    hb.fill.solid(); hb.fill.fore_color.rgb = ACCENT
    hb.line.fill.background(); hb.shadow.inherit = False
    textbox(s, LX + 0.18, 2.99, 4.2, 0.26,
            [("handler  ·  cache-prefix reorg", 11, True, RGBColor(0xFF, 0xFF, 0xFF), SANS)])
    textbox(s, LX + LW - 2.0, 2.99, 1.82, 0.26,
            [("RECOMMEND", 10, True, RGBColor(0xFF, 0xFF, 0xFF), MONO)], align=PP_ALIGN.RIGHT)

    CL, CLW = LX, 1.40
    CV, CVW = 2.23, 1.20
    CD, CDW = 3.51, 3.84
    ry, rh, rp = 3.40, 0.62, 0.66

    def finding(i, field, verdict, kind, desc):
        y = ry + i * rp
        for x, w, txt, k, sz, bold, al in (
                (CL, CLW, field, "det", 10, True, PP_ALIGN.LEFT),
                (CV, CVW, verdict, kind, 10, True, PP_ALIGN.CENTER),
                (CD, CDW, desc, "det", 9, False, PP_ALIGN.LEFT)):
            r = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(rh))
            r.fill.solid(); r.fill.fore_color.rgb = rgb(KIND[k][0])
            r.line.color.rgb = rgb(KIND[k][1]); r.line.width = Pt(0.75); r.shadow.inherit = False
            _fill_text(r, txt, k, sz, bold, MONO if k != "det" else SANS, al)

    finding(0, "Behaviour", "SAFE", "win",
            "5 genuinely different real inputs, cheaper re-run 5× and\nmajority-judged against your model's own 5 outputs")
    finding(1, "Caching", "PROVEN", "live",
            "the provider's own billing counter confirmed the new prefix\ncached on a live round-trip")
    finding(2, "Saving", "BOOKED", "win",
            "priced on the tokens actually recovered, at your call volume")

    at = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(LX), Inches(5.44), Inches(LW), Inches(0.74))
    at.fill.solid(); at.fill.fore_color.rgb = rgb("F1F5F4")
    at.line.color.rgb = rgb("C4D1CF"); at.line.width = Pt(0.75); at.shadow.inherit = False
    textbox(s, LX + 0.18, 5.58, LW - 0.36, 0.5,
            [("Attached: the reworded system prompt, ready to paste · every re-run's output · the "
              "judge's reason for each · the before and after cache counters", 9.5, False, MUTED, SANS)])

    caption(s, LX, 6.26, LW, "Illustrative. Field names and verdicts are the real ones.", 8.5)

    dv = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(7.62), Inches(2.52), Pt(0.75), Inches(3.70))
    dv.fill.solid(); dv.fill.fore_color.rgb = rgb("D2DBDA")
    dv.line.fill.background(); dv.shadow.inherit = False

    RX, RW = 8.02, 4.56
    label(s, RX, 2.52, RW, "WHY THE YES IS TRUSTWORTHY", ACCENT)
    y = 2.92
    for head, body in [
            ("Judged against your output, not a benchmark",
             "The reference is what your agent actually produced in production — never a rubric or a "
             "score we invented."),
            ("Measured against your model's own noise",
             "We measure how inconsistent your current model is on the same input first, so a cheaper "
             "model is never blamed for normal variance."),
            ("Every verdict is a majority vote",
             "Each output is judged several times and the majority wins, so one stray judge call can't flip "
             "the result; the cheaper must fit as often as the original fits itself.")]:
        textbox(s, RX, y, RW, 1.0,
                [(head, 10.5, True, INK, SANS), (body, 9.5, False, MUTED, SANS)], spacing=2)
        y += 1.12

    bar = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.75), Inches(6.44), Inches(0.55), Pt(2.5))
    bar.fill.solid(); bar.fill.fore_color.rgb = rgb("8A6A22")
    bar.line.fill.background(); bar.shadow.inherit = False
    label(s, 0.75, 6.60, 6.0, "AND THE NO IS USEFUL TOO", rgb("8A6A22"))

    gx, gw = 0.75, 3.85
    for head, body in [
            ("NOT-SAFE",
             "Names the input that drifted and the judge's reason, so you don't spend a week "
             "rediscovering it yourself."),
            ("TOO-SMALL",
             "Gives the measured static tokens against your model's cache minimum — a fact, not an "
             "opinion about your prompt."),
            ("LOW-EVIDENCE",
             "Fewer than three genuinely distinct inputs, so it abstains rather than generalise from "
             "one trace.")]:
        textbox(s, gx, 6.88, gw, 0.50,
                [(head, 10, True, INK, MONO), (body, 9, False, MUTED, SANS)], spacing=1)
        gx += 4.03


# ═════════════════════════════════════════════════════════════════════════════
# DIVIDER — a dark section break the presenter can pause on
# ═════════════════════════════════════════════════════════════════════════════
def slide_divider(prs, kicker, title, thesis):
    s = prs.slides.add_slide(prs.slide_layouts[6])
    bg = s.background.fill; bg.solid(); bg.fore_color.rgb = INK
    rule = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.82), Inches(3.05), Inches(0.72), Pt(3))
    rule.fill.solid(); rule.fill.fore_color.rgb = ACCENT
    rule.line.fill.background(); rule.shadow.inherit = False
    textbox(s, 0.8, 3.25, 9, 0.3, [(kicker, 12, False, rgb("6FBAB0"), MONO)])
    textbox(s, 0.8, 3.62, 11.6, 0.9, [(title, 38, False, PAPER, SERIF)])
    textbox(s, 0.8, 4.78, 10.6, 0.7, [(thesis, 14, False, rgb("A9B9B6"), SANS)])
    return s


# ═════════════════════════════════════════════════════════════════════════════
# SLIDE — input sampling: prove across the inputs that actually vary
# ═════════════════════════════════════════════════════════════════════════════
def slide_sampling(prs):
    s = new_slide(prs, "UNDER EVERY PROOF", "We prove across the inputs that actually vary",
                  "Every dollar rests on a handful of real inputs. We pick them from what changes per call — "
                  "not the whole prompt — so a rule only some inputs trigger can't slip through as a false SAFE.")

    # ── left: the trap — most of the prompt never changes ───────────────────
    LX = 0.75
    SW, VW, GAP = 4.55, 1.95, 0.15
    VX = LX + SW + GAP
    label(s, LX, 2.46, 7.0, "ONE CALL-SITE · THREE REAL CALLS")

    rows = [("refund a double\ncharge, order 4471", ),
            ("app crashes on the\nlogin screen", ),
            ("delete my data —\nGDPR request", )]
    y, RH, PITCH = 2.86, 0.62, 0.74
    for i, (ticket,) in enumerate(rows):
        yy = y + i * PITCH
        block(s, LX, yy, SW, RH, "system prompt  ·  8,000 tokens  ·  byte-identical every call",
              "det", size=9, align=PP_ALIGN.LEFT)
        block(s, VX, yy, VW, RH, ticket, "zero", size=9, align=PP_ALIGN.LEFT)

    ay = y + 3 * PITCH - 0.02
    label(s, VX, ay, VW, "↑ all the behaviour is here", BRICK, align=PP_ALIGN.CENTER)
    caption(s, LX, ay + 0.30, SW + GAP + VW,
            "Compare whole prompts and the 8,000 identical tokens bury the ticket — three different inputs "
            "score 99% alike and collapse to one. You'd “prove” the change on a single ticket.", 9.5)

    # ── right: what we do — sample the variable ─────────────────────────────
    dv = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(7.68), Inches(2.52), Pt(0.75), Inches(3.68))
    dv.fill.solid(); dv.fill.fore_color.rgb = rgb("D2DBDA")
    dv.line.fill.background(); dv.shadow.inherit = False

    RX, RW = 8.08, 4.55
    label(s, RX, 2.46, RW, "SO WE SAMPLE THE VARIABLE", ACCENT)
    textbox(s, RX, 2.72, RW, 0.24,      # the actual two-step mechanism, named — not "just byte compare"
            [("1 · byte-match the shared lines   ->   2 · word-overlap >=80% on the rest", 9, False, INK, MONO)])
    yy = 3.08
    for head, body in [
            ("Strip the shared skeleton  (byte-exact)",
             "Keep the lines byte-identical across every call; the per-call remainder is what we test on. The "
             "same static/per-call split the levers optimize on."),
            ("Dedup by word overlap  (fuzzy, >=80%)",
             "Compare the remainders as WORD SETS, not bytes — so near-duplicates merge and genuinely different "
             "inputs stay apart. A rule only some inputs trigger can't be silently dropped."),
            ("One word to a paragraph",
             "Word-overlap works from a single-word label up to a long ticket, or a mix — no shingles to run "
             "out of, no embeddings. Deterministic.")]:
        textbox(s, RX, yy, RW, 1.02,
                [(head, 10.5, True, INK, SANS), (body, 9.5, False, MUTED, SANS)], spacing=2)
        yy += 1.06

    # ── bottom: the safe bias ───────────────────────────────────────────────
    bar = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.75), Inches(6.44), Inches(0.55), Pt(2.5))
    bar.fill.solid(); bar.fill.fore_color.rgb = rgb("8A6A22")
    bar.line.fill.background(); bar.shadow.inherit = False
    label(s, 0.75, 6.60, 6.0, "THE SAFE BIAS", rgb("8A6A22"))
    textbox(s, 0.75, 6.86, 11.85, 0.5,
            [("When we can't tell a real variation from noise, we keep it. Testing a redundant real input "
              "wastes one call; MISSING a real one would call a change “safe” on an input that never "
              "exercised it. Over-sampling is the honest error for a prove-it tool.", 9.5, False, MUTED, SANS)])

    # speaker notes — the exact mechanism, for going into detail on this slide
    s.notes_slide.notes_text_frame.text = (
        "How _distinct actually works — two steps, only the first is a byte compare:\n\n"
        "1. FIND THE SHARED SKELETON — byte-exact, at line granularity. Split each call's full input into lines; "
        "keep the lines byte-identical across EVERY call in the bucket. That is the fixed skeleton (the system "
        "config / template). Pure byte comparison.\n\n"
        "2. DEDUP THE PER-CALL REMAINDER — fuzzy, by word overlap. For each call take the lines NOT in the skeleton "
        "(the per-call variable), turn it into a SET OF WORDS, and treat two calls as the same input when their word "
        "sets overlap >= 80% (Jaccard). This MERGES near-duplicates (\"refund order 12\" vs \"...order 12 please\") "
        "and keeps genuinely different tickets apart. Works from one word up to a paragraph. Deterministic — no "
        "embeddings, no ML, no semantics.\n\n"
        "WHY word-overlap and not byte compare on the remainder: an exact compare counts every trivially different "
        "call as new (a trailing space, a reworded near-dup) and wastes the sample budget; a 3-gram/phrase compare "
        "has no shingles for a one-word input, so it falls back to the whole prompt and collapses a simple "
        "classifier to 1 input — a false SAFE. Word-set is robust across the whole range.\n\n"
        "SAFE BIAS: when we can't tell a real variation from noise (a session id), we keep it. Over-sampling a "
        "redundant real input wastes one call; missing a real variation would call a change safe on an input that "
        "never exercised it.")


prs = Presentation()
prs.slide_width, prs.slide_height = Inches(W), Inches(H)
slide_intro(prs)
slide_for_builders(prs)

slide_divider(prs, "INPUT SAMPLING", "Which inputs we test the change on",
              "Before any lever runs, the auditor picks a handful of real inputs to prove the change on. "
              "Pick them wrong and every proof is hollow. Here's how it picks them.")
slide_sampling(prs)
slide_cache(prs)          # lever one first
slide_downgrade(prs)
slide_compress(prs)
slide_cortex(prs)

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "token-auditor-deck.pptx")
try:
    prs.save(out)
except PermissionError:            # deck is open in PowerPoint -> don't crash, write a preview alongside
    out = out.replace(".pptx", "-preview.pptx")
    prs.save(out)
print("saved", out)
