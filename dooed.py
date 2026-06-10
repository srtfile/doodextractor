"""
DoodStream / playmogo direct video URL extractor.

Reverse-engineered from captures/20260524_223329/ for
    https://dood.watch/e/fihq8fpmmvwo

Flow (verified in full_capture.jsonl):
  1. /e/<id> on a live mirror returns the player HTML.
  2. The HTML contains:
        $.get('/pass_md5/<hash>/<token>', function(data){
            dsplayer.src({ type: "video/mp4", src: data + makePlay() });
        });
        function makePlay(){
            // 10 random A-Za-z0-9 + "?token=<token>&expiry=" + Date.now()
        }
  3. GET /pass_md5/<hash>/<token>  with  Referer: <player URL>
        -> body is plain text:  https://<cdn>/<...>~
  4. final mp4 = body + makePlay()
        Must be fetched with the same Referer + UA as the player page.

There is no .m3u8 / .mpd for this provider - the stream is progressive video/mp4.

The front door (dood.watch / dood.so / d000d.com / ...) sits behind
Cloudflare. Plain `requests` gets a 403. We try, in order:

  a) regular requests with full browser headers, against a list of known
     active mirrors;
  b) cloudscraper (solves the basic CF JS challenge) against the same list.

Usage:
    python dood_extract.py [embed_or_id]
"""

from __future__ import annotations

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
except Exception:  # pragma: no cover
    cloudscraper = None


# ---------------------------------------------------------------------------
# Config

# Order matters: first one that returns 200 wins. Add/remove as mirrors die.
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
    "playmogo.com",   # the one your capture redirected to
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
# Helpers

def _video_id(s: str) -> str:
    """Accept either a full URL or just the file id."""
    s = s.strip()
    m = re.search(r"/[ed]/([A-Za-z0-9]+)", s)
    return m.group(1) if m else s


def _make_play(token: str) -> str:
    rnd = "".join(random.choices(string.ascii_letters + string.digits, k=10))
    return f"{rnd}?token={token}&expiry={int(time.time() * 1000)}"


def _build_session(use_cloudscraper: bool):
    if use_cloudscraper and cloudscraper is not None:
        s = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
    else:
        s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    return s


def _try_mirror(session, mirror: str, vid: str) -> tuple[str, str] | None:
    """Return (final_url, html) if /e/<id> on this mirror serves the player."""
    url = f"https://{mirror}/e/{vid}"
    try:
        r = session.get(url, timeout=20, allow_redirects=True)
    except requests.RequestException:
        return None
    if r.status_code != 200 or not r.text:
        return None
    # Player page must contain the pass_md5 call. Anything else is a
    # placeholder / error / "video not found" page.
    if "/pass_md5/" not in r.text:
        return None
    return r.url, r.text


def _load_player(vid: str, mirrors: Iterable[str]) -> tuple[object, str, str]:
    """Walk mirrors with plain requests then cloudscraper. Return (session, final_url, html)."""
    last_err = None
    for engine in ("requests", "cloudscraper"):
        if engine == "cloudscraper" and cloudscraper is None:
            continue
        session = _build_session(engine == "cloudscraper")
        for m in mirrors:
            hit = _try_mirror(session, m, vid)
            if hit:
                final_url, html = hit
                return session, final_url, html
            last_err = m
    raise RuntimeError(
        f"No mirror served the player for id={vid!r}. Last tried: {last_err}. "
        f"The id may be dead or all mirrors are blocked from this IP."
    )


# ---------------------------------------------------------------------------
# Public API

def extract_dood(url_or_id: str) -> dict:
    vid = _video_id(url_or_id)
    session, player_url, html = _load_player(vid, MIRRORS)

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

    # Optional sanity probe: HEAD the CDN URL so the caller knows it actually
    # streams. Some providers refuse HEAD; treat anything other than 4xx/5xx
    # auth as success.
    probe = {"ok": None, "status": None, "content_type": None, "content_length": None}
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
    print(f"Probe        : status={p['status']}  "
          f"type={p['content_type']}  length={p['content_length']}  ok={p['ok']}")
    print()
    print("Example one-liners:")
    print(f'  ffmpeg -headers "Referer: {info["player_page"]}\\r\\n" '
          f'-user_agent "{UA}" -i "{info["direct_mp4"]}" -c copy out.mp4')
    print(f'  yt-dlp --referer "{info["player_page"]}" '
          f'--user-agent "{UA}" "{info["direct_mp4"]}"')


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "https://dood.watch/e/qjw686j2lvvs"
    try:
        _print_report(extract_dood(target))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
