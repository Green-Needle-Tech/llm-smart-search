#!/usr/bin/env python3
"""Multi-engine web search: Firecrawl + Exa + Tavily in parallel. v2.0.0

Usage:
  python3 search.py "query" [max_results] [--json]
  python3 search.py "query" [max_results] --json        # machine-readable output

Research archiving:
  Queries containing "research" or "deep research" (case-insensitive) are
  automatically archived to the LLM Wiki (Karpathy-style) under WIKI_PATH
  (default ~/wiki) in queries/ — with frontmatter, index.md entry, and
  log.md append. Normal searches are NOT archived. Override with
  --no-wiki (never archive) or --wiki (always archive).

v2.0.0 improvements over v1:
  - Tavily: uses `include_answer` for a synthesized answer + returns published dates
  - Exa: uses neural/keyword auto mode + returns published dates
  - Firecrawl: passes lang filter, tolerates both list and {web:[]} payloads
  - Better scoring: engine agreement + domain quality + recency bonus + position decay
  - Domain-quality scoring (authoritative domains score higher)
  - --json output mode for downstream pipelines
  - Robust retries per engine (one transient failure no longer kills an engine)

Env loader is BUILT IN — reads ~/.hermes/.env automatically.
"""
import os, sys, json, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urlunparse

# --- Built-in env loader (robust: quotes, 'export ' prefix, inline comments) ---
def load_env():
    candidates = [
        os.path.expanduser("~/.hermes/.env"),
        "/root/.hermes/.env",  # fallback: sandboxed shells may override HOME
    ]
    existing = [p for p in candidates if os.path.exists(p)]
    if not existing:
        raise RuntimeError("~/.hermes/.env not found — cannot load API keys")
    # Prefer a file that actually defines search-engine keys (a sandboxed HOME
    # may hold a stub .env with only model keys).
    def has_search_keys(path):
        try:
            with open(path) as f:
                return any(l.startswith(("EXA_API_KEY=", "TAVILY_API_KEY=", "FIRECRAWL_API_KEY="))
                           for l in f)
        except OSError:
            return False
    env_path = next((p for p in existing if has_search_keys(p)), existing[0])
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:]
            k, v = line.split("=", 1)
            v = v.strip()
            if v[:1] in "\"'" and v[-1:] == v[:1] and len(v) >= 2:
                v = v[1:-1]
            else:
                v = v.split(" #")[0].strip()
            os.environ[k.strip()] = v

load_env()

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
JSON_OUT = "--json" in sys.argv
QUERY = ARGS[0] if ARGS else ""
MAX_RESULTS = int(ARGS[1]) if len(ARGS) > 1 else 10
if not QUERY:
    print("usage: search.py QUERY [MAX_RESULTS] [--json] [--no-wiki] [--wiki]"); sys.exit(2)

# --- Research detection: archive to LLM Wiki only for research queries ---
import re as _re
def is_research_query(q):
    return bool(_re.search(r"\b(deep\s+)?research(es|ing)?\b", q, _re.I))

if "--no-wiki" in sys.argv:
    ARCHIVE_TO_WIKI = False
elif "--wiki" in sys.argv:
    ARCHIVE_TO_WIKI = True
else:
    ARCHIVE_TO_WIKI = is_research_query(QUERY)

# --- Domain quality tiers (higher = more authoritative) ---
HIGH_QUALITY_DOMAINS = {
    "arxiv.org", "nature.com", "science.org", "ieee.org", "acm.org", "nih.gov",
    "who.int", "un.org", "gov.uk", "europa.eu", "worldbank.org", "imf.org",
    "oecd.org", "nytimes.com", "reuters.com", "bloomberg.com", "ft.com",
    "wsj.com", "economist.com", "theverge.com", "arstechnica.com", "github.com",
    "stackoverflow.com", "docs.python.org", "developer.mozilla.org",
    "microsoft.com", "google.com", "amazon.com", "aws.amazon.com",
    "openai.com", "anthropic.com", "huggingface.co", "pytorch.org",
}
MEDIUM_QUALITY_DOMAINS = {
    "medium.com", "substack.com", "wikipedia.org", "bbc.com", "cnn.com",
    "techcrunch.com", "wired.com", "zdnet.com", "infoworld.com",
    "venturebeat.com", "engadget.com", "tomshardware.com", "pcgamer.com",
}
LOW_QUALITY_DOMAINS = {
    "pinterest.com", "quora.com", "answers.yahoo.com", "facebook.com",
    "instagram.com", "tiktok.com",
}

