#!/usr/bin/env python3
"""Multi-engine web search: Firecrawl + Exa + Brave + arXiv in parallel. v2.4.0

Usage:
  python3 search.py "query" [max_results] [--json]
  python3 search.py "query" [max_results] --json        # machine-readable output

Research archiving & Memory Architecture:
  Queries containing "research" or "deep research" (case-insensitive) are
  automatically archived to the LLM Wiki (Karpathy-style, L3) under WIKI_PATH
  (default ~/wiki) in queries/ — with frontmatter, index.md entry, and
  log.md append. Additionally, a relevant pointer is retained in Hindsight (L2)
  tagged 'wiki-ref' for fast semantic and temporal retrieval.
  Normal searches are NOT archived. Override with
  --no-wiki (never archive) or --wiki (always archive).

v2.4.0 improvements over v2.3:
  - L2 Hindsight pointer retention: when research queries are archived to L3 LLM Wiki,
    a pointer episode is automatically retained in Hindsight memory (bank: main,
    tags: wiki-ref, research, web-search) containing the query, wiki relative path,
    key domains, and top sources.
  - Configurable via HINDSIGHT_API_URL, HINDSIGHT_BANK_ID, HINDSIGHT_API_KEY, and
    HINDSIGHT_AUTO_RETAIN env vars. Best-effort and non-blocking.
v2.2.0 improvements over v2.1:
  - arXiv engine (4th): research papers via export.arxiv.org API, auto-enabled for
    research-y queries or --arxiv; keyword queries of <=4 words also get arXiv
  - RRF (Reciprocal Rank Fusion) scoring: score = sum(1/(60+rank)) per engine,
    then domain-quality and recency adjustments. Replaces hand-tuned weights.
  - Engine key-guard: engines without API keys are skipped with a clear error
    message instead of dying with KeyError
  - Engine registry: ENGINES dict makes adding engines a 10-line change

v2.0.0 improvements over v1:
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
                return any(l.startswith(("EXA_API_KEY=", "BRAVE_SEARCH_API_KEY=", "FIRECRAWL_API_KEY="))
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
try:
    MAX_RESULTS = int(ARGS[1]) if len(ARGS) > 1 else 10
    if not (1 <= MAX_RESULTS <= 50):
        raise ValueError
except ValueError:
    print(f"error: MAX_RESULTS must be an integer between 1 and 50, got {ARGS[1]!r}\n"
          "usage: search.py QUERY [MAX_RESULTS] [--json] [--no-wiki] [--wiki] [--arxiv] [--no-arxiv]",
          file=sys.stderr)
    sys.exit(2)
if not QUERY:
    print("usage: search.py QUERY [MAX_RESULTS] [--json] [--no-wiki] [--wiki] [--arxiv] [--no-arxiv]"); sys.exit(2)

# --- Research detection: archive to LLM Wiki only for research queries ---
import re as _re
def is_research_query(q):
    return bool(_re.search(r"\b(deep\s+)?research(es|ing)?\b", q, _re.I))

ARXIV_FLAG = "on" if "--arxiv" in sys.argv else ("off" if "--no-arxiv" in sys.argv else "auto")
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
                     {"x-api-key": os.environ["EXA_API_KEY"]}) if os.environ.get("EXA_API_KEY") else (_ for _ in ()).throw(RuntimeError("EXA_API_KEY not set (add it to ~/.hermes/.env)"))
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

# --- Engine 3: Brave Search (web index, X-Subscription-Token auth) ---
def search_brave(q, limit):
    def run():
        if not os.environ.get("BRAVE_SEARCH_API_KEY"):
            raise RuntimeError("BRAVE_SEARCH_API_KEY not set (add it to ~/.hermes/.env)")
        from urllib.parse import urlencode
        url = ("https://api.search.brave.com/res/v1/web/search?"
               + urlencode({"q": q, "count": min(limit, 20), "country": "us",
                            "search_lang": "en", "safesearch": "moderate",
                            "extra_snippets": "false"}))
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": os.environ["BRAVE_SEARCH_API_KEY"],
        })
        import gzip as _gzip
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = _gzip.decompress(raw)
        data = json.loads(raw)
        results = []
        for i, r in enumerate((data.get("web") or {}).get("results", [])):
            # page_age is ISO datetime; age is human-readable ("2 days ago")
            date = r.get("page_age") or ""
            if not date and r.get("age", "").startswith("20"):  # some ages are dates
                date = r["age"]
            results.append({
                "url": r.get("url", ""),
                "title": r.get("title", "") or r.get("url", ""),
                "snippet": (r.get("description") or "")[:300],
                "engine": "brave",
                "rank": i,
                "date": date,
            })
        return results
    return _with_retry(run)


# --- Engine 4: arXiv (research papers; no API key required) ---
import urllib.parse as _uparse
def search_arxiv(q, limit):
    """Search arXiv via the public Atom API. Free, no key.

    Only meaningful for research-style queries — enabled via --arxiv,
    automatically when the query looks research-oriented, or always when
    ARAVX/ARXIV_ALWAYS=1.
    """
    def run():
        url = ("https://export.arxiv.org/api/query?search_query=all:"
               + _uparse.quote(q) + "&start=0&max_results=" + str(min(limit, 20))
               + "&sortBy=relevance&sortOrder=descending")
        req = urllib.request.Request(url, headers={"User-Agent": "llm-smart-search/2.2"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            xml = resp.read().decode("utf-8", "replace")
        results = []
        entries = _re.findall(r"<entry>(.*?)</entry>", xml, _re.S)
        for i, e in enumerate(entries):
            def tag(t):
                m = _re.search(rf"<{t}[^>]*>(.*?)</{t}>", e, _re.S)
                return (m.group(1).strip() if m else "")
            link = next(iter(_re.findall(r'<id>(.*?)</id>', e)), "")
            if "arxiv.org/abs/" in link:
                link = "https://arxiv.org/abs/" + link.split("/abs/")[-1]
            results.append({
                "url": link,
                "title": tag("title").replace("\n", " ") or link,
                "snippet": tag("summary").replace("\n", " ")[:300],
                "engine": "arxiv",
                "rank": i,
                "date": tag("published")[:10],
            })
        return results
    return _with_retry(run)

# --- Engine registry (add a new engine = add one entry here) ---
def _looks_researchy(q):
    """Research-flavored query: research/deep research, paper, survey, arxiv,
    'state of the art', benchmarks, etc."""
    return bool(_re.search(r"\b(research(es|ing)?|deep research|paper|papers|survey|arxiv|literature|state[- ]of[- ]the[- ]art|sota|benchmark)\b", q, _re.I))

USE_ARXIV = (ARXIV_FLAG == "on"
             or (ARXIV_FLAG == "auto" and (_looks_researchy(QUERY)
                 or os.environ.get("ARXIV_ALWAYS") == "1")))

ENGINES = [
    ("firecrawl", lambda: search_firecrawl(QUERY, MAX_RESULTS)),
    ("exa", lambda: search_exa(QUERY, MAX_RESULTS)),
    ("brave", lambda: search_brave(QUERY, MAX_RESULTS)),
]
if USE_ARXIV:
    ENGINES.append(("arxiv", lambda: search_arxiv(QUERY, MAX_RESULTS)))

# --- Run engines in parallel ---
all_results, errors = [], []
with ThreadPoolExecutor(max_workers=len(ENGINES)) as pool:
    futures = {pool.submit(fn): name for name, fn in ENGINES}
    for fut in as_completed(futures):
        engine = futures[fut]
        try:
            all_results.extend(fut.result())
        except Exception as e:
            errors.append(f"{engine}: {e}")

active_engines = [n for n, _ in ENGINES]
if len(active_engines) - sum(1 for e in errors if e.split(":")[0] in active_engines) < 2 and len(active_engines) > 1:
    print(f"WARNING: fewer than 2 engines returned results — rankings below are "
          f"single-engine and may be unreliable. Errors: {'; '.join(errors)}", file=sys.stderr)

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
        merged[key] = {**r, "engines": [r["engine"]],
                       "engine_ranks": {r["engine"]: r.get("rank", 9)},
                       "best_rank": r.get("rank", 9)}
    else:
        merged[key]["engines"].append(r["engine"])
        merged[key]["engine_ranks"][r["engine"]] = r.get("rank", 9)
        if len(r["snippet"]) > len(merged[key]["snippet"]):
            merged[key]["snippet"] = r["snippet"]
        if not merged[key].get("date") and r.get("date"):
            merged[key]["date"] = r["date"]
        merged[key]["best_rank"] = min(merged[key]["best_rank"], r.get("rank", 9))

# --- Scoring v2.2: Reciprocal Rank Fusion + domain quality + recency ---
# RRF (Cormack et al. 2009): score = sum over engines of 1/(k + rank), k=60.
# Tuning-free, uses each engine's full rank list, and is the de-facto standard
# for multi-source fusion (Elasticsearch, OpenSearch, MongoDB all ship it).
RRF_K = 60
now = time.time()
for key, r in merged.items():
    score = 0.0
    # RRF: aggregate each engine's own ranking of this URL.
    engine_ranks = r.get("engine_ranks", {})
    for e, rk in engine_ranks.items():
        score += 1.0 / (RRF_K + 1 + rk)
    # Scale up to a readable range (~ x100)
    score *= 100
    # Domain quality bonus (relative, kept small vs RRF magnitude)
    score += domain_quality(r["url"]) * 0.1
    # Recency bonus
    date = r.get("date") or ""
    if date[:4].isdigit():
        try:
            import datetime
            dt = datetime.datetime.fromisoformat(date.replace("Z", "+00:00"))
            age_days = (now - dt.timestamp()) / 86400
            if age_days < 0: age_days = 0
            if age_days < 90: score += 0.10
            elif age_days < 365: score += 0.05
        except ValueError:
            pass
    r["score"] = round(score, 3)

ranked = sorted(merged.values(), key=lambda x: -x["score"])[:MAX_RESULTS]

# --- Research archiving: L3 LLM Wiki + L2 Hindsight pointer ---
def slugify(text, max_len=60):
    s = _re.sub(r"[^a-z0-9\s-]", "", text.lower()).strip()
    s = _re.sub(r"[\s_-]+", "-", s)
    return (s[:max_len].rstrip("-")) or "research-query"

def retain_to_hindsight(query, wiki_rel_path, results, today):
    """Store an L2 pointer in Hindsight pointing to the L3 wiki page.

    Follows the 3-layer memory architecture: Hindsight (L2) holds the pointer
    (tagged wiki-ref) for semantic/temporal recall; full search results live in
    the LLM Wiki (L3). Best-effort — failure prints to stderr and never breaks search.
    """
    if os.environ.get("HINDSIGHT_AUTO_RETAIN", "1") in ("0", "false", "no"):
        return False
    base_url = os.environ.get("HINDSIGHT_API_URL", "http://localhost:8888").rstrip("/")
    bank_id = os.environ.get("HINDSIGHT_BANK_ID", "main")
    api_key = os.environ.get("HINDSIGHT_API_KEY", "")

    # Top sources & domains
    sources = [r["url"] for r in results[:5] if r.get("url")]
    domains = list(dict.fromkeys(urlparse(u).netloc for u in sources if urlparse(u).netloc))

    content = (
        f"Research query '{query}' was searched via llm-smart-search on {today} and archived to wiki at {wiki_rel_path}. "
        f"Top domains: {', '.join(domains[:5])}. Key sources: {', '.join(sources[:3])}."
    )
    payload = {
        "items": [{
            "content": content,
            "context": f"llm-smart-search research query: {query[:100]}",
            "tags": ["wiki-ref", "research", "web-search"]
        }]
    }
    url = f"{base_url}/v1/default/banks/{bank_id}/memories"
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        _post(url, payload, headers=headers, timeout=10)
        return True
    except Exception as e:
        print(f"hindsight-retain: skipped ({e})", file=sys.stderr)
        return False

def archive_to_wiki(query, results, answer, engine_errors):
    """File the research result as a wiki page under WIKI_PATH/queries/.

    Follows the Karpathy LLM Wiki conventions: frontmatter, index.md entry,
    log.md append, plus an L2 pointer retained in Hindsight.
    Best-effort — archive failures never break the search.
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
            f"(firecrawl + exa + brave, {len(results)} ranked results).*",
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

        # Retain L2 pointer in Hindsight
        rel_wiki_path = f"queries/{os.path.basename(path)}"
        retain_to_hindsight(query, rel_wiki_path, results, today)

        return path
    except Exception as e:
        print(f"wiki-archive: skipped ({e})", file=sys.stderr)
        return None

