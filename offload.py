#!/usr/bin/env python3
"""Optional Cloudflare R2 offload for evidence crops.

Idle unless config.yaml sets r2.enabled — a node that keeps its own crops never
talks to a cloud.

Deletion here is deliberately stricter than FieldKit's. Footage segments are bulk
and replaceable; a crop is the audit evidence behind one counted vehicle, and the
UI reads it constantly. So the local copy goes only when BOTH hold: the upload was
verified, AND the disk is below the floor. Above the floor we upload and keep — the
cloud copy is insurance, not a filing cabinet.
"""

import base64
import hashlib
import shutil
import threading
import time
from pathlib import Path

FINISHED = 60          # a crop still being written is younger than this
SWEEP_SECONDS = 60


def sha256_b64(path):
    """R2 verifies this server-side and rejects the PUT if the bytes disagree."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return base64.b64encode(h.digest()).decode()


class Offload:
    def __init__(self, cfg, data_root):
        self.cfg = cfg              # the live CONFIG dict: config edits land without a reload
        self.crop_root = Path(data_root) / "crops"
        self.client = None
        self.uploaded = 0
        self.deleted = 0
        self.last_key = ""
        self.last_error = ""
        self.done = set()           # keys verified this process, so a second sweep can delete

    def opts(self):
        return self.cfg.get("r2") or {}

    def info(self):
        return {"enabled": bool(self.opts().get("enabled")), "uploaded": self.uploaded,
                "deleted": self.deleted, "last_key": self.last_key,
                "last_error": self.last_error}

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                self.sweep()
            except Exception as e:          # a bad sweep must not kill the thread
                self.last_error = f"{type(e).__name__}: {e}"
                print(f"offload: {self.last_error}", flush=True)
            time.sleep(SWEEP_SECONDS)

    def free_gb(self):
        return shutil.disk_usage(self.crop_root).free / 1e9

    def finished(self):
        """Crops the writer has closed, oldest first: <site>/<date>/<cam>/<file>.jpg."""
        cutoff = time.time() - FINISHED
        files = [f for f in self.crop_root.glob("*/*/*/*.jpg")
                 if f.stat().st_mtime < cutoff and f.stat().st_size]
        return sorted(files, key=lambda f: f.stat().st_mtime)

    def _get_client(self, o):
        if self.client is not None:
            return self.client
        missing = [k for k in ("account_id", "access_key_id", "secret_access_key")
                   if not o.get(k)]
        if missing:
            self.last_error = "r2.enabled but config is missing: " + ", ".join(missing)
            return None
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            self.last_error = "r2.enabled but boto3 missing — pip install boto3"
            print(f"offload: {self.last_error}", flush=True)
            return None
        self.client = boto3.client(
            "s3", endpoint_url=f"https://{o['account_id']}.r2.cloudflarestorage.com",
            aws_access_key_id=o["access_key_id"],
            aws_secret_access_key=o["secret_access_key"], region_name="auto",
            # boto3 >= 1.36 adds its own CRC32 by default, which this S3-compatible
            # endpoint rejects; send only the sha256 we compute.
            config=Config(request_checksum_calculation="when_required",
                          response_checksum_validation="when_required"))
        return self.client

    def sweep(self):
        """Upload every closed crop oldest-first. Delete as we go only while the disk
        sits below the floor, and stop deleting the moment it is back above it."""
        o = self.opts()
        if not o.get("enabled"):
            return
        client = self._get_client(o)
        if client is None:
            return
        bucket = o.get("bucket") or "countkit-crops"
        floor = float(o.get("min_free_gb", 20))
        pressure = self.free_gb() < floor
        for f in self.finished():
            key = "/".join(f.parts[-4:])          # <site>/<date>/<cam>/<crop>.jpg
            if key not in self.done:
                try:
                    with open(f, "rb") as body:
                        client.put_object(Bucket=bucket, Key=key, Body=body,
                                          ChecksumSHA256=sha256_b64(f))
                except Exception as e:
                    # Any failure — network, credentials, checksum mismatch — keeps the
                    # crop. Evidence is only ever deleted after a verified upload.
                    self.last_error = f"{key}: {e}"
                    print(f"offload: {self.last_error}", flush=True)
                    return
                self.done.add(key)
                self.uploaded += 1
                self.last_key, self.last_error = key, ""
            if not pressure:
                continue          # verified copy in the cloud, local copy stays for the UI
            f.unlink()
            self.deleted += 1
            if self.free_gb() >= floor:
                pressure = False  # back above the floor: keep uploading, stop deleting


if __name__ == "__main__":
    import os
    import tempfile

    class FakeClient:
        """Records puts and checks the checksum the way R2 does."""
        def __init__(self, fail=False):
            self.puts, self.fail = [], fail

        def put_object(self, **kw):
            if self.fail:
                raise RuntimeError("boom")
            body = kw["Body"].read()
            assert kw["ChecksumSHA256"] == base64.b64encode(
                hashlib.sha256(body).digest()).decode(), "checksum did not match the body"
            self.puts.append(kw["Key"])

    def tree():
        root = Path(tempfile.mkdtemp())
        d = root / "crops" / "site1" / "2026-08-04" / "cam1"
        d.mkdir(parents=True)
        old = time.time() - FINISHED - 600
        for i, name in enumerate(("a.jpg", "b.jpg")):
            (d / name).write_bytes(b"x" * (10 + i))
            os.utime(d / name, (old + i * 60, old + i * 60))
        (d / "fresh.jpg").write_bytes(b"live")     # still being written
        return root, d

    root, d = tree()
    o = Offload({"r2": {"enabled": True, "bucket": "b"}}, root)
    o.client, o.free_gb = FakeClient(), lambda: 99.0
    o.sweep()
    assert o.client.puts == ["site1/2026-08-04/cam1/a.jpg", "site1/2026-08-04/cam1/b.jpg"]
    assert o.deleted == 0 and (d / "a.jpg").exists(), "deleted with disk space to spare"
    assert (d / "fresh.jpg").exists(), "uploaded a crop still being written"

    print("offload self-check ok — see tests/test_offload.py for the full set")
