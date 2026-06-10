"""
DoodStream / playmogo direct video URL extractor.

Proxies are rotated automatically from the built-in residential pool.
Add more proxies or override via --proxy / DOOD_PROXY env var.

Usage:
    python dood_extract.py https://dood.watch/e/VIDEO_ID
    python dood_extract.py VIDEO_ID --proxy http://user:pass@host:port
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
# Residential proxy pool (Webshare)
# Format: host:port:user:pass

_RAW_PROXIES = [
    "38.154.203.95:5863:klgcswuo:zajxdew027s2",
    "198.105.121.200:6462:klgcswuo:zajxdew027s2",
    "64.137.96.74:6641:klgcswuo:zajxdew027s2",
    "209.127.138.10:5784:klgcswuo:zajxdew027s2",
    "38.154.185.97:6370:klgcswuo:zajxdew027s2",
    "84.247.60.125:6095:klgcswuo:zajxdew027s2",
    "142.111.67.146:5611:klgcswuo:zajxdew027s2",
    "191.96.254.138:6185:klgcswuo:zajxdew027s2",
    "31.58.9.4:6077:klgcswuo:zajxdew027s2",
    "104.239.107.47:5699:klgcswuo:zajxdew027s2",
]

def _parse_proxy_pool(raw: list[str]) -> list[dict]:
    """Parse host:port:user:pass lines into requests proxies dicts."""
    pool = []
    for line in raw:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) == 4:
            host, port, user, pwd = parts
            url = f"http://{user}:{pwd}@{host}:{port}"
            pool.append({"http": url, "https": url})
        elif len(parts) == 2:
            # host:port with no auth
            url = f"http://{parts[0]}:{parts[1]}"
            pool.append({"http": url, "https": url})
    return pool

PROXY_POOL = _parse_proxy_pool(_RAW_PROXIES)


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
    "doodstream.com",
    "doodstream.co",
    "dooood.com",
    "dood.yt",
    "dood.stream",
    "doodapi.com",
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
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
    "sec-ch-ua": '"Google Chrome";v="124", "Not.A/Brand";v="8", "Chromium";v="124"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
    "DNT": "1",
}

# All known regex patterns for the pass_md5 JS call across DoodStream versions
PASS_MD5_PATTERNS = [
    r"""\$\.get\s*\(\s*['"](/pass_md5/[^'"]+)['"]\s*,""",
    r"""\.get\(['"](/pass_md5/[^'"]+)['"]\,""",
    r"""fetch\s*\(\s*['"]([^'"]*pass_md5[^'"]+)['"]\s*\)""",
    r"""['"](/pass_md5/[A-Za-z0-9/]+)['"]""",
]


# ---------------------------------------------------------------------------
# Proxy helpers

def _get_proxy(proxy_arg: str | None = None) -> dict | None:
    """
    Resolve a single override proxy from CLI arg or env vars.
    Returns None to fall back to the built-in pool rotation.
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


def _mask(proxies: dict) -> str:
    """Return a log-safe proxy string with password masked."""
    url = list(proxies.values())[0]
    return re.sub(r":[^@:]+@", ":***@", url)


def _check_proxy(proxies: dict, timeout: int = 15) -> str | None:
    """Verify proxy works; return exit IP string or None on failure."""
    try:
        r = requests.get(
            "https://httpbin.org/ip",
            proxies=proxies,
            headers={"User-Agent": UA},
            timeout=timeout,
        )
        if r.status_code == 200:
            return r.json().get("origin", "unknown")
    except Exception:
        pass
    return None


def _pick_working_proxy(
    pool: list[dict],
    override: dict | None,
    verbose: bool = True,
) -> dict | None:
    """
    Return a working proxy dict.

    Priority:
      1. override (--proxy / env var) — used as-is, no pool fallback
      2. shuffle the built-in pool and return the first one that passes
         a connectivity check
      3. None (direct connection)
    """
    if override:
        ip = _check_proxy(override)
        if ip:
            if verbose:
                print(f"[proxy] override OK  {_mask(override)}  exit-ip={ip}", file=sys.stderr)
            return override
        print(
            f"[proxy] WARNING override proxy failed: {_mask(override)} — "
            "falling back to pool",
            file=sys.stderr,
        )

    if pool:
        candidates = pool[:]
        random.shuffle(candidates)
        for p in candidates:
            if verbose:
                print(f"[proxy] trying {_mask(p)} ...", file=sys.stderr)
            ip = _check_proxy(p, timeout=12)
            if ip:
                if verbose:
                    print(f"[proxy] OK  exit-ip={ip}", file=sys.stderr)
                return p
            if verbose:
                print(f"[proxy] dead, next ...", file=sys.stderr)
        print("[proxy] WARNING: all pool proxies failed — trying direct", file=sys.stderr)

    print("[proxy] no proxy (direct connection)", file=sys.stderr)
    return None


# ---------------------------------------------------------------------------
# Core helpers

def _video_id(s: str) -> str:
    s = s.strip()
    m = re.search(r"/[ed]/([A-Za-z0-9]+)", s)
    return m.group(1) if m else s


def _make_play(token: str) -> str:
    rnd = "".join(random.choices(string.ascii_letters + string.digits, k=10))
    return f"{rnd}?token={token}&expiry={int(time.time() * 1000)}"


def _extract_pass_md5_path(html: str) -> str | None:
    for pat in PASS_MD5_PATTERNS:
        m = re.search(pat, html)
        if m:
            path = m.group(1)
            if not path.startswith("/pass_md5/"):
                path = "/" + path.lstrip("/")
            return path
    return None


def _build_session(use_cloudscraper: bool, proxies: dict | None) -> requests.Session:
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


def _classify_response(r: requests.Response) -> str:
    if r.status_code == 403:
        if "allowlist" in r.text.lower():
            return "403 CF/IP block (proxy needed)"
        return "403 Forbidden"
    if r.status_code != 200:
        return f"HTTP {r.status_code}"
    if not r.text:
        return "empty body"
    tl = r.text.lower()
    if "video not found" in tl or "file not found" in tl:
        return "video not found / dead id"
    if "just a moment" in tl or "checking your browser" in tl:
        return "CF JS challenge (try cloudscraper)"
    return "no pass_md5 in response"


def _try_mirror(
    session: requests.Session,
    mirror: str,
    vid: str,
) -> tuple[str, str] | None:
    url = f"https://{mirror}/e/{vid}"
    try:
        r = session.get(url, timeout=20, allow_redirects=True)
    except requests.RequestException as e:
        print(f"  [{mirror}] {e.__class__.__name__}", file=sys.stderr)
        return None

    if r.status_code == 200 and _extract_pass_md5_path(r.text):
        print(f"  [{mirror}] OK -> {r.url}", file=sys.stderr)
        return r.url, r.text

    print(f"  [{mirror}] {_classify_response(r)}", file=sys.stderr)
    return None


def _load_player(
    vid: str,
    mirrors: list[str],
    proxies: dict | None,
) -> tuple[requests.Session, str, str]:
    """Try mirrors with requests then cloudscraper. Returns (session, url, html)."""
    last = None
    for engine in ("requests", "cloudscraper"):
        if engine == "cloudscraper" and cloudscraper is None:
            print("[engine] cloudscraper not installed, skipping", file=sys.stderr)
            continue
        print(f"[engine] {engine}", file=sys.stderr)
        session = _build_session(engine == "cloudscraper", proxies)
        for mirror in mirrors:
            last = mirror
            hit = _try_mirror(session, mirror, vid)
            if hit:
                return session, hit[0], hit[1]

    raise RuntimeError(
        f"No mirror served the player for id={vid!r} (last tried: {last}).\n"
        "All proxies in the pool may be exhausted or the video id is dead."
    )


# ---------------------------------------------------------------------------
# Public API

def extract_dood(
    url_or_id: str,
    proxy: str | None = None,
    verbose: bool = True,
) -> dict:
    """
    Extract a direct MP4 URL from a DoodStream / playmogo embed.

    Args:
        url_or_id : Full embed URL or bare video id.
        proxy     : Optional single proxy override (http://user:pass@host:port).
                    If None, the built-in residential pool is used automatically.
        verbose   : Print progress to stderr.

    Returns dict with keys:
        input, video_id, mirror, player_page, pass_md5, token,
        direct_mp4, is_m3u8, container, required_headers, probe
    """
    override = _get_proxy(proxy)
    proxies = _pick_working_proxy(PROXY_POOL, override, verbose=verbose)

    vid = _video_id(url_or_id)
    session, player_url, html = _load_player(vid, MIRRORS, proxies)

    parsed = urlparse(player_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    pass_md5_path = _extract_pass_md5_path(html)
    if not pass_md5_path:
        raise RuntimeError(
            "pass_md5 endpoint not found in player HTML. "
            "DoodStream may have changed their embed format."
        )

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
        raise RuntimeError(
            f"pass_md5 returned non-URL body: {body!r}\n"
            "Token may have expired or the video id is dead."
        )

    direct = body + _make_play(token)

    # HEAD probe the CDN URL
    probe: dict = {
        "ok": None, "status": None, "content_type": None, "content_length": None,
    }
    try:
        h = session.head(
            direct,
            headers={"Referer": player_url, "User-Agent": UA},
            allow_redirects=True,
            timeout=15,
        )
        probe.update({
            "status": h.status_code,
            "content_type": h.headers.get("Content-Type"),
            "content_length": h.headers.get("Content-Length"),
            "ok": h.status_code < 400,
        })
    except requests.RequestException as e:
        probe.update({"ok": False, "status": f"err:{e.__class__.__name__}"})

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
    print(f"\nInput        : {info['input']}")
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
    p = info["probe"]
    print(
        f"\nProbe        : status={p['status']}  "
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
        description="Extract a direct MP4 URL from DoodStream / playmogo.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
The script automatically rotates through the built-in residential proxy pool.
To override with your own proxy:
  --proxy http://user:pass@host:port
  --proxy socks5://user:pass@host:port
  export DOOD_PROXY=http://user:pass@host:port
        """,
    )
    parser.add_argument(
        "url_or_id",
        nargs="?",
        default="https://dood.watch/e/qjw686j2lvvs",
        help="Full embed URL or bare video id",
    )
    parser.add_argument(
        "--proxy",
        default=None,
        metavar="URL",
        help="Single proxy override (skips pool rotation)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    try:
        _print_report(extract_dood(args.url_or_id, proxy=args.proxy))
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)
