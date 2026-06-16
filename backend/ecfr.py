"""eCFR source fetcher — the agent's single 'tool'.

eCFR exposes a clean public API (no key needed):
  - .../versioner/v1/titles.json                          -> latest issue date per title
  - .../versioner/v1/full/{date}/title-16.xml?part={part} -> the part's full text (XML)

We deliberately do a *rough* text extract (strip tags, collapse whitespace). Per the
brief: "Don't get stuck on parsing — a rough extract is fine. We care about the loop,
not the scraper."

A small CATALOG drives seeding so the dashboard generalizes to N regulations — the agent
loop, storage, and UI are all part-agnostic; only this list says what we track.
"""
import json
import re
import urllib.request
import urllib.error

USER_AGENT = "RegulationRadar/0.1 (take-home exercise)"
TITLE = "16"
TITLES_URL = "https://www.ecfr.gov/api/versioner/v1/titles.json"

# What we track. Add a row here and the rest of the system picks it up automatically.
CATALOG = [
    {
        "part": "1303",
        "name": "CPSC Lead Limit in Surface Coatings (ASTM F963 / 16 CFR 1303)",
        "applies_to": "BarkBox painted/coated chew toys & puppy toys for households with children",
        "source_label": "16 CFR Part 1303 (CPSC — lead in paint/surface coatings)",
        "source_url": "https://www.ecfr.gov/current/title-16/chapter-II/subchapter-B/part-1303",
        "fallback_summary": (
            "Toys and children's surface coatings must not exceed 0.009% (90 ppm) lead; "
            "confirm BarkBox supplier coatings comply."
        ),
    },
    {
        "part": "1501",
        "name": "Small-Parts / Choking-Hazard Rule (16 CFR 1501)",
        "applies_to": "BarkBox toys & detachable parts in homes with children under 3",
        "source_label": "16 CFR Part 1501 (CPSC — small parts / choking hazard)",
        "source_url": "https://www.ecfr.gov/current/title-16/chapter-II/subchapter-C/part-1501",
        "fallback_summary": (
            "Toys (and detachable parts) small enough to fit a small-parts cylinder are "
            "choking hazards for children under 3; size and label BarkBox products accordingly."
        ),
    },
]


def catalog_entry(part: str):
    for e in CATALOG:
        if e["part"] == part:
            return e
    return None


def _http_get(url: str, timeout: int = 60) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _latest_issue_date() -> str:
    raw = _http_get(TITLES_URL, timeout=30)
    data = json.loads(raw)
    for t in data.get("titles", []):
        if str(t.get("number")) == TITLE:
            d = t.get("latest_issue_date")
            if d:
                return d
    raise RuntimeError("Could not find latest issue date for Title 16")


def _xml_to_text(xml: str) -> str:
    """Crude but stable: drop tags, unescape a few entities, collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", xml)
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#8212;", "—")
        .replace("&#167;", "§")
    )
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fetch_source(part: str = "1303") -> dict:
    """Fetch the live text for a given CFR part.

    Returns a dict that ALWAYS has an `ok` key. On any failure (network error, or an
    empty/too-short body) `ok` is False and `error` explains why — the agent uses this
    to bail out without touching stored guidance.
    """
    entry = catalog_entry(part) or {}
    try:
        date = _latest_issue_date()
        url = (
            f"https://www.ecfr.gov/api/versioner/v1/full/{date}"
            f"/title-{TITLE}.xml?part={part}"
        )
        xml = _http_get(url)
        text = _xml_to_text(xml)

        # Guard: treat a suspiciously short body as an empty/failed fetch.
        if len(text) < 200:
            return {
                "ok": False,
                "error": f"fetched body too short ({len(text)} chars) — treating as empty",
            }

        return {
            "ok": True,
            "raw_text": text,
            "issue_date": date,
            "source_url": entry.get("source_url", ""),
            "source_label": entry.get("source_label", f"16 CFR Part {part}"),
            "error": None,
        }
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    except Exception as e:  # noqa: BLE001 - last-resort guard so a fetch never crashes the loop
        return {"ok": False, "error": f"unexpected: {type(e).__name__}: {e}"}
