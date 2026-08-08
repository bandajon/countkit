#!/usr/bin/env python3
"""Plain stdlib test runner: python3 tests/test_skeleton.py — exits non-zero on failure."""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Must be set before importing app: the first-boot config copy happens at import time.
TMP = Path(tempfile.mkdtemp(prefix="countkit-test-")).resolve()   # macOS /var -> /private/var
os.environ["COUNTKIT_ROOT"] = str(TMP)

import app                                      # noqa: E402
from fastapi.testclient import TestClient       # noqa: E402

C = TestClient(app.app)
FAILS = []


def check(name, fn):
    try:
        fn()
        print(f"ok   {name}")
    except Exception as e:
        FAILS.append(name)
        print(f"FAIL {name}: {e}")


def config_autocreated():
    assert app.CONFIG_PATH == TMP / "config.yaml"
    assert app.CONFIG_PATH.exists(), "first boot must copy config.example.yaml"
    for k in ("ingest_root", "data_root", "site_tz", "classes", "pcu", "nvinfer_config",
              "probe", "r2", "google_routes", "branding"):
        assert k in app.CONFIG, f"config.example.yaml is missing {k}"


def status_route():
    s = C.get("/api/status").json()
    assert s["hostname"], s
    assert isinstance(s["disk_free_gb"], float), s
    assert s["active_job"] is None and "temp_c" in s["gpu"], s


def ingest_empty():
    app.CONFIG["ingest_root"] = "./no-such-ingest"
    assert C.get("/api/ingest").json() == [], "a missing ingest tree is empty, not an error"


def ingest_fixture():
    tree = TMP / "ingest" / "2026-08-04" / "gerlache" / "cam1"
    tree.mkdir(parents=True)
    (tree / "0500.mkv").write_bytes(b"")
    (tree.parent / ".verified").touch()
    (tree.parent / "manifest.json").write_text("{}")
    (TMP / "ingest" / "2026-08-03" / "gerlache" / "cam1").mkdir(parents=True)
    app.CONFIG["ingest_root"] = "./ingest"
    rows = C.get("/api/ingest").json()
    assert [r["date"] for r in rows] == ["2026-08-04", "2026-08-03"], rows   # newest first
    assert rows[0] == {"date": "2026-08-04", "site": "gerlache", "cams": ["cam1"],
                       "verified": True, "manifest": True}, rows[0]
    assert rows[1]["verified"] is False and rows[1]["manifest"] is False, rows[1]


def bad_yaml_rejected():
    before = app.CONFIG_PATH.read_text()
    r = C.post("/api/config", json={"text": "classes: [unclosed\n"})
    assert r.status_code == 400, r.status_code
    r = C.post("/api/config", json={"text": "classes: not-a-list\n"})
    assert r.status_code == 400, r.status_code
    assert app.CONFIG_PATH.read_text() == before, "a rejected config must not be written"
    assert C.get("/api/config").json()["text"] == before


def index_served():
    html = C.get("/").text
    for tab in ("Label", "Analyze", "Counts", "Report"):
        assert f">{tab}</a>" in html, tab
    assert "#111417" in html and "Africa/Lusaka" in html


check("config.yaml auto-created from the example", config_autocreated)
check("/api/status reports host, disk, gpu", status_route)
check("/api/ingest tolerates a missing root", ingest_empty)
check("/api/ingest reads a verified site-day", ingest_fixture)
check("POST /api/config rejects bad YAML without clobbering", bad_yaml_rejected)
check("index.html serves all four tabs", index_served)

print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
sys.exit(1 if FAILS else 0)
