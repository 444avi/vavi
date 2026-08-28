"""
Sentinel source fetchers.

Every fetcher returns a list of normalized items:

    {source, observed_at, event_time, title, body, url,
     entities: [str], event_type, doc_hash, impact_hint, raw}

Entity strings are namespaced: "TICKER:NVDA", "CIK:0001045810",
"NAME:nvidia corp". Clustering treats any shared entity as a link.

Feed formats verified live 2026-07-28; see README "Sentinel" section.
"""

import hashlib
import html
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import sentinel_config as cfg

ET_NS_ATOM = "{http://www.w3.org/2005/Atom}"
ET_NS_NDAQ = "{http://www.nasdaqtrader.com/}"

# US Eastern offset: EDGAR/halt timestamps are ET. DST-correct enough for
# a monitor (second-level precision is not needed for clustering windows).
def _et_offset(dt_naive):
    # DST: second Sunday of March to first Sunday of November
    y = dt_naive.year
    mar = datetime(y, 3, 8)
    dst_start = mar + timedelta(days=(6 - mar.weekday()) % 7)
    nov = datetime(y, 11, 1)
    dst_end = nov + timedelta(days=(6 - nov.weekday()) % 7)
    return -4 if dst_start <= dt_naive < dst_end else -5


def _et_to_utc(dt_naive):
    return dt_naive.replace(
        tzinfo=timezone(timedelta(hours=_et_offset(dt_naive)))
    ).astimezone(timezone.utc)


def now_utc():
    return datetime.now(timezone.utc)


