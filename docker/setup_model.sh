#!/usr/bin/env bash
# Write an nvinfer config for TrafficCamNet into the operator's tree and print the
# config.yaml line that points at it.
#
# The config is never hand-authored: it is the DeepStream sample's own
# config_infer_primary.txt with its relative model paths rewritten absolute. The
# weights come from the container's bundled Primary_Detector when that is complete,
# else from NGC. Idempotent — an existing config is printed and left alone.
set -euo pipefail

DS=/opt/nvidia/deepstream/deepstream
OUT="${COUNTKIT_ROOT:-/countkit}/data/models/trafficcamnet"
CFG="$OUT/config_infer_trafficcamnet.txt"
REF="$DS/samples/configs/deepstream-app/config_infer_primary.txt"
BUNDLED="$DS/samples/models/Primary_Detector"
NGC=https://api.ngc.nvidia.com/v2/models/nvidia/tao/trafficcamnet/versions/pruned_onnx_v1.0.4/files

announce() { echo; echo "[countkit] put this in config.yaml:"; echo "    nvinfer_config: $CFG"; echo; }

# $1 = directory holding the model files, $2 = optional extra sed -E expression.
# The sample config addresses its model as ../../models/Primary_Detector/*, and
# TensorRT builds the engine on first run — park that on the operator's volume so
# the next container start doesn't pay for it again.
write_cfg() {
    sed "s#\.\./\.\./models/Primary_Detector#$1#g" "$REF" \
      | sed -E "s#^(model-engine-file=).*/#\1$OUT/#; ${2:-}" > "$CFG"
}

# Every absolute path the config points at that isn't on disk. model-engine-file is
# excluded: it is the build output, and is meant to be missing on a first run.
missing_files() {
    grep -E '^[a-z][a-z0-9_-]*=/' "$CFG" \
      | grep -v '^model-engine-file=' \
      | sed 's/^[^=]*=//' \
      | while read -r p; do [ -e "$p" ] || echo "$p"; done
}

if [ -f "$CFG" ]; then
    echo "[countkit] $CFG exists — leaving it alone"; announce; exit 0
fi

[ -f "$REF" ] || { echo "[countkit] FATAL: no reference nvinfer config at $REF — this image is not a DeepStream container, or the samples were stripped. Set nvinfer_config yourself." >&2; exit 1; }

mkdir -p "$OUT"
# A half-written config must never survive: the next run would find it, trust it,
# and hand the operator a path to a model that isn't there.
trap 'rm -f "$CFG" "$OUT"/*.part' ERR

write_cfg "$BUNDLED"
if [ -z "$(missing_files)" ]; then
    echo "[countkit] using bundled model at $BUNDLED"
else
    # Whatever the config actually asks for gets downloaded by that same name — the
    # basenames are never guessed. No INT8 calibration table ships with the NGC
    # files, so INT8 would fail at engine build; FP16 is the honest fallback.
    echo "[countkit] bundled model incomplete at $BUNDLED — fetching pruned_onnx_v1.0.4 from NGC"
    write_cfg "$OUT" '/^int8-calib-file=/d; s/^network-mode=.*/network-mode=2/'
    # Download beside the target and rename: an interrupted curl leaves a partial file
    # that the next boot's existence check accepts as a complete model, and the lie is
    # only found much later, inside the TensorRT engine build.
    for p in $(missing_files); do
        curl -fSL -o "$p.part" "$NGC/$(basename "$p")"
        mv "$p.part" "$p"
    done
    echo "[countkit] model in $OUT, network-mode=2 (FP16 — no calibration table)"
fi

# If the sample ever addresses its model some other way, the rewrite above misses
# it and nvinfer would resolve the leftovers against whatever cwd it is started
# in. Better to say so here than to fail at engine build.
if grep -qE '^[a-z][a-z0-9_-]*=\.\.' "$CFG"; then
    echo "[countkit] FATAL: relative model paths left in $CFG after rewrite:" >&2
    grep -nE '^[a-z][a-z0-9_-]*=\.\.' "$CFG" >&2
    rm -f "$CFG"
    exit 1
fi

LEFT=$(missing_files)
if [ -n "$LEFT" ]; then
    echo "[countkit] FATAL: $CFG points at files that do not exist:" >&2
    echo "$LEFT" >&2
    rm -f "$CFG"
    exit 1
fi

echo "[countkit] wrote $CFG"
announce
