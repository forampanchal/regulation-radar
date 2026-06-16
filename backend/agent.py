"""The Regulation Radar agent: fetch -> diff -> decide -> update.

Built as a real LangGraph StateGraph. Each edge is a node you can read top-to-bottom:

    fetch   -> pull live source text (ecfr.fetch_source)
    diff    -> hash-compare against the stored baseline
    decide  -> is the change *material*? (Claude if a key is set, else a deterministic stub)
    update  -> write the result back, NEVER overwriting human guidance silently

Product guards baked in:
  * Failed / empty fetch  -> update NOTHING on the regulation row (only an audit log line).
  * Material change       -> stage a *proposal* + flip status to "needs review". The
                             human-facing `plain_summary` is left untouched until a person
                             approves it.
  * Non-material change    -> accept the new source baseline quietly (hash moves forward so
                             we don't re-flag formatting/date noise), guidance unchanged.
"""
import hashlib
import json
import os
import re
import time

from langgraph.graph import StateGraph, END
from typing import TypedDict, Optional

from . import db
from . import ecfr


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
class AgentState(TypedDict, total=False):
    regulation_id: int
    source_part: str
    stored_hash: Optional[str]
    stored_excerpt: Optional[str]
    stored_summary: str
    # fetch
    fetch_ok: bool
    fetch_error: Optional[str]
    fetched_text: Optional[str]
    fetched_hash: Optional[str]
    source_url: Optional[str]
    # diff / decide
    changed: bool
    material: bool
    reason: str
    proposed_summary: Optional[str]
    # bookkeeping
    model_mode: str
    outcome: str


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalize(text: str) -> str:
    """Lower-case + collapse whitespace so trivial reflow doesn't read as a change."""
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _extract_limits(text: str) -> set:
    """Pull the substantive numeric limits ('0.009 percent', '90 ppm', etc.).

    The whole regulation is about a numeric lead threshold, so a change to these
    numbers is the clearest signal of a *material* change for the stub decider.
    """
    t = (text or "").lower()
    found = set()
    found.update(re.findall(r"\d+(?:\.\d+)?\s*percent", t))
    found.update(re.findall(r"\d+(?:\.\d+)?\s*ppm", t))
    return found


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #
def fetch_node(state: AgentState) -> AgentState:
    res = ecfr.fetch_source(state.get("source_part") or "1303")
    if not res["ok"]:
        return {**state, "fetch_ok": False, "fetch_error": res["error"]}
    text = res["raw_text"]
    return {
        **state,
        "fetch_ok": True,
        "fetch_error": None,
        "fetched_text": text,
        "fetched_hash": _hash(_normalize(text)),
        "source_url": res["source_url"],
    }


def diff_node(state: AgentState) -> AgentState:
    if not state.get("fetch_ok"):
        return {**state, "changed": False}
    changed = state.get("fetched_hash") != state.get("stored_hash")
    return {**state, "changed": changed}


def decide_node(state: AgentState) -> AgentState:
    # Nothing to decide if the fetch failed or the baseline is byte-identical.
    if not state.get("fetch_ok"):
        return {**state, "material": False, "reason": "fetch failed", "model_mode": "n/a"}
    if not state.get("changed"):
        return {
            **state,
            "material": False,
            "reason": "source text matches the stored baseline",
            "model_mode": "no-op",
        }

    old = state.get("stored_excerpt") or ""
    new = state.get("fetched_text") or ""
    current_summary = state.get("stored_summary") or ""

    if _llm_provider():
        decision = _decide_with_llm(old, new, current_summary)
    else:
        decision = _decide_with_stub(old, new, current_summary)

    return {**state, **decision}


def update_node(state: AgentState) -> AgentState:
    reg_id = state["regulation_id"]
    mode = state.get("model_mode", "stub")

    # GUARD 1: bad/empty fetch -> touch nothing on the row.
    if not state.get("fetch_ok"):
        outcome = f"Fetch failed ({state.get('fetch_error')}). Nothing updated."
        db.log_run(reg_id, "fetch_failed", outcome, fetch_ok=False, model_mode=mode)
        return {**state, "outcome": outcome}

    # No change -> just record that we checked.
    if not state.get("changed"):
        db.update_regulation(reg_id, last_checked=db.now_iso(), status="current")
        outcome = "Checked — no change. Source matches stored baseline."
        db.log_run(reg_id, "no_change", outcome, model_mode=mode)
        return {**state, "outcome": outcome}

    # Changed but not material -> quietly move the baseline forward.
    if not state.get("material"):
        db.update_regulation(
            reg_id,
            last_checked=db.now_iso(),
            status="current",
            content_hash=state["fetched_hash"],
            raw_excerpt=state["fetched_text"],
        )
        outcome = f"Non-material change accepted automatically: {state.get('reason')}"
        db.log_run(reg_id, "non_material", outcome, changed=True, model_mode=mode)
        return {**state, "outcome": outcome}

    # GUARD 2: material change -> STAGE a proposal, do NOT overwrite plain_summary.
    db.update_regulation(
        reg_id,
        last_checked=db.now_iso(),
        status="needs review",
        pending_summary=state.get("proposed_summary"),
        pending_excerpt=state["fetched_text"],
        pending_hash=state["fetched_hash"],
        pending_reason=state.get("reason"),
    )
    outcome = f"Material change detected -> flagged 'needs review'. {state.get('reason')}"
    db.log_run(reg_id, "material", outcome, changed=True, material=True, model_mode=mode)
    return {**state, "outcome": outcome}


