"""Pluggable data sources. Every fetcher returns a list of normalized posts:

[{ "id": str, "text": str, "author": str, "url": str, "published": str, "engagement": {...} }]
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.parse
import urllib.request


def _which(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"{name} not found on PATH")
    return path


def fetch_exa(ticker: str, n: int = 6) -> list[dict]:
    """Exa web search over finance commentary (zero-config fallback)."""
    query = f"{ticker} stock bullish bearish outlook what traders say"
    cmd = [
        _which("mcporter"), "call", "exa.web_search_exa",
        f"query={query}", f"numResults={n}",
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    if out.returncode != 0:
        raise RuntimeError(f"exa search failed: {out.stderr[:500]}")
    posts = []
    for i, block in enumerate(_split_blocks(out.stdout)):
        title, url, published, body = _parse_exa_block(block)
        if not body:
            continue
        posts.append({
            "id": f"exa-{i}",
            "text": (title + "\n\n" + body)[:6000],
            "author": _domain(url),
            "url": url,
            "published": published,
            "engagement": {},
        })
    return posts


def fetch_opencli(ticker: str, n: int = 15, product: str = "top", extra: str = "", company: str = "") -> list[dict]:
    """Real X posts via OpenCLI (browser session, no cookies needed).

    product: X search tab — 'top' (hot, engagement-biased) or 'live' (latest, chronological).
    extra: raw X search operators appended to the query, e.g.
           'since:2026-09-19 lang:en min_faves:20 -filter:replies'
    """
    query = f"${ticker}"
    if company:
        query = f'{query} OR "{company}"'
    if extra:
        query = f"({query}) {extra}"
    cmd = [_which("opencli"), "twitter", "search", query, "-f", "json", "--limit", str(n), "--product", product]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    if out.returncode != 0:
        raise RuntimeError(
            f"opencli twitter search failed: {out.stderr[:500]}\n"
            "Is the OpenCLI Chrome extension installed and connected? Run `opencli doctor`."
        )
    raw = json.loads(out.stdout)
    items = raw if isinstance(raw, list) else raw.get("tweets") or raw.get("results") or []
    posts = []
    for i, t in enumerate(items):
        text = t.get("text") or ""
        if not text:
            continue
        posts.append({
            "id": t.get("id") or f"x-{i}",
            "text": text,
            "author": t.get("author") or "",
            "bio": t.get("bio") or "",
            "url": t.get("url") or f"https://x.com/i/status/{t.get('id', '')}",
            "published": t.get("created_at") or t.get("createdAt") or "",
            "engagement": {
                k: t.get(k) for k in ("likes", "replies", "retweets", "views") if t.get(k) is not None
            },
        })
    return posts


def fetch_author_stats(authors: list[str], max_n: int = 12, delay_s: float = 1.5) -> dict[str, dict]:
    """Per-author profile stats (followers/verified/account age/bio) via OpenCLI.

    Rate-limited on purpose: X profiles are sensitive to rapid scraping. Returns
    {username: {followers, verified, created_at, bio}} for the authors it could fetch.
    """
    exe = _which("opencli")
    stats: dict[str, dict] = {}
    for a in authors[:max_n]:
        try:
            out = subprocess.run(
                [exe, "twitter", "profile", a, "-f", "json"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
            )
            if out.returncode == 0:
                data = json.loads(out.stdout)
                item = data[0] if isinstance(data, list) else data
                if item:
                    stats[a] = {k: item.get(k) for k in ("followers", "verified", "created_at", "bio")}
        except Exception:
            pass
        time.sleep(delay_s)
    return stats


def load_paste(path: str) -> list[dict]:
    """Posts pasted by the user, one per line (or --- separated)."""
    with open(path, encoding="utf-8") as f:
        content = f.read()
    chunks = [c.strip() for c in content.split("\n---\n") if c.strip()]
    return [
        {"id": f"paste-{i}", "text": c, "author": "", "url": "", "published": "", "engagement": {}}
        for i, c in enumerate(chunks)
    ]


# ---------- helpers ----------

def _split_blocks(stdout: str) -> list[str]:
    """Split mcporter output into result blocks; each block starts at a 'Title:' line."""
    blocks, cur = [], []
    for line in stdout.splitlines():
        if line.startswith("Title:"):
            if cur:
                blocks.append("\n".join(cur))
            cur = [line]
        elif cur:
            cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    return [b for b in blocks if "URL:" in b]


def _parse_exa_block(block: str) -> tuple[str, str, str, str]:
    title = url = published = ""
    lines = block.splitlines()
    body_start = 0
    for idx, line in enumerate(lines):
        if line.startswith("Title:") and not title:
            title = line[len("Title:"):].strip()
        elif line.startswith("URL:") and not url:
            url = line[len("URL:"):].strip()
            body_start = idx + 1
        elif line.startswith("Published:") and not published:
            published = line[len("Published:"):].strip()
    body = "\n".join(lines[body_start:])
    # strip common noise and mcporter's "..." separator lines
    for marker in ("Highlights:", "Author:"):
        if marker in body:
            body = body.split(marker)[0]
    body = "\n".join(ln for ln in body.splitlines() if ln.strip() not in ("...", ""))
    return title, url, published, body.strip()


def _domain(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc
    except Exception:
        return ""
