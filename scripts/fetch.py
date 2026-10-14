#!/usr/bin/env python3
"""Fetch a URL once, cache it, and refuse to hang or to pull something huge.

    python3 scripts/fetch.py <url>                     # print the body (text) or the saved path
    python3 scripts/fetch.py <url> --head              # status, size and type only, no body
    python3 scripts/fetch.py <url> -o out.zip          # save to a file
    python3 scripts/fetch.py <url> --max-mb 100        # raise the 50 MB cap for this call
    python3 scripts/fetch.py <url> --timeout 30        # default 60 s
    python3 scripts/fetch.py <url> --refresh           # ignore the cache

Why this exists (measured over 61 paper evaluations):
  - the same URL was re-fetched up to 12 times inside one evaluation, and metadata endpoints
    were re-fetched across evaluations. Responses are cached in data/cache/http/ and shared.
  - Europe PMC's supplementaryFiles endpoint averaged 71 s and twice ran past 500 s. The default
    60 s timeout makes that fail fast instead of blocking an evaluation for ten minutes.
  - CLAUDE.md caps downloads at 50 MB. A HEAD runs first, so an oversized file is reported,
    not transferred.

Exit codes: 0 fine, 1 request failed, 2 bigger than the cap (metadata still printed).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "cache" / "http"
UA = "bioml-scout/0.1 (paper evaluation; mailto:hkrupkin@stanford.edu)"
# NCBI allows 3 requests/s without a key, so leave a margin; others are unthrottled.
RATE_LIMITED = {"eutils.ncbi.nlm.nih.gov": 0.4}
STAMP = ROOT / "data" / "cache" / "last_request.json"


def wait_turn(host: str) -> None:
    gap = RATE_LIMITED.get(host)
    if not gap:
        return
    STAMP.parent.mkdir(parents=True, exist_ok=True)
    try:
        last = json.loads(STAMP.read_text()).get(host, 0)
    except (OSError, json.JSONDecodeError):
        last = 0
    sleep = gap - (time.time() - last)
    if sleep > 0:
        time.sleep(sleep)
    try:
        state = json.loads(STAMP.read_text())
    except (OSError, json.JSONDecodeError):
        state = {}
    state[host] = time.time()
    STAMP.write_text(json.dumps(state))


def cache_paths(url: str) -> tuple[Path, Path]:
    key = hashlib.sha256(url.encode()).hexdigest()[:24]
    return CACHE / f"{key}.body", CACHE / f"{key}.meta.json"


# A failure is remembered too. Without this, an endpoint that times out costs its full timeout on
# every call — so prewarming bought nothing on exactly the slow papers it exists to help, and the
# agent re-paid two minutes for an answer the prewarmer already had.
FAIL_TTL_S = 6 * 3600


def fail_path(url: str) -> Path:
    return CACHE / f"{hashlib.sha256(url.encode()).hexdigest()[:24]}.fail.json"


def remembered_failure(url: str) -> dict | None:
    p = fail_path(url)
    try:
        rec = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return rec if time.time() - rec.get("at", 0) < FAIL_TTL_S else None


def remember_failure(url: str, info: dict) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    tmp = fail_path(url).with_suffix(".part")
    tmp.write_text(json.dumps({**info, "at": time.time(), "when": time.strftime("%F %T")}))
    os.replace(tmp, fail_path(url))


def head(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            h = r.headers
            return {"status": r.status, "bytes": int(h.get("Content-Length") or -1),
                    "type": h.get("Content-Type") or "", "url": r.url}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "bytes": -1, "type": "", "url": url}
    except Exception as e:                     # HEAD is advisory: a refusal is not fatal
        return {"status": 0, "bytes": -1, "type": "", "url": url, "note": f"HEAD failed: {e!r}"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("-o", "--out", help="save the body here instead of printing it")
    ap.add_argument("--head", action="store_true", help="metadata only")
    ap.add_argument("--max-mb", type=float, default=50.0)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="no size/cache line on stderr")
    a = ap.parse_args()

    # A hard guarantee that this process ends. urllib's timeout covers a single socket read, so a
    # server that stalls mid-body can hold a request open indefinitely — one ran past 8 minutes.
    def watchdog() -> None:
        time.sleep(a.timeout + 15)
        info = {"error": f"hard deadline: killed after {a.timeout + 15:g}s", "url": a.url,
                "hint": "treat as unavailable"}
        try:
            remember_failure(a.url, info)
        except OSError:
            pass
        sys.stdout.write(json.dumps(info) + "\n")
        sys.stdout.flush()
        os._exit(1)

    threading.Thread(target=watchdog, daemon=True).start()

    host = urllib.parse.urlparse(a.url).netloc
    body_path, meta_path = cache_paths(a.url)
    cap = int(a.max_mb * 1024 * 1024)

    if a.head:
        info = head(a.url, min(a.timeout, 20))
        print(json.dumps(info, indent=1))
        return 0 if info["status"] and info["status"] < 400 else 1

    failed_before = None if a.refresh else remembered_failure(a.url)
    if failed_before:
        print(json.dumps({**failed_before, "cached_failure": True,
                          "hint": "failed recently; use --refresh to try again"}))
        return 1

    cached = body_path.exists() and meta_path.exists() and not a.refresh
    if cached:
        meta = json.loads(meta_path.read_text())
    else:
        info = head(a.url, min(a.timeout, 20))
        if 0 < info["bytes"] > cap:
            print(json.dumps({**info, "skipped": f"{info['bytes'] / 1e6:.1f} MB over the "
                                                 f"{a.max_mb:g} MB cap; record metadata only"}, indent=1))
            return 2
        wait_turn(host)
        req = urllib.request.Request(a.url, headers={"User-Agent": UA})
        # --timeout is a wall-clock deadline, not a socket timeout. urllib's own timeout only
        # covers a single read, so a server that trickles bytes can hold a request open for
        # ever; one such download ran past 8 minutes. Read in chunks and check the clock.
        deadline = time.monotonic() + a.timeout
        try:
            with urllib.request.urlopen(req, timeout=min(a.timeout, 30)) as r:
                chunks, total = [], 0
                while True:
                    if time.monotonic() > deadline:
                        info = {"error": f"deadline: {a.timeout:g}s elapsed after {total} bytes",
                                "url": a.url, "partial_bytes": total,
                                "hint": "the server is slow or stalled; treat as unavailable, "
                                        "or retry with a bigger --timeout"}
                        remember_failure(a.url, info)
                        print(json.dumps(info))
                        return 1
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > cap:
                        break
                data = b"".join(chunks)
                meta = {"status": r.status, "type": r.headers.get("Content-Type") or "",
                        "bytes": len(data), "url": r.url, "fetched": time.strftime("%F %T")}
        except Exception as e:
            info = {"error": repr(e), "url": a.url,
                    "hint": "raise --timeout, or treat as unavailable and say so in the evaluation"}
            # 404/410 and timeouts are worth remembering; a 429 or 5xx might be gone in a minute.
            transient = isinstance(e, urllib.error.HTTPError) and (e.code == 429 or e.code >= 500)
            if not transient:
                remember_failure(a.url, info)
            print(json.dumps(info))
            return 1
        if len(data) > cap:
            print(json.dumps({**meta, "skipped": f"body exceeds the {a.max_mb:g} MB cap"}, indent=1))
            return 2
        CACHE.mkdir(parents=True, exist_ok=True)
        # Write body then meta, each atomically: a concurrent reader checks for both, so it
        # never sees a half-written body (the prewarmer and an agent can race on one URL).
        tmp = body_path.with_suffix(".part")
        tmp.write_bytes(data)
        os.replace(tmp, body_path)
        tmp_meta = meta_path.with_suffix(".part")
        tmp_meta.write_text(json.dumps(meta))
        os.replace(tmp_meta, meta_path)

    if not a.quiet:
        print(f"[{'cache' if cached else 'fetched'}] {meta['bytes']} bytes, {meta['type']}", file=sys.stderr)
    if a.out:
        Path(a.out).write_bytes(body_path.read_bytes())
        print(a.out)
    elif meta["type"].split(";")[0] in {"application/json", "text/plain", "text/xml", "application/xml",
                                        "text/html", "text/csv", "text/tab-separated-values"}:
        sys.stdout.write(body_path.read_text(errors="replace"))
    else:
        print(body_path)                       # binary: hand back the path to open locally
    return 0


if __name__ == "__main__":
    sys.exit(main())