def domain_quality(url):
    netloc = urlparse(url).netloc.lower()
    base = netloc[4:] if netloc.startswith("www.") else netloc
    for d in HIGH_QUALITY_DOMAINS:
        if base == d or base.endswith("." + d): return 4
    for d in MEDIUM_QUALITY_DOMAINS:
        if base == d or base.endswith("." + d): return 2
    for d in LOW_QUALITY_DOMAINS:
        if base == d or base.endswith("." + d): return -3
    return 0  # neutral / unknown

def _post(url, payload, headers=None, timeout=30):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())

def _with_retry(fn, retries=2, backoff=1.5):
    """Retry a search fn on transient failures (429/5xx/timeouts)."""
    last_err = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(backoff * (attempt + 1))
                continue
            raise
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
                continue
            raise
    raise last_err

# --- Engine 1: Firecrawl (self-hosted or cloud) ---
def search_firecrawl(q, limit):
    def run():
        fc_url = os.environ.get("FIRECRAWL_API_URL", "https://api.firecrawl.dev").rstrip("/")
        fc_key = os.environ.get("FIRECRAWL_API_KEY", "")
        headers = {"Authorization": f"Bearer {fc_key}"} if fc_key else {}
        resp = _post(f"{fc_url}/v1/search", {"query": q, "limit": limit}, headers)
        web = resp.get("data", [])
        if isinstance(web, dict):
            web = web.get("web", [])
        results = []
        for i, r in enumerate(web):
            desc = r.get("description") or r.get("snippet") or ""
            results.append({
                "url": r.get("url", ""),
                "title": r.get("title", "") or r.get("url", ""),
                "snippet": desc[:300],
                "engine": "firecrawl",
                "rank": i,
                "date": r.get("publishedDate") or "",
            })
        return results
    return _with_retry(run)

# --- Engine 2: Exa (auto: neural + keyword) ---
def search_exa(q, limit):
    def run():
        resp = _post("https://api.exa.ai/search",
                     {"query": q, "numResults": limit,
                      "type": "auto",
                      "contents": {"highlights": True}},
                     {"x-api-key": os.environ["EXA_API_KEY"]})
        results = []
        for i, r in enumerate(resp.get("results", [])):
            highlights = r.get("highlights", [])
            snippet = highlights[0] if highlights else (r.get("text") or "")
            results.append({
                "url": r.get("url", ""),
                "title": r.get("title", "") or r.get("url", ""),
                "snippet": (snippet or "")[:300],
                "engine": "exa",
                "rank": i,
                "date": r.get("publishedDate") or "",
            })
        return results
    return _with_retry(run)

# --- Engine 3: Tavily (+ synthesized answer) ---
def search_tavily(q, limit):
    def run():
        resp = _post("https://api.tavily.com/search",
                     {"query": q, "max_results": limit,
                      "include_answer": True,
                      "api_key": os.environ["TAVILY_API_KEY"]})
        results = []
        for i, r in enumerate(resp.get("results", [])):
            results.append({
                "url": r.get("url", ""),
                "title": r.get("title", "") or r.get("url", ""),
                "snippet": (r.get("content") or r.get("snippet") or "")[:300],
                "engine": "tavily",
                "rank": i,
                "date": r.get("published_date") or "",
            })
        return results, resp.get("answer") or ""
    return _with_retry(run)

# --- Run all 3 in parallel ---
all_results, errors = [], []
tavily_answer = ""
with ThreadPoolExecutor(max_workers=3) as pool:
    futures = {
        pool.submit(search_firecrawl, QUERY, MAX_RESULTS): "firecrawl",
        pool.submit(search_exa, QUERY, MAX_RESULTS): "exa",
        pool.submit(search_tavily, QUERY, MAX_RESULTS): "tavily",
    }
    for fut in as_completed(futures):
        engine = futures[fut]
        try:
            out = fut.result()
            if engine == "tavily":
                results, tavily_answer = out
                all_results.extend(results)
            else:
                all_results.extend(out)
        except Exception as e:
            errors.append(f"{engine}: {e}")

