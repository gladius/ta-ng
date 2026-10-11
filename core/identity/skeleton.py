"""Group a usecase's prompts into call-sites by the lines they share, with no node id in the log.

A call-site's prompt is a fixed template with per-call content around it. Numbers are masked, the prompt is split
into lines, and prompts are clustered greedily: a prompt joins a cluster when it contains most of the cluster's CORE
(the lines at least half its members have). Per-call context, retrieved passages and a revision line don't break
containment; a different agent's prompt (an orchestrator vs its specialists) shares almost nothing and separates.

Each cluster then gives: static lines (>= 90 % of members), dynamic lines (the rest), and ANCHORS, a few long static
lines no other cluster of the usecase has, so every row in the log can be assigned in SQL by anchor presence (and a
live request fingerprinted the same way). Clusters with few members are marked unsure.
"""
import hashlib
import re
from collections import Counter

_NUM = re.compile(r"\d+")
MIN_LINE = 8            # shorter lines ("Rules:", "- yes") are shared by unrelated prompts
JOIN = 0.6              # share of a cluster's core a prompt must contain to join it
CORE = 0.5              # a core line: present in at least this share of the cluster's members
STATIC = 0.9            # a static line: present in at least this share
ANCHORS = 5
UNSURE = 5              # members below which a cluster is unsure


def mask(text):
    return _NUM.sub("#", text or "")


def lines(prompt, tools=()):
    """The set a prompt is compared on: its masked lines of at least MIN_LINE chars, plus one token per tool name."""
    out = {l.strip() for l in mask(prompt).split("\n")}
    out = {l for l in out if len(l) >= MIN_LINE}
    out |= {"TOOL:" + t for t in tools or ()}
    return out


class Cluster:
    def __init__(self):
        self.n = 0
        self.counts = Counter()
        self.members = []

    def core(self):
        need = max(1, self.n * CORE)
        return {l for l, c in self.counts.items() if c >= need}

    def contains(self, ls):
        """Share of the core's characters the prompt has: a long instruction block outweighs a one-line revision."""
        core = self.core()
        total = sum(len(l) for l in core)
        return sum(len(l) for l in ls & core) / total if total else 0.0

    def add(self, i, ls):
        self.n += 1
        self.counts.update(ls)
        self.members.append(i)

    def static(self):
        need = self.n * STATIC
        return {l for l, c in self.counts.items() if c >= need}


def cluster(prompts):
    """prompts: [(text, tool_names)] -> [Cluster], each with .members = indexes into prompts. Longer prompts seed first
    so a template is learned before its fragments."""
    sets = [lines(p, t) for p, t in prompts]
    order = sorted(range(len(sets)), key=lambda i: -len(sets[i]))
    clusters = []
    for i in order:
        ls = sets[i]
        best, score = None, 0.0
        for c in clusters:
            s = c.contains(ls)
            if s > score:
                best, score = c, s
        if best is None or score < JOIN:
            best = Cluster()
            clusters.append(best)
        best.add(i, ls)
    return merge(sorted(clusters, key=lambda c: -c.n))


def merge(clusters):
    """A cluster whose static lines are contained in a larger cluster's core joins it: the longest prompts seed
    first, and a seed inflated by retrieved passages can reject the plain prompts that then form the real cluster."""
    kept = []
    for c in clusters:
        for k in kept:
            if k.contains(c.static()) >= JOIN:
                k.n += c.n
                k.counts.update(c.counts)
                k.members += c.members
                break
        else:
            kept.append(c)
    return sorted(kept, key=lambda c: -c.n)


def anchors(clusters):
    """Per cluster, up to ANCHORS of its longest static lines that no other cluster's core has (tool tokens
    excluded: those are matched on their own)."""
    cores = [c.core() for c in clusters]
    out = []
    for k, c in enumerate(clusters):
        others = set().union(*(cores[:k] + cores[k + 1:])) if len(cores) > 1 else set()
        own = [l for l in c.static() if l not in others and not l.startswith("TOOL:")]
        own.sort(key=lambda l: (-len(l), l))
        out.append(own[:ANCHORS])
    return out


def site_id(usecase, anchor_lines):
    return hashlib.sha1(("%s\x1f%s" % (usecase, "\n".join(sorted(anchor_lines)))).encode()).hexdigest()[:12]


def version_id(static_lines):
    return hashlib.sha1("\n".join(sorted(static_lines)).encode()).hexdigest()[:8]


def ordered(prompt):
    """The prompt's masked lines in order (the ones `lines` keeps)."""
    return [l.strip() for l in mask(prompt).split("\n") if len(l.strip()) >= MIN_LINE]


def layout(prompts, members, static, sample=200):
    """How the static text sits: static_chars, dynamic_chars, and the share of static characters that come AFTER the
    first dynamic line (text a prefix cache can't reach while the layout stays), plus distinct tool sets."""
    sc = dc = after = 0
    tool_sets = set()
    for i in members[:sample]:
        seen_dynamic = False
        for l in ordered(prompts[i][0]):
            if l in static:
                sc += len(l)
                after += len(l) if seen_dynamic else 0
            else:
                dc += len(l)
                seen_dynamic = True
        tool_sets.add(tuple(sorted(prompts[i][1] or ())))
    n = min(len(members), sample) or 1
    return {"static_chars": sc // n, "dynamic_chars": dc // n, "static_after_dynamic": after / sc if sc else 0.0,
            "tool_sets": len(tool_sets)}


def describe(usecase, prompts, stamps=None):
    """Group a usecase's sampled prompts -> [{site, n, unsure, anchors, static, dynamic, versions, members, layout}].
    `stamps` (one per prompt, sortable) gives versions: the static set per day; a change of it is a new version."""
    cs = cluster(prompts)
    ancs = anchors(cs)
    out = []
    for c, anc in zip(cs, ancs):
        static = c.static()
        dynamic = {l for l in c.counts if l not in static}
        versions = []
        if stamps:
            by_day = {}
            for i in c.members:
                by_day.setdefault(str(stamps[i])[:10], []).append(lines(*prompts[i]))
            seen = {}
            for day in sorted(by_day):
                day_static = set.intersection(*by_day[day])
                v = version_id(day_static & c.counts.keys())
                seen.setdefault(v, {"version": v, "first": day, "days": 0})["days"] += 1
                seen[v]["last"] = day
            versions = list(seen.values())
        out.append({"site": site_id(usecase, anc) if anc else site_id(usecase, ["<no anchors>", str(len(out))]),
                    "n": c.n, "unsure": c.n < UNSURE or not anc, "anchors": anc,
                    "static": len(static), "dynamic": len(dynamic), "versions": versions, "members": c.members,
                    "layout": layout(prompts, c.members, static)})
    return out
