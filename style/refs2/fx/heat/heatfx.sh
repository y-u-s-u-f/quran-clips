#!/bin/bash
# heatfx.sh <in> <out> <#RRGGBB> <band_y> <band_h>
# env: HEAT AMP TSCALE SCROLL XSCALE SS ORDER(pre|post) CRF
set -euo pipefail
PY="${PY:-/Users/yusuf/quran-clips/tools/asr-venv/bin/python}"
IN="$1"; OUT="$2"; TINT="${3:-#C9A227}"; BY="${4:-0}"
W=$(ffprobe -v error -select_streams v:0 -show_entries stream=width -of csv=p=0 "$IN")
H=$(ffprobe -v error -select_streams v:0 -show_entries stream=height -of csv=p=0 "$IN")
FPS=$(ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate -of csv=p=0 "$IN" | awk -F/ '{printf "%.4f",$1/$2}')
BH="${5:-$H}"
HEAT=${HEAT:-1}; AMP=${AMP:-2}; TSCALE=${TSCALE:-0.5}; SCROLL=${SCROLL:--0.004}
XSCALE=${XSCALE:-0.33}; SS=${SS:-1}; ORDER=${ORDER:-pre}; CRF=${CRF:-18}
GLOW_SCATTER=7.0; GLOW_GAIN=0.35; SCAN_SCATTER=4.0; SCAN_RADIUS=0.70; SCAN_GAIN=1.40
SIG=$($PY -c "print(round($GLOW_SCATTER*2.86*$BH/405.0,2))")
SSIG=$($PY -c "print(round($SCAN_SCATTER*1.20*$SCAN_RADIUS*2*$BH/405.0,2))")
read -r TR TG TB <<<"$($PY -c "
s='$TINT'.lstrip('#'); print(*[int(s[i:i+2],16)/255 for i in (0,2,4)])")"
GR=$($PY -c "print(round($TR*$GLOW_GAIN,4))"); GG=$($PY -c "print(round($TG*$GLOW_GAIN,4))"); GB=$($PY -c "print(round($TB*$GLOW_GAIN,4))")
SR=$($PY -c "print(round($TR*$SCAN_GAIN,4))"); SG=$($PY -c "print(round($TG*$SCAN_GAIN,4))"); SB=$($PY -c "print(round($TB*$SCAN_GAIN,4))")
PBH=$((BH-BH%2)); PBW=$((W-W%2))
HERE="$(cd "$(dirname "$0")" && pwd)"
PART="$HERE/.parts_${PBW}x${PBH}_$(echo "$TINT"|tr -d '#')_0.70_0.10.mp4"
[ -f "$PART" ] || $PY "$HERE/particles.py" --w "$PBW" --h "$PBH" --fps "$FPS" --duration 6 \
   --color "$TINT" --amount 0.70 --size 0.10 --speed -1.0 --flicker 1.0 --out "$PART"
LUMA="colorchannelmixer=rr=0.299:rg=0.587:rb=0.114:gr=0.299:gg=0.587:gb=0.114:br=0.299:bg=0.587:bb=0.114"
KNEE="lutrgb=r='clip((val-51)*5.0,0,255)':g='clip((val-51)*5.0,0,255)':b='clip((val-51)*5.0,0,255)'"
SKNEE="lutrgb=r='clip((val-230)*10.2,0,255)':g='clip((val-230)*10.2,0,255)':b='clip((val-230)*10.2,0,255)'"
mkgraph() { # $1 = input label, $2 = output label
  SW=$((W*SS)); SH=$((BH*SS))
  K=$($PY -c "print(round($AMP*$SS/${SDP:-18.4},5))")
  PSRC="perlin=size=${PBW}x${PBH}:rate=${FPS}:octaves=6:persistence=0.6:xscale=${XSCALE}:yscale=${XSCALE}:tscale=${TSCALE}:random_mode=seed"
  PMAP="scroll=vertical=${SCROLL},scale=${SW}:${SH}:flags=bicubic,format=gbrp,lutrgb=r='128+(val-${CTR:-130.26})*${K}':g='128+(val-${CTR:-130.26})*${K}':b='128+(val-${CTR:-130.26})*${K}'"
  if [ "$SS" = "1" ]; then
    echo "${PSRC}:seed=11,${PMAP}[xm];${PSRC}:seed=77,${PMAP}[ym];${1}[xm][ym]displace=edge=smear${2};"
  else
    echo "${PSRC}:seed=11,${PMAP}[xm];${PSRC}:seed=77,${PMAP}[ym];${1}scale=${SW}:${SH}:flags=neighbor[bigb];[bigb][xm][ym]displace=edge=smear,scale=${W}:${BH}:flags=area${2};"
  fi
}
PREG=""; POSTG=""; IN1="[band]"; OUTFX="[bandfx]"
if [ "$HEAT" = "1" ] && [ "$ORDER" = "pre" ]; then PREG="$(mkgraph "[band]" "[warped]")"; IN1="[warped]"; fi
if [ "$HEAT" = "1" ] && [ "$ORDER" = "post" ]; then POSTG="$(mkgraph "[bfx0]" "[bandfx]")"; OUTFX="[bfx0]"; fi
FC="
[0:v]split=2[full][pre];
[pre]format=gbrp,crop=${W}:${BH}:0:${BY}[band];
${PREG}
${IN1}split=3[base][g1][sc0];
[g1]${LUMA},${KNEE},gblur=sigma=${SIG}:steps=3,colorchannelmixer=rr=${GR}:gg=${GG}:bb=${GB}[glow];
[base][glow]blend=all_mode=screen:shortest=1[s1];
[sc0]${LUMA},${SKNEE},gblur=sigma=${SSIG}:steps=3,
 lutrgb=r='clip(val*3,0,255)':g='clip(val*3,0,255)':b='clip(val*3,0,255)',
 colorchannelmixer=rr=${SR}:gg=${SG}:bb=${SB}[scan];
[s1][scan]blend=all_mode=screen:shortest=1[s2];
[1:v]format=gbrp,scale=${W}:${BH}:flags=neighbor,setsar=1[pt];
[s2][pt]blend=all_mode=screen:shortest=1${OUTFX};
${POSTG}
[full][bandfx]overlay=0:${BY}:shortest=1,format=yuv420p[v]
"
ffmpeg -v error -y -i "$IN" -stream_loop -1 -i "$PART" -filter_complex "$FC" \
  -map "[v]" -map 0:a? -shortest -c:v libx264 -crf $CRF -preset medium -pix_fmt yuv420p -c:a copy "$OUT"