# --- Dedupe by normalized URL ---
import re
# Version-like path segments treated as aliases when they follow a docs-style segment
_DOCS_SEGMENTS = {"docs", "doc", "documentation", "api", "reference", "guide", "handbook", "manual", "wiki", "learn"}
_VERSION_RE = re.compile(r"^(v?\d+([._-]\d+)*|current|latest|stable|master|main|next|old|new)$", re.I)

def norm_url(u):
    p = urlparse(u)
    segments = [s for s in p.path.split("/") if s]
    # Collapse docs version aliases: /docs/18/... == /docs/current/... == /docs/
    for i in range(1, len(segments)):
        if segments[i - 1].lower() in _DOCS_SEGMENTS and _VERSION_RE.match(segments[i]):
            segments[i] = "~v~"
            # collapse consecutive version-ish segments (e.g. /docs/18/2/)
            j = i + 1
            while j < len(segments) and _VERSION_RE.match(segments[j]):
                segments.pop(j)
    path = "/" + "/".join(segments)
    return urlunparse((p.scheme, p.netloc.lower(), path.rstrip("/"), "", "", "")).lower()

merged = {}
for r in all_results:
    key = norm_url(r["url"])
    if not key:
        continue
    if key not in merged:
        merged[key] = {**r, "engines": [r["engine"]], "best_rank": r.get("rank", 9)}
    else:
        merged[key]["engines"].append(r["engine"])
        if len(r["snippet"]) > len(merged[key]["snippet"]):
            merged[key]["snippet"] = r["snippet"]
        if not merged[key].get("date") and r.get("date"):
            merged[key]["date"] = r["date"]
        merged[key]["best_rank"] = min(merged[key]["best_rank"], r.get("rank", 9))

# --- Scoring: engine agreement + priority + domain quality + recency + position ---
engine_priority = {"firecrawl": 3, "exa": 2, "tavily": 1}
now = time.time()
for key, r in merged.items():
    score = 0.0
    # Engine agreement (dominant signal)
    score += len(r["engines"]) * 10
    score += sum(engine_priority.get(e, 0) for e in r["engines"])
    # Domain quality
    score += domain_quality(r["url"])
    # Recency bonus (up to +3 for results < 1 year old)
    date = r.get("date") or ""
    if date[:4].isdigit():
        try:
            import datetime
            dt = datetime.datetime.fromisoformat(date.replace("Z", "+00:00"))
            age_days = (now - dt.timestamp()) / 86400
            if age_days < 0: age_days = 0
            if age_days < 90: score += 3
            elif age_days < 365: score += 2
            elif age_days < 730: score += 1
        except ValueError:
            pass
    # Position decay: appearing high in any engine's own ranking adds a little
    score += max(0, 3 - r.get("best_rank", 9)) * 0.5
    r["score"] = round(score, 1)

ranked = sorted(merged.values(), key=lambda x: -x["score"])[:MAX_RESULTS]

# --- Archive research results to the LLM Wiki (research queries only) ---
def slugify(text, max_len=60):
    s = _re.sub(r"[^a-z0-9\s-]", "", text.lower()).strip()
    s = _re.sub(r"[\s_-]+", "-", s)
    return (s[:max_len].rstrip("-")) or "research-query"

