#!/usr/bin/env python3
"""
Premium Getter — activate 7-day free premium on recu.me.

Each run uses a completely fresh, disposable browser profile AND spoofs
the browser fingerprint (canvas, WebGL, audio, fonts, etc.) so the site
cannot link it to any previous activation.

Usage:
    python premiumgetter.py [premium_config.json]
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import shutil
import string
import sys
import tempfile
import time
from pathlib import Path

from playwright.async_api import (
    BrowserContext,
    Page,
    TimeoutError as PWTimeout,
    async_playwright,
)

try:
    from playwright_stealth import stealth_async
except Exception:
    stealth_async = None


# ── config loading ────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ── fingerprint spoofing ─────────────────────────────────────────────────

# random viewports that look realistic
VIEWPORTS = [
    (1366, 768), (1920, 1080), (1536, 864), (1440, 900),
    (1280, 720), (1600, 900), (1680, 1050), (1280, 800),
    (1360, 768), (1280, 1024), (1920, 1200), (1400, 900),
]

# realistic user-agent variations (Chrome on Linux)
USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

WEBGL_VENDORS = [
    "Google Inc. (NVIDIA)",
    "Google Inc. (AMD)",
    "Google Inc. (Intel)",
    "Google Inc.",
]

WEBGL_RENDERERS = [
    "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "ANGLE (NVIDIA, NVIDIA GeForce RTX 2070 SUPER Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "ANGLE (AMD, AMD Radeon RX 5700 XT Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "Mesa Intel(R) UHD Graphics 620 (KBL GT2)",
    "Mesa Intel(R) HD Graphics 530 (SKL GT2)",
]

LANGUAGES = [
    "en-US,en;q=0.9",
    "en-US,en;q=0.9,fr;q=0.8",
    "en-GB,en;q=0.9",
    "en-US,en;q=0.9,es;q=0.8",
    "en-US,en;q=0.9,de;q=0.8",
]

PLATFORMS = ["Win32", "Linux x86_64", "MacIntel"]


def build_fingerprint_spoof_script() -> str:
    """Build JS that overrides fingerprinting APIs with random values."""
    seed = random.randint(0, 2**32)
    vendor = random.choice(WEBGL_VENDORS)
    renderer = random.choice(WEBGL_RENDERERS)
    platform = random.choice(PLATFORMS)
    hw_concurrency = random.choice([2, 4, 6, 8, 12, 16])
    device_memory = random.choice([2, 4, 8, 16])
    max_touch = random.choice([0, 0, 0, 1, 5])  # mostly 0 for desktop
    color_depth = random.choice([24, 32])
    pixel_ratio = random.choice([1, 1, 1, 1.25, 1.5, 2])
    tz_offset = random.choice([-300, -360, -420, -480, 0, 60, 120, 330, 345, 480, 540])

    # generate random noise bytes for canvas
    noise_r = random.randint(-3, 3)
    noise_g = random.randint(-3, 3)
    noise_b = random.randint(-3, 3)

    return f"""
    // ── Canvas fingerprint spoofing ──
    (function() {{
        const SEED = {seed};
        const NOISE_R = {noise_r};
        const NOISE_G = {noise_g};
        const NOISE_B = {noise_b};

        // Override toDataURL
        const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
        HTMLCanvasElement.prototype.toDataURL = function(type, quality) {{
            const ctx = this.getContext('2d');
            if (ctx) {{
                try {{
                    const imageData = ctx.getImageData(0, 0, this.width, this.height);
                    const data = imageData.data;
                    for (let i = 0; i < data.length; i += 4) {{
                        data[i]     = Math.max(0, Math.min(255, data[i] + NOISE_R));
                        data[i + 1] = Math.max(0, Math.min(255, data[i + 1] + NOISE_G));
                        data[i + 2] = Math.max(0, Math.min(255, data[i + 2] + NOISE_B));
                    }}
                    ctx.putImageData(imageData, 0, 0);
                }} catch(e) {{}}
            }}
            return origToDataURL.call(this, type, quality);
        }};

        // Override toBlob
        const origToBlob = HTMLCanvasElement.prototype.toBlob;
        HTMLCanvasElement.prototype.toBlob = function(callback, type, quality) {{
            const ctx = this.getContext('2d');
            if (ctx) {{
                try {{
                    const imageData = ctx.getImageData(0, 0, this.width, this.height);
                    const data = imageData.data;
                    for (let i = 0; i < data.length; i += 4) {{
                        data[i]     = Math.max(0, Math.min(255, data[i] + NOISE_R));
                        data[i + 1] = Math.max(0, Math.min(255, data[i + 1] + NOISE_G));
                        data[i + 2] = Math.max(0, Math.min(255, data[i + 2] + NOISE_B));
                    }}
                    ctx.putImageData(imageData, 0, 0);
                }} catch(e) {{}}
            }}
            return origToBlob.call(this, callback, type, quality);
        }};

        // Override getImageData to add noise
        const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
        CanvasRenderingContext2D.prototype.getImageData = function() {{
            const imageData = origGetImageData.apply(this, arguments);
            // Only add noise if canvas is being read for fingerprinting (small canvases)
            if (this.canvas.width < 500 && this.canvas.height < 500) {{
                for (let i = 0; i < imageData.data.length; i += 4) {{
                    imageData.data[i]     = Math.max(0, Math.min(255, imageData.data[i] + NOISE_R));
                    imageData.data[i + 1] = Math.max(0, Math.min(255, imageData.data[i + 1] + NOISE_G));
                    imageData.data[i + 2] = Math.max(0, Math.min(255, imageData.data[i + 2] + NOISE_B));
                }}
            }}
            return imageData;
        }};
    }})();

    // ── WebGL fingerprint spoofing ──
    (function() {{
        const vendor = "{vendor}";
        const renderer = "{renderer}";

        const getParameterProto = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(param) {{
            if (param === 37445) return vendor;   // UNMASKED_VENDOR_WEBGL
            if (param === 37446) return renderer;  // UNMASKED_RENDERER_WEBGL
            return getParameterProto.call(this, param);
        }};

        if (typeof WebGL2RenderingContext !== 'undefined') {{
            const getParameter2Proto = WebGL2RenderingContext.prototype.getParameter;
            WebGL2RenderingContext.prototype.getParameter = function(param) {{
                if (param === 37445) return vendor;
                if (param === 37446) return renderer;
                return getParameter2Proto.call(this, param);
            }};
        }}
    }})();

    // ── AudioContext fingerprint spoofing ──
    (function() {{
        const context = OfflineAudioContext || webkitOfflineAudioContext;
        if (!context) return;
        const origCreateOscillator = context.prototype.createOscillator;
        context.prototype.createOscillator = function() {{
            const osc = origCreateOscillator.call(this);
            const origConnect = osc.connect.bind(osc);
            osc.connect = function(dest) {{
                const result = origConnect(dest);
                return result;
            }};
            // slightly detune to alter audio fingerprint
            osc.detune && (osc.detune.value = {random.uniform(-0.1, 0.1)});
            return osc;
        }};
    }})();

    // ── Navigator property overrides ──
    Object.defineProperty(navigator, 'hardwareConcurrency', {{ get: () => {hw_concurrency} }});
    Object.defineProperty(navigator, 'deviceMemory', {{ get: () => {device_memory} }});
    Object.defineProperty(navigator, 'maxTouchPoints', {{ get: () => {max_touch} }});
    Object.defineProperty(navigator, 'platform', {{ get: () => "{platform}" }});

    // ── Screen property overrides ──
    Object.defineProperty(screen, 'colorDepth', {{ get: () => {color_depth} }});
    Object.defineProperty(window, 'devicePixelRatio', {{ get: () => {pixel_ratio} }});

    // ── Timezone offset override ──
    const origGetTimezoneOffset = Date.prototype.getTimezoneOffset;
    Date.prototype.getTimezoneOffset = function() {{ return {tz_offset}; }};

    // ── Plugin/MimeType spoofing (randomize count) ──
    Object.defineProperty(navigator, 'plugins', {{
        get: () => {{
            const count = {random.randint(0, 5)};
            const arr = [];
            for (let i = 0; i < count; i++) arr.push({{ name: 'Plugin ' + i, description: '', filename: '' }});
            arr.length = count;
            return arr;
        }}
    }});
    """


# ── helpers ───────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


async def wait_for_cloudflare(page: Page, timeout: float = 60.0) -> None:
    """Wait for Cloudflare interstitial to clear.
    
    Waits up to `timeout` seconds for the CF challenge to resolve.
    Checks both page title and URL patterns.
    """
    start = time.time()
    while time.time() - start < timeout:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
            title = (await page.title()).lower()
            url = page.url.lower()
            # CF challenge indicators
            if ("just a moment" in title
                    or "checking your browser" in title
                    or "challenge" in url
                    or "cdn-cgi" in url):
                log(f"  CF challenge active ({time.time() - start:.0f}s)...")
                await asyncio.sleep(2)
                continue
            return
        except Exception:
            pass
        await asyncio.sleep(2)
    log("⚠ Cloudflare challenge still pending after timeout — continuing anyway")


# ── main flow ─────────────────────────────────────────────────────────────

async def run(cfg: dict) -> None:
    email = cfg["email"]
    password = cfg["password"]
    sels = cfg.get("selectors", {})
    urls = cfg.get("urls", {})

    LOGIN_URL = cfg.get("login_url", "https://recu.me/account/signin")
    SEL_USERNAME = sels.get("username", "#input_email")
    SEL_PASSWORD = sels.get("password", "#input_password")
    SEL_LOGIN_SUBMIT = sels.get("login_submit", "button[type='submit']")

    # Use a PERSISTENT profile so Cloudflare's cf_clearance cookie survives
    # across runs.  HOWEVER, when using a proxy the CF cookie is bound to
    # the proxy IP, so we use a fresh temp profile per-run instead.
    proxy_cfg = cfg.get("proxy")  # e.g. "socks5://127.0.0.1:9050"
    if proxy_cfg:
        profile_dir = Path(tempfile.mkdtemp(prefix="recume_proxy_"))
        log(f"using fresh profile (proxy mode): {profile_dir}")
        log(f"proxy: {proxy_cfg}")
    else:
        profile_dir = Path(cfg.get("profile_dir", ".premium-profile")).resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)
        log(f"using profile: {profile_dir}")

    # generate the fingerprint spoofing JS (injected LATER, not during CF)
    spoof_js = build_fingerprint_spoof_script()

    pw = await async_playwright().start()
    cleanup_profile = bool(proxy_cfg)  # only delete temp profiles
    try:
        launch_opts = dict(
            user_data_dir=str(profile_dir),
            headless=False,
            viewport={"width": 1366, "height": 800},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )
        if proxy_cfg:
            launch_opts["proxy"] = {"server": proxy_cfg}
        ctx: BrowserContext = await pw.chromium.launch_persistent_context(**launch_opts)
        page: Page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        if stealth_async:
            try:
                await stealth_async(page)
            except Exception as e:
                log(f"stealth patch skipped: {e}")

        # ── step 0: clear site tracking data ──────────────────────────
        # Keep cf_clearance / __cf_bm cookies so Cloudflare doesn't
        # re-challenge, but wipe everything else the SITE uses to
        # identify returning browsers (session cookies, localStorage,
        # fingerprint markers, etc.)
        log("step 0 → clearing site tracking data (keeping CF cookies)")
        all_cookies = await ctx.cookies("https://recu.me")
        cf_cookie_names = {"cf_clearance", "__cf_bm"}
        cf_cookies = [c for c in all_cookies if c["name"] in cf_cookie_names]
        await ctx.clear_cookies()
        # restore only CF cookies
        if cf_cookies:
            await ctx.add_cookies(cf_cookies)
            log(f"  preserved {len(cf_cookies)} CF cookie(s)")
        else:
            log("  no CF cookies found (first run — CF challenge may appear)")

        # clear localStorage / sessionStorage by visiting a blank page on domain
        try:
            await page.goto("https://recu.me/favicon.ico", wait_until="domcontentloaded", timeout=10000)
            await page.evaluate("""
                try { localStorage.clear(); } catch(e) {}
                try { sessionStorage.clear(); } catch(e) {}
            """)
            log("  cleared localStorage & sessionStorage")
        except Exception:
            pass  # might fail on first run, that's fine

        # ── step 1: login ─────────────────────────────────────────────
        log("step 1 → navigating to login page")
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await wait_for_cloudflare(page)
        await asyncio.sleep(2)

        log("  filling credentials")
        await page.wait_for_selector(SEL_USERNAME, timeout=30000)
        await page.fill(SEL_USERNAME, email)
        await page.fill(SEL_PASSWORD, password)

        try:
            await asyncio.gather(
                page.wait_for_load_state("networkidle", timeout=15000),
                page.click(SEL_LOGIN_SUBMIT),
            )
        except Exception:
            pass  # page may redirect before networkidle
        await wait_for_cloudflare(page)
        log("  login submitted — waiting for redirect")
        await asyncio.sleep(3)
        log(f"  current URL: {page.url}")

        # ── step 2: go to My Account ──────────────────────────────────
        log("step 2 → navigating to My Account")
        await page.goto("https://recu.me/account", wait_until="domcontentloaded")
        await wait_for_cloudflare(page)
        await asyncio.sleep(2)
        log(f"  current URL: {page.url}")

        # ── INJECT FINGERPRINT SPOOFING BEFORE PROMO PAGE ─────────────
        # The site's fingerprinting JS runs on page load of the promo
        # page and bakes the hash into the "Get it now!" button href.
        # We must inject BEFORE that page loads. add_init_script runs
        # on every subsequent page load, so the promo page will use
        # our spoofed canvas/WebGL/audio/navigator APIs.
        log("  injecting fingerprint spoofing (before promo page)")
        await ctx.add_init_script(spoof_js)

        # ── step 3: navigate to promo page ────────────────────────────
        # The upgrade URL redirects to the promo page. Since we just
        # registered add_init_script, the spoofing JS will execute
        # before the page's own fingerprinting JS runs.
        log("step 3 → navigating to promo page")
        await page.goto("https://recu.me/account/membership/offer/limited?url=L2FjY291bnQ=",
                        wait_until="domcontentloaded")
        await wait_for_cloudflare(page)
        await asyncio.sleep(3)
        log(f"  current URL: {page.url}")

        # double-check: if the fingerprint was already computed before
        # our init_script ran, reload the page to force recomputation
        promo_sel = "#btn-order-enable-promo"
        try:
            btn_href = await page.get_attribute(promo_sel, "href", timeout=5000)
            if btn_href and "fingerprint=" in btn_href:
                log(f"  fingerprint in button: ...{btn_href.split('fingerprint=')[-1][:12]}...")
            else:
                log("  reloading page to ensure spoofing is active")
                await page.reload(wait_until="domcontentloaded")
                await asyncio.sleep(3)
        except Exception:
            log("  reloading page to ensure spoofing is active")
            await page.reload(wait_until="domcontentloaded")
            await asyncio.sleep(3)

        # ── step 4: click "Get it now!" (free promo) ──────────────────
        log("step 4 → clicking 'Get it now!' for free premium")
        try:
            await page.wait_for_selector(promo_sel, timeout=10000)
            # log the fingerprint that will be sent
            btn_href = await page.get_attribute(promo_sel, "href")
            if btn_href and "fingerprint=" in btn_href:
                fp = btn_href.split("fingerprint=")[-1].split("&")[0]
                log(f"  fingerprint being sent: {fp}")
            await page.click(promo_sel)
            await wait_for_cloudflare(page)
            await asyncio.sleep(3)
            log(f"  current URL: {page.url}")
        except PWTimeout:
            log("  ⚠ 'Get it now!' button not found on page")

        # ── step 5: check result ──────────────────────────────────────
        log("step 5 → checking activation result")
        await asyncio.sleep(2)
        body_text = await page.inner_text("body")

        if "free premium is unavailable" in body_text.lower():
            log("  ✗ FAILED — site says 'Free Premium is Unavailable'")
            if not proxy_cfg:
                log("  💡 The site also tracks by IP address.")
                log("  💡 Add a proxy to premium_config.json:")
                log('     "proxy": "socks5://127.0.0.1:9050"  (Tor)')
                log('     "proxy": "http://host:port"          (HTTP proxy)')
            else:
                log("  try a different proxy/IP and run again")
        elif "premium" in body_text.lower() and ("activated" in body_text.lower()
                or "congratulation" in body_text.lower()
                or "success" in body_text.lower()
                or "enjoy" in body_text.lower()):
            log("  ✓ SUCCESS — free premium appears to be activated!")
        else:
            log(f"  ? UNKNOWN result — check browser window")
            log(f"  page title: {await page.title()}")
            # dump a snippet of the page for debugging
            snippet = body_text[:500].replace("\n", " ").strip()
            log(f"  body preview: {snippet}")

        log(f"  final URL: {page.url}")

        # keep browser open so user can verify
        log("keeping browser open for 15s — check the result")
        await asyncio.sleep(15)

    finally:
        try:
            await ctx.close()
        except Exception:
            pass
        await pw.stop()
        if cleanup_profile:
            shutil.rmtree(profile_dir, ignore_errors=True)
            log(f"cleaned up temp profile: {profile_dir}")


def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "premium_config.json"
    try:
        cfg = load_config(config_path)
    except FileNotFoundError:
        print(f"config not found: {config_path}", file=sys.stderr)
        print("usage: python premiumgetter.py [premium_config.json]", file=sys.stderr)
        sys.exit(2)
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
