#!/usr/bin/env python3
"""Plain stdlib test runner: python3 tests/test_label_dom.py — exits non-zero on failure.

Crude on purpose: it only catches a gutted or unwired Label section. The drawing
model itself is only checkable in a browser."""

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["COUNTKIT_ROOT"] = tempfile.mkdtemp(prefix="countkit-dom-")

import app                                      # noqa: E402
from fastapi.testclient import TestClient       # noqa: E402

HTML = TestClient(app.app).get("/").text
SECTION = re.search(r'<section id="label">.*?</section>', HTML, re.S).group(0)
FAILS = []


def check(name, fn):
    try:
        fn()
        print(f"ok   {name}")
    except Exception as e:
        FAILS.append(name)
        print(f"FAIL {name}: {e}")


def controls_present():
    for s in ("save as new version", "flip direction", "draw", "pan", "arm map",
              "activate this version", "delete gate", "undo pt", "done", "entry", "exit"):
        assert s in SECTION.lower(), s
    assert '<canvas id="gatecanvas"' in SECTION


def wired_to_the_backend():
    for route in ("/api/ingest", "/api/frame/", "/api/calib/", "/armmap", "/versions", "/activate"):
        assert route in HTML, route
    assert "image_size" in HTML and "'AB'" in HTML, "the calibration doc shape must be built here"


def states_have_copy():
    for s in ("unsaved changes", "read-only", "double-owned", "fully covered",
              "upload", "count twice"):
        assert s in HTML.lower(), s


def save_failures_read_the_body_safely():
    # A 500 answers text/plain, so `(await r.json()).detail` shows the operator nothing.
    # detail() falls back to the status text; every save path must go through it.
    assert "return (await r.json()).detail; } catch (e) { return r.statusText; }" in HTML
    assert "if (!r.ok) { $('l-res').textContent = await detail(r); return; }" in HTML
    assert "(await r.json()).detail" not in HTML.replace(
        "return (await r.json()).detail;", ""), "an error path still assumes {detail}"


check("Label section carries every control", controls_present)
check("save failures read the body through detail()", save_failures_read_the_body_safely)
check("Label calls the calibration routes", wired_to_the_backend)
check("Label states carry their copy", states_have_copy)

print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
sys.exit(1 if FAILS else 0)
