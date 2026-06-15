"""
Tech Myth-Bust daily scraper — FREE STACK.

Pulls high-reach tech items from HN, Reddit, and RSS feeds, asks Gemini Flash
(free tier) to (1) draft a myth-bust shortlist, then (2) re-verify each flagged
item via Google Search grounding before keeping the flag. Posts the result to a
Google Apps Script web app, which appends rows to a Google Sheet.

Costs: $0. Runs on GitHub Actions free tier, uses Gemini free tier, writes to
a free Google Sheet.

Env vars required:
    GEMINI_API_KEY      - free key from https://aistudio.google.com/apikey
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
import google.generativeai as genai

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

# gemini-2.0-flash has a generous free tier and supports Google Search grounding.
MODEL = "gemini-2.0-flash"
MAX_RAW_ITEMS = 40
MAX_FLAGGED = 8

RSS_FEEDS = [
    ("TechCrunch", "https://techcrunch.com/feed/"),
    ("The Verge", "https://www.theverge.com/rss/index.xml"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
    ("Wired", "https://www.wired.com/feed/rss"),
    ("Engadget", "https://www.engadget.com/rss.xml"),
    ("MIT Tech Review", "https://www.technologyreview.com/feed/"),
]

REDDIT_SUBS = ["technology", "gadgets", "Futurology"]

# -----------------------------------------------------------------------------
# Source pullers
# -----------------------------------------------------------------------------

UA = {"User-Agent": "tech-myth-bust-scraper/1.0 (github actions)"}


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def pull_hn(limit: int = 15) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        r = requests.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json",
            timeout=20, headers=UA,
        )
        r.raise_for_status()
        ids = r.json()[: limit * 2]
        for sid in ids:
            if len(out) >= limit:
                break
            item = requests.get(
                f"https://hacker-news.firebaseio.com/v0/item/{sid}.json",
                timeout=15, headers=UA,
            ).json() or {}
            if item.get("score", 0) < 150:
                continue
            url = item.get("url") or f"https://news.ycombinator.com/item?id={sid}"
            out.append({
                "title": item.get("title", "").strip(),
                "url": url,
                "source": f"Hacker News ({item.get('score')} pts)",
                "summary": "",
                "reach_signal": f"HN front page, {item.get('score')} points, {item.get('descendants',0)} comments",
            })
    except Exception as e:
        print(f"[hn] error: {e}", file=sys.stderr)
    return out


def pull_reddit(subs: list[str], limit_per_sub: int = 8) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sub in subs:
        try:
            r = requests.get(
                f"https://www.reddit.com/r/{sub}/top.json?t=day&limit={limit_per_sub}",
                timeout=20, headers=UA,
            )
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
                if pub:
                    pub_dt = dt.datetime(*pub[:6])
                    if pub_dt < cutoff:
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


# -----------------------------------------------------------------------------
# Gemini passes
# -----------------------------------------------------------------------------

FLAG_PROMPT = """You curate tech news for an Instagram myth-busting channel.

From the candidate items below, pick which ones contain HYPE / EXAGGERATED / MISLEADING claims worth debunking publicly.

Flag-worthy signals:
- "world's first", "10x faster", "human-level", "cures X", "replaces [profession]"
- specific quantitative claims without proper benchmarks
- viral demos that may be cherry-picked or staged
- marketing-only specs (peak vs sustained, lab vs real-world)
- studies framed as conclusive when they're preliminary

NOT flag-worthy: routine product launches, price drops, executive shuffles, earnings, normal feature updates, anything without a specific claim that could be checked.

Return STRICT JSON only, no prose, no code fences. Schema:
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

Pick at most 8 items. Skip if nothing is bust-worthy.

Candidates:
"""

VERIFY_PROMPT = """You re-verify whether a tech claim is genuinely worth myth-busting.

Use Google Search to look up:
1. The original source - is the claim actually being made, or is it a headline distortion?
2. Counter-evidence, expert pushback, fact-checks, benchmark scrutiny
3. Whether the claim is specific and checkable, or vague marketing

Then decide:
- KEEP: claim is specific, contains a likely-exaggerated assertion, there is signal it is wrong or unverified. Worth a video.
- DROP: claim is accurate as stated, too vague to bust, or already widely understood. Not worth a video.