def archive_to_wiki(query, results, answer, engine_errors):
    """File the research result as a wiki page under WIKI_PATH/queries/.

    Follows the Karpathy LLM Wiki conventions: frontmatter, index.md entry,
    log.md append. Best-effort — archive failures never break the search.
    Returns the created file path, or None on failure/skip.
    """
    if not results:
        return None
    try:
        import datetime, hashlib
        wiki = os.environ.get("WIKI_PATH", os.path.expanduser("~/wiki"))
        qdir = os.path.join(wiki, "queries")
        os.makedirs(qdir, exist_ok=True)
        today = datetime.date.today().isoformat()
        slug = slugify(query)
        path = os.path.join(qdir, f"{slug}.md")
        n = 1
        while os.path.exists(path):  # don't clobber earlier research runs
            n += 1
            path = os.path.join(qdir, f"{slug}-{n}.md")
        sources = [r["url"] for r in results if r.get("url")]
        lines = [
            "---",
            f"title: 'Research: {query[:100]}'",
            f"created: {today}",
            f"updated: {today}",
            "type: query",
            "tags: [research, web-search]",
            f"sources: {json.dumps(sources[:10])}",
            "---",
            "",
            f"# Research: {query}",
            "",
            f"*Auto-archived by llm-smart-search on {today} "
            f"(firecrawl + exa + tavily, {len(results)} ranked results).*",
            "",
        ]
        if answer:
            lines += ["## Synthesized answer", "", answer, ""]
        lines += ["## Top results", ""]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. **[{r['title']}]({r['url']})** — score {r['score']}, "
                         f"engines: {', '.join(sorted(set(r['engines'])))}"
                         + (f", {r['date'][:10]}" if r.get("date") else ""))
            if r.get("snippet"):
                lines.append(f"   > {r['snippet'][:250]}")
        if engine_errors:
            lines += ["", f"*Engine errors: {'; '.join(engine_errors)}*"]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        # index.md entry (append under ## Queries, or create header)
        idx = os.path.join(wiki, "index.md")
        entry = f"- [[{os.path.splitext(os.path.basename(path))[0]}]] — {query[:80]}"
        if os.path.exists(idx):
            with open(idx, encoding="utf-8") as f:
                content = f.read()
            if "## Queries" in content:
                head, _, tail = content.rpartition("## Queries")
                # insert before next section header or at end
                nxt = _re.search(r"\n## ", tail)
                insert_at = nxt.start() if nxt else len(tail)
                tail = tail[:insert_at].rstrip("\n") + "\n" + entry + "\n" + tail[insert_at:]
                content = head + "## Queries" + tail
            else:
                content = content.rstrip("\n") + "\n\n## Queries\n\n" + entry + "\n"
            with open(idx, "w", encoding="utf-8") as f:
                f.write(content)
        else:
            with open(idx, "w", encoding="utf-8") as f:
                f.write(f"# Wiki Index\n\n## Queries\n\n{entry}\n")
        # log.md append
        log = os.path.join(wiki, "log.md")
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"\n## [{today}] ingest | llm-smart-search: {query[:80]}\n"
                    f"- Archived {len(results)} ranked results to queries/{os.path.basename(path)}\n")
        return path
    except Exception as e:
        print(f"wiki-archive: skipped ({e})", file=sys.stderr)
        return None

wiki_path = archive_to_wiki(QUERY, ranked, tavily_answer, errors) if ARCHIVE_TO_WIKI else None

# --- Output ---
if JSON_OUT:
    print(json.dumps({
        "query": QUERY,
        "engines": ["firecrawl", "exa", "tavily"],
        "answer": tavily_answer,
        "raw": len(all_results),
        "deduped": len(merged),
        "errors": errors,
        "archived_to_wiki": wiki_path,
        "results": ranked,
    }, ensure_ascii=False, indent=2))
    sys.exit(0)

print(f"Query: {QUERY}")
print("Engines: firecrawl, exa, tavily")
print(f"Raw: {len(all_results)} | Deduped: {len(merged)} | Top: {len(ranked)}")
if ARCHIVE_TO_WIKI:
    print(f"Wiki: {'archived -> ' + wiki_path if wiki_path else 'archive skipped (no results or error)'}")
if errors:
    print(f"Errors: {', '.join(errors)}")
if tavily_answer:
    print("=" * 80)
    print(f"Synthesized answer (Tavily): {tavily_answer[:500]}")
print("=" * 80)
for i, r in enumerate(ranked, 1):
    print(f"\n[{i}] {r['title']}")
    print(f"    URL: {r['url']}")
    meta = [f"engines: {', '.join(sorted(set(r['engines'])))}", f"score: {r['score']}"]
    if r.get("date"):
        meta.append(f"date: {r['date'][:10]}")
    print(f"    {' | '.join(meta)}")
    print(f"    {r['snippet'][:200]}")
