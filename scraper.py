"""
Tech Myth-Bust daily scraper - FREE STACK (Groq + DuckDuckGo).

Pulls high-reach tech items from HN, Reddit, RSS feeds. Uses Groq
(llama-3.3-70b-versatile, free tier) for the flag pass and the verify pass.
The verify pass uses DuckDuckGo search results as grounding so the model
can re-check claims against real sources. Posts results to a Google Apps
Script web app, which appends rows to a Google Sheet.

Cost: $0. No credit card anywhere.

Env vars required:
    GROQ_API_KEY        - free key from https://console.groq.com/keys
    SHEET_WEBHOOK_URL   - Google Apps Script web app URL (POST endpoint)
    SHEET_SECRET        - shared secret sent in body to gate writes
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import html
import datetime as dt
from typing import Any
from urllib.parse import urlparse

import feedparser
import requests
from groq import Groq, RateLimitError, APIError

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS  # type: ignore

# ---------- Config ----------

MODEL = "llama-3.3-70b-versatile"
MAX_RAW_ITEMS = 35
MAX_FLAGGED = 6
SLEEP_BETWEEN_CALLS = 2.5
RETRIES = 4

RSS_FEEDS = [
    ("TechCrunch", "https://techcrunch.com/feed/"),
    ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
    ("Wired", "https://www.wired.com/feed/rss"),
    ("Engadget", "https://www.engadget.com/rss.xml"),
    ("MIT Tech Review", "https://www.technologyreview.com/feed/"),
]

REDDIT_SUBS = ["technology", "gadgets", "Futurology"]
UA = {"User-Agent": "tech-myth-bust-scraper/1.0 (github actions)"}

# ---------- Source pullers ----------

def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def pull_hn(limit: int = 15) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        r = requests.get("https://hacker-news.firebaseio.com/v0/topstories.json", timeout=20, headers=UA)
        r.raise_for_status()
        ids = r.json()[: limit * 2]
        for sid in ids:
            if len(out) >= limit:
                break
            item = requests.get(f"https://hacker-news.firebaseio.com/v0/item/{sid}.json", timeout=15, headers=UA).json() or {}
            if item.get("score", 0) < 150:
                continue
            url = item.get("url") or f"https://news.ycombinator.com/item?id={sid}"
            out.append({
                "title": item.get("title", "").strip(),
                "url": url,
                "source": f"Hacker News ({item.get('score')} pts)",
                "summary": "",
                "reach_signal": f"HN front page, {item.get('score')} pts, {item.get('descendants', 0)} comments",
            })
    except Exception as e:
        print(f"[hn] error: {e}", file=sys.stderr)
    return out


def pull_reddit(subs: list[str], limit_per_sub: int = 8) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sub in subs:
        try:
            r = requests.get(f"https://www.reddit.com/r/{sub}/top.json?t=day&limit={limit_per_sub}", timeout=20, headers=UA)
            r.raise_for_status()
            for child in r.json().get("data", {}).get("children", []):
                d = child.get("data", {})
                if d.get("ups", 0) < 1000:
                    continue
                out.append({
                    "title": d.get("title", "").strip(),
                    "url": d.get("url_overridden_by_dest") or f"https://reddit.com{d.get('permalink')}",
                    "source": f"r/{sub}",
                    "summary": _strip_html(d.get("selftext", ""))[:300],
                    "reach_signal": f"r/{sub} top of day, {d.get('ups')} upvotes, {d.get('num_comments')} comments",
                })
            time.sleep(1)
        except Exception as e:
            print(f"[reddit {sub}] error: {e}", file=sys.stderr)
    return out


def pull_rss(feeds: list[tuple[str, str]], per_feed: int = 5) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cutoff = dt.datetime.utcnow() - dt.timedelta(hours=36)
    for name, url in feeds:
        try:
            parsed = feedparser.parse(url, request_headers=UA)
            for entry in parsed.entries[:per_feed]:
                pub = entry.get("published_parsed") or entry.get("updated_parsed")
                if pub and dt.datetime(*pub[:6]) < cutoff:
                    continue
                out.append({
                    "title": entry.get("title", "").strip(),
                    "url": entry.get("link", ""),
                    "source": name,
                    "summary": _strip_html(entry.get("summary", ""))[:400],
                    "reach_signal": f"{name} front page",
                })
        except Exception as e:
            print(f"[rss {name}] error: {e}", file=sys.stderr)
    return out


def dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    out: list[dict[str, Any]] = []
    for it in items:
        u = (it.get("url") or "").strip().lower()
        t = re.sub(r"[^a-z0-9]+", "", (it.get("title") or "").lower())[:60]
        if not u or not t:
            continue
        try:
            p = urlparse(u)
            key = f"{p.netloc}{p.path}".rstrip("/")
        except Exception:
            key = u
        if key in seen_urls or t in seen_titles:
            continue
        seen_urls.add(key)
        seen_titles.add(t)
        out.append(it)
    return out

# ---------- Prompts ----------

FLAG_SYSTEM = """You curate tech news for an Instagram myth-busting channel.