def _get(url, timeout=cfg.HTTP_TIMEOUT_SECONDS):
    req = urllib.request.Request(url, headers={"User-Agent": cfg.USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _norm_name(name):
    """Normalize a company name for the NAME: entity namespace."""
    s = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    s = re.sub(
        r"\b(inc|corp|corporation|company|co|ltd|limited|plc|llc|lp|sa|nv|"
        r"holdings?|group|international|the|class [a-z]|common stock|"
        r"ordinary shares?)\b", " ", s)
    return " ".join(s.split())


def _hash(*parts):
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]


# ---------------------------------------------------------------------------
# EDGAR — real-time Atom feed of latest filings
# ---------------------------------------------------------------------------
# Verified live format: <entry><title>8-K - COMPANY (0001234567) (Filer)
# </title><link href=...index.htm/><summary> "Filed: ... AccNo: ... Size:.."
# plus "Item 5.02: ..." lines </summary><updated>ISO with -04:00</updated>
# <category term="8-K"/><id>...accession-number=0001234567-26-000022</id>

_RE_EDGAR_TITLE = re.compile(r"^(.+?)\s+-\s+(.+?)\s+\((\d{10})\)")
_RE_EDGAR_ITEMS = re.compile(r"Item\s+(\d+\.\d+)")
_RE_EDGAR_ACCNO = re.compile(r"accession-number=([\d-]+)")


def fetch_edgar(cik_to_ticker, forms=None):
    """Poll the EDGAR 'current events' Atom feed for each form type.
    cik_to_ticker: dict of 10-digit-CIK-string -> ticker (may be empty)."""
    items = []
    for form in (forms or cfg.EDGAR_FORMS):
        raw = _get(cfg.EDGAR_ATOM_URL.format(form=form.replace(" ", "+")))
        root = ET.fromstring(raw)
        for entry in root.iter(ET_NS_ATOM + "entry"):
            title_el = entry.find(ET_NS_ATOM + "title")
            summary_el = entry.find(ET_NS_ATOM + "summary")
            updated_el = entry.find(ET_NS_ATOM + "updated")
            link_el = entry.find(ET_NS_ATOM + "link")
            id_el = entry.find(ET_NS_ATOM + "id")
            cat_el = entry.find(ET_NS_ATOM + "category")
            if title_el is None or id_el is None:
                continue
            title = title_el.text or ""
            m = _RE_EDGAR_TITLE.match(title)
            if not m:
                continue
            form_type, company, cik = m.group(1), m.group(2), m.group(3)
            # The feed returns supersets (e.g. "8-K" query includes 8-K/A).
            summary = html.unescape(summary_el.text or "") if summary_el is not None else ""
            item_nums = _RE_EDGAR_ITEMS.findall(summary)
            accno_m = _RE_EDGAR_ACCNO.search(id_el.text or "")
            accno = accno_m.group(1) if accno_m else _hash(title)
            when = None
            if updated_el is not None and updated_el.text:
                try:
                    when = datetime.fromisoformat(updated_el.text)
                except ValueError:
                    pass
            when = (when or now_utc()).astimezone(timezone.utc)

            root_form = form_type.split("/")[0]
            if root_form == "8-K" and item_nums:
                # one event_type per filing: the highest-impact item drives it
                best = max(
                    item_nums,
                    key=lambda i: cfg.ITEM_IMPACT_8K.get(i, (cfg.IMPACT_DEFAULT_8K, ""))[0],
                )
                event_type = "8-K:" + best
                impact = cfg.ITEM_IMPACT_8K.get(best, (cfg.IMPACT_DEFAULT_8K, ""))[0]
            elif root_form == "8-K":
                event_type, impact = "8-K:?", cfg.IMPACT_DEFAULT_8K
            elif root_form.startswith("SC 13D"):
                event_type, impact = "SC13D", cfg.IMPACT_SC13D
            elif root_form == "425":
                event_type, impact = "M&A:425", cfg.IMPACT_425
            else:
                event_type, impact = root_form, cfg.IMPACT_DEFAULT_8K

            entities = ["CIK:" + cik, "NAME:" + _norm_name(company)]
            ticker = cik_to_ticker.get(cik)
            if ticker:
                entities.append("TICKER:" + ticker)

            items.append({
                "source": "edgar",
                "observed_at": now_utc().isoformat(),
                "event_time": when.isoformat(),
                "title": f"{form_type} — {company}",
                "body": summary.strip()[:2000],
                "url": link_el.get("href") if link_el is not None else "",
                "entities": entities,
                "event_type": event_type,
                "doc_hash": "edgar:" + accno,
                "impact_hint": impact,
                "raw": {"form": form_type, "cik": cik, "items": item_nums,
                        "accno": accno},
            })
    return items


# ---------------------------------------------------------------------------
# Nasdaq trading halts RSS (covers Nasdaq + AMEX + others)
# ---------------------------------------------------------------------------
# Verified live: RSS 2.0, ndaq: namespace. Items persist while halted (it is
# a snapshot of current halts, not an event stream) -> dedup by
# symbol+date+time. HaltTime is ET.

def fetch_nasdaq_halts():
    raw = _get(cfg.NASDAQ_HALTS_URL)
    root = ET.fromstring(raw)
    items = []
    for it in root.iter("item"):
        def g(tag):
            el = it.find(ET_NS_NDAQ + tag)
            return (el.text or "").strip() if el is not None and el.text else ""
        sym = g("IssueSymbol")
        if not sym:
            continue
        name, market, code = g("IssueName"), g("Market"), g("ReasonCode")
        hdate, htime = g("HaltDate"), g("HaltTime")
        resumed = bool(g("ResumptionTradeTime") or g("ResumptionQuoteTime"))
        event_time = None
        try:
            naive = datetime.strptime(
                f"{hdate} {htime.split('.')[0]}", "%m/%d/%Y %H:%M:%S")
            event_time = _et_to_utc(naive)
        except ValueError:
            event_time = now_utc()
        impact, note = cfg.HALT_REASON_IMPACT.get(
            code, (cfg.HALT_REASON_DEFAULT, code or "unknown"))
        if is_derivative_issue(name):
            impact = round(impact * cfg.DERIVATIVE_ISSUE_DISCOUNT, 3)
        items.append({
            "source": "nasdaq_halts",
            "observed_at": now_utc().isoformat(),
            "event_time": event_time.isoformat(),
            "title": f"HALT {sym} ({code}: {note}) — {name}",
            "body": f"Trading halt on {market}. Reason {code} ({note}). "
                    f"Halted {hdate} {htime} ET."
                    + (" Resumption scheduled." if resumed else ""),
            "url": "https://www.nasdaqtrader.com/trader.aspx?id=TradeHalts",
            "entities": ["TICKER:" + sym, "NAME:" + _norm_name(name)],
            "event_type": "HALT:" + (code or "?"),
            "doc_hash": _hash("halt", sym, hdate, htime),
            "impact_hint": impact,
            "raw": {"symbol": sym, "code": code, "market": market,
                    "halt_et": f"{hdate} {htime}"},
        })
    return items


# ---------------------------------------------------------------------------
# NYSE trade halts CSV (covers NYSE + Nasdaq-listed; prose reasons)
# ---------------------------------------------------------------------------
# Verified live: header "Halt Date,Halt Time,Symbol,Name,Exchange,Reason,
# Resume Date,NYSE Resume Time"; dates YYYY-MM-DD, times ET.

def fetch_nyse_halts():
    import csv
    import io
    raw = _get(cfg.NYSE_HALTS_URL).decode("utf-8", "replace")
    items = []
    for row in csv.DictReader(io.StringIO(raw)):
        sym = (row.get("Symbol") or "").strip()
        if not sym:
            continue
        name = (row.get("Name") or "").strip().strip('"')
        reason = (row.get("Reason") or "").strip()
        code = cfg.NYSE_REASON_TO_CODE.get(reason.lower(), "NYSE:" + reason[:12])
        hdate = (row.get("Halt Date") or "").strip()
        htime = (row.get("Halt Time") or "").strip()
        try:
            event_time = _et_to_utc(
                datetime.strptime(f"{hdate} {htime}", "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            event_time = now_utc()
        impact, note = cfg.HALT_REASON_IMPACT.get(
            code, (cfg.HALT_REASON_DEFAULT, reason or "unknown"))
        if is_derivative_issue(name):
            impact = round(impact * cfg.DERIVATIVE_ISSUE_DISCOUNT, 3)
        items.append({
            "source": "nyse_halts",
            "observed_at": now_utc().isoformat(),
            "event_time": event_time.isoformat(),
            "title": f"HALT {sym} ({code}: {reason}) — {name}",
            "body": f"Trading halt on {row.get('Exchange','')}. "
                    f"Reason: {reason}. Halted {hdate} {htime} ET.",
            "url": "https://www.nyse.com/trade-halt",
            "entities": ["TICKER:" + sym, "NAME:" + _norm_name(name)],
            "event_type": "HALT:" + code,
            "doc_hash": _hash("halt", sym, hdate, htime.split(":")[0] + ":" +
                              (htime.split(":") + ["", ""])[1]),
            "impact_hint": impact,
            "raw": dict(row),
        })
    return items


# ---------------------------------------------------------------------------
# EDGAR filing text — fetched on demand for LLM triage of email candidates.
# ---------------------------------------------------------------------------
# Primary docs are usually behind the inline-XBRL viewer (/ix?doc=...);
# plain /Archives links on the index page are mostly exhibits. ex99* is
# typically the attached press release — often the most informative text.
_RE_DOC_LINK = re.compile(
    r'href="(?:/ix\?doc=)?(/Archives/edgar/data/[^"]+?\.htm)"', re.I)
_RE_TAG = re.compile(r"<[^>]+>")


def _fetch_htm_text(path):
    raw = _get("https://www.sec.gov" + path).decode("utf-8", "replace")
    text = html.unescape(_RE_TAG.sub(" ", raw))
    return " ".join(text.split())


def fetch_edgar_doc(index_url, max_chars=6000):
    """Given a filing's -index.htm URL, return primary-doc text plus the
    ex99* press release when present. Best-effort: '' on any failure.
    2-3 requests, SEC-rate-friendly."""
    try:
        idx = _get(index_url).decode("utf-8", "replace")
        links = _RE_DOC_LINK.findall(idx)
        primary = next(
            (l for l in links
             if "index" not in l.lower()
             and not re.search(r"/ex[-_0-9]|exhibit", l.lower())),
            None)
        press = next(
            (l for l in links if re.search(r"/ex[-_]?99", l.lower())), None)
        parts = []
        for path in (primary, press):
            if path and len(" ".join(parts)) < max_chars:
                try:
                    parts.append(_fetch_htm_text(path))
                except Exception:
                    pass
        return " ".join(parts)[:max_chars]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Market cap — resolved on demand for notification candidates, cached weekly.
# ---------------------------------------------------------------------------
def fetch_market_cap(symbol, token):
    """Return a ticker's market cap in USD via Finnhub, or None if unavailable.

    Finnhub stock/profile2 reports marketCapitalization in MILLIONS of the
    reporting currency (USD for US listings). Returns None for symbols Finnhub
    doesn't cover (it answers `{}`) or reports as 0 — the caller treats None as
    'unknown' and, being fail-open, still notifies. Raises on transport errors
    so the caller can distinguish a transient failure from a genuine no-data.
    """
    url = cfg.FINNHUB_PROFILE_URL.format(
        symbol=urllib.parse.quote(symbol), token=token)
    data = json.loads(_get(url))
    mc = data.get("marketCapitalization")
    if not mc:
        return None
    return float(mc) * 1_000_000


_RE_DERIVATIVE = re.compile(r"\b(warrant|wt|right|rt|unit)s?\b", re.I)


def is_derivative_issue(name):
    """Warrant/right/unit listings halt alongside their common stock; they
    are the same event and shouldn't alert separately."""
    return bool(_RE_DERIVATIVE.search(name or ""))


FETCHERS = {
    "edgar": None,   # bound in sentinel.py (needs the CIK map)
    "nasdaq_halts": fetch_nasdaq_halts,
    "nyse_halts": fetch_nyse_halts,
}
