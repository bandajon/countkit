#!/usr/bin/env python3
"""Move footage site-days through the R2 bucket: push from the machine that has the
drive, pull on the machine that will analyze. Run: python3 r2_ingest.py <cmd>

The bucket is transport, not truth. A pull plants the .verified marker only after
every other file has landed and checked out, so engine.check_ready keeps meaning what
it means — a site-day that half-arrived is not analyzable. The manifest (and with it
every clock offset) travels inside the tree like any other file.

Keys live under ingest/<date>/<site>/...; the crops the offload sweep uploads live
under <site>/<date>/... in the same bucket, and the two trees never collide.

Commands (COUNTKIT_ROOT and config.yaml resolve exactly as for app.py):
  list                  site-days present in the bucket
  push <date> <site>    local ingest tree -> bucket
  pull <date> <site>    bucket -> local ingest tree
"""

import base64
import hashlib
import os
import sys
from pathlib import Path

import yaml

import offload

PREFIX = "ingest/"
ROOT = Path(os.environ.get("COUNTKIT_ROOT") or Path(__file__).resolve().parent)


def site_keys(client, bucket, date, site):
    """Every key under ingest/<date>/<site>/ with its size, across pages."""
    out, token = {}, None
    while True:
        kw = {"Bucket": bucket, "Prefix": f"{PREFIX}{date}/{site}/"}
        if token:
            kw["ContinuationToken"] = token
        r = client.list_objects_v2(**kw)
        for o in r.get("Contents") or []:
            out[o["Key"]] = o["Size"]
        if not r.get("IsTruncated"):
            return out
        token = r.get("NextContinuationToken")


def _verified_last(names, is_verified):
    """The marker moves last in BOTH directions: whoever sees .verified must be
    seeing a complete tree."""
    return sorted(names, key=lambda n: (is_verified(n), n))


def push(client, bucket, root, date, site):
    d = Path(root) / date / site
    files = [p for p in d.rglob("*") if p.is_file()]
    if not files:
        sys.exit(f"{d}: nothing to push")
    have = site_keys(client, bucket, date, site)
    sent = skipped = 0
    for p in _verified_last(files, lambda p: p.name == ".verified"):
        key = PREFIX + p.relative_to(root).as_posix()
        if have.get(key) == p.stat().st_size:
            skipped += 1
            continue
        # R2 recomputes the sha256 server-side and rejects a PUT that disagrees.
        with open(p, "rb") as body:
            client.put_object(Bucket=bucket, Key=key, Body=body,
                              ChecksumSHA256=offload.sha256_b64(p))
        sent += 1
        print(f"push {key}")
    print(f"{site} {date}: {sent} uploaded · {skipped} already in the bucket")


def pull(client, bucket, root, date, site):
    have = site_keys(client, bucket, date, site)
    if not have:
        sys.exit(f"nothing in the bucket for {site} {date} — push it from the "
                 "machine that has the footage")
    got = skipped = 0
    for key in _verified_last(have, lambda k: k.endswith("/.verified")):
        p = Path(root) / key[len(PREFIX):]
        if p.is_file() and p.stat().st_size == have[key]:
            skipped += 1
            continue
        r = client.get_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
        p.parent.mkdir(parents=True, exist_ok=True)
        # Stream to a .part file while hashing — a segment can be hundreds of MB and
        # the Nano's RAM is not the place to hold one; the rename below means a died
        # download never masquerades as a landed file.
        tmp, h = p.with_name(p.name + ".part"), hashlib.sha256()
        with open(tmp, "wb") as f:
            for chunk in r["Body"].iter_chunks(1 << 20):
                f.write(chunk)
                h.update(chunk)
        want = r.get("ChecksumSHA256") or ""
        if want and base64.b64encode(h.digest()).decode() != want:
            tmp.unlink()
            sys.exit(f"{key}: sha256 mismatch on download — refusing to keep it "
                     "(re-run the pull)")
        tmp.replace(p)
        got += 1
        print(f"pull {key[len(PREFIX):]}")
    print(f"{site} {date}: {got} downloaded · {skipped} already local")


def list_site_days(client, bucket):
    out, token = {}, None
    while True:
        kw = {"Bucket": bucket, "Prefix": PREFIX}
        if token:
            kw["ContinuationToken"] = token
        r = client.list_objects_v2(**kw)
        for o in r.get("Contents") or []:
            parts = o["Key"][len(PREFIX):].split("/")
            if len(parts) >= 2:
                d = out.setdefault((parts[0], parts[1]), {"files": 0, "verified": False})
                d["files"] += 1
                d["verified"] |= parts[-1] == ".verified"
        if not r.get("IsTruncated"):
            break
        token = r.get("NextContinuationToken")
    if not out:
        print("bucket has no footage under ingest/ yet")
    for (date, site), d in sorted(out.items(), reverse=True):
        print(f"{date}  {site:20} {d['files']:4} files"
              + ("" if d["verified"] else "  (incomplete — no .verified)"))


def main(argv):
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text()) or {}
    o = cfg.get("r2") or {}
    try:
        client = offload.make_client(o)
    except ValueError as e:
        sys.exit(f"r2 {e} (in config.yaml)")
    bucket = o.get("bucket") or "countkit-crops"
    root = ROOT / cfg.get("ingest_root", "./ingest")
    if argv[:1] == ["list"]:
        list_site_days(client, bucket)
    elif len(argv) == 3 and argv[0] in ("push", "pull"):
        (push if argv[0] == "push" else pull)(client, bucket, root, *argv[1:])
    else:
        sys.exit(__doc__.strip())


if __name__ == "__main__":
    main(sys.argv[1:])
