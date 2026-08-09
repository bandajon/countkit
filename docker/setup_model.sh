#!/usr/bin/env bash
# Write an nvinfer config for TrafficCamNet into the operator's tree and print the
# config.yaml line that points at it.
#
# The config is never hand-authored: it is the DeepStream sample's own
# config_infer_primary.txt with its relative model paths rewritten absolute. The
# weights come from the container's bundled Primary_Detector when present, else
# from NGC. Idempotent — an existing config is printed and left alone.
set -euo pipefail

DS=/opt/nvidia/deepstream/deepstream
OUT="${COUNTKIT_ROOT:-/countkit}/data/models/trafficcamnet"
CFG="$OUT/config_infer_trafficcamnet.txt"
REF="$DS/samples/configs/deepstream-app/config_infer_primary.txt"
BUNDLED="$DS/samples/models/Primary_Detector"
NGC=https://api.ngc.nvidia.com/v2/models/nvidia/tao/trafficcamnet/versions/pruned_onnx_v1.0.4/files

announce() { echo; echo "[countkit] put this in config.yaml:"; echo "    nvinfer_config: $CFG"; echo; }

if [ -f "$CFG" ]; then
    echo "[countkit] $CFG exists — leaving it alone"; announce; exit 0
fi

[ -f "$REF" ] || { echo "[countkit] FATAL: no reference nvinfer config at $REF — this image is not a DeepStream container, or the samples were stripped. Set nvinfer_config yourself." >&2; exit 1; }

mkdir -p "$OUT"

if [ -f "$BUNDLED/resnet18_trafficcamnet_pruned.onnx" ]; then
    echo "[countkit] using bundled model at $BUNDLED"
    MODELDIR="$BUNDLED"
    DOWNLOADED=0
else
    echo "[countkit] no bundled model — fetching pruned_onnx_v1.0.4 from NGC"
    curl -fSL -o "$OUT/resnet18_trafficcamnet_pruned.onnx" "$NGC/resnet18_trafficcamnet_pruned.onnx"
    curl -fSL -o "$OUT/labels.txt" "$NGC/labels.txt"
    MODELDIR="$OUT"
    DOWNLOADED=1
fi

# The sample config addresses its model as ../../models/Primary_Detector/*.
sed "s#\.\./\.\./models/Primary_Detector#$MODELDIR#g" "$REF" > "$CFG"
# TensorRT builds the engine on first run; park it on the operator's volume so the
# next container start doesn't pay for it again.
sed -i -E "s#^(model-engine-file=).*/#\1$OUT/#" "$CFG"

if [ "$DOWNLOADED" = 1 ]; then
    # The NGC download has no INT8 calibration table, so INT8 would fail at engine
    # build. FP16 is the honest fallback.
    sed -i -E '/^int8-calib-file=/d; s/^network-mode=.*/network-mode=2/' "$CFG"
    echo "[countkit] no INT8 calibration table with the NGC download — set network-mode=2 (FP16)"
fi

# If the sample ever addresses its model some other way, the rewrite above misses
# it and nvinfer would resolve the leftovers against whatever cwd it is started
# in. Better to say so here than to fail at engine build.
if grep -qE '^[a-z-]+file[a-z-]*=\.\.' "$CFG"; then
    echo "[countkit] FATAL: relative model paths left in $CFG after rewrite:" >&2
    grep -nE '^[a-z-]+file[a-z-]*=\.\.' "$CFG" >&2
    rm -f "$CFG"
    exit 1
fi

echo "[countkit] wrote $CFG"
announce
