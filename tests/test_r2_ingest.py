#!/usr/bin/env python3
"""Plain stdlib test runner: python3 tests/test_r2_ingest.py — exits non-zero on failure.

A fake in-memory bucket stands in for R2; what is under test is the transport's own
promises — .verified moves last in both directions, downloads are hashed, a died or
corrupt transfer never plants a file, re-runs skip what already arrived."""

import base64
import hashlib
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import r2_ingest                                 # noqa: E402

FAILS = []
DATE, SITE = "2026-08-04", "gerlache"
TMP = Path(tempfile.mkdtemp(prefix="countkit-r2i-")).resolve()


def check(name, fn):
    try:
        fn()
        print(f"ok   {name}")
    except Exception as e:
        FAILS.append(name)
        print(f"FAIL {name}: {e}")


class FakeBody:
    def __init__(self, data):
        self.data = data

    def iter_chunks(self, size):
        for i in range(0, len(self.data), size):
            yield self.data[i:i + size]


class FakeR2:
    """The slice of the S3 API the transport uses, with R2's checksum behaviour."""

    def __init__(self):
        self.store = {}          # key -> bytes
        self.order = []          # keys in the order they were written/read
        self.corrupt_checksums = set()

    def put_object(self, Bucket, Key, Body, ChecksumSHA256):
        data = Body.read()
        assert ChecksumSHA256 == base64.b64encode(
            hashlib.sha256(data).digest()).decode(), "checksum did not match the body"
        self.store[Key] = data
        self.order.append(Key)

    def get_object(self, Bucket, Key, ChecksumMode):
        self.order.append(Key)
        data = self.store[Key]
        want = base64.b64encode(hashlib.sha256(data).digest()).decode()
        if Key in self.corrupt_checksums:
            want = base64.b64encode(hashlib.sha256(data + b"x").digest()).decode()
        return {"Body": FakeBody(data), "ChecksumSHA256": want}

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        # Two pages on purpose: the pagination loop is part of the contract.
        keys = sorted(k for k in self.store if k.startswith(Prefix))
        page, rest = keys[:2], keys[2:]
        if ContinuationToken:
            page, rest = keys[2:], []
        return {"Contents": [{"Key": k, "Size": len(self.store[k])} for k in page],
                "IsTruncated": bool(rest), "NextContinuationToken": "page2"}


def tree(root):
    d = root / DATE / SITE
    (d / "cam1").mkdir(parents=True)
    (d / "cam1" / "20260804-070000.mkv").write_bytes(b"m" * 2048)
    (d / "cam1" / "20260804-071000.mkv").write_bytes(b"n" * 1024)
    (d / "manifest.json").write_text('{"time_offset_s": {"cam1": 0.0}}')
    (d / ".verified").touch()
    return d


SRC = TMP / "src-ingest"
DST = TMP / "dst-ingest"
tree(SRC)
R2 = FakeR2()


def push_uploads_verified_last():
    r2_ingest.push(R2, "b", SRC, DATE, SITE)
    assert len(R2.store) == 4, sorted(R2.store)
    assert R2.order[-1] == f"ingest/{DATE}/{SITE}/.verified", R2.order
    assert R2.store[f"ingest/{DATE}/{SITE}/cam1/20260804-070000.mkv"] == b"m" * 2048


def push_again_skips_everything():
    n = len(R2.order)
    r2_ingest.push(R2, "b", SRC, DATE, SITE)
    assert len(R2.order) == n, "an unchanged file was re-uploaded"


def pull_restores_the_tree_verified_last():
    R2.order.clear()
    r2_ingest.pull(R2, "b", DST, DATE, SITE)
    got = sorted(p.relative_to(DST).as_posix() for p in DST.rglob("*") if p.is_file())
    want = sorted(p.relative_to(SRC).as_posix() for p in SRC.rglob("*") if p.is_file())
    assert got == want, (got, want)
    assert (DST / DATE / SITE / "cam1" / "20260804-070000.mkv").read_bytes() == b"m" * 2048
    assert R2.order[-1] == f"ingest/{DATE}/{SITE}/.verified", R2.order
    assert not list(DST.rglob("*.part")), "a .part file survived a clean pull"


def pull_again_skips_everything():
    R2.order.clear()
    r2_ingest.pull(R2, "b", DST, DATE, SITE)
    assert R2.order == [], "an already-local file was re-downloaded"


def corrupt_download_plants_nothing():
    bad = TMP / "bad-ingest"
    R2.corrupt_checksums = {f"ingest/{DATE}/{SITE}/cam1/20260804-070000.mkv"}
    try:
        r2_ingest.pull(R2, "b", bad, DATE, SITE)
        raise AssertionError("a corrupt download was accepted")
    except SystemExit as e:
        assert "sha256 mismatch" in str(e), e
    finally:
        R2.corrupt_checksums = set()
    assert not (bad / DATE / SITE / ".verified").exists(), \
        "a failed pull left the site-day looking verified"
    assert not list(bad.rglob("*.part")), "a corrupt .part file was left behind"


def empty_bucket_and_empty_tree_fail_loudly():
    try:
        r2_ingest.pull(R2, "b", DST, DATE, "nosite")
        raise AssertionError("an empty prefix pulled successfully")
    except SystemExit as e:
        assert "push it" in str(e), e
    try:
        r2_ingest.push(R2, "b", TMP / "empty-ingest", DATE, SITE)
        raise AssertionError("an empty tree pushed successfully")
    except SystemExit as e:
        assert "nothing to push" in str(e), e


check("push uploads the tree, .verified last", push_uploads_verified_last)
check("a second push skips unchanged files", push_again_skips_everything)
check("pull restores the tree byte-identical, .verified last", pull_restores_the_tree_verified_last)
check("a second pull downloads nothing", pull_again_skips_everything)
check("a corrupt download is refused and plants no marker", corrupt_download_plants_nothing)
check("empty bucket and empty tree fail loudly", empty_bucket_and_empty_tree_fail_loudly)

print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
sys.exit(1 if FAILS else 0)
