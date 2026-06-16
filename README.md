# 🐾 Regulation Radar for Pet Toys

A dashboard that tracks the safety regulations applying to BarkBox's pet toys, watched by
an AI agent that re-checks the official source, decides whether anything **materially**
changed, and flags it for a human to review — **without ever silently overwriting trusted
guidance.**

---

## In plain English (no tech background needed)

**The problem.** Companies that sell pet toys must follow government safety rules — like
*"surface paint can't contain more than 90 ppm of lead"* or *"parts can't be small enough
to be a choking hazard."* These rules live in dense legal documents and they **change over
time**. Most teams track them in a spreadsheet nobody trusts or updates.

**What this does.** Regulation Radar is a small web app with two parts:

1. **A dashboard** that lists each rule in **plain English**, says what it applies to, links
   to the **official government source**, shows when it was last checked, and a status
   (*Current* or *Needs review*).
2. **An AI agent** you trigger with a **"Run check now"** button. It re-reads the official
   source, compares it to what we have on file, and uses AI to judge whether anything
   **important** actually changed.

**The important part:** if the AI thinks a rule changed, it does **not** rewrite the
guidance on its own. It puts the change in a "**Needs review**" state and shows a
**before-vs-after** so a person can click **Approve** or **Reject**. Every automated check
and every human decision is recorded in an **activity log** — so there's always a clear
trail of who changed what and when.

When a change is flagged, a **second AI step** also lists **which BarkBox product lines** the
change affects (e.g. a lead-coating change → painted/coated toys), so the right team knows
what to re-check.

Think of it as a **smoke detector for regulations**: it watches constantly and alerts a
human, but a human decides what to do.

---

## Try it in 30 seconds

```bash
python run.py
```

Then open **http://127.0.0.1:8000**.

That one command installs anything missing, loads the current rules live from the
government source, and opens the dashboard. **Then:**

1. Click **Run check now** → the agent checks the live source (nothing has changed → it
   says so).
2. Click **Simulate drift** → this is a demo button that rewinds our stored copy to the
   *old* pre-2009 lead limit (600 ppm).
3. Click **Run check now** again → the agent now sees the source is stricter (90 ppm), flags
   it **Needs review**, and shows a before/after.
4. Click **Approve** → the guidance updates, and the log shows a **human** approved it.

> Requirements: **Python 3.10+**. No Node/npm, no build step, no internet needed for the UI.

---

## What we track

