# 🐾 Regulation Radar for Pet Toys

A small full-stack slice that tracks pet-toy regulations for BarkBox and runs an
**agent** that re-fetches the source, decides whether anything *materially* changed,
and flags it for human review — without ever silently overwriting trusted guidance.

Built end-to-end and proven first on **one regulation** — the **CPSC lead limit in
surface coatings** (ASTM F963 / **16 CFR Part 1303**, the 0.009% / **90 ppm** lead limit)
— then generalized to a **second** (**16 CFR Part 1501**, the small-parts / choking-hazard
rule) to show the pipeline scales. Both are sourced live from the **eCFR API**. Adding a
third is a **one-entry edit** to the catalog in [`backend/ecfr.py`](backend/ecfr.py) — the
agent loop, storage, and UI are all part-agnostic.

---

## Run it (single command)

```bash
python run.py
```

Then open **http://127.0.0.1:8000**.

`run.py` installs any missing dependencies from `requirements.txt`, seeds the database
from a **live eCFR fetch** on first launch, and serves both the API and the dashboard.

- **Python 3.10+** required. No Node / npm / build step (see scope cuts).
- **No API key needed to run.** The agent's model call is **provider-agnostic and
  stubbable** — with no key it uses a deterministic decision function; with a key it uses a
  real LLM. Precedence: **Anthropic Claude** if `ANTHROPIC_API_KEY` is set, else **Google
  Gemini** if `GEMINI_API_KEY` is set, else the stub. To use a real model:

  ```bash
  cp .env.example .env   # then paste GEMINI_API_KEY (or ANTHROPIC_API_KEY)
  ```

To reset to a fresh demo state, delete `data/radar.db` and restart.

---

## What you can do in the UI

- See the tracked regulation with: name · applies-to · one-line plain-English guidance ·
  source link · last-checked timestamp · status (`current` / `needs review`).
- **Run check now** — runs the agent loop against the live source.
- **Approve / Reject** — when the agent stages a change, a diff (current vs proposed)
  appears with human-in-the-loop controls.
- **Agent activity log** — an audit trail of every run (fetch ok? changed? material? mode).
- **Simulate drift** *(demo only)* — rewinds the stored baseline to the historical
  pre-2009 **600 ppm** text so a real re-check detects the tightening to **90 ppm** as a
  material change. This is how you demo the changed → needs-review → approve flow on
  camera without waiting for the CFR to actually change. Not part of the production loop.

---

## The agent loop (fetch → diff → decide → update)

Implemented as a real **LangGraph `StateGraph`** in [`backend/agent.py`](backend/agent.py).
Each node is small and readable:

| Node | Does | Guard |
|------|------|-------|
| **fetch** | Pulls live Part 1303 text from the eCFR API (latest issue date → full part XML → rough text extract). | Network error **or** a too-short/empty body → `fetch_ok = False`. |
| **diff** | SHA-256 of the *normalized* text (lowercased, whitespace-collapsed) vs the stored baseline hash. | — |
| **decide** | Is the change **material**? A real LLM (Claude or Gemini) if a key is set, else a deterministic stub. | Skips entirely if fetch failed or nothing changed; LLM error → falls back to the stub. |
| **update** | Writes the result + an audit-log row. | See the three guards below. |

### How "did it change materially?" is decided
- **Diff** is a cheap, exact hash comparison on normalized text — it catches *any*
  change but ignores pure whitespace/reflow noise.
- **Materiality** is the judgment call:
  - **With an LLM (Gemini or Claude):** the model sees the old text, new text, and current
    guidance, and is told that a material change alters an *obligation* (a numeric limit,
    covered products, a deadline, an exemption) — while reformatting, renumbering, and
    issue-date changes are **not** material. It returns structured JSON (`material`,
    `reason`, `proposed_summary`). The model layer is **provider-agnostic** (one
    `_llm_complete` function, swappable backends) so we're not locked to a vendor.
    Tool boundary: the model can only *judge text we hand it* — it can't fetch, browse, or
    write to the DB. Our code acts on its verdict.
  - **Without a key (stub):** materiality = "did the substantive **lead limits** change?"
    We extract the `% / ppm` figures from each version and compare the diff. Since this
    regulation *is* a numeric threshold, that's a sharp, honest signal. Other text changes
    are treated as non-material.

