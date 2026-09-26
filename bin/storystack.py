#!/usr/bin/env python3
"""
storystack - build labeled story compilations, unattended.

Point it at a Google Drive folder (or a list of Drive links, or a local
directory). It pulls the clips, burns a "Story N" label on each one,
normalizes everything to identical encode settings, and concatenates the
result into one compilation with YouTube chapters.

No NLE, no editor, no timeline. One command.

Requires: ffmpeg, ffprobe, and rclone (only for Drive sources).
Standard library only.
"""

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

VIDEO_EXTS = {
    ".mp4", ".mov", ".mkv", ".m4v", ".webm", ".avi",
    ".mpg", ".mpeg", ".ts", ".flv", ".wmv", ".m2ts",
}

# Bump when the render recipe changes, so cached intermediates are invalidated.
RECIPE_VERSION = "7"

DEFAULT_CONFIG = {
    "channel": "untitled",
    "source": {
        "type": "rclone",
        "remote": "",
        "remote_name": "gdrive:",
        "links_file": None,
        "path": None,
        "recursive": False,
    },
    "order": {
        "by": "name",
        "list_file": None,
        "reverse": False,
        "start_number": 1,
    },
    "label": {
        "template": "Story {n}",
        "font": "Montserrat",
        "font_size_pct": 5.4,
        "position": "top",
        "margin_pct": 4.0,
        "duration": 5.0,
        "fade_in": 0.5,
        "fade_out": 0.05,
        "fade": None,          # legacy: sets both when fade_in/out are absent
        "color": "FFFFFF",
        "outline_color": "000000",
        "outline": 0.0,
        "shadow": 0.0,
        "box": True,           # the dark plate behind the text
        "box_color": "1A1A1A",
        "box_opacity": 0.35,
        "box_radius_pct": 50.0,   # corner radius, percent of the plate height
        "box_pad_x_em": 0.55,
        "box_pad_y_em": 0.28,
        "text_width_scale": 0.88,
        "bold": True,
        "all_caps": True,
    },
    "video": {
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "crf": 20,
        "preset": "veryfast",
        "pad_color": "black",
        # How a clip that does not match the output shape is handled:
        #   pad  - letterbox or pillarbox with a flat colour
        #   blur - the clip sits sharp in front of a blurred copy of itself,
        #          so a vertical short in a 16:9 frame has no black bars
        #   crop - zoom until it fills, losing the edges
        "fill": "pad",
        "blur_darken": 0.10,
    },
    "audio": {
        "bitrate": "192k",
        "sample_rate": 48000,
        "loudnorm": True,
        "target_lufs": -14.0,
    },
    "build": {
        "work_dir": None,
        "jobs": 0,
        "max_hours_per_part": 0.0,
        "keep_intermediates": True,
    },
    "output": {
        "path": "compilation.mp4",
        "write_chapters": True,
        "write_manifest": True,
        "embed_chapters": True,
        "post_command": None,
    },
}

# Named output shapes. A preset only sets width and height; everything else
# about the render is unchanged.
ASPECTS = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
}


def apply_aspect(cfg):
    """Let a config say aspect: "16:9" instead of spelling out the pixels."""
    name = str(cfg.get("video", {}).get("aspect") or "").strip()
    if name in ASPECTS:
        width, height = ASPECTS[name]
        cfg["video"]["width"], cfg["video"]["height"] = width, height
    return cfg


ALIGNMENT = {
    "bottom-left": 1, "bottom": 2, "bottom-center": 2, "bottom-right": 3,
    "left": 4, "center": 5, "middle": 5, "right": 6,
    "top-left": 7, "top": 8, "top-center": 8, "top-right": 9,
}


# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------

def log(msg, *, level="info"):
    stamp = time.strftime("%H:%M:%S")
    prefix = {"info": "  ", "warn": "! ", "err": "X ", "ok": "+ "}.get(level, "  ")
    stream = sys.stderr if level in ("warn", "err") else sys.stdout
    print(f"[{stamp}] {prefix}{msg}", file=stream, flush=True)


def die(msg, code=1):
    log(msg, level="err")
    sys.exit(code)


def deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def run(cmd, *, capture=True, check=True, quiet=False):
    if not quiet:
        log(" ".join(str(c) for c in cmd[:6]) + (" ..." if len(cmd) > 6 else ""))
    proc = subprocess.run(
        [str(c) for c in cmd],
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
    )
    if check and proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-25:]
        raise RuntimeError(
            "command failed (%d): %s\n%s"
            % (proc.returncode, " ".join(str(c) for c in cmd), "\n".join(tail))
        )
    return proc


def natural_key(text):
    """Sort so story2 comes before story10."""
    return [int(p) if p.isdigit() else p.lower()
            for p in re.split(r"(\d+)", str(text))]


def require(binary):
    path = shutil.which(binary)
    if not path:
        die(f"'{binary}' is not installed or not on PATH.")
    return path


def hms(seconds):
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ----------------------------------------------------------------------------
# probing
# ----------------------------------------------------------------------------

def probe(path):
    proc = run([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ], quiet=True)
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = 0.0
    for candidate in (data.get("format", {}).get("duration"),
                      (video or {}).get("duration")):
        try:
            duration = float(candidate)
            if duration > 0:
                break
        except (TypeError, ValueError):
            continue

    fps = 0.0
    if video:
        raw = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/0"
        try:
            num, den = raw.split("/")
            fps = float(num) / float(den) if float(den) else 0.0
        except (ValueError, ZeroDivisionError):
            fps = 0.0

    return {
        "duration": duration,
        "width": int((video or {}).get("width") or 0),
        "height": int((video or {}).get("height") or 0),
        "fps": round(fps, 3),
        "has_video": video is not None,
        "has_audio": audio is not None,
    }


