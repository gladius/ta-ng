# Deck

The exec/engineering deck is **generated from [deck.py](deck.py)**, not hand-drawn. Edit the script, not
the `.pptx` — a rebuild overwrites the file.

```sh
python docs/deck.py                     # -> docs/token-auditor-deck.pptx
```

Requires `python-pptx` (`pip install python-pptx`). No other dependency; nothing is fetched at build time.

## Why generated

Every box, arrow, diamond and label is a **native PowerPoint shape** — no images. So the deck stays
editable by anyone who opens it (and imports cleanly into Google Slides), while the source of truth stays
in version control next to the code it describes. When a verdict name or a config default changes, the
deck is a one-line edit and a rebuild rather than a manual pass over five slides.

## Slides

| # | Slide | What it argues |
|---|---|---|
| 1 | What it is | A cost is only waste if removing it doesn't break the agent — that's behavioural, not accounting, which is why findings never get acted on. |
| 2 | For agent teams | The deliverable itself: a finding card with real field names and verdicts, plus why a `NOT-SAFE` is as useful as a `SAFE`. |
| 3 | Cache-prefix expansion | Static and per-call lines interleaved across both turns; the model writes one contiguous prefix; behaviour and caching are each a veto. |
| 4 | Model-tier downgrade | The full path including the **self-variance noise floor** — the branch that stops a cheaper model being blamed for the original model's own variance. |
| 5 | Production coverage | Which prompt shape each lever needs, and where Cortex lands. |

## Rendering slides to PNG

PowerPoint is driven over COM (Windows, Office installed) — useful for review or for dropping slides into
another document:

```powershell
$d = "$PWD\docs"
$app = New-Object -ComObject PowerPoint.Application
$p = $app.Presentations.Open("$d\token-auditor-deck.pptx", -1, 0, 0)
for ($i = 1; $i -le $p.Slides.Count; $i++) { $p.Slides.Item($i).Export("$d\slide-$i.png", "PNG", 1600, 900) }
$p.Close(); $app.Quit()
```

## Conventions

- **Palette and type live at the top of `deck.py`** (`KIND`, `SERIF`/`SANS`/`MONO`). Colour is semantic:
  teal = proven/counted, brick = zero, sand = abstained, violet = AI judgement, pale = deterministic/free.
  Keep that mapping — several slides rely on it without a legend.
- **Helpers do the drawing**: `box`, `block`, `diamond`, `arrow` (real arrowheads via raw DrawingML —
  python-pptx has no API for them), `edge_label`, `textbox`, `new_slide`.
- **Arrows are unbound connectors.** Moving a shape in PowerPoint will not drag its arrows. That's the
  price of controlling the routing; fix positions in the script instead.
- **Numbers on slides must be real.** Sampling figures come from `app/config.py` (`AUDIT_SAMPLES=5`,
  `AUDIT_REPEATS=3`); cache minimums come from `auditor/models.json`. The one illustrative element is the
  finding card on slide 2, and it is labelled as such.

## Open items

- Slide 5 states the cache minimum as the general `1,024–4,096` range rather than Cortex's measured
  `recoverable_tok` against its own model minimum. Detection is $0 and deterministic — running it turns
  that row from an argument into a finding.
- No dollar figure appears anywhere in the deck. That is honest today, and it is also the weakest thing
  about it in front of an exec audience. The fix is a detection run over the non-prod fleet, not an
  estimate.
- Slide 5 lists compression as `designed · P1`. If the `feat/compression` branch lands, that label and the
  slide's conclusion both need revisiting.