Two real U.S. consumer-safety rules (CPSC, Title 16 of the federal regulations), both
pulled live from the **[eCFR API](https://www.ecfr.gov/)**:

| Rule | What it covers |
|------|----------------|
| **16 CFR Part 1303** | Lead limit in surface coatings — the 0.009% / **90 ppm** lead limit (ASTM F963). |
| **16 CFR Part 1501** | Small-parts / **choking-hazard** rule for products used around young children. |

Adding a third is a **one-entry edit** to the catalog in [`backend/ecfr.py`](backend/ecfr.py)
— the agent, storage, and UI are all rule-agnostic.

---

# For engineers

## Stack
- **Backend:** FastAPI + SQLite (Python)
- **Frontend:** single-file React, served by the backend (no build step — see scope cuts)
- **Agent:** LangGraph state machine
- **Model:** provider-agnostic — **Gemini**, **Claude**, or a deterministic **stub**
- **Source:** eCFR API (federal regulations), fetched with stdlib `urllib`

## Using a real LLM (optional — it runs without one)
The agent's "did this change materially?" decision runs on a real model **if a key is set**,
otherwise on a deterministic stub (so it runs with zero setup). Precedence: **Claude** if
`ANTHROPIC_API_KEY` is set, else **Gemini** if `GEMINI_API_KEY` is set, else the stub.

```bash
cp .env.example .env   # then paste GEMINI_API_KEY (or ANTHROPIC_API_KEY)
```

Default Gemini model is `gemini-2.5-flash`; Claude default is `claude-sonnet-4-6`. To reset
to a fresh demo state, delete `data/radar.db` and restart.

## The agent loop (fetch → diff → decide → update)
A real **LangGraph `StateGraph`** in [`backend/agent.py`](backend/agent.py). Four small nodes:

| Node | Does | Guard |
|------|------|-------|
| **fetch** | Pulls the rule's live text from the eCFR API (latest issue date → full part XML → rough text extract). | Network error **or** a too-short/empty body → `fetch_ok = False`. |
| **diff** | SHA-256 of the *normalized* text (lowercased, whitespace-collapsed) vs the stored baseline hash. | — |
| **decide** | Is the change **material**? A real LLM if a key is set, else the stub. | Skips if fetch failed or nothing changed; LLM error → retries, then falls back to the stub. |
| **update** | Writes the result + an audit-log row. | See the three guards below. |

### How "did it change materially?" is decided
- **Diff** is a cheap exact hash comparison on normalized text — catches *any* change but
  ignores pure whitespace/reflow noise.
- **Materiality** is the judgment call:
  - **With an LLM (Gemini or Claude):** the model sees the old text, the new text, and the
    current guidance, and is told a material change alters an *obligation* (a numeric limit,
    covered products, a deadline, an exemption) — while reformatting, renumbering, and
    issue-date changes are **not** material. It returns structured JSON (`material`,
    `reason`, `proposed_summary`). The model layer is **provider-agnostic** (one
    `_llm_complete()` with swappable backends + retry/backoff), so we're not locked to a
    vendor. **Tool boundary:** the model can only *judge text we hand it* — it can't fetch,
    browse, or write to the DB. Our code acts on its verdict.
  - **Without a key (stub):** materiality = "did the substantive numeric limits change?" We
    extract the `% / ppm` figures from each version and compare. Since the lead rule *is* a
    numeric threshold, that's a sharp, honest signal; richer rules are best judged by an LLM.

### What happens on update — the three guards
1. **Bad / empty fetch → update nothing.** Only an audit-log line is written; the regulation
   row (guidance, status, timestamp, hash) is untouched.
2. **Material change → never overwrite.** The agent **stages a proposal** (`pending_*`
   columns) and flips status to **`needs review`**. The human-facing `plain_summary` is left
   exactly as-is.
3. **Non-material change → accept the baseline quietly.** The stored hash moves forward (so
   we don't re-flag the same formatting diff every run) but the guidance is unchanged.

## Human-in-the-loop: never overwrite reviewed guidance
Enforced **structurally**, not by convention: the human guidance and the agent's proposal
live in **separate database columns**. The agent can only ever write to the `pending_*`
columns; the visible `plain_summary` changes **only** when a human clicks **Approve**.

- **Approve** → proposal becomes the live guidance, baseline advances, status → `current`,
  row marked `human_reviewed` with a timestamp.
- **Reject** → live guidance is **kept**, but the baseline advances so we don't nag about the
  same diff forever (a reject means "I saw this and our guidance still stands").

Every agent check **and** every human approve/reject is recorded in the **activity log**
(`agent_runs`), tagged by actor (agent vs human) and outcome — a real audit trail.

## Second agent: product impact
When a material change is staged, a small **second agent** (`classify_affected_products` in
[`backend/agent.py`](backend/agent.py)) maps the change to the **BarkBox product lines** it
affects — LLM-reasoned over product descriptions when a key is set, else a deterministic
tag match. The result shows in the review panel so a human knows exactly what to re-check.

## Data model
[`backend/db.py`](backend/db.py), SQLite:
- **`regulations`** — one row per rule: the live guidance, source link, status, content hash,
  and the staged-proposal (`pending_*`) columns.
- **`agent_runs`** — append-only audit log of every check and human decision (`actor`,
  `kind`, `outcome`, timestamp).

## Scope cuts & assumptions (deliberate, for the time box)
- **Depth over breadth.** Part 1303 (lead) is the fully-realized path — the no-key stub is
  tuned to its numeric limit and "Simulate drift" tells its real pre-2009 story. Part 1501
  (choking) is the second row proving the system generalizes; its deep materiality/guidance
  authoring is strongest with a real LLM key.
- **React with no build step, vendored locally.** The frontend is a single `index.html`
  using React + Babel served from `frontend/vendor/` (not a CDN), so it has no external
  dependency and runs fully offline. JSX is compiled in the browser with Babel's *classic*
  runtime. Trade-off: in-browser compile, no bundler/tests/TypeScript — fine for one screen,
  not how I'd ship a real app.
- **Rough text extract**, per the brief ("we care about the loop, not the scraper") — strip
  tags + collapse whitespace rather than parse the CFR XML structure.
- **Provider-agnostic, stubbable model call** so the project runs with zero setup and isn't
  locked to one vendor; degrades to the stub if a live LLM call errors.
- **`urllib` over `httpx`** — one fewer dependency.
- **No auth, no multi-user, no real scheduler** — "Run check now" stands in for cron.

## What I'd build next (with more time)
- **Real scheduler** (cron / APScheduler) so checks run automatically + a real "changed since
  you last looked" count.
- **Section-level diffing & a richer diff view** — highlight the exact changed clause.
- **Smarter product classifier** — the second agent currently maps a changed rule to
  affected product lines from a small in-code catalog; next would be a real product DB + per-SKU mapping.
- **Versioned guidance history** (who approved what, when, and the prior text).
- **Eval harness** for the materiality judgment (labeled changed/unchanged pairs) to measure
  precision/recall before trusting it.
- **Tests** (loop guards, approve/reject transitions) and stricter structured-output parsing.

## Project layout
```
run.py                  single-command launcher (deps + server)
requirements.txt
backend/
  main.py               FastAPI routes + serves the frontend
  agent.py              LangGraph loop: fetch → diff → decide → update (+ stub/Gemini/Claude)
  ecfr.py               eCFR source fetcher (the agent's one tool) + the rule catalog
  db.py                 SQLite storage (regulations + agent_runs audit log)
  seed.py               first-run seed from a LIVE fetch (not hardcoded)
frontend/
  index.html            React dashboard: table, run/approve/reject, diff view, activity log
  vendor/               React + Babel, served locally (no CDN)
```
