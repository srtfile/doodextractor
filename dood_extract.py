"""
DoodStream / playmogo direct video URL extractor.

Supports residential proxy routing to bypass Cloudflare/IP blocks
when running from GitHub Actions or other datacenter environments.

Proxy config (pick one):
  - Set env var DOOD_PROXY=http://user:pass@host:port   (HTTP/HTTPS proxy)
  - Set env var DOOD_PROXY=socks5://user:pass@host:port (SOCKS5 proxy)
  - Pass --proxy flag on CLI

Usage:
    python dood_extract.py [embed_or_id] [--proxy http://user:pass@host:port]
"""

from __future__ import annotations

import argparse
import os
import random
import re
import string
import sys
import time
from typing import Iterable
from urllib.parse import urlparse

import requests

try:
    import cloudscraper  # type: ignore
except ImportError:
    cloudscraper = None


# ---------------------------------------------------------------------------
# Config

MIRRORS = [
    "dood.watch",
    "dood.re",
    "dood.so",
    "dood.la",
    "dood.pm",
    "dood.ws",
    "dood.wf",
    "dood.to",
    "dood.cx",
    "dood.sh",
    "dood.li",
    "doods.pro",
    "ds2play.com",
    "ds2video.com",
    "d000d.com",
    "d0000d.com",
    "d-s.io",
    "vidply.com",
    "playmogo.com",
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

BROWSER_HEADERS = {
    "User-Agent": UA,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
    "DNT": "1",
}


# ---------------------------------------------------------------------------
# Proxy helpers

def _get_proxy(proxy_arg: str | None = None) -> dict | None:
    """
    Returns a requests-compatible proxies dict, or None if no proxy is set.

    Resolution order:
      1. --proxy CLI argument (passed in as proxy_arg)
      2. DOOD_PROXY environment variable
      3. HTTPS_PROXY / HTTP_PROXY environment variables (standard)
    """
    raw = (
        proxy_arg
        or os.environ.get("DOOD_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
    )
    if not raw:
        return None
    return {"http": raw, "https": raw}


def _check_proxy(proxies: dict) -> bool:
    """Quick connectivity check through the proxy. Returns True if reachable."""
    try:
        r = requests.get(
            "https://httpbin.org/ip",
            proxies=proxies,
            headers={"User-Agent": UA},
            timeout=15,
        )
        if r.status_code == 200:
            ip = r.json().get("origin", "unknown")
            print(f"[proxy] connected  exit-IP={ip}", file=sys.stderr)
            return True
        return False
    except Exception as e:
        print(f"[proxy] connectivity check failed: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Helpers

def _video_id(s: str) -> str:
    """Accept either a full URL or just the file id."""
    s = s.strip()
    m = re.search(r"/[ed]/([A-Za-z0-9]+)", s)
    return m.group(1) if m else s


def _make_play(token: str) -> str:
    rnd = "".join(random.choices(string.ascii_letters + string.digits, k=10))
    return f"{rnd}?token={token}&expiry={int(time.time() * 1000)}"


def _build_session(use_cloudscraper: bool, proxies: dict | None = None):
    """
    Build a requests/cloudscraper session.
    If proxies is provided it is applied to every request automatically.
    """
    if use_cloudscraper and cloudscraper is not None:
        s = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
    else:
        s = requests.Session()

    s.headers.update(BROWSER_HEADERS)

    if proxies:
        s.proxies.update(proxies)

    return s


def _try_mirror(session, mirror: str, vid: str) -> tuple[str, str] | None:
    """Return (final_url, html) if /e/<id> on this mirror serves the player."""
    url = f"https://{mirror}/e/{vid}"
    try:
        r = session.get(url, timeout=20, allow_redirects=True)
    except requests.RequestException as e:
        print(f"  [mirror] {mirror} -> request error: {e.__class__.__name__}", file=sys.stderr)
        return None

    if r.status_code == 403:
        print(f"  [mirror] {mirror} -> 403 Forbidden (CF block)", file=sys.stderr)
        return None
    if r.status_code != 200 or not r.text:
        print(f"  [mirror] {mirror} -> HTTP {r.status_code}", file=sys.stderr)
        return None
    if "/pass_md5/" not in r.text:
        print(f"  [mirror] {mirror} -> no pass_md5 in response (placeholder page)", file=sys.stderr)
        return None

    print(f"  [mirror] {mirror} -> OK", file=sys.stderr)
    return r.url, r.text


def _load_player(
    vid: str,
    mirrors: Iterable[str],
    proxies: dict | None = None,
) -> tuple[object, str, str]:
    """
    Walk mirrors with plain requests then cloudscraper.
    Returns (session, final_url, html).
    """
    mirrors = list(mirrors)
    last_tried = None

    for engine in ("requests", "cloudscraper"):
        if engine == "cloudscraper" and cloudscraper is None:
            print("[engine] cloudscraper not installed, skipping", file=sys.stderr)
            continue

        print(f"[engine] trying {engine} ...", file=sys.stderr)
        session = _build_session(engine == "cloudscraper", proxies)

        for mirror in mirrors:
            last_tried = mirror
            hit = _try_mirror(session, mirror, vid)
            if hit:
                final_url, html = hit
                return session, final_url, html

    raise RuntimeError(
        f"No mirror served the player for id={vid!r}. "
        f"Last tried: {last_tried}. "
        "The id may be dead, or all mirrors are blocked from this IP. "
        "Set the DOOD_PROXY env var to a residential proxy and retry."
    )


# ---------------------------------------------------------------------------
# Public API

def extract_dood(url_or_id: str, proxy: str | None = None) -> dict:
    """
    Extract a direct MP4 URL from a DoodStream / playmogo embed.

    Args:
        url_or_id: Full embed URL  or bare video id.
        proxy:     Optional proxy string, e.g. 'http://user:pass@host:port'.
                   If None, falls back to DOOD_PROXY / HTTPS_PROXY env vars.

    Returns:
        dict with keys: input, video_id, mirror, player_page, pass_md5,
                        token, direct_mp4, is_m3u8, container,
                        required_headers, probe
    """
    proxies = _get_proxy(proxy)

    if proxies:
        print(f"[proxy] using proxy: {list(proxies.values())[0]}", file=sys.stderr)
        _check_proxy(proxies)
    else:
        print("[proxy] no proxy configured (direct connection)", file=sys.stderr)

    vid = _video_id(url_or_id)
    session, player_url, html = _load_player(vid, MIRRORS, proxies)

    parsed = urlparse(player_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    m = re.search(r"\$\.get\(['\"](/pass_md5/[^'\"]+)['\"]", html)
    if not m:
        raise RuntimeError("pass_md5 endpoint not present in player HTML.")
    pass_md5_path = m.group(1)
    token = pass_md5_path.rstrip("/").rsplit("/", 1)[-1]

    r2 = session.get(
        base + pass_md5_path,
        headers={
            "Referer": player_url,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "*/*",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        },
        timeout=20,
    )
    r2.raise_for_status()
    body = r2.text.strip()

    if body == "RELOAD" or not body.startswith("http"):
        raise RuntimeError(f"pass_md5 returned non-URL body: {body!r}")

    direct = body + _make_play(token)

    # Optional sanity probe
    probe: dict = {"ok": None, "status": None, "content_type": None, "content_length": None}
    try:
        h = session.head(
            direct,
            headers={"Referer": player_url, "User-Agent": UA},
            allow_redirects=True,
            timeout=15,
        )
        probe["status"] = h.status_code
        probe["content_type"] = h.headers.get("Content-Type")
        probe["content_length"] = h.headers.get("Content-Length")
        probe["ok"] = h.status_code < 400
    except requests.RequestException as e:
        probe["ok"] = False
        probe["status"] = f"err: {e.__class__.__name__}"

    return {
        "input": url_or_id,
        "video_id": vid,
        "mirror": base,
        "player_page": player_url,
        "pass_md5": base + pass_md5_path,
        "token": token,
        "direct_mp4": direct,
        "is_m3u8": False,
        "container": "video/mp4 (progressive)",
        "required_headers": {"User-Agent": UA, "Referer": player_url},
        "probe": probe,
    }


# ---------------------------------------------------------------------------
# CLI

def _print_report(info: dict) -> None:
    print(f"Input        : {info['input']}")
    print(f"Video ID     : {info['video_id']}")
    print(f"Live mirror  : {info['mirror']}")
    print(f"Player page  : {info['player_page']}")
    print(f"pass_md5 URL : {info['pass_md5']}")
    print(f"Token        : {info['token']}")
    print()
    print("=== DIRECT STREAM URL ===")
    print(info["direct_mp4"])
    print()
    print("Required headers when fetching the stream:")
    for k, v in info["required_headers"].items():
        print(f"  {k}: {v}")
    print()
    p = info["probe"]
    print(
        f"Probe        : status={p['status']}  "
        f"type={p['content_type']}  length={p['content_length']}  ok={p['ok']}"
    )
    print()
    print("Example one-liners:")
    print(
        f'  ffmpeg -headers "Referer: {info["player_page"]}\\r\\n" '
        f'-user_agent "{UA}" -i "{info["direct_mp4"]}" -c copy out.mp4'
    )
    print(
        f'  yt-dlp --referer "{info["player_page"]}" '
        f'--user-agent "{UA}" "{info["direct_mp4"]}"'
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract a direct MP4 URL from a DoodStream / playmogo embed.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Proxy examples:
  --proxy http://user:pass@host:port
  --proxy socks5://user:pass@host:port

Environment variables (checked in order if --proxy not given):
  DOOD_PROXY    residential / SOCKS5 proxy for DoodStream specifically
  HTTPS_PROXY   standard HTTPS proxy
  HTTP_PROXY    standard HTTP proxy
        """,
    )
    parser.add_argument(
        "url_or_id",
        nargs="?",
        default="https://dood.watch/e/qjw686j2lvvs",
        help="Full embed URL or bare video id (default: demo id)",
    )
    parser.add_argument(
        "--proxy",
        default=None,
        help="Proxy URL (overrides env vars)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    try:
        _print_report(extract_dood(args.url_or_id, proxy=args.proxy))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