# ----------------------------------------------------------------------------
# ASS label generation
#
# libass is used rather than drawtext because it handles fades natively,
# gives real outline/shadow typography, and shapes CJK and RTL text
# correctly through harfbuzz and fribidi. That matters for the Japanese
# and Arabic channels.
# ----------------------------------------------------------------------------

def ass_color(hex_rgb, opacity=1.0):
    """RRGGBB + 0..1 opacity -> &HAABBGGRR.

    ASS stores colour as BGR, and its alpha byte is inverted: 00 is fully
    opaque and FF is fully transparent. Getting this backwards renders the
    label invisible, so the conversion lives in one place.
    """
    h = str(hex_rgb).lstrip("#").strip()
    if len(h) != 6 or not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        h = "FFFFFF"
    rr, gg, bb = h[0:2], h[2:4], h[4:6]
    aa = f"{max(0, min(255, int(round((1.0 - float(opacity)) * 255)))):02X}"
    return f"&H{aa}{bb}{gg}{rr}".upper()


def inline_color(hex_rgb):
    """&HBBGGRR& for a \\c override tag, which takes no alpha byte."""
    h = str(hex_rgb).lstrip("#").strip()
    if len(h) != 6:
        h = "000000"
    return f"&H{h[4:6]}{h[2:4]}{h[0:2]}&".upper()


def ass_escape(text):
    return (str(text)
            .replace("\\", "\\\\")
            .replace("{", "\\{")
            .replace("}", "\\}")
            .replace("\n", "\\N"))


def estimate_text_width(text, font_size):
    """Rough advance width, in script units.

    libass will not tell us how wide a line renders, and the plate behind the
    text has to be sized before rendering. These per-character ratios are close
    enough for a short label; the padding absorbs the error.
    """
    total = 0.0
    for ch in text:
        code = ord(ch)
        if ch == " ":
            total += 0.30
        elif (0x1100 <= code <= 0x11FF or 0x2E80 <= code <= 0xA4CF
              or 0xAC00 <= code <= 0xD7A3 or 0xF900 <= code <= 0xFAFF
              or 0xFF00 <= code <= 0xFF60 or 0x3000 <= code <= 0x30FF):
            total += 1.02          # CJK and kana are full width
        elif 0x0600 <= code <= 0x06FF or 0x0750 <= code <= 0x077F:
            total += 0.52          # Arabic
        elif ch.isupper():
            total += 0.68
        elif ch.isdigit():
            total += 0.58
        else:
            total += 0.54
    return total * float(font_size)


def rounded_rect_drawing(box_w, box_h, radius):
    r"""An ASS \p1 rounded rectangle spanning (0,0) to (box_w, box_h).

    Anchored from its own top-left corner rather than centred, so it can be
    placed with \\an7 and land exactly where the arithmetic says.
    """
    w = max(1.0, float(box_w))
    h = max(1.0, float(box_h))
    r = max(0.0, min(float(radius), w / 2.0, h / 2.0))
    if r <= 0.5:
        return f"m 0 0 l {w:.0f} 0 l {w:.0f} {h:.0f} l 0 {h:.0f}"
    k = r * 0.5523  # control-point offset for a true circular quarter arc
    return (
        f"m {r:.0f} 0 "
        f"l {w - r:.0f} 0 "
        f"b {w - r + k:.0f} 0 {w:.0f} {r - k:.0f} {w:.0f} {r:.0f} "
        f"l {w:.0f} {h - r:.0f} "
        f"b {w:.0f} {h - r + k:.0f} {w - r + k:.0f} {h:.0f} {w - r:.0f} {h:.0f} "
        f"l {r:.0f} {h:.0f} "
        f"b {r - k:.0f} {h:.0f} 0 {h - r + k:.0f} 0 {h - r:.0f} "
        f"l 0 {r:.0f} "
        f"b 0 {r - k:.0f} {r - k:.0f} 0 {r:.0f} 0"
    )