### What happens on update — the three guards
1. **Bad / empty fetch → update nothing.** Only an audit-log line is written; the
   regulation row (guidance, status, timestamp, hash) is untouched.
2. **Material change → never overwrite.** The agent **stages a proposal**
   (`pending_summary` / `pending_excerpt` / `pending_hash`) and flips status to
   **`needs review`**. The human-facing `plain_summary` is left exactly as-is.
3. **Non-material change → accept baseline quietly.** The stored hash moves forward (so we
   don't re-flag the same formatting diff every run) but the guidance is unchanged.

---

## Human-in-the-loop: not overwriting reviewed guidance

This is enforced *structurally*, not by convention: stored guidance and the agent's
proposal live in **separate columns**. The agent can only ever write to the `pending_*`
columns. The visible `plain_summary` changes **only** when a human clicks **Approve**.

- **Approve** → proposal becomes the live guidance, baseline hash advances, status →
  `current`, row marked `human_reviewed` with a timestamp.
- **Reject** → live guidance is **kept**, but the baseline hash still advances to the new
  source so we don't nag about the same diff forever. (Product call: a reject means "I saw
  this and our guidance still stands," not "pretend the source never changed.")

---

## Data model

`regulations` (one row per tracked reg) + `agent_runs` (append-only audit log). See
[`backend/db.py`](backend/db.py). SQLite, no migrations — fine for one reg.

---

## Scope cuts & assumptions (deliberate, for the time box)

- **Depth over breadth.** Part 1303 (lead) is the fully-realized path — the no-key stub
  is tuned to its numeric limit, and "Simulate drift" tells its real pre-2009 story. Part
  1501 (choking) is the second row proving the system generalizes; its deep
  materiality/guidance authoring is strongest with a real LLM key (the stub keys on numeric
  limits, which the lead rule has and the choking rule doesn't).
- **React with no build step, vendored locally.** The frontend is a single `index.html`
  using React + Babel-standalone served from `frontend/vendor/` (not a CDN), so it has no
  external dependency and runs fully offline. JSX is compiled in the browser with Babel's
  *classic* runtime. Trade-off: in-browser JSX compile, no bundler/tests/TypeScript — fine
  for one screen, not how I'd ship a real app (I'd precompile/bundle in production).
- **Rough text extract**, per the brief ("we care about the loop, not the scraper") —
  strip tags + collapse whitespace rather than parse the CFR XML structure.
- **Provider-agnostic, stubbable model call** so the project runs with zero setup and isn't
  locked to one vendor. Demoed on **Gemini** (`gemini-2.5-flash`); also supports Claude
  (`CLAUDE_MODEL`, default `claude-sonnet-4-6`); falls back to the deterministic stub when
  no key is set, and degrades to the stub if a live LLM call errors.
- **`urllib` over `httpx`** — one fewer dependency.
- **No auth, no multi-user, no real scheduler** — "Run check now" stands in for cron.

## What I'd build next (with more time)
- **Real scheduler** (cron / APScheduler) so checks run automatically + the notification
  count reflects "changed since you last looked."
- **Section-level diffing & a richer diff view** — highlight the exact changed clause
  instead of comparing whole-part text.
- **Multiple regulations + a second "which products are affected?" classifier agent.**
- **Versioned guidance history** (who approved what, when, and the prior text) for an audit
  trail regulators would actually accept.
- **Eval harness** for the materiality judgment (labeled changed/unchanged pairs) so we can
  measure precision/recall of "material change" before trusting it.
- **Tests** (loop guards, approve/reject transitions) and structured-output / retries on
  the model call.

## Project layout
```
run.py                  single-command launcher (deps + server)
requirements.txt
backend/
  main.py               FastAPI routes + serves the frontend
  agent.py              LangGraph loop: fetch → diff → decide → update (+ stub/Gemini/Claude)
  ecfr.py               eCFR source fetcher (the agent's one tool) + fetch guards
  db.py                 SQLite storage (regulations + agent_runs audit log)
  seed.py               first-run seed from a LIVE fetch (not hardcoded)
frontend/
  index.html            React (CDN) dashboard: table, run/approve/reject, diff, log
```
