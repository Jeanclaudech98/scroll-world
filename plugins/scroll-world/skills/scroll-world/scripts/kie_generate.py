#!/usr/bin/env python3
"""Generate scroll-world stills and frame-locked clips through Kie.ai.

The script deliberately owns only provider work. The existing scroll-world pipeline
continues to own frame extraction, ffmpeg encoding, poster generation, SSIM checks,
and browser playback.

Environment:
  KIE_API_KEY  Required for non-dry-run calls.

Examples:
  # Generate an anchor still
  python3 scripts/kie_generate.py still --prompt-file /tmp/world/still_surface.txt \
    --output /tmp/world/still_surface.png --aspect-ratio 3:2

  # Generate a dive from a local still
  python3 scripts/kie_generate.py video --prompt-file /tmp/world/dive_surface.txt \
    --first-frame /tmp/world/still_surface.png --output /tmp/world/dive_surface.mp4 \
    --model bytedance/seedance-2-mini --duration 8 --resolution 720p

  # Generate a strict first-frame / last-frame connector
  python3 scripts/kie_generate.py video --prompt-file /tmp/world/conn_1.txt \
    --first-frame /tmp/world/last_surface.png --last-frame /tmp/world/first_leak.png \
    --output /tmp/world/conn_1.mp4 --model bytedance/seedance-2 --duration 5

Kie documents first/last-frame image-to-video and multimodal reference-to-video as
mutually exclusive. This script intentionally does not accept reference assets for a
connector so strict seam conditioning cannot be accidentally weakened.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API = "https://api.kie.ai"
UPLOAD_API = "https://kieai.redpandaai.co/api/file-stream-upload"
DEFAULT_IMAGE_MODEL = "seedream/5-pro-text-to-image"
DEFAULT_VIDEO_MODEL = "bytedance/seedance-2"
TERMINAL_FAILURES = {"failed", "fail", "error", "cancelled", "canceled"}


class KieError(RuntimeError):
    pass


def require_key() -> str:
    key = os.getenv("KIE_API_KEY", "").strip()
    if not key:
        raise KieError("KIE_API_KEY is required. Export it in the shell; never put it in frontend code.")
    return key


def read_prompt(args: argparse.Namespace) -> str:
    if args.prompt:
        return args.prompt.strip()
    if args.prompt_file:
        return Path(args.prompt_file).read_text(encoding="utf-8").strip()
    raise KieError("Supply --prompt or --prompt-file.")


def request_json(method: str, url: str, key: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=90) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise KieError(f"Kie HTTP {exc.code}: {raw[:800]}") from exc
    except URLError as exc:
        raise KieError(f"Kie network error: {exc.reason}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise KieError(f"Kie returned non-JSON: {raw[:800]}") from exc
    if parsed.get("code") not in (None, 200):
        raise KieError(f"Kie API error {parsed.get('code')}: {parsed.get('msg', parsed)}")
    return parsed


def multipart_upload(path: Path, key: str) -> str:
    """Upload a local frame and return Kie's temporary HTTPS URL.

    Uploads are intentionally not cached: Kie upload URLs are short-lived. Generated
    outputs are cached locally, so normal resume runs do not need any re-upload.
    """
    if not path.is_file():
        raise KieError(f"Upload input does not exist: {path}")
    boundary = f"----scrollworld-{uuid.uuid4().hex}"
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    chunks: list[bytes] = []

    def field(name: str, value: str) -> None:
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            value.encode(),
            b"\r\n",
        ])

    field("uploadPath", "images/scroll-world")
    field("fileName", f"{path.stem}-{hashlib.sha256(path.read_bytes()).hexdigest()[:12]}{path.suffix}")
    chunks.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode(),
        f"Content-Type: {mime}\r\n\r\n".encode(),
        path.read_bytes(),
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    req = Request(UPLOAD_API, data=b"".join(chunks), method="POST")
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Accept", "application/json")
    try:
        with urlopen(req, timeout=180) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise KieError(f"Kie upload HTTP {exc.code}: {raw[:800]}") from exc
    if not payload.get("success") and payload.get("code") not in (None, 200):
        raise KieError(f"Kie upload failed: {payload.get('msg', payload)}")
    data = payload.get("data") or {}
    url = data.get("downloadUrl") or data.get("fileUrl")
    if not url:
        raise KieError(f"Kie upload did not return a URL: {payload}")
    return str(url)


def submit_and_wait(payload: dict[str, Any], key: str, poll_seconds: float, timeout_seconds: int) -> dict[str, Any]:
    created = request_json("POST", f"{API}/api/v1/jobs/createTask", key, payload)
    task_id = ((created.get("data") or {}).get("taskId"))
    if not task_id:
        raise KieError(f"Kie did not return taskId: {created}")
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = request_json("GET", f"{API}/api/v1/jobs/recordInfo?taskId={task_id}", key)
        data = result.get("data") or {}
        state = str(data.get("state", "")).lower()
        if state == "success":
            return data
        if state in TERMINAL_FAILURES:
            raise KieError(f"Kie task {task_id} {state}: {data.get('failMsg') or data.get('failCode') or data}")
        progress = data.get("progress", "?")
        print(f"task={task_id} state={state or 'pending'} progress={progress}", file=sys.stderr)
        time.sleep(poll_seconds)
    raise KieError(f"Timed out waiting for Kie task {task_id} after {timeout_seconds}s")


def result_url(task: dict[str, Any]) -> str:
    raw = task.get("resultJson")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise KieError(f"Kie resultJson was not valid JSON: {raw[:800]}") from exc
    raw = raw or {}
    urls = raw.get("resultUrls") or raw.get("result_urls") or []
    if not urls:
        raise KieError(f"Kie task has no result URL: {task}")
    return str(urls[0])


def download(url: str, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".part")
    try:
        with urlopen(url, timeout=300) as response, temp.open("wb") as dest:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                dest.write(chunk)
    except (HTTPError, URLError) as exc:
        temp.unlink(missing_ok=True)
        raise KieError(f"Could not download generated asset: {exc}") from exc
    if temp.stat().st_size == 0:
        temp.unlink(missing_ok=True)
        raise KieError("Kie generated asset download was empty")
    temp.replace(output)


def write_task_record(output: Path, provider: str, model: str, task: dict[str, Any], source_url: str) -> None:
    record = {
        "provider": provider,
        "model": model,
        "taskId": task.get("taskId"),
        "state": task.get("state"),
        "creditsConsumed": task.get("creditsConsumed"),
        "costTime": task.get("costTime"),
        "resultUrl": source_url,
        "downloadedPath": str(output),
    }
    output.with_suffix(output.suffix + ".task.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def create_still(args: argparse.Namespace) -> int:
    output = Path(args.output)
    if output.exists() and output.stat().st_size:
        print(f"cached {output}")
        return 0
    prompt = read_prompt(args)
    reference = Path(args.reference_image) if args.reference_image else None
    model = args.model
    if reference and model == DEFAULT_IMAGE_MODEL:
        model = "seedream/5-pro-image-to-image"
    input_data: dict[str, Any] = {
        "prompt": prompt,
        "aspect_ratio": args.aspect_ratio,
        "quality": args.quality,
        "output_format": "png",
        "nsfw_checker": bool(args.nsfw_checker),
    }
    payload: dict[str, Any] = {"model": model, "input": input_data}
    if args.callback_url:
        payload["callBackUrl"] = args.callback_url
    if args.dry_run:
        if reference:
            input_data["image_urls"] = [f"UPLOAD:{reference}"]
        print(json.dumps(payload, indent=2))
        return 0
    key = require_key()
    if reference:
        input_data["image_urls"] = [multipart_upload(reference, key)]
    task = submit_and_wait(payload, key, args.poll_seconds, args.timeout)
    url = result_url(task)
    download(url, output)
    write_task_record(output, "kie", model, task, url)
    print(f"created {output}")
    return 0


def create_video(args: argparse.Namespace) -> int:
    output = Path(args.output)
    if output.exists() and output.stat().st_size:
        print(f"cached {output}")
        return 0
    first = Path(args.first_frame)
    last = Path(args.last_frame) if args.last_frame else None
    prompt = read_prompt(args)
    if args.duration < 4 or args.duration > 15:
        raise KieError("Seedance duration must be between 4 and 15 seconds.")
    if args.dry_run:
        payload: dict[str, Any] = {
            "model": args.model,
            "callBackUrl": args.callback_url or None,
            "input": {
                "prompt": prompt,
                "first_frame_url": f"UPLOAD:{first}",
                "generate_audio": False,
                "resolution": args.resolution,
                "aspect_ratio": args.aspect_ratio,
                "duration": args.duration,
                "web_search": False,
            },
        }
        if last:
            payload["input"]["last_frame_url"] = f"UPLOAD:{last}"
        print(json.dumps(payload, indent=2))
        return 0
    key = require_key()
    first_url = multipart_upload(first, key)
    input_data: dict[str, Any] = {
        "prompt": prompt,
        "first_frame_url": first_url,
        "generate_audio": False,
        "resolution": args.resolution,
        "aspect_ratio": args.aspect_ratio,
        "duration": args.duration,
        "web_search": False,
    }
    if last:
        input_data["last_frame_url"] = multipart_upload(last, key)
    payload = {"model": args.model, "input": input_data}
    if args.callback_url:
        payload["callBackUrl"] = args.callback_url
    task = submit_and_wait(payload, key, args.poll_seconds, args.timeout)
    url = result_url(task)
    download(url, output)
    write_task_record(output, "kie", args.model, task, url)
    print(f"created {output}")
    return 0


def doctor(_: argparse.Namespace) -> int:
    """Safely report whether the installed plugin can run, without contacting Kie."""
    checks = {
        "KIE_API_KEY": bool(os.getenv("KIE_API_KEY", "").strip()),
        "python3": True,
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
    }
    for name, passed in checks.items():
        print(f"{'PASS' if passed else 'MISSING'}  {name}")
    print("INFO  no Kie API call or credits used")
    return 0 if all(checks.values()) else 1


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--prompt")
    parser.add_argument("--prompt-file")
    parser.add_argument("--output", required=True)
    parser.add_argument("--callback-url", help="Optional public HTTPS callback. Polling remains the default.")
    parser.add_argument("--poll-seconds", type=float, default=10)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--dry-run", action="store_true", help="Print request payload without uploading or spending credits.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kie.ai provider adapter for scroll-world")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("doctor", help="Check key and local prerequisites without calling Kie")
    check.set_defaults(func=doctor)

    still = sub.add_parser("still", help="Generate a style-locked scene anchor")
    add_common(still)
    still.add_argument("--model", default=DEFAULT_IMAGE_MODEL)
    still.add_argument("--reference-image", help="Approved anchor image; uses Seedream 5 Pro image-to-image by default.")
    still.add_argument("--aspect-ratio", default="3:2")
    still.add_argument("--quality", default="basic")
    still.add_argument("--nsfw-checker", action="store_true")
    still.set_defaults(func=create_still)

    video = sub.add_parser("video", help="Generate a dive or strict first/last-frame connector")
    add_common(video)
    video.add_argument("--model", default=DEFAULT_VIDEO_MODEL)
    video.add_argument("--first-frame", required=True)
    video.add_argument("--last-frame")
    video.add_argument("--resolution", default="720p", choices=["480p", "720p", "1080p", "4k"])
    video.add_argument("--aspect-ratio", default="16:9")
    video.add_argument("--duration", type=int, default=8)
    video.set_defaults(func=create_video)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except KieError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
