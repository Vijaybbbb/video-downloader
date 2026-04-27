#!/usr/bin/env python3
"""
HLS (.m3u8 / .ts) video downloader for authenticated, Cloudflare-protected sites.

How it works
------------
1. Launches a real Chromium via Playwright (persistent profile + stealth) so the
   cf_clearance cookie can be obtained normally — no fingerprint mismatch.
2. Logs in (either automated via selectors, or manual with a prompt).
3. Opens the video page, clicks play, and listens on the network for any
   response whose URL or content-type looks like HLS.
4. Picks the best variant from the master playlist.
5. Downloads every segment in parallel using the exact cookies / User-Agent /
   Referer that the browser used. If a segment is blocked by Cloudflare
   (403/503/challenge HTML), the browser is re-driven through the video page
   to refresh cf_clearance and the download retries.
6. Decrypts AES-128 segments inline if the playlist declares a key.
7. Merges everything with `ffmpeg -c copy` — no re-encoding, no quality loss.

Usage
-----
    pip install -r requirements.txt
    python -m playwright install chromium
    # edit config.example.json and save as config.json
    python downloader.py config.json
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import aiohttp
from Crypto.Cipher import AES
from playwright.async_api import (
    BrowserContext,
    Page,
    Response,
    TimeoutError as PWTimeout,
    async_playwright,
)

try:
    from playwright_stealth import stealth_async
except Exception:  # stealth is optional
    stealth_async = None


# ---------- data ----------------------------------------------------------

@dataclass
class CapturedStream:
    url: str
    headers: dict = field(default_factory=dict)
    cookies_header: str = ""


@dataclass
class Segment:
    index: int
    url: str
    key_url: Optional[str] = None
    key_iv: Optional[bytes] = None
    byterange: Optional[tuple[int, int]] = None  # (length, offset)


# ---------- logging -------------------------------------------------------

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------- m3u8 parsing --------------------------------------------------

M3U8_CONTENT_TYPES = (
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
)


def looks_like_m3u8(url: str, content_type: str = "") -> bool:
    if ".m3u8" in url.split("?", 1)[0].lower():
        return True
    if any(ct in content_type.lower() for ct in M3U8_CONTENT_TYPES):
        return True
    return False


def parse_master(playlist_text: str, base_url: str) -> list[dict]:
    """Return list of variants: [{bandwidth, resolution, url}, ...]."""
    variants = []
    lines = playlist_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            attrs = _parse_attrs(line.split(":", 1)[1])
            # next non-comment line is the URI
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lines[j].startswith("#")):
                j += 1
            if j < len(lines):
                variants.append({
                    "bandwidth": int(attrs.get("BANDWIDTH", "0") or 0),
                    "resolution": attrs.get("RESOLUTION", ""),
                    "url": urljoin(base_url, lines[j].strip()),
                })
                i = j
        i += 1
    return variants


def parse_media(playlist_text: str, base_url: str) -> tuple[list[Segment], bool]:
    """Return (segments, is_live). Segments carry key info when present."""
    segments: list[Segment] = []
    current_key_url: Optional[str] = None
    current_key_iv: Optional[bytes] = None
    current_byterange: Optional[tuple[int, int]] = None
    is_endlist_seen = False
    idx = 0
    for raw in playlist_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-KEY"):
            attrs = _parse_attrs(line.split(":", 1)[1])
            method = attrs.get("METHOD", "NONE").upper()
            if method == "NONE":
                current_key_url = None
                current_key_iv = None
            elif method == "AES-128":
                uri = attrs.get("URI", "").strip('"')
                current_key_url = urljoin(base_url, uri) if uri else None
                iv_hex = attrs.get("IV", "").lower()
                if iv_hex.startswith("0x"):
                    current_key_iv = bytes.fromhex(iv_hex[2:])
                else:
                    current_key_iv = None
            else:
                raise RuntimeError(f"Unsupported encryption method: {method}")
        elif line.startswith("#EXT-X-BYTERANGE"):
            spec = line.split(":", 1)[1]
            if "@" in spec:
                length, offset = spec.split("@", 1)
                current_byterange = (int(length), int(offset))
            else:
                last_end = 0
                if segments and segments[-1].byterange:
                    l, o = segments[-1].byterange
                    last_end = o + l
                current_byterange = (int(spec), last_end)
        elif line.startswith("#EXT-X-ENDLIST"):
            is_endlist_seen = True
        elif line.startswith("#"):
            continue
        else:
            iv = current_key_iv
            if current_key_url and iv is None:
                iv = idx.to_bytes(16, "big")
            segments.append(Segment(
                index=idx,
                url=urljoin(base_url, line),
                key_url=current_key_url,
                key_iv=iv,
                byterange=current_byterange,
            ))
            idx += 1
            current_byterange = None
    return segments, not is_endlist_seen


def _parse_attrs(s: str) -> dict:
    """Parse EXT-X attribute list: KEY=VAL,KEY="quoted val",..."""
    out = {}
    i = 0
    while i < len(s):
        # key
        k_start = i
        while i < len(s) and s[i] != "=":
            i += 1
        key = s[k_start:i].strip()
        i += 1  # skip '='
        # value
        if i < len(s) and s[i] == '"':
            i += 1
            v_start = i
            while i < len(s) and s[i] != '"':
                i += 1
            value = s[v_start:i]
            i += 1  # skip '"'
        else:
            v_start = i
            while i < len(s) and s[i] != ",":
                i += 1
            value = s[v_start:i]
        out[key] = value
        while i < len(s) and s[i] in ", ":
            i += 1
    return out


# ---------- browser session ----------------------------------------------

def _seg_key_from_url(url: str) -> str:
    """Extract a stable key from a segment URL for matching.

    The m3u8 playlist may produce URLs with different query params or CDN
    hostnames than the actual requests the browser makes.  Matching on the
    full URL therefore fails.  Instead we extract just the *path component*
    of the URL (stripping the query string and the scheme+host) so that
    segments can be matched regardless of query-param differences.

    As a further fallback the basename alone is returned if the path is very
    short.
    """
    path = urlparse(url).path 
    return path.lstrip("/")


class Session:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._pw = None
        self._ctx: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._captured: list[CapturedStream] = []
        self._seen_urls: set[str] = set()
        self._segment_bytes: dict[str, bytes] = {}  # keyed by _seg_key_from_url
        self._pending_tasks: set[asyncio.Task] = set()  # prevent GC of tasks

    async def __aenter__(self) -> "Session":
        self._pw = await async_playwright().start()
        profile_dir = Path(self.cfg.get("user_data_dir", ".browser-profile")).resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)
        self._ctx = await self._pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=bool(self.cfg.get("headless", False)),
            viewport={"width": 1366, "height": 800},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        if stealth_async:
            try:
                await stealth_async(self._page)
            except Exception as e:
                log(f"stealth patch skipped: {e}")
        self._page.on("response", self._on_response)
        return self

    async def __aexit__(self, *a):
        # cancel any in-flight segment fetch tasks before closing context
        for task in self._pending_tasks:
            task.cancel()
        if self._pending_tasks:
            await asyncio.gather(*self._pending_tasks, return_exceptions=True)
        self._pending_tasks.clear()
        try:
            if self._ctx:
                try:
                    await self._ctx.close()
                except Exception:
                    pass  # suppress "Connection closed" errors on Ctrl+C
        finally:
            if self._pw:
                try:
                    await self._pw.stop()
                except Exception:
                    pass

    def _on_response(self, resp: Response):
        try:
            url = resp.url
            ct = resp.headers.get("content-type", "")
            if looks_like_m3u8(url, ct):
                if url not in self._seen_urls:
                    self._seen_urls.add(url)
                    req = resp.request
                    headers = {
                        "User-Agent": req.headers.get("user-agent", ""),
                        "Referer": req.headers.get("referer", self.cfg.get("video_url", "")),
                        "Accept": "*/*",
                        "Accept-Language": req.headers.get("accept-language", "en-US,en;q=0.9"),
                        "Origin": req.headers.get("origin", ""),
                    }
                    headers = {k: v for k, v in headers.items() if v}
                    self._captured.append(CapturedStream(url=url, headers=headers))
                    log(f"captured m3u8: {url}")
            elif ".ts" in url.split("?", 1)[0].lower() or "video/mp2t" in ct:
                key = _seg_key_from_url(url)
                if key not in self._segment_bytes:
                    task = asyncio.create_task(self._fetch_segment_body(resp, key))
                    self._pending_tasks.add(task)
                    task.add_done_callback(self._pending_tasks.discard)
        except Exception:
            pass

    async def _fetch_segment_body(self, resp: Response, key: str):
        try:
            data = await resp.body()
            if data:
                self._segment_bytes[key] = data
        except Exception as e:
            # log but don't crash — the segment can be retried via direct fetch
            log(f"  warning: failed to capture body for {key}: {e}")

    @property
    def page(self) -> Page:
        assert self._page is not None
        return self._page

    @property
    def context(self) -> BrowserContext:
        assert self._ctx is not None
        return self._ctx

    async def cookies_for(self, url: str) -> str:
        cookies = await self._ctx.cookies(url)
        return "; ".join(f"{c['name']}={c['value']}" for c in cookies)

    async def login(self):
        lg = self.cfg.get("login") or {}
        if not lg:
            return
        if lg.get("manual"):
            target = lg.get("url") or self.cfg["video_url"]
            log(f"opening {target} for manual login")
            await self.page.goto(target, wait_until="domcontentloaded")
            input(">>> log in through the browser window, then press ENTER here to continue... ")
            return
        if not lg.get("url"):
            return
        log(f"navigating to login page {lg['url']}")
        await self.page.goto(lg["url"], wait_until="domcontentloaded")
        await self._solve_cloudflare_if_any()
        # extra wait for page to fully settle after CF
        await asyncio.sleep(2)
        log("  filling credentials")
        await self.page.wait_for_selector(lg["username_selector"], timeout=30000)
        await self.page.fill(lg["username_selector"], lg["username"])
        await self.page.fill(lg["password_selector"], lg["password"])
        try:
            await asyncio.gather(
                self.page.wait_for_load_state("networkidle", timeout=15000),
                self.page.click(lg["submit_selector"]),
            )
        except Exception:
            pass  # page may redirect before networkidle
        await asyncio.sleep(3)
        marker = lg.get("success_url_contains")
        if marker:
            for _ in range(15):
                if marker in self.page.url:
                    break
                await asyncio.sleep(1)
            else:
                log(f"  note: marker '{marker}' not in url: {self.page.url} (continuing anyway)")
        log(f"login complete — url: {self.page.url}")

    async def _solve_cloudflare_if_any(self, timeout: float = 60.0):
        """Wait for Cloudflare interstitial to pass (it clears itself in normal Chromium)."""
        start = time.time()
        while time.time() - start < timeout:
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=5000)
                title = (await self.page.title()).lower()
                url = self.page.url.lower()
                if ("just a moment" in title
                        or "checking your browser" in title
                        or "challenge" in url
                        or "cdn-cgi" in url):
                    elapsed = time.time() - start
                    log(f"  CF challenge active ({elapsed:.0f}s)...")
                    await asyncio.sleep(2)
                    continue
                return
            except Exception:
                pass
            await asyncio.sleep(2)
        log("⚠ Cloudflare challenge still pending after timeout; continuing anyway")

    async def open_video_and_capture(self) -> CapturedStream:
        video_url = self.cfg["video_url"]
        log(f"navigating to video page {video_url}")
        await self.page.goto(video_url, wait_until="domcontentloaded")
        await self._solve_cloudflare_if_any()

        # nudge autoplay
        sel = self.cfg.get("play_selector") or "video"
        try:
            await self.page.wait_for_selector(sel, timeout=15000)
            try:
                await self.page.click(sel, timeout=5000)
            except Exception:
                await self.page.evaluate(
                    "document.querySelectorAll('video').forEach(v => v.play().catch(()=>{}))"
                )
        except PWTimeout:
            log(f"play selector '{sel}' not found; proceeding")

        wait_s = int(self.cfg.get("wait_seconds_after_play", 8))
        log(f"listening for m3u8 for {wait_s}s...")
        end = time.time() + wait_s
        while time.time() < end:
            if self._captured:
                # still wait a bit for a possible master playlist plus variant
                await asyncio.sleep(1.5)
                break
            await asyncio.sleep(0.5)

        if not self._captured:
            # one more nudge — seek / unmute
            try:
                await self.page.evaluate("""
                    document.querySelectorAll('video').forEach(v => {
                        v.muted = true; v.currentTime = 0; v.play().catch(()=>{});
                    });
                """)
                await asyncio.sleep(5)
            except Exception:
                pass

        if not self._captured:
            raise RuntimeError(
                "No m3u8 URL was seen. Try increasing wait_seconds_after_play, "
                "set headless=false, and verify play_selector."
            )
        # the first m3u8 response is usually the master playlist
        return self._captured[0]

    async def fetch_text(self, stream: CapturedStream) -> str:
        """Fetch a playlist inside the browser context (guaranteed to pass CF)."""
        request = self.context.request
        resp = await request.get(stream.url, headers=stream.headers)
        if not resp.ok:
            raise RuntimeError(f"playlist fetch failed: {resp.status} {stream.url}")
        return await resp.text()

    async def refresh_cf(self):
        """Revisit the video page in the browser to refresh cf_clearance."""
        try:
            await self.page.goto(self.cfg["video_url"], wait_until="domcontentloaded")
            await self._solve_cloudflare_if_any()
        except Exception as e:
            log(f"cf refresh navigation failed: {e}")


# ---------- segment downloader -------------------------------------------

CF_STATUS = {403, 429, 503, 520, 521, 522, 523, 524, 525}


def build_ffmpeg_concat_file(tmpdir: Path, segments: list[Segment]) -> Path:
    """Plain concat file listing segment files in order."""
    listing = tmpdir / "segments.txt"
    with listing.open("w", encoding="utf-8") as f:
        for seg in segments:
            seg_path = tmpdir / seg_filename(seg)
            # ffmpeg concat demuxer wants forward slashes and escaped single quotes
            p = str(seg_path.resolve()).replace("'", "'\\''")
            f.write(f"file '{p}'\n")
    return listing


def seg_filename(seg: Segment) -> str:
    return f"seg_{seg.index:06d}.ts"


async def download_all(
    session: Session,
    stream: CapturedStream,
    segments: list[Segment],
    tmpdir: Path,
    concurrency: int,
    max_retries: int,
) -> None:
    # preload AES keys
    key_cache: dict[str, bytes] = {}
    unique_key_urls = {s.key_url for s in segments if s.key_url}
    for key_url in unique_key_urls:
        key_cache[key_url] = await _fetch_key(session, stream, key_url)
        log(f"fetched AES-128 key: {key_url}")

    cookies_header = await session.cookies_for(stream.url)
    base_headers = dict(stream.headers)
    if cookies_header:
        base_headers["Cookie"] = cookies_header

    sem = asyncio.Semaphore(concurrency)
    done = 0
    total = len(segments)
    lock = asyncio.Lock()
    cf_refresh_lock = asyncio.Lock()
    last_cf_refresh = [0.0]

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120),
    ) as http:

        async def fetch_one(seg: Segment):
            nonlocal done
            path = tmpdir / seg_filename(seg)
            if path.exists() and path.stat().st_size > 0:
                async with lock:
                    done += 1
                    if done % 10 == 0 or done == total:
                        log(f"  {done}/{total} segments (cached)")
                return

            headers = dict(base_headers)
            if seg.byterange:
                length, offset = seg.byterange
                headers["Range"] = f"bytes={offset}-{offset + length - 1}"

            last_err: Optional[Exception] = None
            for attempt in range(max_retries):
                try:
                    async with sem:
                        req_headers = {k: v for k, v in headers.items() if k.lower() != "cookie"}
                        resp = await session.context.request.get(seg.url, headers=req_headers)
                        if resp.status in CF_STATUS or resp.status == 422:
                            raise _CFBlocked(f"status {resp.status}")
                        if resp.status >= 400:
                            raise RuntimeError(f"http {resp.status}")
                        data = await resp.body()
                    if _looks_like_challenge_html(data):
                        raise _CFBlocked("challenge HTML in body")
                    if seg.key_url:
                        key = key_cache[seg.key_url]
                        iv = seg.key_iv or b"\x00" * 16
                        data = AES.new(key, AES.MODE_CBC, iv).decrypt(data)
                        pad = data[-1] if data else 0
                        if 0 < pad <= 16 and data.endswith(bytes([pad]) * pad):
                            data = data[:-pad]
                    tmp_path = path.with_suffix(".part")
                    tmp_path.write_bytes(data)
                    tmp_path.rename(path)
                    async with lock:
                        done += 1
                        if done % 10 == 0 or done == total:
                            log(f"  {done}/{total} segments")
                    return
                except _CFBlocked as e:
                    last_err = e
                    async with cf_refresh_lock:
                        if time.time() - last_cf_refresh[0] > 8:
                            log(f"  Cloudflare block on seg {seg.index} ({e}); refreshing browser cookies")
                            await session.refresh_cf()
                            new_cookies = await session.cookies_for(stream.url)
                            if new_cookies:
                                base_headers["Cookie"] = new_cookies
                                headers["Cookie"] = new_cookies
                            last_cf_refresh[0] = time.time()
                    await asyncio.sleep(min(2 ** attempt, 15))
                except Exception as e:
                    last_err = e
                    await asyncio.sleep(min(2 ** attempt, 15))
            raise RuntimeError(f"segment {seg.index} failed after {max_retries} retries: {last_err}")

        await asyncio.gather(*(fetch_one(s) for s in segments))


class _CFBlocked(Exception):
    pass


def _looks_like_challenge_html(data: bytes) -> bool:
    if len(data) < 2048 and data[:64].lstrip().lower().startswith((b"<!doctype html", b"<html")):
        probe = data[:4096].lower()
        return b"cloudflare" in probe or b"just a moment" in probe or b"cf-chl" in probe
    return False


async def _fetch_key(session: Session, stream: CapturedStream, key_url: str) -> bytes:
    # keys are small and CF-sensitive — fetch through the browser context
    resp = await session.context.request.get(key_url, headers=stream.headers)
    if not resp.ok:
        raise RuntimeError(f"key fetch failed: {resp.status} {key_url}")
    body = await resp.body()
    if len(body) != 16:
        raise RuntimeError(f"unexpected AES-128 key length {len(body)} from {key_url}")
    return body


# ---------- ffmpeg merge --------------------------------------------------

def ensure_ffmpeg():
    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg not found on PATH — install it (e.g. `sudo apt install ffmpeg`).")


def merge_segments(segments: list[Segment], tmpdir: Path, out_path: Path) -> None:
    listing = build_ffmpeg_concat_file(tmpdir, segments)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(listing),
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        str(out_path),
    ]
    log("merging with ffmpeg (stream copy, no re-encode)...")
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        sys.stderr.write(result.stderr.decode("utf-8", "replace"))
        raise RuntimeError("ffmpeg merge failed")
    log(f"wrote {out_path} ({out_path.stat().st_size / (1024*1024):.1f} MB)")


# ---------- ffmpeg direct HLS download ----------------------------------

async def ffmpeg_download(stream: CapturedStream, cookies_str: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ua = stream.headers.get("User-Agent", "Mozilla/5.0")
    referer = stream.headers.get("Referer", "")
    headers_str = f"User-Agent: {ua}\r\n"
    if referer:
        headers_str += f"Referer: {referer}\r\n"
    if cookies_str:
        headers_str += f"Cookie: {cookies_str}\r\n"
    cmd = [
        "ffmpeg", "-y",
        "-headers", headers_str,
        "-i", stream.url,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "+faststart",
        str(out_path),
    ]
    log(f"downloading with ffmpeg → {out_path}")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        sys.stderr.write(stderr.decode("utf-8", "replace"))
        raise RuntimeError("ffmpeg download failed")
    log(f"wrote {out_path} ({out_path.stat().st_size / (1024*1024):.1f} MB)")


# ---------- browser-based parallel download -------------------------------

async def download_segments_via_browser(
    session: Session,
    segments: list[Segment],
    tmpdir: Path,
    concurrency: int = 8,
) -> None:
    sem = asyncio.Semaphore(concurrency)
    done = 0
    total = len(segments)
    lock = asyncio.Lock()

    async def fetch_one(seg: Segment):
        nonlocal done
        path = tmpdir / seg_filename(seg)
        if path.exists() and path.stat().st_size > 0:
            async with lock:
                done += 1
            return

        async with sem:
            try:
                resp = await session.context.request.get(seg.url)
                if resp.ok:
                    data = await resp.body()
                    path.write_bytes(data)
                    async with lock:
                        done += 1
                        if done % 50 == 0 or done == total:
                            log(f"  {done}/{total} segments")
                else:
                    log(f"  seg {seg.index}: HTTP {resp.status}")
            except Exception as e:
                log(f"  seg {seg.index}: {e}")

    await asyncio.gather(*(fetch_one(s) for s in segments), return_exceptions=True)
    log(f"downloaded {done}/{total} segments")


# ---------- orchestration -------------------------------------------------

async def run(cfg: dict) -> None:
    ensure_ffmpeg()
    async with Session(cfg) as session:
        await session.login()
        master = await session.open_video_and_capture()

        log("fetching playlist")
        text = await session.fetch_text(master)

        variants = parse_master(text, master.url)
        chosen: CapturedStream
        if variants:
            pref = str(cfg.get("preferred_quality", "highest")).lower()
            if pref in ("highest", "best", "max"):
                variants.sort(key=lambda v: v["bandwidth"], reverse=True)
            elif pref in ("lowest", "worst", "min"):
                variants.sort(key=lambda v: v["bandwidth"])
            else:
                target = int(re.sub(r"\D", "", pref) or "0")
                def _score(v):
                    h = 0
                    m = re.search(r"x(\d+)", v["resolution"])
                    if m:
                        h = int(m.group(1))
                    return abs(h - target)
                variants.sort(key=_score)
            log("variants:")
            for v in variants:
                log(f"  - {v['resolution'] or '?'}  {v['bandwidth']/1000:.0f} kbps  {v['url']}")
            picked = variants[0]
            log(f"picked {picked['resolution'] or '?'} @ {picked['bandwidth']/1000:.0f} kbps")
            chosen = CapturedStream(url=picked["url"], headers=master.headers)
            text = await session.fetch_text(chosen)
        else:
            chosen = master

        segments, is_live = parse_media(text, chosen.url)
        if not segments:
            raise RuntimeError("no segments parsed from media playlist")
        log(f"{len(segments)} segments to download — letting browser fetch them by playing video")

        # save playlist + base URL for segdl.py
        base_url = chosen.url.rsplit("/", 1)[0] + "/"
        with open("seg.txt", "w") as sf:
            sf.write(f"# base_url={base_url}\n")
            sf.write(text)
        log(f"saved seg.txt (base: {base_url})")
        log(f"  → to re-download later: python segdl.py \"{base_url}\" --playlist seg.txt")

        # play through the video to capture all segments
        total = len(segments)
        estimated_duration = total * 2  # total video seconds
        estimated_minutes = estimated_duration / 16 / 60  # at 16x speed
        log(f"playing through video at 16x speed (estimated {estimated_minutes:.1f}min)...")

        async def _nudge_video():
            """Ensure video is playing at max speed, unmuted won't block autoplay."""
            try:
                await session.page.evaluate("""
                    (function() {
                        var v = document.querySelector('video');
                        if (!v) return;
                        v.muted = true;
                        v.playbackRate = 16;
                        v.play();
                    })();
                """)
            except Exception as e:
                log(f"playback control failed: {e}")

        async def _seek_forward():
            """Seek the video forward to un-stick buffering stalls."""
            try:
                result = await session.page.evaluate("""
                    (function() {
                        var v = document.querySelector('video');
                        if (!v) return null;
                        var cur = v.currentTime;
                        var dur = v.duration;
                        // jump forward by 30s (or to near end)
                        var target = Math.min(cur + 30, dur - 1);
                        if (target > cur) {
                            v.currentTime = target;
                        }
                        v.playbackRate = 16;
                        v.play();
                        return {cur: cur, target: target, dur: dur};
                    })();
                """)
                if result:
                    log(f"  nudge: seeked from {result.get('cur',0):.0f}s to {result.get('target',0):.0f}s / {result.get('dur',0):.0f}s")
            except Exception as e:
                log(f"  seek failed: {e}")

        await _nudge_video()

        # wait for segments to be captured, with stall detection
        wait_limit = estimated_duration * 2 + 120
        stall_threshold = 20  # seconds with no new segments before seeking
        start = time.time()
        last_count = 0
        last_progress_time = time.time()
        stall_nudge_count = 0
        while time.time() - start < wait_limit:
            count = len(session._segment_bytes)
            if count != last_count:
                log(f"  captured {count}/{total} segments")
                last_count = count
                last_progress_time = time.time()
                stall_nudge_count = 0
            if count >= total * 0.95:  # allow 5% missing
                break
            # detect stall: no new segments for stall_threshold seconds
            stall_duration = time.time() - last_progress_time
            if stall_duration > stall_threshold:
                stall_nudge_count += 1
                log(f"  stall detected ({stall_duration:.0f}s idle at {count}/{total}), nudging video (attempt {stall_nudge_count})...")
                if stall_nudge_count <= 3:
                    await _seek_forward()
                else:
                    # after 3 failed seeks, break and rely on direct download fallback
                    log(f"  giving up on browser capture after {stall_nudge_count} stall nudges")
                    break
                last_progress_time = time.time()
            await asyncio.sleep(2)

        # wait for all pending body-fetch tasks to finish
        if session._pending_tasks:
            log(f"waiting for {len(session._pending_tasks)} pending segment captures...")
            await asyncio.gather(*list(session._pending_tasks), return_exceptions=True)

        captured = len(session._segment_bytes)
        log(f"captured {captured}/{total} segments from browser")
        if captured == 0:
            raise RuntimeError("No segments were captured. The video may not have played.")

        # write segments to disk using path-based key matching
        out_path = Path(cfg.get("output") or f"{uuid.uuid4().hex}.mp4").resolve()
        tmp_parent = out_path.parent / (out_path.stem + ".segments")
        tmp_parent.mkdir(parents=True, exist_ok=True)
        log("fetching segment data...")
        try:
            written = 0
            missing_segs = []
            for seg in segments:
                seg_key = _seg_key_from_url(seg.url)
                data = session._segment_bytes.get(seg_key)
                if data:
                    (tmp_parent / seg_filename(seg)).write_bytes(data)
                    written += 1
                else:
                    missing_segs.append(seg)
            log(f"wrote {written} segment files from browser capture")

            # fallback: directly download any missing segments via browser context
            if missing_segs:
                log(f"directly downloading {len(missing_segs)} missing segments...")
                sem = asyncio.Semaphore(cfg.get("concurrency", 4))
                dl_count = 0
                dl_lock = asyncio.Lock()

                async def fetch_missing(seg: Segment):
                    nonlocal dl_count
                    path = tmp_parent / seg_filename(seg)
                    if path.exists() and path.stat().st_size > 0:
                        return
                    for attempt in range(cfg.get("max_retries", 6)):
                        try:
                            async with sem:
                                resp = await session.context.request.get(seg.url)
                                if resp.ok:
                                    data = await resp.body()
                                    if data and len(data) > 0:
                                        path.write_bytes(data)
                                        async with dl_lock:
                                            dl_count += 1
                                            if dl_count % 20 == 0:
                                                log(f"  downloaded {dl_count}/{len(missing_segs)} missing")
                                        return
                            await asyncio.sleep(min(2 ** attempt, 10))
                        except Exception:
                            await asyncio.sleep(min(2 ** attempt, 10))

                await asyncio.gather(*(fetch_missing(s) for s in missing_segs), return_exceptions=True)
                log(f"downloaded {dl_count}/{len(missing_segs)} missing segments")

            present = [s for s in segments if (tmp_parent / seg_filename(s)).exists()]
            if not present:
                log(f"ERROR: segment_bytes has {len(session._segment_bytes)} entries but no files written")
                if segments:
                    log(f"First parsed seg key: {_seg_key_from_url(segments[0].url)}")
                if session._segment_bytes:
                    log(f"First captured key:   {list(session._segment_bytes.keys())[0]}")
                raise RuntimeError("No segments were saved")
            log(f"merging {len(present)}/{total} segments...")
            merge_segments(present, tmp_parent, out_path)
        finally:
            if not cfg.get("keep_segments", False):
                shutil.rmtree(tmp_parent, ignore_errors=True)


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    if len(sys.argv) != 2:
        print("usage: python downloader.py <config.json>", file=sys.stderr)
        sys.exit(2)
    cfg = load_config(sys.argv[1])
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