wiki_path = archive_to_wiki(QUERY, ranked, "", errors) if ARCHIVE_TO_WIKI else None

# --- Output ---
if JSON_OUT:
    print(json.dumps({
        "query": QUERY,
        "engines": active_engines,
        "raw": len(all_results),
        "deduped": len(merged),
        "errors": errors,
        "archived_to_wiki": wiki_path,
        "results": ranked,
    }, ensure_ascii=False, indent=2))
    sys.exit(0)

print(f"Query: {QUERY}")
print(f"Engines: {', '.join(active_engines)}")
print(f"Raw: {len(all_results)} | Deduped: {len(merged)} | Top: {len(ranked)}")
if ARCHIVE_TO_WIKI:
    print(f"Wiki: {'archived -> ' + wiki_path if wiki_path else 'archive skipped (no results or error)'}")
if errors:
    print(f"Errors: {', '.join(errors)}")
print("=" * 80)
for i, r in enumerate(ranked, 1):
    print(f"\n[{i}] {r['title']}")
    print(f"    URL: {r['url']}")
    meta = [f"engines: {', '.join(sorted(set(r['engines'])))}", f"score: {r['score']}"]
    if r.get("date"):
        meta.append(f"date: {r['date'][:10]}")
    print(f"    {' | '.join(meta)}")
    print(f"    {r['snippet'][:200]}")
