"""Fetch a Dryad file past the Anubis proof-of-work bot check.

Usage: python3 scripts/dryad_get.py <url> <outfile>
Solves sha256(randomData + nonce) with N leading hex zeros, then follows the
pass-challenge endpoint to get a session cookie, then downloads the file.
"""
import hashlib
import json
import re
import sys
import time

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def solve(random_data: str, difficulty: int) -> tuple[str, int]:
    prefix = "0" * difficulty
    nonce = 0
    while True:
        h = hashlib.sha256(f"{random_data}{nonce}".encode()).hexdigest()
        if h.startswith(prefix):
            return h, nonce
        nonce += 1


def main() -> None:
    url, out = sys.argv[1], sys.argv[2]
    s = requests.Session()
    s.headers["User-Agent"] = UA
    r = s.get(url, timeout=120)
    body = r.text[:200000] if "text/html" in r.headers.get("content-type", "") else ""
    if "anubis_challenge" not in body:
        with open(out, "wb") as fh:
            fh.write(r.content)
        print(f"direct: HTTP {r.status_code}, {len(r.content)} bytes -> {out}")
        return
    m = re.search(r'id="anubis_challenge"[^>]*>(\{.*?\})\s*</script>', body, re.S)
    ch = json.loads(m.group(1))
    diff = ch["rules"]["difficulty"]
    rd = ch["challenge"]["randomData"]
    cid = ch["challenge"]["id"]
    t0 = time.time()
    h, nonce = solve(rd, diff)
    el = int((time.time() - t0) * 1000)
    print(f"solved difficulty {diff} in {nonce} tries, {el} ms")
    path = url.split("datadryad.org", 1)[1]
    r2 = s.get(
        "https://datadryad.org/.within.website/x/cmd/anubis/api/pass-challenge",
        params={"id": cid, "response": h, "nonce": nonce, "redir": path,
                "elapsedTime": el},
        timeout=300,
    )
    with open(out, "wb") as fh:
        fh.write(r2.content)
    print(f"HTTP {r2.status_code} {r2.headers.get('content-type')} "
          f"{len(r2.content)} bytes -> {out}")


if __name__ == "__main__":
    main()
