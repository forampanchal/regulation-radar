"""Seed the tracked regulations from a REAL eCFR fetch, one row per catalog entry.

We hardcode only editorial product metadata (which product line a rule applies to, the
human-readable name). The source baseline text, hash, last-checked timestamp, and source
link all come from a live fetch. The plain-English one-liner is Claude-generated when a key
is present, else a curated fallback (see ecfr.CATALOG).
"""
from . import db
from . import agent
from . import ecfr


def seed_if_empty():
    db.init_db()
    if db.count_regulations() > 0:
        return "already seeded"

    results = []
    for entry in ecfr.CATALOG:
        base = agent.baseline_from_source(entry["part"])
        if base is None:
            # Honest degraded state — no silent hardcoding pretending we fetched.
            db.insert_regulation(
                name=entry["name"],
                applies_to=entry["applies_to"],
                plain_summary=entry.get("fallback_summary", "(initial fetch failed)"),
                source_url=entry["source_url"],
                source_label=entry["source_label"],
                source_part=entry["part"],
                status="needs review",
                last_checked=None,
                human_reviewed=0,
                content_hash=None,
                raw_excerpt=None,
            )
            results.append(f"{entry['part']}:fetch-failed")
            continue

        db.insert_regulation(
            name=entry["name"],
            applies_to=entry["applies_to"],
            plain_summary=base["summary"],
            source_url=base["source_url"],
            source_label=base["source_label"],
            source_part=entry["part"],
            status="current",
            last_checked=db.now_iso(),
            human_reviewed=0,
            content_hash=base["content_hash"],
            raw_excerpt=base["raw_excerpt"],
        )
        results.append(f"{entry['part']}:live")

    return "seeded " + ", ".join(results)


if __name__ == "__main__":
    print(seed_if_empty())
