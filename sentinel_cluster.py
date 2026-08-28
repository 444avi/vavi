"""
Sentinel clustering + scoring. Cheap-first, Pi-friendly: SimHash + entity
overlap; only the ambiguous band goes to an LLM. No embeddings.
"""

import hashlib
import re
from datetime import datetime, timedelta, timezone

import sentinel_config as cfg


# ---------------------------------------------------------------------------
# SimHash (64-bit) over word 3-grams of title+body
# ---------------------------------------------------------------------------
_RE_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text):
    words = _RE_TOKEN.findall(text.lower())
    if len(words) < 3:
        return words
    return [" ".join(words[i:i + 3]) for i in range(len(words) - 2)]


def simhash(text):
    v = [0] * cfg.SIMHASH_BITS
    for tok in _tokens(text):
        h = int.from_bytes(
            hashlib.blake2b(tok.encode(), digest_size=8).digest(), "big")
        for b in range(cfg.SIMHASH_BITS):
            v[b] += 1 if (h >> b) & 1 else -1
    out = 0
    for b in range(cfg.SIMHASH_BITS):
        if v[b] > 0:
            out |= 1 << b
    # two's-complement signed so it fits SQLite's signed 64-bit INTEGER
    if out >= 1 << (cfg.SIMHASH_BITS - 1):
        out -= 1 << cfg.SIMHASH_BITS
    return out


_SIMHASH_MASK = (1 << cfg.SIMHASH_BITS) - 1


def lexical_sim(h1, h2):
    """1.0 = identical simhash, 0.0 = maximally distant."""
    return 1.0 - bin((h1 ^ h2) & _SIMHASH_MASK).count("1") / cfg.SIMHASH_BITS


def entity_sim(e1, e2):
    """Jaccard over namespaced entity strings, but a shared TICKER/CIK is
    a strong identity signal on its own."""
    s1, s2 = set(e1), set(e2)
    if not s1 or not s2:
        return 0.0
    shared = s1 & s2
    strong = any(e.split(":")[0] in ("TICKER", "CIK") for e in shared)
    j = len(shared) / len(s1 | s2)
    return max(j, 0.8 if strong else 0.0)


def combined_sim(item, cluster):
    """item vs cluster centroid: weighted lexical + entity similarity."""
    lex = lexical_sim(item["simhash"], cluster["simhash"])
    ent = entity_sim(item["entities"], cluster["entities"])
    return cfg.W_LEXICAL * lex + cfg.W_ENTITY * ent, lex, ent


def decide(item, cluster):
    """Returns 'merge', 'new', or 'ambiguous' for item vs one cluster.
    Auto-merge additionally requires event_type agreement (prevents cluster
    collapse via entity overlap alone)."""
    score, lex, ent = combined_sim(item, cluster)
    same_type = item["event_type"] == cluster["event_type"]
    if score >= cfg.SIM_MERGE and same_type:
        return "merge", score
    if score < cfg.SIM_NEW:
        return "new", score
    # Ambiguous band. Cross-type pairs (e.g. HALT:T12 vs 8-K:8.01 on the same
    # ticker) land here too when entity overlap is strong — exactly the case
    # the LLM should adjudicate rather than a threshold.
    return "ambiguous", score


# ---------------------------------------------------------------------------
# Novelty & impact
# ---------------------------------------------------------------------------
def novelty_score(is_new_cluster, event_type, suppressed_by_calendar,
                  repeats_7d, repeats_30d, lag_hours):
    n = cfg.NOVELTY_NEW_CLUSTER if is_new_cluster else 0.15
    if event_type in cfg.SUPPRESSED_EVENT_TYPES or suppressed_by_calendar:
        n += cfg.NOVELTY_SUPPRESSED
    if repeats_7d:
        n += cfg.NOVELTY_REPEAT_7D
    elif repeats_30d:
        n += cfg.NOVELTY_REPEAT_30D
    if lag_hours > 0:
        n -= min(lag_hours / cfg.NOVELTY_LAG_PENALTY_H, 1.0) * 0.20
    return max(0.0, min(1.0, round(n + 0.45, 3)))  # rebase to ~0-1


def impact_score(items):
    """Cluster impact = max of members' impact hints, nudged up when
    independent sources corroborate."""
    if not items:
        return 0.0
    base = max(i["impact_hint"] for i in items)
    n_sources = len({i["source"] for i in items})
    return round(min(1.0, base + 0.05 * (n_sources - 1)), 3)


def parse_iso(s):
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def lag_hours(item):
    try:
        obs = parse_iso(item["observed_at"])
        evt = parse_iso(item["event_time"])
        return max(0.0, (obs - evt).total_seconds() / 3600)
    except Exception:
        return 0.0
