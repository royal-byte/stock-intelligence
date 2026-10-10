"""Pluggable data sources. Every fetcher returns a list of normalized posts:

[{ "id": str, "text": str, "author": str, "url": str, "published": str, "engagement": {...} }]
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.parse
import urllib.request

# Browser-bridge attach failures (e.g. "attach failed: Cannot access a
# chrome-extension:// URL of different extension") are profile-local: another
# connected browser instance usually works. These markers trigger profile fallback.
_ATTACH_ERR_MARKERS = ("attach failed", "chrome-extension", "profile")

# Production recovery target: launch Chrome by full path, never via `start chrome`
# (which Windows can redirect to the default browser — here Edge, whose Browser
# Bridge instance was unhealthy). Override with OPENCLI_CHROME_PATH if needed.
_CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
)
_HEALTH_TIMEOUT_S = 60
# Neutral health-check URL: browser/bridge capability must be judged
# independently of the business data source. If x.com is down, that is a
# DATA-SOURCE failure, not a browser failure — never conflste the two.
_DEFAULT_HEALTH_URL = "https://www.google.com/"
# Circuit breaker: after this many consecutive failed recovery attempts
# (launch Chrome + health check), stop relaunching entirely and fail fast.
# Prevents a data-source outage from escalating into a Chrome kill/start loop.
_MAX_RECOVERY_FAILURES = 2
# process-level cache: profile -> bool (True = navigate check passed).
# `profile list`/`bind` succeeding does NOT prove the browser is drivable —
# only a real navigation does — so health must be established once per run.
_profile_health_cache: dict[str, bool] = {}
_ignored_profiles_reported: set[str] = set()
_ensure_fail_streak = 0


def _load_browser_config() -> dict:
    """Skill-root browser.json: {"allow_profiles": [..]} — production instances only."""
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "browser.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


_BROWSER_CONFIG = _load_browser_config()


def _health_url() -> str:
    return _BROWSER_CONFIG.get("health_url") or _DEFAULT_HEALTH_URL


def _chrome_exe() -> str | None:
    """The one production browser executable (config pin > env > known paths).

    Chrome is not the 'highest priority' option — it is the ONLY allowed
    production browser; nothing else is ever launched.
    """
    exe = os.environ.get("OPENCLI_CHROME_PATH") or _BROWSER_CONFIG.get("chrome_path")
    if exe and os.path.exists(exe):
        return exe
    return next((p for p in _CHROME_CANDIDATES if os.path.exists(p)), None)


def _filter_allowed(profiles: list[str]) -> list[str]:
    """Keep only allow-listed profiles (browser.json allow_profiles, or
    OPENCLI_PROFILES env override, comma-separated). Empty allow-list = all.
    Retired/broken instances (e.g. Edge/vmawp5gu) are excluded here so they
    are never launched, health-checked, or retried again."""
    allow = os.environ.get("OPENCLI_PROFILES")
    if not allow:
        allow = _BROWSER_CONFIG.get("allow_profiles")
    if isinstance(allow, str):
        allow = [a.strip() for a in allow.split(",") if a.strip()]
    if not allow:
        return profiles
    return [p for p in profiles if p in allow]


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


def _opencli_all_connected(exe: str) -> list[str]:
    """ALL connected Browser Bridge profile names, unfiltered."""
    try:
        out = subprocess.run([exe, "profile", "list"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60)
        return re.findall(r"^\s*([A-Za-z0-9_-]+)(?:\s+default)?\s+—\s+connected", out.stdout, re.M)
    except Exception:
        return []


def _opencli_connected_profiles(exe: str) -> list[str]:
    """Connected Browser Bridge profiles, default-DENY filtered to the
    production allow-list (browser.json / OPENCLI_PROFILES)."""
    return _filter_allowed(_opencli_all_connected(exe))


def _opencli_default_profile(exe: str) -> str | None:
    try:
        out = subprocess.run([exe, "profile", "list"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60)
        m = re.search(r"^\s*([A-Za-z0-9_-]+)\s+default\s+—\s+connected", out.stdout, re.M)
        return m.group(1) if m else None
    except Exception:
        return None


def _profile_health(exe: str, profiles: list[str], force: bool = False) -> tuple[list[str], list[str]]:
    """Split profiles into (healthy, unhealthy) via a real navigation test.

    A profile is healthy only if `opencli --profile P browser open <url>`
    actually navigates; profile list / tab list / bind succeeding is not proof.
    Results are cached for the process lifetime.
    """
    nav_url = _health_url()
    healthy: list[str] = []
    unhealthy: list[str] = []
    for p in profiles:
        if not force and p in _profile_health_cache:
            (healthy if _profile_health_cache[p] else unhealthy).append(p)
            continue
        try:
            r = subprocess.run(
                [exe, "--profile", p, "browser", "ohealth", "open", nav_url, "--window", "background"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=_HEALTH_TIMEOUT_S,
            )
            ok = r.returncode == 0
        except Exception:
            ok = False
        _profile_health_cache[p] = ok
        (healthy if ok else unhealthy).append(p)
    return healthy, unhealthy


def _launch_chrome(url: str | None = None) -> bool:
    """Launch THE production Chrome by absolute path (detached). Returns False
    if it cannot be resolved — no other browser is ever launched."""
    exe = _chrome_exe()
    if not exe:
        return False
    try:
        flags = subprocess.DETACHED_PROCESS if os.name == "nt" else 0
        subprocess.Popen([exe] if url is None else [exe, url], creationflags=flags, close_fds=True)
        return True
    except Exception:
        return False


def _ensure_opencli_browser(exe: str, wait_s: int = 45) -> tuple[list[str], list[str]]:
    """Guarantee at least one *healthy* whitelisted profile; return (healthy, unhealthy).

    Recovery ladder (bounded — no infinite relaunch loop):
      1. If a whitelisted connected profile passes the navigate health check, use it.
      2. Otherwise launch THE production Chrome once (absolute path) and poll
         `profile list` until its extension reconnects.
      3. Retry the health check once. Still nothing healthy -> count one
         recovery failure; after _MAX_RECOVERY_FAILURES consecutive failures
         the circuit opens and further calls fail fast instead of relaunching.
    Non-whitelisted profiles (e.g. retired Edge/vmawp5gu) are ignored and
    reported once, never health-checked or retried.
    """
    global _ensure_fail_streak
    if _ensure_fail_streak >= _MAX_RECOVERY_FAILURES:
        raise RuntimeError(
            "Browser recovery circuit OPEN "
            f"({_ensure_fail_streak} consecutive recovery failures). Not relaunching Chrome again.\n"
            "Check manually: Chrome running? Browser Bridge extension connected? "
            "`opencli profile list` shows the production profile? Then rerun."
        )
    healthy: list[str] = []
    unhealthy: list[str] = []
    for attempt in (1, 2):
        raw = _opencli_all_connected(exe)
        allowed = _filter_allowed(raw)
        new_ignored = [p for p in raw if p not in allowed and p not in _ignored_profiles_reported]
        if new_ignored:
            _ignored_profiles_reported.update(new_ignored)
            print(f"[browser] non-whitelisted profile(s) ignored: {', '.join(new_ignored)}")
        if allowed:
            healthy, unhealthy = _profile_health(exe, allowed, force=(attempt > 1))
            if healthy:
                _ensure_fail_streak = 0
                return healthy, unhealthy
        if attempt == 1 and _launch_chrome():
            deadline = time.time() + wait_s
            while time.time() < deadline:
                time.sleep(2)
                if _filter_allowed(_opencli_all_connected(exe)):
                    break
    _ensure_fail_streak += 1
    return healthy, unhealthy


def _run_opencli(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    """Run an opencli command with browser-health-aware profile fallback.

    args[0] must be the opencli executable path. Ensures a healthy browser
    first (launching Chrome if needed), then tries the default command and —
    only on attach-type failures — retries with healthy profiles in a
    background window. Unhealthy profiles are skipped and reported, never
    retried, so a broken instance can't masquerade as transient flakiness.
    """
    healthy, unhealthy = _ensure_opencli_browser(args[0])
    if not healthy:
        raise RuntimeError(
            "Browser health check failed — no healthy whitelisted Browser Bridge profile. "
            f"Unhealthy: {', '.join(unhealthy) or '(none)'}. "
            "Check Chrome and its Browser Bridge extension."
        )
    if unhealthy:
        print(f"[browser] unhealthy profile(s) skipped (navigate check failed): {', '.join(unhealthy)}")
    print(f"[browser] healthy profile(s): {', '.join(healthy)}")
    # Chrome-only: the profileless default attempt must target a healthy,
    # whitelisted profile — otherwise it silently hits opencli's default
    # (the retired Edge). Skip unless the default profile is proven healthy.
    default_p = _opencli_default_profile(args[0])
    attempts = [args] if default_p in healthy else []
    for p in healthy:
        attempts.append([args[0], "--profile", p] + args[1:] + ["--window", "background"])
    last: subprocess.CompletedProcess | None = None
    for i, cmd in enumerate(attempts):
        last = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
        if last.returncode == 0:
            return last
        err = (last.stderr or "") + (last.stdout or "")
        if i + 1 < len(attempts) and any(m in err for m in _ATTACH_ERR_MARKERS):
            continue
        break
    return last


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
    out = _run_opencli(
        [_which("opencli"), "twitter", "search", query, "-f", "json", "--limit", str(n), "--product", product],
        timeout=180,
    )
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
            out = _run_opencli([exe, "twitter", "profile", a, "-f", "json"], timeout=90)
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
