"""Assembly: scene list + narration -> an MP4 and an SRT, drawn locally with Pillow and ffmpeg.

No network, no GPU, no image generation. Every frame is either a card drawn from text, a
timeline drawn from the script's own lines, or a credited image from a local file. Captions are
the narration cues (exact, because the voice was synthesised sentence by sentence) burned in as
small transparent overlays, and the same chunks are written as an SRT so the upload carries
real captions. The video ends on a credits card that prints the sources, the image credits and
the disclosure line, so the credit is in the picture and not only in a description.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..social.card import ACCENT, BACKGROUND, BOLD, INK, MUTED, REGULAR, SEMIBOLD, _font
from .disclosure import DISCLOSURE, credit_lines
from .narrate import Cue, Narration
from .niche import Niche
from .passages import ImageRef, Pack
from .script import Scene, Script

FPS = 25
REFERENCE = (1920, 1080)
END_CARD_SECONDS = 6.0
CAPTION_LINES = 2
CAPTION_BAND = 300               # height of the strip the captions are drawn in
CAPTION_FOOT = 100               # clear space under the captions, for the image credit
TIMELINE_NODES = 5
KEN_BURNS = 0.0006               # zoom per frame on an image scene (about +9% over 6 s)


class RenderUnavailable(RuntimeError):
    """ffmpeg is missing or refused the job; the message carries its last words."""


@dataclass(frozen=True, slots=True)
class Chunk:
    start: float
    end: float
    text: str


@dataclass(frozen=True, slots=True)
class Rendered:
    video: Path
    srt: Path
    thumbnail: Path
    duration: float
    size_bytes: int
    width: int
    height: int
    credits: tuple[str, ...]
    images_used: tuple[ImageRef, ...]


def find_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if not found:
        raise RenderUnavailable("ffmpeg is not on PATH")
    return found


def wrap_px(text: str, font, width: int) -> list[str]:
    """Greedy wrap by measured width; a word wider than the line is split, never dropped."""
    lines: list[str] = []
    line = ""
    for word in text.split():
        while font.getlength(word) > width:
            cut = len(word) - 1
            while cut > 1 and font.getlength(word[:cut]) > width:
                cut -= 1
            if line:
                lines.append(line)
                line = ""
            lines.append(word[:cut])
            word = word[cut:]
        trial = f"{line} {word}".strip()
        if font.getlength(trial) <= width:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def caption_chunks(cues: tuple[Cue, ...] | list[Cue], font, width: int,
                   max_lines: int = CAPTION_LINES) -> list[Chunk]:
    """Each cue cut into pieces that fit ``max_lines`` of captions, the cue's time shared out
    by characters so a long sentence is read in steps instead of in one unreadable block."""
    out: list[Chunk] = []
    for cue in cues:
        lines = wrap_px(cue.text, font, width)
        groups = [lines[i:i + max_lines] for i in range(0, len(lines), max_lines)] or [[""]]
        total = sum(len(" ".join(g)) for g in groups) or 1
        at = cue.start
        for n, group in enumerate(groups):
            text = " ".join(group)
            end = cue.end if n == len(groups) - 1 else at + (cue.end - cue.start) * len(text) / total
            out.append(Chunk(round(at, 3), round(end, 3), "\n".join(group)))
            at = end
    return out


def srt_time(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(chunks: list[Chunk], path: Path) -> None:
    blocks = [f"{n}\n{srt_time(c.start)} --> {srt_time(c.end)}\n{c.text}\n"
              for n, c in enumerate(chunks, 1)]
    path.write_text("\n".join(blocks), encoding="utf-8")


def _canvas(size: tuple[int, int]):
    from PIL import Image
    return Image.new("RGB", size, BACKGROUND)


def _label(draw, size, text: str) -> None:
    draw.text((96, 64), text.upper(), font=_font(34, SEMIBOLD), fill=ACCENT)


def _centered_block(draw, size, lines: list[str], font, top: int, fill, spacing: int = 18) -> int:
    for line in lines:
        draw.text((96, top), line, font=font, fill=fill)
        top += font.size + spacing
    return top


def _shorten(text: str, limit: int) -> str:
    first = text.split(". ")[0].strip()
    if len(first) <= limit:
        return first
    cut = first[:limit].rsplit(" ", 1)[0]
    return cut + "..."


def _heading_font(heading: str, width: int, lines: int = 4):
    for size in (96, 84, 72, 62, 54):
        font = _font(size, BOLD)
        if len(wrap_px(heading, font, width)) <= lines:
            return font
    return _font(48, BOLD)


def draw_card(scene: Scene, series: str, size: tuple[int, int], path: Path) -> None:
    from PIL import ImageDraw
    image = _canvas(size)
    draw = ImageDraw.Draw(image)
    _label(draw, size, series)
    width = size[0] - 192
    font = _heading_font(scene.heading, width)
    lines = wrap_px(scene.heading, font, width)
    top = (size[1] - len(lines) * (font.size + 18)) // 2 - 80
    _centered_block(draw, size, lines, font, top, INK)
    draw.rectangle((96, top - 36, 96 + 160, top - 28), fill=ACCENT)
    image.save(path)


def draw_timeline(scene: Scene, series: str, size: tuple[int, int], path: Path) -> None:
    from PIL import ImageDraw
    image = _canvas(size)
    draw = ImageDraw.Draw(image)
    _label(draw, size, series)
    head = _font(64, BOLD)
    width = size[0] - 192
    top = _centered_block(draw, size, wrap_px(scene.heading, head, width)[:2], head, 150, INK)
    body = _font(40, REGULAR)
    nodes = [_shorten(line.text, 110) for line in scene.lines[:TIMELINE_NODES]]
    step = max(80, (size[1] - top - 200) // max(1, len(nodes)))
    x = 130
    y0 = top + 60
    draw.line((x, y0, x, y0 + step * (len(nodes) - 1)), fill=MUTED, width=4)
    for n, text in enumerate(nodes):
        y = y0 + n * step
        draw.ellipse((x - 14, y - 14, x + 14, y + 14), fill=ACCENT)
        for k, row in enumerate(wrap_px(text, body, width - 120)[:2]):
            draw.text((x + 50, y - 24 + k * 48), row, font=body, fill=INK)
    image.save(path)


def draw_image(scene: Scene, ref: ImageRef, series: str, size: tuple[int, int], path: Path) -> None:
    from PIL import Image, ImageDraw
    with Image.open(ref.path) as source:
        picture = source.convert("RGB")
    scale = max(size[0] / picture.width, size[1] / picture.height)
    picture = picture.resize((max(size[0], round(picture.width * scale)),
                              max(size[1], round(picture.height * scale))))
    left, top = (picture.width - size[0]) // 2, (picture.height - size[1]) // 2
    picture = picture.crop((left, top, left + size[0], top + size[1]))
    shade = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(shade).rectangle((0, size[1] - 330, size[0], size[1]), fill=(8, 12, 16, 150))
    picture = Image.alpha_composite(picture.convert("RGBA"), shade).convert("RGB")
    draw = ImageDraw.Draw(picture)
    draw.text((64, 52), series.upper(), font=_font(30, SEMIBOLD), fill=ACCENT,
              stroke_width=2, stroke_fill=(0, 0, 0))
    credit = _font(26, REGULAR)
    row = wrap_px("Image: " + ref.credit, credit, size[0] - 128)[0]
    draw.text((64, size[1] - 60), row, font=credit, fill=INK, stroke_width=2,
              stroke_fill=(0, 0, 0))
    picture.save(path)


def draw_credits(script: Script, images: list[ImageRef], size: tuple[int, int], path: Path) -> None:
    from PIL import ImageDraw
    image = _canvas(size)
    draw = ImageDraw.Draw(image)
    _label(draw, size, "Sources and credits")
    width = size[0] - 192
    body = _font(30, REGULAR)
    y = 130
    for line in credit_lines(script.passages, images):
        for row in wrap_px(line, body, width)[:2]:
            if y > size[1] - 330:
                break
            draw.text((96, y), row, font=body, fill=INK)
            y += 40
        y += 10
    note = _font(32, SEMIBOLD)
    top = size[1] - 70 - 44 * len(wrap_px(DISCLOSURE, note, width))
    draw.rectangle((96, top - 24, 96 + 160, top - 18), fill=ACCENT)
    for row in wrap_px(DISCLOSURE, note, width):
        draw.text((96, top), row, font=note, fill=MUTED)
        top += 44
    image.save(path)


def draw_caption(text: str, size: tuple[int, int], path: Path, band: int = CAPTION_BAND) -> None:
    from PIL import Image, ImageDraw
    image = Image.new("RGBA", (size[0], band), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    font = _font(46, SEMIBOLD)
    rows = text.split("\n")
    height = len(rows) * 62 + 30
    top = band - height - CAPTION_FOOT
    draw.rounded_rectangle((150, top, size[0] - 150, top + height), radius=18,
                           fill=(8, 12, 16, 205))
    for k, row in enumerate(rows):
        w = font.getlength(row)
        draw.text(((size[0] - w) / 2, top + 14 + k * 62), row, font=font, fill=INK)
    image.save(path)


def draw_thumbnail(script: Script, series: str, path: Path) -> None:
    from PIL import ImageDraw
    size = (1280, 720)
    image = _canvas(size)
    draw = ImageDraw.Draw(image)
    _label(draw, size, series)
    width = size[0] - 192
    font = _heading_font(script.title, width, lines=4)
    lines = wrap_px(script.title, font, width)
    top = (size[1] - len(lines) * (font.size + 18)) // 2
    draw.rectangle((96, top - 36, 96 + 160, top - 28), fill=ACCENT)
    _centered_block(draw, size, lines, font, top, INK)
    image.save(path)


def _ffmpeg_graph(frames: list[int], captions: list[Chunk], scene_kinds: list[str],
                  size: tuple[int, int], total: float, caption_y: int) -> str:
    w, h = size
    parts = []
    for n, (count, kind) in enumerate(zip(frames, scene_kinds)):
        zoom = f"min(1+{KEN_BURNS}*on,1.12)" if kind == "image" else "1"
        parts.append(f"[{n}:v]scale={w * 2}:{h * 2},zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':"
                     f"y='ih/2-(ih/zoom/2)':d={count}:s={w}x{h}:fps={FPS},setsar=1[s{n}]")
    scenes = len(frames)
    parts.append("".join(f"[s{n}]" for n in range(scenes)) + f"concat=n={scenes}:v=1:a=0[v0]")
    last = "v0"
    for k, chunk in enumerate(captions):
        nxt = f"v{k + 1}"
        parts.append(f"[{last}][{scenes + 1 + k}:v]overlay=0:{caption_y}:"
                     f"enable='between(t,{chunk.start:.3f},{chunk.end:.3f})'[{nxt}]")
        last = nxt
    parts.append(f"[{last}]format=yuv420p[vout]")
    parts.append(f"[{scenes}:a]apad=whole_dur={total:.3f}[aout]")
    return ";\n".join(parts)


def render_video(script: Script, narration: Narration, niche: Niche, pack: Pack,
                 out_dir: Path, *, size: tuple[int, int] = (1920, 1080)) -> Rendered:
    """Draw, caption and assemble. Returns paths and measurements read off the finished file.

    Everything is drawn on the 1920x1080 reference canvas (the fonts and margins are in those
    pixels) and ffmpeg scales the finished frames to ``size``, so a small sample looks like the
    real thing, only smaller.
    """
    width, height = size
    if width * 9 != height * 16 or width % 2 or height % 2 or width < 160:
        raise RenderUnavailable(f"size {width}x{height} must be 16:9 with even sides")
    shown, size = size, REFERENCE
    ffmpeg = find_ffmpeg()
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "frames"
    work.mkdir(exist_ok=True)
    series = niche.series[0]
    images_used: list[ImageRef] = []
    scene_pngs: list[Path] = []
    kinds: list[str] = []
    for n, scene in enumerate(script.scenes):
        png = work / f"scene{n:03d}.png"
        if scene.type == "image":
            ref = pack.image(scene.image or "")
            if ref is None:
                raise RenderUnavailable(f"scene {n + 1}: image {scene.image!r} is not in the pack")
            if ref not in images_used:
                images_used.append(ref)
            draw_image(scene, ref, series, size, png)
        elif scene.type == "timeline":
            draw_timeline(scene, series, size, png)
        else:
            draw_card(scene, series, size, png)
        scene_pngs.append(png)
        kinds.append(scene.type)
    end_png = work / "credits.png"
    draw_credits(script, images_used, size, end_png)
    scene_pngs.append(end_png)
    kinds.append("card")

    total = narration.duration + END_CARD_SECONDS
    bounds = [span[0] for span in narration.scene_spans] + [narration.duration, total]
    cuts = [round(b * FPS) for b in bounds]
    cuts[0] = 0
    frames = [max(1, cuts[i + 1] - cuts[i]) for i in range(len(scene_pngs))]

    font = _font(46, SEMIBOLD)
    chunks = caption_chunks(narration.cues, font, size[0] - 340)
    srt = out_dir / "captions.srt"
    write_srt(chunks, srt)
    caption_pngs = []
    for k, chunk in enumerate(chunks):
        png = work / f"caption{k:04d}.png"
        draw_caption(chunk.text, size, png)
        caption_pngs.append((chunk, png))

    inputs: list[str] = []
    for png in scene_pngs:
        inputs += ["-i", str(png)]
    inputs += ["-i", str(narration.wav)]
    for _chunk, png in caption_pngs:
        inputs += ["-i", str(png)]
    graph = _ffmpeg_graph(frames, [c for c, _ in caption_pngs], kinds, size, total,
                          size[1] - CAPTION_BAND)
    graph_file = work / "graph.txt"
    graph_file.write_text(graph, encoding="utf-8")
    video = out_dir / "video.mp4"
    command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *inputs,
               "-/filter_complex", str(graph_file), "-map", "[vout]", "-map", "[aout]",
               "-s", f"{shown[0]}x{shown[1]}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-r", str(FPS),
               "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-t", f"{total:.3f}",
               str(video)]
    done = subprocess.run(command, capture_output=True, text=True, timeout=3600)
    if done.returncode != 0 or not video.is_file() or video.stat().st_size == 0:
        raise RenderUnavailable(f"ffmpeg failed ({done.returncode}): {done.stderr[-600:]}")
    thumbnail = out_dir / "thumbnail.png"
    draw_thumbnail(script, series, thumbnail)
    shutil.rmtree(work, ignore_errors=True)
    return Rendered(video, srt, thumbnail, probe_duration(video), video.stat().st_size,
                    shown[0], shown[1], tuple(credit_lines(script.passages, images_used)),
                    tuple(images_used))


def probe_duration(video: Path) -> float:
    """The duration read back off the finished file, not the plan."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RenderUnavailable("ffprobe is not on PATH")
    done = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                           "-of", "default=nw=1:nk=1", str(video)],
                          capture_output=True, text=True, timeout=60)
    try:
        return round(float(done.stdout.strip()), 3)
    except ValueError as error:
        raise RenderUnavailable(f"ffprobe could not read {video}: {done.stderr[-300:]}") from error