# --------------------------------------------------------------------------- #
# Decision strategies
# --------------------------------------------------------------------------- #
def _decide_with_stub(old: str, new: str, current_summary: str) -> dict:
    """Deterministic fallback. Material iff the substantive lead limits differ.

    We focus on the *diff* of limits (added/removed) rather than the full set, so
    unrelated numbers elsewhere in the text don't muddy the reason.
    """
    old_limits = _extract_limits(old)
    new_limits = _extract_limits(new)
    added = sorted(new_limits - old_limits)
    removed = sorted(old_limits - new_limits)

    if added or removed:
        parts = []
        if added:
            parts.append(f"now cites {added}")
        if removed:
            parts.append(f"no longer cites {removed}")
        reason = "Numeric limit changed: source " + " and ".join(parts) + "."
        # The tightest newly-introduced lead-% limit is the obligation to track.
        proposed = _stub_summary(added or sorted(new_limits))
        return {"material": True, "reason": reason, "proposed_summary": proposed,
                "model_mode": "stub"}

    # Text differs but the key numbers don't -> treat as non-material (formatting/dates).
    return {
        "material": False,
        "reason": "Text differs but the substantive lead limits are unchanged.",
        "proposed_summary": None,
        "model_mode": "stub",
    }


def _stub_summary(limits) -> str:
    """Build one-line guidance from the relevant limit(s), adding a ppm gloss for %."""
    pcts = sorted(
        float(re.match(r"[\d.]+", l).group()) for l in limits if "percent" in l
    )
    if pcts:
        lead = pcts[0]  # tightest (smallest) lead percentage
        ppm = int(round(lead * 10000))
        return (
            f"Toys and children's surface coatings must not exceed {lead:g}% ({ppm} ppm) "
            f"lead. Review the updated 16 CFR Part 1303 text and confirm BarkBox supplier "
            f"coatings stay within this limit."
        )
    # Non-lead regulation (or no % limit found): keep it honest and generic.
    return (
        "The source text for this regulation changed — review the updated requirement "
        "and confirm BarkBox products still comply."
    )


# --------------------------------------------------------------------------- #
# Provider-agnostic LLM layer
#
# The agent is not coupled to one vendor. It picks a provider from whichever key is
# set (Anthropic > Gemini), and falls back to the deterministic stub when neither is.
# Adding a third provider is one more branch in _llm_complete().
# --------------------------------------------------------------------------- #
def _llm_provider():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return "gemini"
    return None


def _llm_label() -> str:
    p = _llm_provider()
    if p == "anthropic":
        return f"claude:{os.environ.get('CLAUDE_MODEL', 'claude-sonnet-4-6')}"
    if p == "gemini":
        return f"gemini:{os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash')}"
    return "stub"