def build_ass(text, cfg, out_path):
    lab = cfg["label"]
    vid = cfg["video"]
    width, height = int(vid["width"]), int(vid["height"])

    font_size = max(8, int(round(height * float(lab["font_size_pct"]) / 100.0)))
    margin = max(0, int(round(height * float(lab["margin_pct"]) / 100.0)))
    align = ALIGNMENT.get(str(lab["position"]).lower(), 8)
    bold = -1 if lab.get("bold", True) else 0

    body_text = text.upper() if lab.get("all_caps") else text
    primary = ass_color(lab["color"])
    outline_col = ass_color(lab["outline_color"])

    # Fades: separate in and out, because a slow fade in and a near-instant cut
    # out is a specific look and one shared value cannot express it.
    legacy = lab.get("fade")
    fade_in = lab.get("fade_in")
    fade_out = lab.get("fade_out")
    if fade_in is None:
        fade_in = legacy if legacy is not None else 0.35
    if fade_out is None:
        fade_out = legacy if legacy is not None else 0.35
    in_ms = max(0, int(round(float(fade_in) * 1000)))
    out_ms = max(0, int(round(float(fade_out) * 1000)))
    fade_tag = f"\\fad({in_ms},{out_ms})" if (in_ms or out_ms) else ""

    duration = float(lab["duration"])
    end = duration if duration and duration > 0 else 359999.0

    def ts(sec):
        sec = max(0.0, float(sec))
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        return f"{h}:{m:02d}:{sec % 60:05.2f}"

    events = []
    use_box = bool(lab.get("box"))

    if use_box:
        pad_x = float(lab.get("box_pad_x_em", 0.75)) * font_size
        pad_y = float(lab.get("box_pad_y_em", 0.42)) * font_size
        # fonts differ in width; text_width_scale tunes the plate to the font
        text_w = estimate_text_width(body_text, font_size) * float(lab.get("text_width_scale", 1.0))
        box_w = text_w + 2 * pad_x
        box_h = font_size * 1.18 + 2 * pad_y
        radius = box_h * float(lab.get("box_radius_pct", 22.0)) / 100.0

        col = (align - 1) % 3
        row = (align - 1) // 3
        side = max(margin, int(round(width * 0.04)))
        if col == 0:
            cx = side + box_w / 2.0
        elif col == 2:
            cx = width - side - box_w / 2.0
        else:
            cx = width / 2.0
        if row == 2:
            cy = margin + box_h / 2.0
        elif row == 1:
            cy = height / 2.0
        else:
            cy = height - margin - box_h / 2.0

        plate = rounded_rect_drawing(box_w, box_h, radius)
        plate_col = inline_color(lab["box_color"])
        plate_alpha = max(0, min(255, int(round(
            (1.0 - float(lab.get("box_opacity", 0.72))) * 255))))
        events.append(
            f"Dialogue: 0,{ts(0)},{ts(end)},Plate,,0,0,0,,"
            f"{{\\pos({cx - box_w / 2.0:.0f},{cy - box_h / 2.0:.0f})\\an7\\p1"
            f"\\c{plate_col}\\alpha&H{plate_alpha:02X}&{fade_tag}}}{plate}")
        events.append(
            f"Dialogue: 1,{ts(0)},{ts(end)},Label,,0,0,0,,"
            f"{{\\pos({cx:.0f},{cy:.0f})\\an5{fade_tag}}}{ass_escape(body_text)}")
        style_outline, style_shadow, border_style = 0.0, 0.0, 1
    else:
        style_outline = float(lab.get("outline") or 0) or max(2.0, font_size * 0.06)
        style_shadow = float(lab.get("shadow") or 0)
        border_style = 1
        events.append(
            f"Dialogue: 0,{ts(0)},{ts(end)},Label,,0,0,0,,"
            f"{{{fade_tag}}}{ass_escape(body_text)}" if fade_tag
            else f"Dialogue: 0,{ts(0)},{ts(end)},Label,,0,0,0,,{ass_escape(body_text)}")

    side_margin = max(10, int(round(width * 0.04)))
    styles = (
        f"Style: Label,{lab['font']},{font_size},{primary},{primary},"
        f"{outline_col},{ass_color('000000', 0.5)},{bold},0,0,0,100,100,0,0,"
        f"{border_style},{style_outline:.1f},{style_shadow:.1f},{align},"
        f"{side_margin},{side_margin},{margin},1\n"
        f"Style: Plate,{lab['font']},{font_size},{primary},{primary},"
        f"{primary},{primary},0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1"
    )

    content = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
{styles}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""" + "\n".join(events) + "\n"
    Path(out_path).write_text(content, encoding="utf-8")
    return out_path


def filter_path(path):
    """Escape a path for use inside an ffmpeg filtergraph single-quoted value."""
    return str(path).replace("\\", "\\\\").replace("'", r"\'")


def check_font(cfg):
    """Warn if the configured font is missing.

    libass does not fail when a font is absent, it silently substitutes one.
    For the Japanese and Arabic channels that means a compilation full of
    empty tofu boxes that nobody notices until it is live, so check up front.
    """
    wanted = str(cfg["label"]["font"]).strip()
    fc = shutil.which("fc-list")
    if not fc or not wanted:
        return
    try:
        proc = run([fc, ":", "family"], quiet=True, check=False)
    except Exception:  # noqa: BLE001
        return
    families = set()
    for line in (proc.stdout or "").splitlines():
        for fam in line.split(","):
            families.add(fam.strip().lower())
    if wanted.lower() in families:
        return
    log(f"font {wanted!r} is not installed. libass will silently substitute a "
        f"different font, which shows as empty boxes for non-Latin text.",
        level="warn")
    log("  on Ubuntu:  sudo apt install fonts-noto-core fonts-noto-cjk "
        "fonts-dejavu-core", level="warn")


def extract_drive_id(line):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    for pattern in DRIVE_ID_PATTERNS:
        m = pattern.search(line)
        if m:
            return m.group(1)
    return None


def list_video_files(directory, recursive=False):
    directory = Path(directory)
    globber = directory.rglob("*") if recursive else directory.glob("*")
    return [p for p in globber
            if p.is_file() and p.suffix.lower() in VIDEO_EXTS and not p.name.startswith(".")]


def fetch_sources(cfg, download_dir, *, dry_run=False):
    """Return a list of local Paths, pulling from Drive if needed."""
    src = cfg["source"]
    kind = str(src.get("type", "rclone")).lower()
    download_dir = Path(download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    if kind == "local":
        base = src.get("path")
        if not base:
            die("source.type is 'local' but source.path is not set.")
        base = Path(os.path.expanduser(base))
        if not base.is_dir():
            die(f"source.path does not exist or is not a directory: {base}")
        files = list_video_files(base, src.get("recursive", False))
        log(f"found {len(files)} video files in {base}")
        return files

    if kind == "rclone":
        require("rclone")
        remote = src.get("remote")
        if not remote:
            die("source.type is 'rclone' but source.remote is not set "
                "(example: \"gdrive:His Story Japan/2026-09\").")
        if dry_run:
            proc = run(["rclone", "lsjson", remote, "--files-only"], quiet=True)
            names = [e["Name"] for e in json.loads(proc.stdout or "[]")
                     if Path(e["Name"]).suffix.lower() in VIDEO_EXTS]
            log(f"[dry-run] remote holds {len(names)} video files")
            return [download_dir / n for n in sorted(names, key=natural_key)]
        cmd = ["rclone", "copy", remote, str(download_dir),
               "--transfers", "8", "--checkers", "16",
               "--drive-acknowledge-abuse", "--progress", "--stats", "10s"]
        if not src.get("recursive", False):
            cmd += ["--max-depth", "1"]
        log(f"pulling from {remote} (rclone skips files already downloaded)")
        run(cmd, capture=False, quiet=True)
        files = list_video_files(download_dir, src.get("recursive", False))
        log(f"{len(files)} video files available locally")
        return files

    if kind == "links":
        require("rclone")
        links_file = src.get("links_file")
        if not links_file:
            die("source.type is 'links' but source.links_file is not set.")
        links_path = Path(os.path.expanduser(links_file))
        if not links_path.is_file():
            die(f"links_file not found: {links_path}")
        remote_name = src.get("remote_name") or "gdrive:"
        if not remote_name.endswith(":"):
            remote_name += ":"

        ids, seen = [], set()
        for raw in links_path.read_text(encoding="utf-8").splitlines():
            file_id = extract_drive_id(raw)
            if file_id and file_id not in seen:
                seen.add(file_id)
                ids.append(file_id)
        if not ids:
            die(f"no Drive file IDs could be parsed out of {links_path}")
        log(f"parsed {len(ids)} Drive IDs from {links_path.name}")
        if dry_run:
            return []

        existing = {p.name for p in list_video_files(download_dir)}
        ordered = []
        for idx, file_id in enumerate(ids, 1):
            marker = download_dir / f".id-{file_id}"
            if marker.is_file():
                name = marker.read_text(encoding="utf-8").strip()
                candidate = download_dir / name
                if candidate.is_file():
                    ordered.append(candidate)
                    continue
            log(f"downloading {idx}/{len(ids)}  {file_id}")
            run(["rclone", "backend", "copyid", remote_name, file_id,
                 str(download_dir) + os.sep, "--drive-acknowledge-abuse"],
                capture=False, quiet=True)
            now = {p.name for p in list_video_files(download_dir)}
            fresh = now - existing
            existing = now
            if not fresh:
                log(f"nothing new landed for {file_id}; skipping", level="warn")
                continue
            name = sorted(fresh)[0]
            marker.write_text(name, encoding="utf-8")
            ordered.append(download_dir / name)
        # For links, the file order IS the story order.
        return ordered

    die(f"unknown source.type: {kind!r} (use rclone, links, or local)")


# ----------------------------------------------------------------------------
# planning
# ----------------------------------------------------------------------------

def load_order_list(list_file):
    """CSV with a 'file' column, plus optional 'number' and 'title'."""
    path = Path(os.path.expanduser(list_file))
    if not path.is_file():
        die(f"order.list_file not found: {path}")
    rows = []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            die(f"{path} has no header row. Expected at least a 'file' column.")
        headers = {h.strip().lower(): h for h in reader.fieldnames}
        file_col = headers.get("file") or headers.get("filename") or headers.get("name")
        if not file_col:
            die(f"{path} needs a 'file' column (found: {reader.fieldnames}).")
        for row in reader:
            name = (row.get(file_col) or "").strip()
            if not name:
                continue
            number = (row.get(headers.get("number", ""), "") or "").strip()
            title = (row.get(headers.get("title", ""), "") or "").strip()
            rows.append({"file": name, "number": number, "title": title})
    return rows


def build_plan(files, cfg, order_source_is_list):
    order = cfg["order"]
    by = str(order.get("by", "name")).lower()
    start = int(order.get("start_number", 1))

    by_name = {}
    for f in files:
        by_name.setdefault(f.name, f)
        by_name.setdefault(f.stem, f)

    items = []

    if by == "list" or order.get("list_file"):
        rows = load_order_list(order["list_file"])
        missing = []
        for row in rows:
            match = by_name.get(row["file"]) or by_name.get(Path(row["file"]).stem)
            if not match:
                missing.append(row["file"])
                continue
            items.append({"path": match, "number": row["number"], "title": row["title"]})
        if missing:
            log(f"{len(missing)} rows in the list had no matching file "
                f"(first few: {', '.join(missing[:5])})", level="warn")
        listed = {it["path"] for it in items}
        extra = [f for f in files if f not in listed]
        if extra:
            log(f"{len(extra)} downloaded files were not in the list and are excluded",
                level="warn")
    else:
        if by == "modified":
            ordered = sorted(files, key=lambda p: (p.stat().st_mtime, natural_key(p.name)))
        else:
            ordered = sorted(files, key=lambda p: natural_key(p.name))
        items = [{"path": p, "number": "", "title": ""} for p in ordered]

    if order.get("reverse"):
        items.reverse()

    total = len(items)
    for idx, item in enumerate(items):
        item["index"] = idx
        item["n"] = int(item["number"]) if str(item["number"]).isdigit() else start + idx
        item["total"] = total
    return items


def render_label_text(item, cfg):
    template = cfg["label"]["template"]
    try:
        return template.format(
            n=item["n"],
            total=item["total"],
            index=item["index"] + 1,
            title=item.get("title") or "",
            stem=item["path"].stem,
        )
    except (KeyError, IndexError) as exc:
        die(f"label.template uses an unknown placeholder: {exc}. "
            "Available: {n} {total} {index} {title} {stem}")


# ----------------------------------------------------------------------------
# rendering
# ----------------------------------------------------------------------------

def recipe_fingerprint(item, cfg, label_text):
    src = item["path"]
    try:
        stat = src.stat()
        identity = f"{src.name}:{stat.st_size}:{int(stat.st_mtime)}"
    except OSError:
        identity = src.name
    payload = json.dumps({
        "v": RECIPE_VERSION,
        "id": identity,
        "label": label_text,
        "trim": [float(item.get("trim_start") or 0), float(item.get("trim_end") or 0)],
        "video": cfg["video"],
        "audio": cfg["audio"],
        "fill": cfg["video"].get("fill"),
        "style": {k: v for k, v in cfg["label"].items() if k != "template"},
    }, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _trim_window(item, info, fps):
    """Resolve trims into (trim_start, target_frames, target_duration).

    Guarded so a trim longer than the clip cannot produce an empty segment; in
    that case the trim is dropped and the clip kept whole. Each segment is cut
    to a whole number of frames and the audio padded to match: otherwise the AAC
    tail runs a few ms past the last video frame, and the concat demuxer turns
    every one of those into a visible hitch at the join.
    """
    trim_start = max(0.0, float(item.get("trim_start") or 0))
    trim_end = max(0.0, float(item.get("trim_end") or 0))
    source_duration = float(info["duration"])
    usable = source_duration - trim_start - trim_end
    if source_duration > 0 and usable < 0.30:
        trim_start, trim_end = 0.0, 0.0
        usable = source_duration
    target_frames = max(1, math.floor(usable * fps))
    return trim_start, trim_end, target_frames, target_frames / float(fps)


def _encode(src, info, cfg, out, *, ass_path=None, trim_start=0.0,
            frames=None, duration=None, video_only=False):
    """One ffmpeg pass: normalize (and optionally label) a clip into MP4.

    MP4 rather than MPEG-TS on purpose: in TS the AAC priming makes the audio
    stream start ~21ms before the video, and the concat demuxer offsets each
    segment by container duration, turning that into a visible hitch at every
    single join. MP4 edit lists carry the priming, so the joins are exact.
    """
    vid, aud = cfg["video"], cfg["audio"]
    width, height, fps = int(vid["width"]), int(vid["height"]), int(vid["fps"])
    gop = max(1, fps * 2)

    fill = str(vid.get("fill") or "pad").lower()
    subs = f",subtitles=filename='{filter_path(ass_path)}'" if ass_path else ""

    if fill == "crop":
        vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
              f"crop={width}:{height},setsar=1,fps={fps}{subs}")
        graph = None
    elif fill == "blur":
        # Downscale hard, blur, upscale: the round trip does most of the
        # blurring for almost nothing, so this costs about 20% over a flat pad
        # rather than the several times a full-resolution gblur would.
        darken = float(vid.get("blur_darken") or 0.10)
        vf = None
        graph = (
            f"[0:v]fps={fps},split=2[bg][fg];"
            f"[bg]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},scale=iw/12:-2,gblur=sigma=4,"
            f"scale={width}:{height},eq=brightness=-{darken:.2f}[bgb];"
            f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[fgs];"
            f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1{subs}[v]"
        )
    else:
        vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
              f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={vid['pad_color']},"
              f"setsar=1,fps={fps}{subs}")
        graph = None

    af_chain = []
    if aud.get("loudnorm"):
        af_chain.append(f"loudnorm=I={float(aud['target_lufs'])}:TP=-1.5:LRA=11")
    af_chain.append(f"aresample={int(aud['sample_rate'])}:first_pts=0")
    af_chain.append("apad")
    af = ",".join(af_chain)

    tmp_out = Path(out).with_name(Path(out).stem + ".tmp.mp4")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
           "-fflags", "+genpts"]
    if trim_start > 0:
        # Before -i: seeks by keyframe then decodes to the exact point. Accurate
        # because this pass re-encodes anyway, and far faster than an output seek.
        cmd += ["-ss", f"{trim_start:.3f}"]
    cmd += ["-i", str(src)]

    if video_only:
        if graph:
            cmd += ["-filter_complex", graph, "-map", "[v]"]
        else:
            cmd += ["-map", "0:v:0", "-vf", vf]
        cmd += ["-an"]
    else:
        if info["has_audio"]:
            audio_map = "0:a:0"
        else:
            cmd += ["-f", "lavfi", "-i",
                    f"anullsrc=channel_layout=stereo:sample_rate={int(aud['sample_rate'])}"]
            audio_map = "1:a:0"
        if graph:
            # With a filter_complex the audio goes through the same graph rather
            # than -af, which would otherwise fight it for the output stream.
            cmd += ["-filter_complex", f"{graph};[{audio_map}]{af}[a]",
                    "-map", "[v]", "-map", "[a]"]
        else:
            cmd += ["-map", "0:v:0", "-map", audio_map, "-vf", vf, "-af", af]
        cmd += ["-c:a", "aac", "-b:a", str(aud["bitrate"]),
                "-ar", str(int(aud["sample_rate"])), "-ac", "2"]

    # The x264 settings must be identical for every pass: a label "head" is
    # stream-copied in front of a cached "body", which only joins cleanly when
    # both halves share the same codec parameters and a fixed keyframe grid.
    cmd += [
        "-c:v", "libx264", "-preset", str(vid["preset"]), "-crf", str(vid["crf"]),
        "-pix_fmt", "yuv420p", "-profile:v", "high",
        "-x264-params", f"keyint={gop}:min-keyint={gop}:scenecut=0",
        "-video_track_timescale", "90000",
        "-max_muxing_queue_size", "2048",
    ]
    if frames:
        cmd += ["-frames:v", str(int(frames))]
    if duration:
        cmd += ["-t", f"{duration:.6f}"]
    cmd += ["-f", "mp4", str(tmp_out)]

    run(cmd, quiet=True)
    os.replace(tmp_out, out)


# ------------------------------------------------------------ fast builds
#
# Every compilation used to re-encode every clip in full, although the only
# thing that differs between two compilations is the "Story N" label in the
# first few seconds. So each clip is now encoded once without a label (the
# "body", cached across builds), and a build only encodes a short labeled
# "head" and stream-copies the rest of the body behind it. Same encoder, same
# settings, same source: the output is identical in quality, and a build does
# roughly a twentieth of the encoding work.

BODY_VERSION = "1"


def cache_dir(cfg, work_dir=None):
    configured = (cfg.get("build") or {}).get("cache_dir")
    if configured:
        return Path(configured)
    base = Path(work_dir).parent if work_dir else Path(tempfile.gettempdir())
    return base / "cache" / "bodies"


def body_fingerprint(item, cfg):
    src = Path(item["path"])
    try:
        stat = src.stat()
        identity = f"{src.name}:{stat.st_size}:{int(stat.st_mtime)}"
    except OSError:
        identity = src.name
    payload = json.dumps({
        "v": f"{RECIPE_VERSION}.{BODY_VERSION}",
        "id": identity,
        "trim": [float(item.get("trim_start") or 0), float(item.get("trim_end") or 0)],
        "video": cfg["video"],
        "audio": cfg["audio"],
    }, sort_keys=True)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def prepare_body(item, cfg, bodies_dir):
    """Encode (or reuse) the unlabeled full clip. Returns (path, info, cached)."""
    src = Path(item["path"])
    bodies_dir = Path(bodies_dir)
    bodies_dir.mkdir(parents=True, exist_ok=True)
    out = bodies_dir / f"{body_fingerprint(item, cfg)}.mp4"
    info = probe(src)
    if out.is_file() and out.stat().st_size > 1024:
        try:
            os.utime(out)  # mark as recently used for the cache pruner
        except OSError:
            pass
        return out, info, True
    if not info["has_video"]:
        raise RuntimeError(f"{src.name} has no video stream")
    fps = int(cfg["video"]["fps"])
    trim_start, _, _, target_duration = _trim_window(item, info, fps)
    _encode(src, info, cfg, out, trim_start=trim_start, duration=target_duration)
    return out, info, False


def head_seconds(cfg):
    """Length of the labeled head: the label's end, rounded up to a keyframe.

    None means the fast path does not apply (a label that never ends).
    """
    duration = float(cfg["label"].get("duration") or 0)
    if duration <= 0:
        return None
    gop_s = 2.0  # keyint is fps * 2 frames, see _encode
    return gop_s * math.ceil((duration + 0.001) / gop_s)


def prune_cache(bodies_dir, max_gb):
    """Delete the least recently used bodies until the cache fits max_gb."""
    bodies_dir = Path(bodies_dir)
    if not bodies_dir.is_dir() or not max_gb:
        return 0
    files = sorted(bodies_dir.glob("*.mp4"), key=lambda f: f.stat().st_mtime)
    total = sum(f.stat().st_size for f in files)
    limit = float(max_gb) * 1e9
    removed = 0
    for f in files:
        if total <= limit:
            break
        size = f.stat().st_size
        try:
            f.unlink()
            total -= size
            removed += 1
        except OSError:
            pass
    return removed


def _concat_quote(path):
    return str(path).replace("'", "'\\''")


def render_clip(item, cfg, work_dir):
    """Normalize + label one clip into an MP4 part. Cached."""
    src = item["path"]
    label_text = render_label_text(item, cfg)
    digest = recipe_fingerprint(item, cfg, label_text)
    parts_dir = Path(work_dir) / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    out = parts_dir / f"{item['index']:04d}_{digest}.mp4"
    meta_path = out.with_suffix(".json")

    if out.is_file() and meta_path.is_file() and out.stat().st_size > 1024:
        try:
            cached = json.loads(meta_path.read_text(encoding="utf-8"))
            if cached.get("duration", 0) > 0:
                cached["cached"] = True
                return cached
        except (json.JSONDecodeError, OSError):
            pass

    fps = int(cfg["video"]["fps"])
    ass_path = parts_dir / f"{item['index']:04d}_{digest}.ass"
    build_ass(label_text, cfg, ass_path)

    head_s = head_seconds(cfg) if (cfg.get("build") or {}).get("fast", True) else None
    mode = "full"
    info = None
    if head_s:
        info = probe(src)
        if not info["has_video"]:
            raise RuntimeError(f"{Path(src).name} has no video stream")
        trim_start, trim_end, target_frames, target_duration = _trim_window(item, info, fps)
        head_frames = int(round(head_s * fps))
        # Only worth it when there is a real body left after the head.
        if target_frames > head_frames + fps:
            body, info, body_cached = prepare_body(item, cfg, cache_dir(cfg, work_dir))
            head = parts_dir / f"{item['index']:04d}_{digest}.head.mp4"
            _encode(src, info, cfg, head, ass_path=ass_path, trim_start=trim_start,
                    frames=head_frames, video_only=True)
            listing = parts_dir / f"{item['index']:04d}_{digest}.concat.txt"
            listing.write_text(f"file '{_concat_quote(Path(head).resolve())}'\n"
                               f"file '{_concat_quote(Path(body).resolve())}'\n"
                               f"inpoint {head_s:.6f}\n", encoding="utf-8")
            tmp_out = out.with_name(out.stem + ".tmp.mp4")
            run(["ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
                 "-f", "concat", "-safe", "0", "-i", str(listing),
                 "-i", str(body), "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
                 "-t", f"{target_duration:.6f}", "-video_track_timescale", "90000",
                 "-f", "mp4", str(tmp_out)], quiet=True)
            os.replace(tmp_out, out)
            for f in (head, listing):
                try:
                    f.unlink()
                except OSError:
                    pass
            mode = "fast (body reused)" if body_cached else "fast (body encoded)"

    if mode == "full":
        info = info or probe(src)
        if not info["has_video"]:
            raise RuntimeError(f"{Path(src).name} has no video stream")
        trim_start, trim_end, target_frames, target_duration = _trim_window(item, info, fps)
        _encode(src, info, cfg, out, ass_path=ass_path, trim_start=trim_start,
                duration=target_duration)

    rendered = probe(out)
    meta = {
        "index": item["index"],
        "n": item["n"],
        "source": str(src),
        "source_name": Path(src).name,
        "label": label_text,
        "title": item.get("title") or "",
        "part": str(out),
        "duration": rendered["duration"],
        "source_duration": info["duration"],
        "trim_start": trim_start,
        "trim_end": trim_end,
        "source_resolution": f"{info['width']}x{info['height']}",
        "source_fps": info["fps"],
        "had_audio": info["has_audio"],
        "cached": False,
        "mode": mode,
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


# ----------------------------------------------------------------------------
# assembly
# ----------------------------------------------------------------------------

def ffmeta_escape(text):
    return re.sub(r"([=;#\\\n])", r"\\\1", str(text))


def write_chapter_metadata(metas, path):
    lines = [";FFMETADATA1"]
    cursor = 0.0
    for meta in metas:
        start_ms = int(round(cursor * 1000))
        cursor += float(meta["duration"])
        end_ms = int(round(cursor * 1000))
        if end_ms <= start_ms:
            end_ms = start_ms + 1
        title = meta["title"] or meta["label"]
        lines += ["", "[CHAPTER]", "TIMEBASE=1/1000",
                  f"START={start_ms}", f"END={end_ms}",
                  f"title={ffmeta_escape(title)}"]
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_chapter_text(metas, path):
    lines, cursor = [], 0.0
    for meta in metas:
        title = meta["title"] or meta["label"]
        lines.append(f"{hms(cursor)} {title}")
        cursor += float(meta["duration"])
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_manifest(metas, path):
    with Path(path).open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["order", "story_number", "label", "start", "duration_s",
                         "trim_start_s", "trim_end_s", "source_file",
                         "source_resolution", "source_fps", "had_audio"])
        cursor = 0.0
        for i, meta in enumerate(metas, 1):
            writer.writerow([
                i, meta["n"], meta["label"], hms(cursor),
                f"{float(meta['duration']):.3f}",
                f"{float(meta.get('trim_start') or 0):.2f}",
                f"{float(meta.get('trim_end') or 0):.2f}",
                meta["source_name"],
                meta["source_resolution"], meta["source_fps"],
                "yes" if meta["had_audio"] else "no",
            ])
            cursor += float(meta["duration"])


def concat_parts(metas, cfg, out_path, work_dir):
    work_dir = Path(work_dir)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    list_file = work_dir / f"concat_{out_path.stem}.txt"
    with list_file.open("w", encoding="utf-8") as fh:
        for meta in metas:
            escaped = str(meta["part"]).replace("'", r"'\''")
            fh.write(f"file '{escaped}'\n")

    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
           "-f", "concat", "-safe", "0", "-i", str(list_file)]

    maps = ["-map", "0:v:0", "-map", "0:a:0"]
    if cfg["output"].get("embed_chapters", True):
        meta_file = work_dir / f"chapters_{out_path.stem}.ffmeta"
        write_chapter_metadata(metas, meta_file)
        cmd += ["-i", str(meta_file)]
        maps += ["-map_metadata", "1"]

    cmd += maps + [
        "-c", "copy",
        "-movflags", "+faststart", "-fflags", "+genpts",
    ]

    # keep the real extension so ffmpeg can still infer the muxer
    tmp_out = out_path.with_name(out_path.stem + ".tmp" + out_path.suffix)
    cmd += [str(tmp_out)]
    run(cmd, quiet=True)
    os.replace(tmp_out, out_path)  # atomic, so a running stream never sees a half file
    return out_path


def split_into_parts(metas, max_hours):
    if not max_hours or max_hours <= 0:
        return [metas]
    limit = float(max_hours) * 3600.0
    groups, current, running = [], [], 0.0
    for meta in metas:
        dur = float(meta["duration"])
        if current and running + dur > limit:
            groups.append(current)
            current, running = [], 0.0
        current.append(meta)
        running += dur
    if current:
        groups.append(current)
    return groups


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def load_config(path):
    cfg_path = Path(os.path.expanduser(path))
    if not cfg_path.is_file():
        die(f"config not found: {cfg_path}")
    try:
        user_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"{cfg_path} is not valid JSON: {exc}")
    cfg = apply_aspect(deep_merge(DEFAULT_CONFIG, user_cfg))
    if not cfg["build"].get("work_dir"):
        cfg["build"]["work_dir"] = str(cfg_path.parent / f".storystack-{cfg['channel']}")
    return cfg


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="storystack",
        description="Build a labeled story compilation from a Drive folder. No NLE required.")
    parser.add_argument("config", help="path to a channel config JSON file")
    parser.add_argument("--dry-run", action="store_true",
                        help="show the running order and exit without encoding")
    parser.add_argument("--limit", type=int, default=0,
                        help="only process the first N clips (for a test run)")
    parser.add_argument("--jobs", type=int, default=0,
                        help="parallel encodes (default: config, else CPU count)")
    parser.add_argument("--out", default=None, help="override output.path")
    parser.add_argument("--rebuild", action="store_true",
                        help="ignore cached intermediates and re-encode everything")
    args = parser.parse_args(argv)

    require("ffmpeg")
    require("ffprobe")

    cfg = load_config(args.config)
    if args.out:
        cfg["output"]["path"] = args.out
    check_font(cfg)

    work_dir = Path(os.path.expanduser(cfg["build"]["work_dir"]))
    download_dir = work_dir / "source"
    work_dir.mkdir(parents=True, exist_ok=True)

    if args.rebuild:
        shutil.rmtree(work_dir / "parts", ignore_errors=True)
        log("rebuild requested: cached intermediates cleared")

    log(f"channel: {cfg['channel']}")
    files = fetch_sources(cfg, download_dir, dry_run=args.dry_run)
    if not files:
        die("no source video files found.")

    order_is_list = bool(cfg["order"].get("list_file"))
    items = build_plan(files, cfg, order_is_list)
    if args.limit and args.limit > 0:
        items = items[:args.limit]
        for item in items:
            item["total"] = len(items)
    if not items:
        die("nothing to build after ordering.")

    log(f"{len(items)} clips in the running order")

    if args.dry_run:
        print()
        for item in items[:40]:
            print(f"  {item['index']+1:>4}. {render_label_text(item, cfg):<24} "
                  f"{item['path'].name}")
        if len(items) > 40:
            print(f"  ... and {len(items) - 40} more")
        print()
        log("dry run complete; nothing was encoded")
        return 0

    jobs = args.jobs or int(cfg["build"].get("jobs") or 0) or (os.cpu_count() or 2)
    jobs = max(1, min(jobs, 16))
    log(f"encoding with {jobs} parallel job(s); cached clips are skipped")

    metas, failures = [], []
    started = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(render_clip, item, cfg, work_dir): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            done += 1
            try:
                meta = future.result()
                metas.append(meta)
                tag = "cached" if meta.get("cached") else f"{meta['duration']:.1f}s"
                log(f"[{done}/{len(items)}] {meta['label']}  ({tag})")
            except Exception as exc:  # noqa: BLE001 - report and continue
                failures.append((item["path"].name, str(exc)))
                log(f"[{done}/{len(items)}] FAILED {item['path'].name}: "
                    f"{str(exc).splitlines()[-1][:180]}", level="warn")

    if failures:
        log(f"{len(failures)} clip(s) failed and were left out of the compilation",
            level="warn")
    if not metas:
        die("every clip failed; nothing to assemble.")

    metas.sort(key=lambda m: m["index"])
    total_seconds = sum(float(m["duration"]) for m in metas)
    log(f"encoded {len(metas)} clips, {hms(total_seconds)} total "
        f"(took {hms(time.time() - started)})", level="ok")

    out_cfg = cfg["output"]
    base_out = Path(os.path.expanduser(out_cfg["path"]))
    groups = split_into_parts(metas, cfg["build"].get("max_hours_per_part", 0))
    written = []

    for i, group in enumerate(groups, 1):
        if len(groups) == 1:
            target = base_out
        else:
            target = base_out.with_name(f"{base_out.stem}_part{i:02d}{base_out.suffix}")
        log(f"assembling {target.name} ({len(group)} clips, "
            f"{hms(sum(float(m['duration']) for m in group))})")
        concat_parts(group, cfg, target, work_dir)
        written.append(target)

        if out_cfg.get("write_chapters", True):
            write_chapter_text(group, target.with_suffix(".chapters.txt"))
        if out_cfg.get("write_manifest", True):
            write_manifest(group, target.with_suffix(".manifest.csv"))

    for target in written:
        size_gb = target.stat().st_size / (1024 ** 3)
        log(f"wrote {target}  ({size_gb:.2f} GB)", level="ok")

    if not cfg["build"].get("keep_intermediates", True):
        shutil.rmtree(work_dir / "parts", ignore_errors=True)
        log("intermediates cleared")

    post = out_cfg.get("post_command")
    if post:
        log(f"running post_command: {post}")
        proc = subprocess.run(post, shell=True)
        if proc.returncode != 0:
            log(f"post_command exited {proc.returncode}", level="warn")

    if failures:
        print()
        log("failed clips:", level="warn")
        for name, err in failures:
            log(f"  {name}: {err.splitlines()[-1][:160]}", level="warn")
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("interrupted; already-encoded clips are cached, just run it again",
            level="warn")
        sys.exit(130)
