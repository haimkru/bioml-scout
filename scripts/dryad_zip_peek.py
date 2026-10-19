"""List, and optionally extract, members of a remote zip on Dryad via HTTP range requests.

Avoids downloading the whole archive. Solves the Anubis proof-of-work bot check first.

Usage:
  python3 scripts/dryad_zip_peek.py <zip_url> list
  python3 scripts/dryad_zip_peek.py <zip_url> get <outdir> <substr> [<substr> ...]
"""
import hashlib
import io
import json
import re
import struct
import sys
import time
import zlib

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BASE = "https://datadryad.org"


def anubis_session(url: str) -> tuple[requests.Session, str]:
    s = requests.Session()
    s.headers["User-Agent"] = UA
    r = s.get(url, headers={"Range": "bytes=0-1023"}, timeout=120)
    txt = r.text if r.headers.get("content-type", "").startswith("text/html") else ""
    if "anubis_challenge" not in txt:
        return s, url
    ch = json.loads(re.search(r'id="anubis_challenge"[^>]*>(\{.*?\})\s*</script>',
                              txt, re.S).group(1))
    diff, rd, cid = ch["rules"]["difficulty"], ch["challenge"]["randomData"], ch["challenge"]["id"]
    t0, nonce = time.time(), 0
    while True:
        h = hashlib.sha256(f"{rd}{nonce}".encode()).hexdigest()
        if h.startswith("0" * diff):
            break
        nonce += 1
    el = int((time.time() - t0) * 1000)
    s.get(f"{BASE}/.within.website/x/cmd/anubis/api/pass-challenge",
          params={"id": cid, "response": h, "nonce": nonce,
                  "redir": "/", "elapsedTime": el}, timeout=120,
          allow_redirects=False)
    print(f"[anubis] solved difficulty {diff} in {nonce} tries", file=sys.stderr)
    return s, url


def rng(s: requests.Session, url: str, start: int, end: int) -> bytes:
    r = s.get(url, headers={"Range": f"bytes={start}-{end}"}, timeout=300)
    r.raise_for_status()
    return r.content


def central_dir(s: requests.Session, url: str):
    probe = s.get(url, headers={"Range": "bytes=0-0"}, timeout=120)
    cr = probe.headers.get("Content-Range")
    if cr:
        size = int(cr.split("/")[1])
    else:
        size = int(probe.headers["Content-Length"])
    print(f"[zip] {size} bytes, content-range={cr}", file=sys.stderr)
    tail = rng(s, url, max(0, size - 300000), size - 1)
    # zip64 or classic EOCD
    i = tail.rfind(b"PK\x05\x06")
    cd_size, cd_off = struct.unpack("<II", tail[i + 12:i + 20])
    if cd_off == 0xFFFFFFFF:
        j = tail.rfind(b"PK\x06\x06")
        cd_size, cd_off = struct.unpack("<QQ", tail[j + 40:j + 56])
    cd = rng(s, url, cd_off, cd_off + cd_size - 1)
    out, p = [], 0
    while p < len(cd) and cd[p:p + 4] == b"PK\x01\x02":
        (method, _, _, crc, csize, usize, nlen, elen, clen, _, _, _,
         lho) = struct.unpack("<HHHIIIHHHHHII", cd[p + 10:p + 46])
        name = cd[p + 46:p + 46 + nlen].decode("utf-8", "replace")
        extra = cd[p + 46 + nlen:p + 46 + nlen + elen]
        if usize == 0xFFFFFFFF or csize == 0xFFFFFFFF or lho == 0xFFFFFFFF:
            q = 0
            while q + 4 <= len(extra):
                hid, hsz = struct.unpack("<HH", extra[q:q + 4])
                if hid == 1:
                    vals = list(struct.unpack(f"<{hsz // 8}Q", extra[q + 4:q + 4 + hsz]))
                    k = 0
                    if usize == 0xFFFFFFFF:
                        usize = vals[k]; k += 1
                    if csize == 0xFFFFFFFF:
                        csize = vals[k]; k += 1
                    if lho == 0xFFFFFFFF:
                        lho = vals[k]
                    break
                q += 4 + hsz
        out.append(dict(name=name, method=method, csize=csize, usize=usize, off=lho))
        p += 46 + nlen + elen + clen
    return size, out


def extract(s, url, e, outdir):
    hdr = rng(s, url, e["off"], e["off"] + 29)
    nlen, elen = struct.unpack("<HH", hdr[26:30])
    data_off = e["off"] + 30 + nlen + elen
    raw = rng(s, url, data_off, data_off + e["csize"] - 1)
    buf = zlib.decompress(raw, -15) if e["method"] == 8 else raw
    safe = e["name"].replace("/", "__")
    path = f"{outdir}/{safe}"
    with open(path, "wb") as fh:
        fh.write(buf)
    print(f"[get] {e['name']} -> {path} ({len(buf)} bytes)", file=sys.stderr)


def main():
    url, cmd = sys.argv[1], sys.argv[2]
    s, url = anubis_session(url)
    total, entries = central_dir(s, url)
    if cmd == "list":
        for e in sorted(entries, key=lambda x: -x["usize"]):
            print(f"{e['usize']:>12} unc {e['csize']:>12} cmp  {e['name']}")
        print(f"[n entries] {len(entries)}  [archive bytes] {total}", file=sys.stderr)
        return
    outdir, pats = sys.argv[3], sys.argv[4:]
    got = 0
    for e in entries:
        if e["usize"] and any(p.lower() in e["name"].lower() for p in pats):
            extract(s, url, e, outdir)
            got += len(open(f"{outdir}/{e['name'].replace('/', '__')}", 'rb').read())
    print(f"[total extracted] {got} bytes", file=sys.stderr)


if __name__ == "__main__":
    main()
