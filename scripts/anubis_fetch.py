#!/usr/bin/env python3
"""Fetch a URL from a site protected by an Anubis proof-of-work challenge.

Usage: python3 scripts/anubis_fetch.py <url> <outfile>

Plain steps:
 1. GET the url with a normal browser User-Agent. If we get the file, save it.
 2. If we get the Anubis "Validating..." page, read the JSON challenge embedded
    in it (script id="anubis_challenge").
 3. Find a nonce so that sha256(randomData + nonce) starts with N zeros
    (N = challenge difficulty, here 4 -> about 65k tries).
 4. Hand that back to /.../api/pass-challenge, which sets a cookie.
 5. GET the url again with the cookie and save the bytes.
"""
import hashlib
import json
import re
import sys
import time
import urllib.parse

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")


def solve(random_data: str, difficulty: int) -> tuple[str, int]:
    prefix = "0" * difficulty
    nonce = 0
    while True:
        h = hashlib.sha256(f"{random_data}{nonce}".encode()).hexdigest()
        if h.startswith(prefix):
            return h, nonce
        nonce += 1


def main() -> int:
    url, out = sys.argv[1], sys.argv[2]
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "*/*"})

    r = s.get(url, timeout=120, allow_redirects=True)
    if "Validating" not in r.text[:4000] or "anubis_challenge" not in r.text:
        open(out, "wb").write(r.content)
        print(f"direct: {r.status_code} {len(r.content)} bytes -> {out}")
        return 0

    m = re.search(r'id="anubis_challenge"[^>]*>(.*?)</script>', r.text, re.S)
    blob = json.loads(m.group(1))
    chal, rules = blob["challenge"], blob["rules"]
    diff = rules["difficulty"]
    t0 = time.time()
    digest, nonce = solve(chal["randomData"], diff)
    elapsed = int((time.time() - t0) * 1000)
    print(f"solved difficulty {diff} in {elapsed} ms, nonce={nonce}, hash={digest[:12]}")

    base = f"{urllib.parse.urlsplit(url).scheme}://{urllib.parse.urlsplit(url).netloc}"
    pass_url = base + "/.within.website/x/cmd/anubis/api/pass-challenge?" + urllib.parse.urlencode(
        {"id": chal["id"], "response": digest, "nonce": nonce,
         "redir": url, "elapsedTime": elapsed})
    r2 = s.get(pass_url, timeout=120, allow_redirects=True)
    print("pass-challenge:", r2.status_code, r2.headers.get("content-type"),
          len(r2.content), "cookies:", list(s.cookies.keys()))

    if r2.content and "text/html" not in (r2.headers.get("content-type") or ""):
        open(out, "wb").write(r2.content)
    else:
        r3 = s.get(url, timeout=300, allow_redirects=True)
        print("refetch:", r3.status_code, r3.headers.get("content-type"), len(r3.content))
        open(out, "wb").write(r3.content)
    print("wrote", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
