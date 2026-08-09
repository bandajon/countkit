#!/usr/bin/env bash
# Install the DeepStream python bindings (pyds) inside a DeepStream container.
#
# Up to DS 8.0 there is a prebuilt wheel and the container's own installer script
# fetches it. From DS 9.0 pyds is deprecated upstream — no wheel is published, so
# the bindings must be built from source. Both paths land in the same place:
# `import pyds` works, or this script fails the build.
set -euo pipefail

DS=/opt/nvidia/deepstream/deepstream

if python3 -c 'import pyds' 2>/dev/null; then
    echo "[pyds] already importable — nothing to do"; exit 0
fi

# The symlink target carries the version (deepstream-7.1); the version file is a
# fallback for images that don't use the versioned directory layout.
DS_VER=$(basename "$(readlink -f "$DS")" | sed 's/^deepstream-//')
case "$DS_VER" in
    [0-9]*.[0-9]*) ;;
    *) DS_VER=$(grep -oE '[0-9]+\.[0-9]+' "$DS/version" | head -1) ;;
esac
[ -n "$DS_VER" ] || { echo "[pyds] FATAL: cannot determine DeepStream version under $DS" >&2; exit 1; }
echo "[pyds] DeepStream $DS_VER"

# Prebuilt wheel releases: DS 7.1 -> pyds 1.2.0, DS 8.0 -> pyds 1.2.2.
case "$DS_VER" in
    7.1) PYDS_REL=1.2.0 ;;
    8.0) PYDS_REL=1.2.2 ;;
    *)   PYDS_REL="" ;;
esac

INSTALLER="$DS/user_deepstream_python_apps_install.sh"
if [ -n "$PYDS_REL" ] && [ -f "$INSTALLER" ]; then
    echo "[pyds] installing prebuilt wheel $PYDS_REL via $INSTALLER"
    bash "$INSTALLER" --version "$PYDS_REL"
else
    echo "[pyds] no prebuilt wheel for DS $DS_VER — building bindings from source"
    apt-get update
    apt-get install -y --no-install-recommends \
        git cmake g++ build-essential ninja-build meson m4 autoconf automake libtool \
        python3-dev python3-pip python3-gi python3-gst-1.0 python-gi-dev \
        libglib2.0-dev libglib2.0-dev-bin libgstreamer1.0-dev libcairo2-dev \
        libgirepository1.0-dev
    rm -rf /var/lib/apt/lists/*
    pip3 install --no-cache-dir build || pip3 install --no-cache-dir --break-system-packages build

    SRC=/tmp/deepstream_python_apps
    rm -rf "$SRC"
    git clone --depth 1 https://github.com/NVIDIA-AI-IOT/deepstream_python_apps "$SRC"
    # master is the only source for DS 9.x — no wheel is published and no tag tracks
    # the release. Record WHICH master went into this image: without it a build that
    # breaks tomorrow is neither reportable upstream nor reproducible here.
    git -C "$SRC" rev-parse HEAD > /opt/countkit-pyds-rev
    echo "[pyds] deepstream_python_apps master @ $(cat /opt/countkit-pyds-rev)"
    git -C "$SRC" submodule update --init --depth 1
    python3 "$SRC/bindings/3rdparty/git-partial-submodule/git-partial-submodule.py" restore-sparse

    # gst-python has to be built and installed before the bindings link against it.
    ( cd "$SRC/bindings/3rdparty/gstreamer/subprojects/gst-python" \
      && meson setup build && ninja -C build && ninja -C build install )

    export CMAKE_BUILD_PARALLEL_LEVEL="$(nproc)"
    export CMAKE_ARGS="-DDS_VERSION=$DS_VER -DDS_PATH=$(readlink -f "$DS")"
    ( cd "$SRC/bindings" && python3 -m build )
    pip3 install --no-cache-dir "$SRC"/bindings/dist/pyds-*.whl \
      || pip3 install --no-cache-dir --break-system-packages "$SRC"/bindings/dist/pyds-*.whl
    rm -rf "$SRC"
fi

python3 -c 'import pyds' || {
    REV=$(cat /opt/countkit-pyds-rev 2>/dev/null || echo "n/a — prebuilt wheel path")
    cat >&2 <<EOF
[pyds] FATAL: pyds still not importable after install on DeepStream $DS_VER.
       pyds is deprecated from DS 9.0 and the source build tracks the master
       branch of NVIDIA-AI-IOT/deepstream_python_apps, which may not yet support
       this DeepStream release. Source revision built here: $REV
       Either pin a DeepStream base image with a published wheel (7.1 or 8.0),
       or use the yolo profile.
EOF
    exit 1
}
echo "[pyds] ok"