Return STRICT JSON only, no prose, no code fences. Schema:
{
  "verdict": "KEEP" | "DROP",
  "reason": "<1-2 sentences citing what you found>",
  "refined_myth_angle": "<if KEEP, a sharper bust angle; if DROP, empty string>"
}

Item:
"""


def _parse_json(text: str) -> dict | None:
    text = text.strip()
    text = re.sub(r"^```(?:json)?", "", text)
    text = re.sub(r"```$", "", text).strip()
    # grab the largest {...} block
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        text = m.group(0)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def gemini_flag_pass(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload = [
        {"i": i, "title": it["title"], "source": it["source"], "summary": it["summary"][:300]}
        for i, it in enumerate(items)
    ]
    model = genai.GenerativeModel(MODEL)
    resp = model.generate_content(
        FLAG_PROMPT + json.dumps(payload, ensure_ascii=False),
        generation_config={"response_mime_type": "application/json", "temperature": 0.2},
    )
    data = _parse_json(resp.text or "")
    if not data:
        print(f"[flag] could not parse JSON: {(resp.text or '')[:300]}", file=sys.stderr)
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


def gemini_verify(item: dict[str, Any]) -> dict[str, Any]:
    prompt = VERIFY_PROMPT + (
        f"Title: {item['title']}\n"
        f"Source: {item['source']}\n"
        f"URL: {item['url']}\n"
        f"Claim: {item['claim']}\n"
        f"Initial myth angle: {item['myth_angle']}\n"
    )
    # Use Google Search grounding (free with gemini-2.0-flash).
    model = genai.GenerativeModel(
        MODEL,
        tools=[{"google_search": {}}],
    )
    try:
        resp = model.generate_content(
            prompt,
            generation_config={"temperature": 0.2},
        )
    except Exception as e:
        print(f"[verify] api error: {e}", file=sys.stderr)
        return {"verdict": "DROP", "reason": f"verification failed: {e}", "refined_myth_angle": ""}

    data = _parse_json(resp.text or "")
    if not data:
        print(f"[verify] could not parse JSON: {(resp.text or '')[:300]}", file=sys.stderr)
        return {"verdict": "DROP", "reason": "could not parse verification response", "refined_myth_angle": ""}
    return data


# -----------------------------------------------------------------------------
# Sheet write
# -----------------------------------------------------------------------------

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


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main() -> int:
    today = dt.date.today().isoformat()
    print(f"[run] {today}")

    genai.configure(api_key=os.environ["GEMINI_API_KEY"])

    raw = pull_hn() + pull_reddit(REDDIT_SUBS) + pull_rss(RSS_FEEDS)
    raw = dedupe(raw)[:MAX_RAW_ITEMS]
    print(f"[raw] {len(raw)} items after dedupe")
    if not raw:
        print("no items; exiting")
        return 0

    flagged = gemini_flag_pass(raw)
    print(f"[flag] {len(flagged)} candidates")

    verified_keep: list[dict[str, Any]] = []
    verified_drop: list[dict[str, Any]] = []
    for item in flagged:
        v = gemini_verify(item)
        item["verify_verdict"] = v.get("verdict", "DROP")
        item["verify_reason"] = v.get("reason", "")
        if v.get("verdict") == "KEEP":
            if v.get("refined_myth_angle"):
                item["myth_angle"] = v["refined_myth_angle"]
            verified_keep.append(item)
        else:
            verified_drop.append(item)
        time.sleep(1.5)  # stay under free-tier RPM

    print(f"[verify] kept {len(verified_keep)}, dropped {len(verified_drop)}")

    flagged_urls = {f["url"] for f in flagged}
    rows: list[dict[str, Any]] = []

    def make_row(it: dict[str, Any], flag_label: str) -> dict[str, Any]:
        return {
            "date": today,
            "flagged": flag_label,
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

    for it in verified_keep:
        rows.append(make_row(it, "YES"))
    for it in verified_drop:
        rows.append(make_row(it, "DROPPED"))
    for it in raw:
        if it["url"] in flagged_urls:
            continue
        rows.append(make_row(it, "NO"))

    post_to_sheet(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
