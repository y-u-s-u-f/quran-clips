#!/bin/bash
# nodefx.sh -- reproduce the Node Video "Glow + Glow Scan + Snow + Heat Wave"
# stack in ffmpeg.  See FX_RECIPE.md for the parameter mapping.
#
#   nodefx <in.mp4> <out.mp4> <#RRGGBB> [band_y] [band_h]
#
# band_y/band_h describe the letterboxed footage rectangle; the FX are confined
# to it (the reference reels show ZERO glow/particle bleed into the black bars).
# Pass 0 and the full height to treat the whole frame as the band.

set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${PY:-$HERE/venv/bin/python}"
FF="${FF:-ffmpeg}"

IN="$1"; OUT="$2"; TINT="${3:-#C9A227}"

W=$(ffprobe -v error -select_streams v:0 -show_entries stream=width  -of csv=p=0 "$IN")
H=$(ffprobe -v error -select_streams v:0 -show_entries stream=height -of csv=p=0 "$IN")
FPS=$(ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate -of csv=p=0 "$IN" | awk -F/ '{printf "%.4f", $1/$2}')
BY="${4:-0}"; BH="${5:-$H}"

# ---- tunables (defaults = the tutorial's settings, remapped -- see recipe) ---
GLOW_LO=${GLOW_LO:-0.20}       # Threshold range low knee
GLOW_HI=${GLOW_HI:-0.40}       # Threshold range high knee
GLOW_SCATTER=${GLOW_SCATTER:-7.0}   # Scattering
GLOW_GAIN=${GLOW_GAIN:-0.35}   # Intensity (Node 1.5 -> ~0.35 here; see recipe)
SCAN_THR=${SCAN_THR:-0.90}     # Glow Scan Threshold
SCAN_SCATTER=${SCAN_SCATTER:-4.0}
SCAN_RADIUS=${SCAN_RADIUS:-0.70}
SCAN_GAIN=${SCAN_GAIN:-1.40}
SNOW_AMOUNT=${SNOW_AMOUNT:-0.70}
SNOW_SIZE=${SNOW_SIZE:-0.10}
SNOW_SPEED=${SNOW_SPEED:--1.0}
SNOW_FLICKER=${SNOW_FLICKER:-1.0}
HEAT=${HEAT:-0}                # 1 to enable Heat Wave (see recipe: negligible)
HEAT_AMP_PX=${HEAT_AMP_PX:-2}

# Scattering -> gaussian sigma, scaled to the band height (tuned at BH=405).
SIG=$($PY   -c "print(round($GLOW_SCATTER * 2.86 * $BH/405.0, 2))")
SSIG=$($PY  -c "print(round($SCAN_SCATTER * 1.20 * $SCAN_RADIUS * 2 * $BH/405.0, 2))")
LO=$($PY -c "print(round($GLOW_LO*255))")
HI=$($PY -c "print(round($GLOW_HI*255))")
KN=$($PY -c "print(round(255.0/($HI-$LO), 6))")
STH=$($PY -c "print(round($SCAN_THR*255))")
SKN=$($PY -c "print(round(255.0/(255-$STH), 6))")

read -r TR TG TB <<<"$($PY - "$TINT" <<'EOF'
import sys
s = sys.argv[1].lstrip('#')
print(*[int(s[i:i+2], 16)/255 for i in (0, 2, 4)])
EOF
)"
GR=$($PY -c "print(round($TR*$GLOW_GAIN,4))"); GG=$($PY -c "print(round($TG*$GLOW_GAIN,4))"); GB=$($PY -c "print(round($TB*$GLOW_GAIN,4))")
SR=$($PY -c "print(round($TR*$SCAN_GAIN,4))"); SG=$($PY -c "print(round($TG*$SCAN_GAIN,4))"); SB=$($PY -c "print(round($TB*$SCAN_GAIN,4))")

# ---- SNOW layer (pre-rendered, seamless 6 s loop) --------------------------
PBH=$(( BH - BH % 2 )); PBW=$(( W - W % 2 ))
PART="$HERE/.parts_${PBW}x${PBH}_$(echo "$TINT" | tr -d '#')_${SNOW_AMOUNT}_${SNOW_SIZE}.mp4"
[ -f "$PART" ] || $PY "$HERE/particles.py" --w "$PBW" --h "$PBH" --fps "$FPS" \
    --duration 6 --color "$TINT" --amount "$SNOW_AMOUNT" --size "$SNOW_SIZE" \
    --speed "$SNOW_SPEED" --flicker "$SNOW_FLICKER" --out "$PART"

LUMA="colorchannelmixer=rr=0.299:rg=0.587:rb=0.114:gr=0.299:gg=0.587:gb=0.114:br=0.299:bg=0.587:bb=0.114"
KNEE="lutrgb=r='clip((val-${LO})*${KN},0,255)':g='clip((val-${LO})*${KN},0,255)':b='clip((val-${LO})*${KN},0,255)'"
SKNEE="lutrgb=r='clip((val-${STH})*${SKN},0,255)':g='clip((val-${STH})*${SKN},0,255)':b='clip((val-${STH})*${SKN},0,255)'"

# ---- HEAT WAVE (optional; see recipe -- visually negligible, costs bitrate) --
if [ "$HEAT" = "1" ]; then
  # perlin output here spans ~127..186 (centre 157); remap to 128 +- amp px.
  K=$($PY -c "print(round($HEAT_AMP_PX/29.5,4))")
  PMAP="scroll=vertical=-0.004,scale=${W}:${BH},format=gbrp,lutrgb=r='128+(val-157)*${K}':g='128+(val-157)*${K}':b='128+(val-157)*${K}'"
  PSRC="perlin=size=${PBW}x${PBH}:rate=${FPS}:octaves=6:persistence=0.6:xscale=0.33:yscale=0.33:tscale=0.5:random_mode=seed"
  HEATG="
${PSRC}:seed=11,${PMAP}[xm];
${PSRC}:seed=77,${PMAP}[ym];
[band][xm][ym]displace=edge=smear[warped];
"
  WARP="[warped]"
else
  HEATG=""
  WARP="[band]"
fi

FC="
[0:v]split=2[full][pre];
[pre]format=gbrp,crop=${W}:${BH}:0:${BY}[band];
${HEATG}
${WARP}split=3[base][g1][sc0];
[g1]${LUMA},${KNEE},gblur=sigma=${SIG}:steps=3,
    colorchannelmixer=rr=${GR}:gg=${GG}:bb=${GB}[glow];
[base][glow]blend=all_mode=screen:shortest=1[s1];
[sc0]${LUMA},${SKNEE},gblur=sigma=${SSIG}:steps=3,
    lutrgb=r='clip(val*3,0,255)':g='clip(val*3,0,255)':b='clip(val*3,0,255)',
    colorchannelmixer=rr=${SR}:gg=${SG}:bb=${SB}[scan];
[s1][scan]blend=all_mode=screen:shortest=1[s2];
[1:v]format=gbrp,scale=${W}:${BH}:flags=neighbor,setsar=1[pt];
[s2][pt]blend=all_mode=screen:shortest=1[bandfx];
[full][bandfx]overlay=0:${BY}:shortest=1,format=yuv420p[v]
"

$FF -v error -y -i "$IN" -stream_loop -1 -i "$PART" \
    -filter_complex "$FC" -map "[v]" -map 0:a? -shortest \
    -c:v libx264 -crf 18 -preset medium -pix_fmt yuv420p -c:a copy "$OUT"
echo "wrote $OUT"