From the candidate items, pick which ones contain HYPE / EXAGGERATED / MISLEADING claims worth debunking publicly.

Flag-worthy signals:
- "world's first", "10x faster", "human-level", "cures X", "replaces [profession]"
- specific quantitative claims without proper benchmarks
- viral demos that may be cherry-picked or staged
- marketing-only specs (peak vs sustained, lab vs real-world)
- studies framed as conclusive when they are preliminary

NOT flag-worthy: routine product launches, price drops, executive shuffles, earnings, normal feature updates, anything without a specific claim that could be checked.

Return STRICT JSON only. Schema:
{
  "items": [
    {
      "index": <int, 0-based index into the input list>,
      "claim": "<the specific claim quoted from title/summary>",
      "myth_angle": "<1 sentence: what the bust would actually be>",
      "confidence": <float 0-1>
    }
  ]
}

Pick at most 6 items. Skip entirely if nothing is bust-worthy."""

VERIFY_SYSTEM = """You re-verify whether a tech claim is genuinely worth myth-busting, using the search snippets provided.

Look at the snippets for:
1. Whether the claim is actually being made by the original source, or is a headline distortion.
2. Counter-evidence, expert pushback, fact-checks, benchmark scrutiny.
3. Whether the claim is specific and checkable, or vague marketing.

Decide:
- KEEP: claim is specific, contains a likely-exaggerated assertion, there is signal in the snippets that it is wrong or unverified. Worth a video.
- DROP: snippets confirm the claim is accurate, OR claim is too vague to bust, OR no useful signal. Not worth a video.

