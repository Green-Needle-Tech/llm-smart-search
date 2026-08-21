# Multi-Engine Web Search — v2.0.0

> Firecrawl + Exa + Tavily in parallel, merged, scored, and ranked — with synthesized answers, recency-aware scoring, domain-quality weighting, and docs-alias dedup.

**Version:** 2.0.0 · **Updated:** 2026-08-21 · **Script:** `scripts/search.py`

---

## What It Is

A single Python script that fans one query out to three search engines simultaneously, dedupes and cross-confirms results, and returns a ranked list plus a one-line synthesized answer. Built for AI agents and RAG pipelines where a single engine misses too much.

## Quick Start

```bash
# Basic usage (text output)
python3 scripts/search.py "query" [max_results]

# Machine-readable output for pipelines / cron jobs
python3 scripts/search.py "query" 10 --json
```

Or from a Hermes session:

```python
from hermes_tools import terminal
r = terminal("python3 /root/.hermes/skills/research/multi-engine-search/scripts/search.py \"query\" 10")
print(r["output"])
```

**No env setup needed** — the env loader is baked into the script and reads `~/.hermes/.env` itself (`FIRECRAWL_API_KEY`, `FIRECRAWL_API_URL`, `EXA_API_KEY`, `TAVILY_API_KEY`). It cannot be omitted by pasting, which was v1's main failure mode.

---

## Architecture

```
        ┌──> Firecrawl (self-hosted :3002 or cloud) ──┐
Query ──┤──> Exa (api.exa.ai, type=auto) ─────────────┼──> Dedupe ──> Score ──> Rank ──> Output
        └──> Tavily (api.tavily.com, +answer) ────────┘
              (ThreadPoolExecutor, max_workers=3, per-engine retry ×2)
```

## Scoring Model (v2)

Each result is scored on five signals. Engine agreement remains the dominant factor.

| Signal | Weight | Notes |
|---|---|---|
| **Engine agreement** | +10 per engine + priority (firecrawl=3, exa=2, tavily=1) | In all 3 engines → 30+ |
| **Domain quality** | +4 / +2 / −3 | Authoritative domains (arxiv, github, postgresql.org, reuters, docs.\*) vs medium (medium, wikipedia) vs low-signal (pinterest, quora, social) |
| **Recency** | +3 / +2 / +1 | < 90 days / < 1 year / < 2 years (from `publishedDate`) |
| **Position decay** | +0.5 × (3 − best rank) | Ranking high in any single engine's own results helps slightly |
| **Snippet length** | tie-break | Longer snippet wins during merge |

**Score guide:** 30+ = confirmed by all 3 engines · 20–29 = two engines · 12–19 = single engine (often still valuable, esp. from Exa semantic).

## Deduplication

URLs are normalized (lowercase netloc, stripped trailing slash, query/fragment ignored). **v2 adds docs-alias collapsing:**

- `/docs/18/…` ≡ `/docs/current/…` ≡ `/docs/latest/…` → merged as one result
- Nested versions collapse too: `/docs/18/2/…` ≡ `/docs/18/…`
- Conservative: version-like segments only collapse after docs-style segments (`docs`, `api`, `reference`, `guide`, `wiki`, `learn`, …)
- Content-bearing versions stay separate: `/blog/pg-18-tuning` ≠ `/blog/pg-19-tuning`; `/guides/2026/…` ≠ `/guides/2027/…`

Verified effect: `postgresql.org/docs/18/performance-tips` and `/docs/current/performance-tips` merged into a single #1 result with 3-engine confirmation at score 39.0 (previously two split entries).

---

## v2.0.0 Changes vs v1.1.0

| Area | v1.1.0 | v2.0.0 |
|---|---|---|
| Tavily | Plain search | `include_answer: true` — synthesized one-line answer at top of output |
| Exa | Neural only | `type: "auto"` — neural + keyword blended; better on exact-term queries |
| Dates | Not captured | `publishedDate` / `published_date` captured and displayed |
| Scoring | Engine count + priority only | + domain quality, recency, position decay |
| Dedup | Exact URL match | + docs-alias collapsing (versioned docs paths merged) |
| Error handling | One failed call drops the engine | Per-engine retry ×2 with backoff on 429/5xx/timeout |
| Output | Text only | Text + `--json` machine-readable mode |
| Snippet length | Fixed 300 chars (score-deciding) | Fixed 300 chars (tie-break only, longer wins) |

## A/B Test Results (2026-08-21)

Tested head-to-head on "Kubernetes GPU scheduling for LLM inference 2026" and "PostgreSQL 18 performance tuning guide":

- **Accuracy:** Same #1 result on both queries; v2's ranks 2–8 measurably better (NVIDIA, Red Hat, Percona, postgresql.org promoted over random single-engine blogs)
- **Comprehensiveness:** On the PG18 query, v1 collapsed to Exa-only (firecrawl/tavily silently returned 0); v2's retries + Exa auto mode kept all 3 engines live — 24 raw results vs v1's 16
- **Synthesized answers:** Returned coherent one-line takeaways on all test queries (DRA/KAI scheduling; PG18 async I/O + EXPLAIN ANALYZE)

## JSON Output Schema

```json
{
  "query": "...",
  "engines": ["firecrawl", "exa", "tavily"],
  "answer": "Tavily synthesized one-line answer",
  "raw": 24, "deduped": 18,
  "errors": [],
  "results": [
    {"url": "...", "title": "...", "snippet": "...",
     "engine": "exa", "rank": 0, "date": "2026-03-17",
     "engines": ["exa", "tavily"], "best_rank": 0, "score": 26.5}
  ]
}
```

## Pitfalls

- **Firecrawl is self-hosted** (`FIRECRAWL_API_URL` → local instance, port 3002). It sometimes returns `data: []` for niche queries — not broken. Exa and Tavily carry the load for obscure topics.
- **Tavily answer quality varies** — treat the synthesized answer as a lead, not ground truth; verify against the ranked results.
- **Domain-quality list is hand-curated** — a niche authoritative site scores 0 (neutral), never negative; only known low-signal domains are penalized.
- **Snippets truncated at 300 chars** — for full content, run `web_extract` on top URLs after ranking.
- **Rate limits** — retries handle transient 429s; if persistent, reduce `MAX_RESULTS`.
- **Exa auto mode** costs the same as neural but may occasionally behave like keyword search on very short queries.

## Verification Checklist

After any change, run a test query and confirm:

1. ✅ All three engines returned (no `Errors:` line)
2. ✅ Top result scores 30+ on a common query (3-engine confirmation)
3. ✅ No duplicate URLs — and no docs-alias pairs (`/docs/18/` vs `/docs/current/`)
4. ✅ Tavily synthesized answer appears above results
5. ✅ Dates render in result metadata where published
6. ✅ `--json` mode emits valid JSON parseable by `json.loads`

## Environment

| Variable | Source | Notes |
|---|---|---|
| `FIRECRAWL_API_KEY` | `~/.hermes/.env` | Self-hosted instance key |
| `FIRECRAWL_API_URL` | `~/.hermes/.env` | Defaults to `https://api.firecrawl.dev` if unset |
| `EXA_API_KEY` | `~/.hermes/.env` | api.exa.ai |
| `TAVILY_API_KEY` | `~/.hermes/.env` | api.tavily.com |
