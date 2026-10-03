"""``pionir video ...``: make a video, build its staging pages, list the niches.

    pionir video niches
    pionir video make --niche ID --pack FILE [--out DIR] [--size WxH]
    pionir video site [--base-url URL]
    pionir video fetch --niche ID --source SOURCE --ref REF --pack FILE [--topic TEXT]
    pionir video tutorial --steps FILE --pack FILE
    pionir video published ID

``make`` writes a package under the video folder (PIONIR_VIDEO_DIR, else ~/.pionir/video). A
video from a live niche is then parked in the approval queue as ``video.youtube_upload``;
``--out DIR`` builds a sample into DIR instead and never parks anything. The script is written
by a local model on the CPU and the voice is Kokoro on the CPU, so neither takes a GPU lease.
``fetch`` reads one allowlisted public source politely (rate limit, user agent, robots, cache in
the video folder) into a pack file; ``tutorial`` runs a steps file in the build sandbox and records
the real runs into a pack; ``published`` records that Ian made an uploaded video public in Studio.
Nothing here uploads anything (only an approved ``video.youtube_upload`` does, private), and
nothing deploys the pages.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .bootstrap import build_runtime
from .config import PionirSettings
from .video.narrate import KokoroSynth
from .video.niche import load_niches
from .video.pages import DEFAULT_BASE_URL, build_site
from .video.pipeline import CAPABILITY, make_video
from .video.writer import OllamaWriter

_SIZE = re.compile(r"^(\d{3,4})x(\d{3,4})$")


def add_parsers(commands: Any) -> None:
    video = commands.add_parser(
        "video", help="the video pipeline: make a video from a source pack, build staging pages")
    sub = video.add_subparsers(dest="video_command", required=True)
    sub.add_parser("niches", help="list the configured niches and whether each is live")
    make = sub.add_parser("make", help="write, narrate and render one video from a source pack")
    make.add_argument("--niche", required=True)
    make.add_argument("--pack", type=Path, required=True, help="the reviewed passage pack (JSON)")
    make.add_argument("--out", type=Path, default=None,
                      help="build a sample here instead of the video folder; never parks")
    make.add_argument("--size", default="1920x1080", help="WIDTHxHEIGHT (small for a sample)")
    site = sub.add_parser("site", help="build the static pages for every staged video")
    site.add_argument("--base-url", default=DEFAULT_BASE_URL,
                      help="the https URL the pages will eventually be served under")


    fetch = sub.add_parser("fetch", help="read one allowlisted public source into a pack file")
    fetch.add_argument("--niche", required=True)
    fetch.add_argument("--source", required=True, help="a source id the niche allowlists")
    fetch.add_argument("--ref", required=True,
                       help="the item: archive identifier, Commons 'File:...' title, LoC item id, "
                            "Chronicling America 'lccn/date/ed-1/seq-1', UW 'alias/id', US patent")
    fetch.add_argument("--pack", type=Path, required=True, help="the pack file to add to")
    fetch.add_argument("--topic", default="", help="the topic, when the pack file is new")
    tut = sub.add_parser("tutorial", help="run a steps file in the build sandbox into a pack")
    tut.add_argument("--steps", type=Path, required=True, help="JSON list of steps")
    tut.add_argument("--pack", type=Path, required=True)
    pub = sub.add_parser("published",
                         help="record that an uploaded video was made public in YouTube Studio")
    pub.add_argument("video_id")


def _print(value: Mapping[str, Any]) -> None:
    import json
    sys.stdout.write(json.dumps(value, indent=2, default=str) + "\n")


def run(args: argparse.Namespace, settings: PionirSettings) -> int:
    niches = load_niches()
    if args.video_command == "niches":
        _print({"niches": [{"id": n.id, "title": n.title, "kind": n.kind, "live": n.live,
                            "example": n.example, "cadence_days": n.cadence_days}
                           for n in niches]})
        return 0
    if args.video_command == "site":
        site = build_site(settings.video_path, base_url=args.base_url)
        _print({"status": "ok", "site": str(site.root), "pages": len(site.pages),
                "skipped": site.skipped, "deployed": False})
        return 0
    if args.video_command == "published":
        from .video.upload import UploadError, mark_public

        try:
            _print({"status": "ok", "uploaded": mark_public(settings.video_path, args.video_id)})
        except (UploadError, ValueError) as error:
            _print({"status": "error", "message": str(error)})
            return 1
        return 0
    if args.video_command == "tutorial":
        import json

        from .video.tutorial import TutorialError, parse_steps, run_steps, write_runs

        try:
            steps = parse_steps(json.loads(args.steps.read_text(encoding="utf-8")))
            runs = run_steps(steps)
            write_runs(args.pack, runs)
        except (TutorialError, OSError, ValueError) as error:
            _print({"status": "error", "message": str(error)})
            return 1
        _print({"status": "ok", "runs": [{"id": r.id, "ok": r.ok, "exit_code": r.exit_code,
                                          "measured": [m.name for m in r.measured]} for r in runs]})
        return 0 if all(r.ok for r in runs) else 1
    niche = next((n for n in niches if n.id == args.niche), None)
    if niche is None:
        _print({"status": "error", "message": f"no niche {args.niche!r}",
                "niches": [n.id for n in niches]})
        return 1
    if args.video_command == "fetch":
        from .video.fetch import FetchError, NoRedirectHttp, PoliteFetcher, fetch_item, write_pack

        fetcher = PoliteFetcher(NoRedirectHttp(), Path(settings.video_path) / "cache")
        try:
            item = fetch_item(fetcher, niche, args.source, args.ref)
            counts = write_pack(args.pack, args.topic or niche.title, [item])
        except FetchError as error:
            _print({"status": "error", "message": str(error)})
            return 1
        _print({"status": "ok", "title": item.title, "license_basis": item.license_basis,
                "url": item.url, "pack": str(args.pack), **counts})
        return 0
    size = _SIZE.match(args.size)
    if not size:
        _print({"status": "error", "message": "--size must look like 1280x720"})
        return 1
    submit = None
    root = args.out or settings.video_path
    if args.out is None:
        from .server import PionirApp  # server imports cli, so this cannot be top-level

        app = PionirApp(build_runtime(settings))

        def submit(payload: Mapping[str, Any]) -> Any:
            return app.run_task(CAPABILITY, dict(payload), permissions=[])
    made = make_video(niche, args.pack, Path(root), writer=OllamaWriter(), synth=KokoroSynth(),
                      size=(int(size[1]), int(size[2])), submit=submit)
    m = made.package.manifest
    _print({"status": "ok", "id": made.package.id, "folder": str(made.package.dir),
            "title": m["title"], "duration_seconds": m["duration_seconds"],
            "size_bytes": m["size_bytes"], "problems": list(made.problems),
            "parked": made.parked, "not_parked_because": made.not_parked_because})
    return 1 if made.problems else 0
