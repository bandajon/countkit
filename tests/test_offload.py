#!/usr/bin/env python3
"""Plain stdlib test runner: python3 tests/test_offload.py — exits non-zero on failure."""

import base64
import hashlib
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import offload                                   # noqa: E402

FAILS = []
KEYS = ["site1/2026-08-04/cam1/a.jpg", "site1/2026-08-04/cam1/b.jpg"]


def check(name, fn):
    try:
        fn()
        print(f"ok   {name}")
    except Exception as e:
        FAILS.append(name)
        print(f"FAIL {name}: {e}")


class FakeClient:
    """Records puts and verifies the checksum exactly as R2 does server-side."""

    def __init__(self, fail=False):
        self.puts, self.fail = [], fail
        self.present = {}        # key -> size already in the bucket

    def head_object(self, Bucket, Key):
        if Key not in self.present:
            raise RuntimeError("An error occurred (404) calling HeadObject")
        return {"ContentLength": self.present[Key]}

    def put_object(self, **kw):
        if self.fail:
            raise RuntimeError("boom")
        body = kw["Body"].read()
        want = base64.b64encode(hashlib.sha256(body).digest()).decode()
        assert kw["ChecksumSHA256"] == want, "checksum did not match the uploaded bytes"
        assert kw["Bucket"], "no bucket"
        self.puts.append(kw["Key"])


def tree():
    """Two closed crops (a.jpg older than b.jpg) plus one the writer still has open."""
    root = Path(tempfile.mkdtemp(prefix="countkit-off-"))
    d = root / "crops" / "site1" / "2026-08-04" / "cam1"
    d.mkdir(parents=True)
    old = time.time() - offload.FINISHED - 600
    for i, name in enumerate(("a.jpg", "b.jpg")):
        (d / name).write_bytes(b"x" * (10 + i))
        os.utime(d / name, (old + i * 60, old + i * 60))
    (d / "fresh.jpg").write_bytes(b"still being written")
    return root, d


def rig(free, fail=False, cfg=None):
    root, d = tree()
    # `cfg or ...` would swallow the empty-config case, which is the no-op we test.
    default = {"r2": {"enabled": True, "bucket": "b", "min_free_gb": 20}}
    o = offload.Offload(default if cfg is None else cfg, root)
    o.client = FakeClient(fail)
    seq = list(free) if isinstance(free, list) else None
    o.free_gb = (lambda: seq.pop(0) if seq else 99.0) if seq else (lambda: free)
    return o, d


# ---- above the floor: the cloud copy is insurance, the local copy stays ----

def above_the_floor_uploads_but_never_deletes():
    o, d = rig(99.0)
    o.sweep()
    assert o.client.puts == KEYS, o.client.puts          # oldest first
    assert o.uploaded == 2 and o.deleted == 0, o.info()
    assert (d / "a.jpg").exists() and (d / "b.jpg").exists(), "deleted with space to spare"
    assert not o.last_error, o.last_error


def a_crop_still_being_written_is_left_alone():
    o, d = rig(0.0)
    o.sweep()
    assert "fresh.jpg" not in " ".join(o.client.puts), o.client.puts
    assert (d / "fresh.jpg").exists(), "uploaded and deleted a half-written crop"


# ---- below the floor: upload then delete, oldest first, stop at the floor ----

def below_the_floor_deletes_what_it_verified():
    o, d = rig(0.0)
    o.sweep()
    assert o.client.puts == KEYS, o.client.puts
    assert o.deleted == 2, o.info()
    assert not (d / "a.jpg").exists() and not (d / "b.jpg").exists(), "kept a shipped crop"


def deleting_stops_the_moment_the_floor_is_met():
    # Empty at entry, healthy again after the first delete.
    o, d = rig([0.0, 99.0])
    o.sweep()
    assert o.client.puts == KEYS, "uploading must continue past the floor"
    assert o.deleted == 1, o.info()
    assert not (d / "a.jpg").exists(), "the oldest crop should go first"
    assert (d / "b.jpg").exists(), "kept deleting after the disk recovered"


# ---- failures keep the evidence ----

def a_failed_upload_keeps_the_crop():
    o, d = rig(0.0, fail=True)
    o.sweep()
    assert sorted(f.name for f in d.glob("*.jpg")) == ["a.jpg", "b.jpg", "fresh.jpg"]
    assert o.uploaded == 0 and o.deleted == 0, o.info()
    assert "boom" in o.last_error, o.last_error
    assert o.info()["last_error"], "the failure must reach /api/status"


def enabled_but_unconfigured_explains_itself():
    o, d = rig(0.0, cfg={"r2": {"enabled": True}})
    o.client = None                     # force the real client path
    o.sweep()
    assert "account_id" in o.last_error, o.last_error
    assert (d / "a.jpg").exists(), "dropped a crop without a working client"


# ---- disabled is silent ----

def disabled_is_a_no_op():
    for cfg in ({}, {"r2": {"enabled": False, "min_free_gb": 20}}):
        o, d = rig(0.0, cfg=cfg)
        o.sweep()
        assert o.client.puts == [], cfg
        assert o.info()["enabled"] is False, o.info()
        assert not o.last_error, o.last_error
        assert (d / "a.jpg").exists(), "an offload-disabled node deleted a crop"


def a_second_sweep_does_not_re_upload():
    o, d = rig(99.0)
    o.sweep()
    o.sweep()
    assert o.client.puts == KEYS, "re-uploaded crops that were already verified"


def a_key_already_in_the_bucket_survives_a_restart():
    # The done-set is process-local: after a restart the HEAD is what stops the whole
    # retained corpus going back up the field link.
    o, d = rig(99.0)
    o.client.present = {KEYS[0]: 10}          # a.jpg is 10 bytes, shipped last boot
    o.sweep()
    assert o.client.puts == [KEYS[1]], o.client.puts
    assert o.uploaded == 1, o.info()
    # A size that disagrees is not the same crop — upload it.
    o2, _ = rig(99.0)
    o2.client.present = {KEYS[0]: 9}
    o2.sweep()
    assert o2.client.puts == KEYS, o2.client.puts


check("above the floor: uploads, never deletes", above_the_floor_uploads_but_never_deletes)
check("a crop still being written is skipped", a_crop_still_being_written_is_left_alone)
check("below the floor: deletes only what it verified", below_the_floor_deletes_what_it_verified)
check("deleting stops at the floor, uploading does not", deleting_stops_the_moment_the_floor_is_met)
check("a failed upload keeps the crop and surfaces the error", a_failed_upload_keeps_the_crop)
check("enabled but unconfigured names the missing key", enabled_but_unconfigured_explains_itself)
check("disabled is a silent no-op", disabled_is_a_no_op)
check("an already-verified crop is not re-sent", a_second_sweep_does_not_re_upload)
check("a crop already in the bucket is not re-uploaded after a restart",
      a_key_already_in_the_bucket_survives_a_restart)

print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
sys.exit(1 if FAILS else 0)
