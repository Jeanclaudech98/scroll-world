# Kie.ai pipeline — scroll-world provider adapter

This is the Kie.ai alternative to the upstream Higgsfield CLI pipeline. It keeps the
existing scroll-world asset contract intact:

```text
anchor stills → dive clips → extracted boundary frames → connector clips
→ encode → matching posters → SSIM seam gate → scrub engine
```

Only the provider calls change. The existing `scrub-engine.js`, FFmpeg encodes, poster
extraction, mobile tiers, SEO copy, and SSIM validation remain unchanged.

## Requirements

```bash
export KIE_API_KEY='...' # keep it out of git and browser code
command -v python3
command -v ffmpeg
command -v ffprobe
command -v cwebp # optional but recommended for WebP stills/posters
```

The adapter is standard-library Python; it has no package installation step. After marketplace
installation, Claude Code adds the plugin's `bin/` directory to Bash `PATH`, so invoke it as:

```bash
scroll-world-kie --help
```

When developing from a local checkout before installing the plugin, use
`python3 scripts/kie_generate.py` instead.

Kie task results and upload URLs are temporary. The adapter downloads every successful
output immediately to the project work directory and writes a sidecar `*.task.json` with
the task ID and Kie-reported credits consumed.

## Model selection

```bash
IMAGE_MODEL=seedream/5-pro-text-to-image
PREVIZ_MODEL=bytedance/seedance-2-mini
FINAL_MODEL=bytedance/seedance-2
```

Kie documents `bytedance/seedance-2`, `bytedance/seedance-2-fast`, and
`bytedance/seedance-2-mini` as supporting strict first-frame / last-frame video. Use a
model with that support for every connector. Connectors intentionally use no reference
image/video/audio arrays: Kie documents strict first-and-last-frame mode as mutually
exclusive with multimodal reference-to-video.

## 1. Set the world paths

```bash
# Marketplace-installed plugin:
GEN=scroll-world-kie
# Local checkout alternative:
# GEN="/path/to/scroll-world/plugins/scroll-world/skills/scroll-world/scripts/kie_generate.py"
WORK=/tmp/scroll-world
ASSETS=./assets
NAMES="surface leak seal proof retirement"
mkdir -p "$WORK" "$ASSETS/vid"
```

Create one prompt file for every still, dive, and connector. Reuse the existing prompt
structure in `prompts.md`; it is provider-neutral.

```text
$WORK/still_surface.txt
$WORK/dive_surface.txt
$WORK/conn_1.txt
```

## 2. Anchor still approval gate

Do not batch every still before approving art direction. Generate one representative
anchor, inspect it, revise the shared style preamble if necessary, then proceed.

```bash
python3 "$GEN" still \
  --prompt-file "$WORK/still_surface.txt" \
  --output "$WORK/still_surface.png" \
  --model "$IMAGE_MODEL" \
  --aspect-ratio 3:2
```

After approval, generate each remaining anchor with the approved reference image. The adapter
switches the default Seedream model to `seedream/5-pro-image-to-image` when
`--reference-image` is supplied, which preserves the anchor's visual language. It is
idempotent: an existing non-empty output is reused.

```bash
for n in $NAMES; do
  python3 "$GEN" still \
    --prompt-file "$WORK/still_$n.txt" \
    --reference-image "$WORK/still_surface.png" \
    --output "$WORK/still_$n.png" \
    --aspect-ratio 3:2 &
done
wait
```

## 3. Previz — all dives and connectors at Mini tier

First validate pacing, camera grammar, and seam handoffs at low cost.

```bash
DIVE_DUR=8
CONN_DUR=5

for n in $NAMES; do
  python3 "$GEN" video \
    --prompt-file "$WORK/dive_$n.txt" \
    --first-frame "$WORK/still_$n.png" \
    --output "$WORK/dive_$n.mp4" \
    --model "$PREVIZ_MODEL" --resolution 720p \
    --duration "$DIVE_DUR" &
done
wait
```

## 4. Extract actual boundary frames

The connector must receive **real rendered video frames**, never the original stills.

```bash
for n in $NAMES; do
  ffmpeg -v error -y -ss 0 -i "$WORK/dive_$n.mp4" \
    -frames:v 1 -q:v 2 "$WORK/first_$n.png"
  ffmpeg -v error -y -sseof -0.15 -i "$WORK/dive_$n.mp4" \
    -frames:v 1 -q:v 2 "$WORK/last_$n.png"
done
```

## 5. Generate strict first/last-frame connectors

```bash
set -- $NAMES
i=0
prev=""
for n in "$@"; do
  if [ -n "$prev" ]; then
    i=$((i+1))
    python3 "$GEN" video \
      --prompt-file "$WORK/conn_$i.txt" \
      --first-frame "$WORK/last_$prev.png" \
      --last-frame "$WORK/first_$n.png" \
      --output "$WORK/conn_$i.mp4" \
      --model "$PREVIZ_MODEL" --resolution 720p \
      --duration "$CONN_DUR" &
  fi
  prev="$n"
done
wait
```

Kie documents first-and-last-frame mode as the right choice when the frames must match
strictly. Do not supply reference assets to these jobs.

## 6. Approve previz, then render final clips

Once the assembled draft reads correctly, clear only video artifacts. Stills remain
reused. Swap the model, render the dives again, extract the new boundary frames, and render
the new connectors.

```bash
rm -f "$WORK"/dive_*.mp4 "$WORK"/conn_*.mp4 \
  "$WORK"/first_*.png "$WORK"/last_*.png
FINAL_MODEL=bytedance/seedance-2
# Re-run sections 3–5 with FINAL_MODEL in place of PREVIZ_MODEL.
```

Start at 720p. Change `--resolution 1080p` only after validating actual Kie pricing and
quality for the selected model in the Kie dashboard.

## 7. Encoding, posters, mobile variants, and SSIM

Resume at section 5 in the existing `pipeline.md`:

- encode source clips into short-GOP MP4s;
- extract posters from encoded clips;
- create `-m.mp4` phone variants if selected;
- run the existing SSIM seam gate;
- wire the resulting clips into `scrub-engine.js`.

The outputs keep exactly the same names expected by the engine:

```text
assets/vid/surface.mp4
assets/vid/conn1.mp4
assets/surface-poster.webp
```

## Dry-run validation

Review provider requests without uploading assets or spending credits:

```bash
python3 "$GEN" video \
  --prompt 'A slow continuous camera move.' \
  --first-frame "$WORK/still_surface.png" \
  --last-frame "$WORK/first_leak.png" \
  --output "$WORK/conn_1.mp4" \
  --model bytedance/seedance-2-mini \
  --duration 5 --dry-run
```

## Operational notes

- Kie jobs are asynchronous. The adapter polls `recordInfo` by default; pass `--callback-url`
  only when a public HTTPS webhook receiver is available.
- The Kie API key belongs only in the local environment or protected server secret store.
- Store final source assets in project-controlled storage. Kie download/upload links expire.
- Keep the upstream SSIM threshold: `>=0.90` pass, `0.75–0.90` inspect, `<0.75` regenerate.
- The adapter writes task-sidecar JSON so actual `creditsConsumed` is preserved per asset.