Return STRICT JSON only. Schema:
{
  "verdict": "KEEP" | "DROP",
  "reason": "<1-2 sentences citing what the snippets showed>",
  "refined_myth_angle": "<if KEEP, a sharper bust angle; if DROP, empty string>"
}"""

# ---------- Groq + DDG ----------

def _parse_json(text: str) -> dict | None:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?", "", text)
    text = re.sub(r"```$", "", text).strip()
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        text = m.group(0)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def groq_chat(client: Groq, system: str, user: str) -> str:
    last_err: Exception | None = None
    for attempt in range(RETRIES):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
                max_tokens=1500,
            )
            return resp.choices[0].message.content or ""
        except RateLimitError as e:
            wait = (2 ** attempt) * 5
            print(f"[groq] rate limited, sleeping {wait}s ({attempt+1}/{RETRIES})", file=sys.stderr)
            time.sleep(wait)
            last_err = e
        except APIError as e:
            wait = (2 ** attempt) * 3
            print(f"[groq] api error: {e}, sleeping {wait}s", file=sys.stderr)
            time.sleep(wait)
            last_err = e
    raise RuntimeError(f"groq failed after {RETRIES} retries: {last_err}")


def ddg_search(query: str, max_results: int = 4) -> list[dict[str, str]]:
    try:
        with DDGS() as d:
            results = list(d.text(query, max_results=max_results))
        return [
            {
                "title": (r.get("title") or "")[:200],
                "url": r.get("href") or r.get("url") or "",
                "snippet": (r.get("body") or r.get("snippet") or "")[:400],
            }
            for r in results
        ]
    except Exception as e:
        print(f"[ddg] error for {query!r}: {e}", file=sys.stderr)
        return []


def groq_flag_pass(client: Groq, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload = [
        {"i": i, "title": it["title"], "source": it["source"], "summary": it["summary"][:250]}
        for i, it in enumerate(items)
    ]
    user_msg = f"Candidates:\n{json.dumps(payload, ensure_ascii=False)}"
    text = groq_chat(client, FLAG_SYSTEM, user_msg)
    data = _parse_json(text)
    if not data:
        print(f"[flag] could not parse JSON: {text[:300]}", file=sys.stderr)
        return []
    flagged: list[dict[str, Any]] = []
    for entry in data.get("items", [])[:MAX_FLAGGED]:
        i = entry.get("index")
        if not isinstance(i, int) or not (0 <= i < len(items)):
            continue
        src = items[i].copy()
        src["claim"] = entry.get("claim", "")
        src["myth_angle"] = entry.get("myth_angle", "")
        src["flag_confidence"] = entry.get("confidence", 0.0)
        flagged.append(src)
    return flagged


def groq_verify(client: Groq, item: dict[str, Any]) -> dict[str, Any]:
    queries = [
        f"{item['title']} fact check OR debunked",
        f"{item['claim'][:120]} accuracy OR overhyped" if item['claim'] else f"{item['title']} criticism",
    ]
    snippets: list[dict[str, str]] = []
    for q in queries:
        snippets.extend(ddg_search(q, max_results=4))
        time.sleep(1)
    seen: set[str] = set()
    uniq = []
    for s in snippets:
        if not s["url"] or s["url"] in seen:
            continue
        seen.add(s["url"])
        uniq.append(s)
    uniq = uniq[:8]
    if not uniq:
        return {"verdict": "DROP", "reason": "no search results available", "refined_myth_angle": ""}
    user_msg = (
        f"Item:\nTitle: {item['title']}\nSource: {item['source']}\n"
        f"URL: {item['url']}\nClaim: {item['claim']}\nInitial myth angle: {item['myth_angle']}\n\n"
        f"Search snippets:\n{json.dumps(uniq, ensure_ascii=False, indent=2)}\n\n"
        f"Decide KEEP or DROP based on the snippets."
    )
    try:
        text = groq_chat(client, VERIFY_SYSTEM, user_msg)
    except Exception as e:
        return {"verdict": "DROP", "reason": f"verification failed: {e}", "refined_myth_angle": ""}
    data = _parse_json(text)
    if not data:
        return {"verdict": "DROP", "reason": "could not parse verification response", "refined_myth_angle": ""}
    return data

# ---------- Sheet write ----------

def post_to_sheet(rows: list[dict[str, Any]]) -> None:
    url = os.environ["SHEET_WEBHOOK_URL"]
    secret = os.environ["SHEET_SECRET"]
    r = requests.post(
        url,
        json={"secret": secret, "rows": rows},
        timeout=30,
        headers={"Content-Type": "application/json"},
    )
    r.raise_for_status()
    print(f"[sheet] posted {len(rows)} rows: {r.status_code} {r.text[:200]}")

# ---------- Main ----------

def main() -> int:
    today = dt.date.today().isoformat()
    print(f"[run] {today}")
    client = Groq(api_key=os.environ["GROQ_API_KEY"])

    raw = pull_hn() + pull_reddit(REDDIT_SUBS) + pull_rss(RSS_FEEDS)
    raw = dedupe(raw)[:MAX_RAW_ITEMS]
    print(f"[raw] {len(raw)} items after dedupe")
    if not raw:
        print("no items; exiting")
        return 0

    flagged = groq_flag_pass(client, raw)
    print(f"[flag] {len(flagged)} candidates")
    time.sleep(SLEEP_BETWEEN_CALLS)

    keep: list[dict[str, Any]] = []
    drop: list[dict[str, Any]] = []
    for item in flagged:
        v = groq_verify(client, item)
        item["verify_verdict"] = v.get("verdict", "DROP")
        item["verify_reason"] = v.get("reason", "")
        if v.get("verdict") == "KEEP":
            if v.get("refined_myth_angle"):
                item["myth_angle"] = v["refined_myth_angle"]
            keep.append(item)
        else:
            drop.append(item)
        time.sleep(SLEEP_BETWEEN_CALLS)

    print(f"[verify] kept {len(keep)}, dropped {len(drop)}")

    flagged_urls = {f["url"] for f in flagged}
    rows: list[dict[str, Any]] = []

    def row(it: dict[str, Any], label: str) -> dict[str, Any]:
        return {
            "date": today,
            "flagged": label,
            "title": it["title"],
            "source": it["source"],
            "url": it["url"],
            "summary": it.get("summary", ""),
            "claim": it.get("claim", ""),
            "reach_signal": it.get("reach_signal", ""),
            "myth_angle": it.get("myth_angle", ""),
            "verify_verdict": it.get("verify_verdict", ""),
            "verify_reason": it.get("verify_reason", ""),
        }

    for it in keep:
        rows.append(row(it, "YES"))
    for it in drop:
        rows.append(row(it, "DROPPED"))
    for it in raw:
        if it["url"] in flagged_urls:
            continue
        rows.append(row(it, "NO"))

    post_to_sheet(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
