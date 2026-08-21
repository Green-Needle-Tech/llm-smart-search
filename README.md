# LLM Smart Search

Multi-engine web search for AI agents and RAG pipelines — **Firecrawl + Exa + Tavily in parallel, merged, scored, and ranked**, with a synthesized one-line answer on top.

One Python script. Zero pip dependencies (stdlib only, Python 3.8+). Built to run inside [Hermes Agent](https://hermes-agent.nousresearch.com) sessions, cron jobs, or any pipeline where a single search engine misses too much.

## Why

Any single search engine has blind spots — niche sources, deep-web pages, semantically-related-but-different phrasing. This tool fans one query out to three engines simultaneously, then cross-confirms results: a URL returned by multiple engines is almost always the authoritative source.

```
        ┌──> Firecrawl (self-hosted or cloud) ────────┐
Query ──┤──> Exa (neural + keyword, type=auto) ───────┼──> Dedupe ──> Score ──> Rank ──> Output
        └──> Tavily (LLM-tuned, +synthesized answer) ┘
              (ThreadPoolExecutor, max_workers=3, per-engine retry ×2)
```

## Quick Start

```bash
# Basic usage (text output)
python3 scripts/search.py "query" [max_results]

# Machine-readable output for pipelines / cron jobs
python3 scripts/search.py "query" 10 --json
```

### API keys

Set these in your environment or `~/.hermes/.env` (the script's built-in env loader reads it automatically):

| Variable | Notes |
|---|---|
| `FIRECRAWL_API_KEY` | Firecrawl key |
| `FIRECRAWL_API_URL` | Optional — defaults to `https://api.firecrawl.dev`; point at a self-hosted instance if you have one |
| `EXA_API_KEY` | api.exa.ai |
| `TAVILY_API_KEY` | api.tavily.com |

## Scoring Model

Each result is scored on five signals:

| Signal | Weight | Notes |
|---|---|---|
| **Engine agreement** | +10 per engine + priority (firecrawl=3, exa=2, tavily=1) | In all 3 engines → 30+ |
| **Domain quality** | +4 / +2 / −3 | Authoritative domains (arxiv, github, docs.\*) vs medium vs low-signal |
| **Recency** | +3 / +2 / +1 | < 90 days / < 1 year / < 2 years |
| **Position decay** | +0.5 × (3 − best rank) | High rank in any engine's own results |
| **Snippet length** | tie-break | Longer snippet wins during merge |

**Score guide:** 30+ = confirmed by all 3 engines · 20–29 = two engines · 12–19 = single engine.

## Deduplication

URLs are normalized (lowercase netloc, trailing slash stripped, query/fragment ignored), plus **docs-alias collapsing**:

- `/docs/18/…` ≡ `/docs/current/…` ≡ `/docs/latest/…` → merged as one result
- Conservative: version segments only collapse after docs-style segments (`docs`, `api`, `reference`, `guide`, `wiki`, `learn`)
- Content-bearing versions stay separate: `/blog/pg-18-tuning` ≠ `/blog/pg-19-tuning`

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

## Documentation

- [`docs/MULTI_ENGINE_SEARCH_v2.md`](docs/MULTI_ENGINE_SEARCH_v2.md) — full skill documentation (architecture, scoring, dedup rules, A/B test results)
- [`docs/MULTI_ENGINE_SEARCH_v2_INSTALL.md`](docs/MULTI_ENGINE_SEARCH_v2_INSTALL.md) — single-file installable package (docs + complete source)

## Install as a Hermes Agent skill

1. `mkdir -p ~/.hermes/skills/research/llm-smart-search/scripts`
2. Copy `scripts/search.py` into it
3. Add the 3–4 API keys to `~/.hermes/.env`
4. Verify: `python3 scripts/search.py "test query" 5`

## License

MIT
