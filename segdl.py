#!/usr/bin/env python3
"""
segdl.py — Fast parallel HLS segment downloader.

Reads an M3U8 playlist file (e.g. seg.txt), downloads all .ts segments
in parallel via direct HTTP, and merges them into a single MP4 with ffmpeg.

Usage:
    python segdl.py <base_url> [options]

    base_url:  CDN base URL where segments live, e.g.
               https://f113.mediafront.net/vod/_floret_joy_/2026-04-20,15-30/

Options:
    --playlist FILE   M3U8 playlist file (default: seg.txt)
    --output FILE     Output filename (default: output.mp4)
    --concurrency N   Parallel downloads (default: 24)
    --retries N       Max retries per segment (default: 5)
    --header K:V      Extra HTTP header (repeatable)
"""

import asyncio
import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import aiohttp


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def parse_m3u8(path: str) -> tuple[list[str], str | None]:
    """Extract segment filenames and optional base_url from an M3U8 file."""
    segments = []
    base_url = None
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if line.startswith("# base_url="):
                base_url = line.split("=", 1)[1].strip()
                continue
            if not line or line.startswith("#"):
                continue
            segments.append(line)
    return segments, base_url


# track the first error for debugging
_first_error_logged = False
_first_error_lock = asyncio.Lock()


async def download_segment(
    session: aiohttp.ClientSession,
    url: str,
    dest: Path,
    sem: asyncio.Semaphore,
    retries: int = 5,
) -> bool:
    """Download a single segment with retries."""
    global _first_error_logged
    async with sem:
        for attempt in range(1, retries + 1):
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        dest.write_bytes(data)
                        return True
                    else:
                        async with _first_error_lock:
                            if not _first_error_logged:
                                _first_error_logged = True
                                body = (await resp.text())[:300]
                                log(f"  ✗ first error: HTTP {resp.status} for {url[:100]}...")
                                log(f"    response: {body}")
                        if resp.status in (403, 410, 401):
                            return False  # auth failure — no point retrying
                        if attempt < retries:
                            await asyncio.sleep(0.5 * attempt)
            except Exception as e:
                async with _first_error_lock:
                    if not _first_error_logged:
                        _first_error_logged = True
                        log(f"  ✗ first error: {type(e).__name__}: {e}")
                if attempt < retries:
                    await asyncio.sleep(0.5 * attempt)
        return False


async def run(args):
    # parse playlist
    segments, embedded_base = parse_m3u8(args.playlist)
    if not segments:
        log(f"no segments found in {args.playlist}")
        sys.exit(1)

    # resolve base URL: CLI arg > embedded in playlist > error
    base_raw = args.base_url or embedded_base
    if not base_raw:
        log("ERROR: no base_url provided and none found in playlist file")
        log("  Either provide it as an argument or use a seg.txt saved by downloader.py")
        sys.exit(1)

    total = len(segments)
    log(f"found {total} segments in {args.playlist}")

    # prepare output directory for segment files
    seg_dir = Path("_segments")
    seg_dir.mkdir(exist_ok=True)

    # prepare base URL
    base = base_raw.rstrip("/") + "/"
    log(f"base URL: {base}")

    # build full URLs
    urls = []
    dests = []
    for i, seg in enumerate(segments, 1):
        # segment is like "seg-1-v1-a1.ts?uid=...&..." — relative to base
        full_url = base + seg
        dest = seg_dir / f"seg_{i:05d}.ts"
        urls.append(full_url)
        dests.append(dest)

    # figure out which segments we already have (resume support)
    to_download = []
    for i, (url, dest) in enumerate(zip(urls, dests)):
        if dest.exists() and dest.stat().st_size > 0:
            continue  # already downloaded
        to_download.append((i, url, dest))

    already = total - len(to_download)
    if already:
        log(f"resuming — {already} segments already downloaded, {len(to_download)} remaining")

    if not to_download:
        log("all segments already downloaded — skipping to merge")
    else:
        # prepare headers — Referer is critical for CDN auth
        referer = args.referer or "https://recu.me/"
        headers = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Referer": referer,
            "Origin": referer.rstrip("/"),
        }
        if args.header:
            for h in args.header:
                k, v = h.split(":", 1)
                headers[k.strip()] = v.strip()
        log(f"using Referer: {referer}")

        sem = asyncio.Semaphore(args.concurrency)
        done = 0
        failed = 0
        lock = asyncio.Lock()

        async def dl_and_report(idx, url, dest):
            nonlocal done, failed
            ok = await download_segment(session, url, dest, sem, args.retries)
            async with lock:
                if ok:
                    done += 1
                else:
                    failed += 1
                count = done + failed
                if count % 25 == 0 or count == len(to_download):
                    log(f"  progress: {done}/{len(to_download)} downloaded"
                        f" ({failed} failed)")

        connector = aiohttp.TCPConnector(limit=args.concurrency + 5, force_close=False)
        async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
            log(f"downloading {len(to_download)} segments with concurrency={args.concurrency}...")
            start = time.time()

            tasks = [dl_and_report(i, url, dest) for i, url, dest in to_download]
            await asyncio.gather(*tasks)

            elapsed = time.time() - start
            log(f"downloads complete: {done} ok, {failed} failed in {elapsed:.1f}s")

        if failed:
            log(f"⚠ {failed} segments failed — output may have gaps")

    # merge with ffmpeg
    log("merging segments with ffmpeg...")

    # create ffmpeg concat file
    concat_file = seg_dir / "concat.txt"
    with open(concat_file, "w") as f:
        for dest in dests:
            if dest.exists() and dest.stat().st_size > 0:
                f.write(f"file '{dest.resolve()}'\n")

    output = Path(args.output)
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_file),
        "-c", "copy",
        str(output),
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        log(f"ffmpeg error: {result.stderr.decode()[-500:]}")
        sys.exit(1)

    size_mb = output.stat().st_size / (1024 * 1024)
    log(f"✓ wrote {output} ({size_mb:.1f} MB)")

    # cleanup segments
    if not args.keep:
        shutil.rmtree(seg_dir, ignore_errors=True)
        log("cleaned up segment files")


def main():
    parser = argparse.ArgumentParser(description="Fast parallel HLS segment downloader")
    parser.add_argument("base_url", nargs="?", default=None, help="CDN base URL for segments (auto-detected from seg.txt if saved by downloader.py)")
    parser.add_argument("--playlist", default="seg.txt", help="M3U8 playlist file (default: seg.txt)")
    parser.add_argument("--output", default="output.mp4", help="Output filename (default: output.mp4)")
    parser.add_argument("--concurrency", type=int, default=24, help="Parallel downloads (default: 24)")
    parser.add_argument("--retries", type=int, default=5, help="Max retries per segment (default: 5)")
    parser.add_argument("--header", action="append", help="Extra HTTP header as K:V")
    parser.add_argument("--referer", default=None, help="Referer header (default: https://recu.me/)")
    parser.add_argument("--keep", action="store_true", help="Keep segment files after merge")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