def _llm_complete(prompt: str, max_tokens: int = 600, retries: int = 2) -> str:
    """Send one prompt to the active provider and return its text. Provider-agnostic.

    Retries with backoff on transient errors (LLM endpoints occasionally return 5xx /
    overloaded). If all attempts fail, raises so the caller can degrade to the stub.
    """
    p = _llm_provider()

    def _call() -> str:
        if p == "anthropic":
            import anthropic

            client = anthropic.Anthropic()
            msg = client.messages.create(
                model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6"),
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
        if p == "gemini":
            from google import genai

            client = genai.Client(
                api_key=os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            )
            resp = client.models.generate_content(
                model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
                contents=prompt,
            )
            return resp.text or ""
        return ""

    last_err = None
    for attempt in range(retries + 1):
        try:
            return _call()
        except Exception as e:  # noqa: BLE001 - retry transient failures, then re-raise
            last_err = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise last_err


def _decide_prompt(old: str, new: str, current_summary: str) -> str:
    return f"""You are a compliance analyst for a pet-toy company (BarkBox) tracking CPSC
toy-safety regulations (e.g. lead limits, choking hazards).

Compare the STORED baseline text against the freshly FETCHED source text and decide
whether anything *materially* changed for compliance purposes. A material change alters
an obligation: a numeric limit, what products are covered, a deadline, or an
exemption. Reformatting, renumbering, typo fixes, or date-of-issue changes are NOT material.

CURRENT HUMAN GUIDANCE (one line shown on the dashboard):
{current_summary}

STORED BASELINE (truncated):
\"\"\"{old[:6000]}\"\"\"

FRESHLY FETCHED (truncated):
\"\"\"{new[:6000]}\"\"\"

Respond with ONLY a JSON object, no prose:
{{
  "material": true|false,
  "reason": "<=2 sentences citing the specific difference (or why it's immaterial)",
  "proposed_summary": "one-line plain-English guidance reflecting the NEW text (only if material, else null)"
}}"""


def _decide_with_llm(old: str, new: str, current_summary: str) -> dict:
    """Ask the active LLM whether the change is material and, if so, draft new guidance.

    Tool boundaries: the model only sees the two text excerpts + the current guidance.
    It cannot fetch, browse, or act — it returns a structured judgment that our code acts
    on. Fail-safe: if the model call errors, we fall back to the deterministic stub so a
    bad/rate-limited LLM call never breaks the loop.
    """
    try:
        text = _llm_complete(_decide_prompt(old, new, current_summary), max_tokens=600)
        data = _parse_json(text)
        return {
            "material": bool(data.get("material")),
            "reason": data.get("reason", "(no reason returned)"),
            "proposed_summary": data.get("proposed_summary"),
            "model_mode": _llm_label(),
        }
    except Exception as e:  # noqa: BLE001 - degrade to the heuristic rather than crash
        d = _decide_with_stub(old, new, current_summary)
        d["reason"] = f"(LLM call failed: {type(e).__name__}; used heuristic) {d['reason']}"
        d["model_mode"] = f"{_llm_label()}->stub"
        return d


def summarize_for_seed(text: str, entry: dict = None) -> str:
    """One-line plain-English guidance for the INITIAL load (no baseline to diff against).

    With an LLM key (Gemini or Claude), the model summarizes the freshly fetched source
    text. Without one, we fall back to a curated one-liner per regulation (the source
    baseline/hash/timestamp still come from the live fetch — only this human-readable
    gloss is templated when no LLM is available).
    """
    entry = entry or {}
    if _llm_provider():
        try:
            out = _llm_complete(
                "Assume this U.S. regulation is in scope for BarkBox's painted/coated pet "
                "toys sold into households with young children. In ONE plain-English "
                "sentence, state the concrete obligation it imposes (the limit or "
                "requirement) and what BarkBox must verify. No preamble, no caveats about "
                "whether pet toys are covered.\n\n"
                f"\"\"\"{text[:6000]}\"\"\"",
                max_tokens=200,
            ).strip()
            if out:
                return out
        except Exception:  # noqa: BLE001 - fall back to a curated line rather than fail seeding
            pass
    # No key: prefer the curated per-regulation line; else a limit-derived line.
    return entry.get("fallback_summary") or _stub_summary(_extract_limits(text))


def baseline_from_source(part: str = "1303"):
    """Fetch one part and return the fields needed to seed it, or None on failure."""
    entry = ecfr.catalog_entry(part) or {}
    res = ecfr.fetch_source(part)
    if not res["ok"]:
        return None
    text = res["raw_text"]
    return {
        "raw_excerpt": text,
        "content_hash": _hash(_normalize(text)),
        "source_url": res["source_url"],
        "source_label": res["source_label"],
        "summary": summarize_for_seed(text, entry),
    }


def _parse_json(text: str) -> dict:
    """Tolerant JSON extraction — models sometimes wrap JSON in prose or fences."""
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {"material": False, "reason": "could not parse model output", "proposed_summary": None}


# --------------------------------------------------------------------------- #
# Graph
# --------------------------------------------------------------------------- #
def build_graph():
    g = StateGraph(AgentState)
    g.add_node("fetch", fetch_node)
    g.add_node("diff", diff_node)
    g.add_node("decide", decide_node)
    g.add_node("update", update_node)
    g.set_entry_point("fetch")
    g.add_edge("fetch", "diff")
    g.add_edge("diff", "decide")
    g.add_edge("decide", "update")
    g.add_edge("update", END)
    return g.compile()


_GRAPH = None


def run_check(regulation_id: int) -> dict:
    """Run the full loop for one regulation and return the audit-friendly result."""
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()

    reg = db.get_regulation(regulation_id)
    if not reg:
        raise ValueError(f"regulation {regulation_id} not found")

    initial: AgentState = {
        "regulation_id": regulation_id,
        "source_part": reg.get("source_part") or "1303",
        "stored_hash": reg.get("content_hash"),
        "stored_excerpt": reg.get("raw_excerpt"),
        "stored_summary": reg.get("plain_summary", ""),
    }
    final = _GRAPH.invoke(initial)
    return {
        "regulation_id": regulation_id,
        "fetch_ok": final.get("fetch_ok", False),
        "changed": final.get("changed", False),
        "material": final.get("material", False),
        "outcome": final.get("outcome", ""),
        "model_mode": final.get("model_mode", "stub"),
    }
