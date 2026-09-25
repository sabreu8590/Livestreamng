#!/usr/bin/env python3
"""His Story / Her Story Shorts dubbing pipeline.

Finished English Short in, dubbed Short out: original music and SFX kept, one
cloned narrator voice for every language, translation adapted per language
with fixed character names, and every line fitted to the original timing.

Stages (each caches its output in the work dir, so re-runs skip finished work):
  separate    demucs htdemucs two-stem split -> vocals.wav / no_vocals.wav
  transcribe  faster-whisper large-v3, word timestamps, hallucination guard
  segment     group words into sentence lines (the unit of translation and TTS)
  translate   Claude, with the series glossary and a per-line syllable budget
  tts         ElevenLabs API with the narrator clone, or a folder of pre-rendered clips
  retime      trim silence, speed up only when a line overruns its slot, level-match
  mix         dubbed narration + original music/SFX stem -> dubbed mp4 + audio-track m4a
  qa          back-transcribe the dub and report WER, speed-ups and overruns

Usage:
  python dub.py run SRC.mp4 --lang es --work work/18910 --glossary glossary.json
  python dub.py run SRC.mp4 --lang es --work work/18910 --clips-dir mm/   # skip the tts stage
  python dub.py eleven-dub SRC.mp4 --lang es --out eleven_es.mp4          # ElevenLabs Dubbing Studio A/B
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

SR = 44100
MAX_TEMPO = 1.25      # past this, atempo artifacts are audible; the line goes back for a rewrite
GAP = 0.04            # minimum silence kept between two dubbed lines
LANG_NAMES = {"es": "Latin American Spanish", "pt": "Brazilian Portuguese", "de": "German",
              "fr": "French", "it": "Italian", "ja": "Japanese", "ko": "Korean",
              "hi": "Hindi", "id": "Indonesian", "tr": "Turkish", "pl": "Polish",
              "ar": "Arabic", "ru": "Russian", "fil": "Filipino"}
# Comfortable narration speed in syllables per second. Anything above is flagged for a rewrite.
MAX_SYL_PER_SEC = {"es": 7.8, "pt": 7.5, "it": 7.8, "fr": 7.5, "de": 6.5, "default": 7.0}


def sh(*cmd):
    subprocess.run([str(c) for c in cmd], check=True)


def ffmpeg(*args):
    sh("ffmpeg", "-loglevel", "error", "-y", *args)


def load_audio(path, sr=SR, mono=True):
    tmp = Path(str(path) + f".{sr}.wav")
    if not tmp.exists():
        ffmpeg("-i", path, "-ac", "1" if mono else "2", "-ar", sr, tmp)
    a, _ = sf.read(tmp, dtype="float32")
    return a


def duration(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
        return float(out)
    except (FileNotFoundError, ValueError):
        return len(load_audio(path)) / SR


def syllables(text):
    return len(re.findall(r"[aeiouyáéíóúàèìòùâêîôûãõäëïöüœæ]+", text.lower()))


# --------------------------------------------------------------------------- stages

def separate(src, work):
    out = work / "stems"
    vocals, bed = out / "htdemucs/src/vocals.wav", out / "htdemucs/src/no_vocals.wav"
    if vocals.exists():
        return vocals, bed
    wav = work / "src.wav"
    ffmpeg("-i", src, "-vn", "-ac", "2", "-ar", SR, wav)
    sh(sys.executable, "-m", "demucs", "--two-stems=vocals", "-n", "htdemucs", "-o", out, wav)
    return vocals, bed


def _repeats(words, n=4):
    """True if any n-word sequence occurs twice back to back (Whisper's looping failure)."""
    toks = [re.sub(r"\W", "", w["w"].lower()) for w in words]
    for i in range(len(toks) - 2 * n + 1):
        if toks[i:i + n] == toks[i + n:i + 2 * n]:
            return True
    return False


def transcribe(vocals, work, model_name="large-v3"):
    out = work / "transcript.json"
    if out.exists():
        return json.loads(out.read_text())
    from faster_whisper import WhisperModel
    model = WhisperModel(model_name, device="auto", compute_type="int8")

    def run(path, offset=0.0):
        segs, _ = model.transcribe(str(path), language="en", word_timestamps=True, vad_filter=True,
                                   condition_on_previous_text=False)
        return [{"w": w.word, "s": round(w.start + offset, 2), "e": round(w.end + offset, 2)}
                for s in segs for w in s.words]

    words = run(vocals)
    # Re-transcribe any window where Whisper looped, on a short clip with no context carried over.
    i = 0
    while i < len(words) - 8:
        if _repeats(words[i:i + 16]):
            a, b = max(0, words[i]["s"] - 2), words[min(i + 16, len(words) - 1)]["e"] + 2
            clip = work / "retry.wav"
            ffmpeg("-ss", a, "-to", b, "-i", vocals, clip)
            fixed = run(clip, a)
            words = [w for w in words if w["e"] <= a] + fixed + [w for w in words if w["s"] >= b]
            i = next(k for k, w in enumerate(words) if w["s"] >= b - 0.01) if any(
                w["s"] >= b for w in words) else len(words)
        else:
            i += 1
    out.write_text(json.dumps(words, indent=0))
    return words


def segment(words, work, max_gap=0.35):
    out = work / "lines.json"
    if out.exists():
        return json.loads(out.read_text())
    lines, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1]["s"] if i + 1 < len(words) else None
        end_sentence = w["w"].strip()[-1:] in ".!?"
        if nxt is None or end_sentence or nxt - w["e"] > max_gap:
            lines.append({"id": len(lines), "start": cur[0]["s"], "end": cur[-1]["e"],
                          "en": "".join(x["w"] for x in cur).strip()})
            cur = []
    out.write_text(json.dumps(lines, indent=1, ensure_ascii=False))
    return lines


TRANSLATE_SYSTEM = """You translate the narration of "His Story" / "Her Story", animated first-person \
storytime YouTube Shorts watched mostly by kids and teens. The translation is spoken by a voice \
clone of the English narrator and must fit the original timing.

Rules:
- Write how a young native speaker of the target language actually talks: natural slang and \
kid-friendly gross-out humor where the English has it. Adapt idioms, never translate them word for word.
- Glossary terms (nicknames, recurring phrases) MUST use the glossary rendering, every time. \
Character first names stay exactly as in English.
- Each line has a time budget. Stay at or under max_syllables. Cut filler, merge clauses, \
use shorter synonyms; keep every plot beat and punchline.
- Quoted dialogue stays dialogue. Keep line boundaries: one output line per input line, same ids.
- No stage directions, no notes, only the spoken text."""


def translate(lines, lang, glossary, work):
    out = work / f"lines.{lang}.json"
    if out.exists():
        return json.loads(out.read_text())
    import anthropic
    from pydantic import BaseModel

    class Line(BaseModel):
        id: int
        text: str

    class Translation(BaseModel):
        lines: list[Line]

    rate = MAX_SYL_PER_SEC.get(lang, MAX_SYL_PER_SEC["default"])
    terms = {en: t[lang] for en, t in glossary.get("terms", {}).items() if lang in t}
    client = anthropic.Anthropic()
    system = [{"type": "text", "text": TRANSLATE_SYSTEM},
              {"type": "text", "text": f"Glossary (English -> {LANG_NAMES.get(lang, lang)}):\n"
                                       + json.dumps(terms, ensure_ascii=False, indent=1),
               "cache_control": {"type": "ephemeral"}}]

    def budget(l, nxt):
        slot = (nxt["start"] if nxt else l["end"] + 0.6) - l["start"]
        return max(3, int(slot * rate))

    todo = [dict(l, max_syllables=budget(l, lines[i + 1] if i + 1 < len(lines) else None))
            for i, l in enumerate(lines)]
    result = {}
    for attempt in range(3):
        payload = [{"id": l["id"], "en": l["en"], "seconds": round(l["end"] - l["start"], 2),
                    "max_syllables": l["max_syllables"],
                    **({"previous_try_too_long": result[l["id"]]} if l["id"] in result else {})}
                   for l in todo]
        resp = client.messages.parse(
            model="claude-opus-5", max_tokens=16000, system=system,
            thinking={"type": "adaptive"}, output_config={"effort": "high"},
            messages=[{"role": "user", "content":
                       f"Target language: {LANG_NAMES.get(lang, lang)}.\nFull story for context:\n"
                       + " ".join(l["en"] for l in lines)
                       + "\n\nTranslate these lines:\n" + json.dumps(payload, ensure_ascii=False)}],
            output_format=Translation)
        if resp.stop_reason != "end_turn" or resp.parsed_output is None:
            raise RuntimeError(f"translation stopped: {resp.stop_reason}")
        for l in resp.parsed_output.lines:
            result[l.id] = l.text
        todo = [l for l in todo if syllables(result.get(l["id"], "")) > l["max_syllables"] * 1.1]
        if not todo:
            break
    missing = [l["id"] for l in lines if l["id"] not in result]
    if missing:
        raise RuntimeError(f"translation missing lines {missing}")
    for l in lines:
        for en, tr in terms.items():
            if en.lower() in l["en"].lower() and tr.lower() not in result[l["id"]].lower():
                print(f"[translate] line {l['id']}: glossary term '{tr}' missing", file=sys.stderr)
    out_lines = [dict(l, text=result[l["id"]]) for l in lines]
    out.write_text(json.dumps(out_lines, indent=1, ensure_ascii=False))
    return out_lines


def tts_elevenlabs(lines, lang, voice_id, work, model="eleven_multilingual_v2"):
    """One request per line; previous/next text keeps the intonation continuous across lines."""
    import requests
    key = os.environ["ELEVENLABS_API_KEY"]
    d = work / f"tts.{lang}"
    d.mkdir(exist_ok=True)
    for i, l in enumerate(lines):
        p = d / f"{l['id']:03d}.mp3"
        if p.exists():
            continue
        body = {"text": l["text"], "model_id": model, "language_code": lang,
                "previous_text": lines[i - 1]["text"] if i else None,
                "next_text": lines[i + 1]["text"] if i + 1 < len(lines) else None,
                "voice_settings": {"stability": 0.45, "similarity_boost": 0.85, "style": 0.3,
                                   "use_speaker_boost": True}}
        for attempt in range(4):
            r = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                              params={"output_format": "mp3_44100_192"},
                              headers={"xi-api-key": key}, json=body, timeout=120)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            p.write_bytes(r.content)
            break
    return d


def _trim(a, thresh_db=-40):
    """Strip leading and trailing silence, keeping 30 ms of padding."""
    env = np.convolve(np.abs(a), np.ones(441) / 441, mode="same")
    idx = np.where(env > 10 ** (thresh_db / 20))[0]
    if not len(idx):
        return a
    pad = int(0.03 * SR)
    return a[max(0, idx[0] - pad): idx[-1] + pad]


def _rms(a):
    return float(np.sqrt(np.mean(a ** 2))) if len(a) else 0.0


def retime(lines, clip_dir, vocals, total, work, lang):
    """Place each line at its original start. Speed up only when it would run into the next line;
    past MAX_TEMPO the line overflows and pushes the following lines later instead."""
    orig = load_audio(vocals)
    track = np.zeros(int(total * SR) + SR, dtype=np.float32)
    report, cursor = [], 0.0
    clips = sorted(Path(clip_dir).glob("*.*"))
    clips = [c for c in clips if c.suffix in (".mp3", ".wav") and not c.name.endswith(f".{SR}.wav")]
    assert len(clips) == len(lines), f"{len(clips)} clips for {len(lines)} lines"
    for i, (l, c) in enumerate(zip(lines, clips)):
        a = _trim(load_audio(c))
        nxt = lines[i + 1]["start"] if i + 1 < len(lines) else total
        at = max(l["start"], cursor)
        avail = nxt - at - GAP
        tempo = 1.0
        if len(a) / SR > avail > 0:
            tempo = min(MAX_TEMPO, (len(a) / SR) / avail)
            tmp = work / f"_tempo_{i}.wav"
            sf.write(work / "_in.wav", a, SR)
            ffmpeg("-i", work / "_in.wav", "-filter:a", f"atempo={tempo:.4f}", "-ar", SR, tmp)
            a, _ = sf.read(tmp, dtype="float32")
        # Match the loudness of the original line so shouts stay shouts.
        ref = orig[int(l["start"] * SR): int(l["end"] * SR)]
        if _rms(a) > 0 and _rms(ref) > 0:
            a = a * np.clip(_rms(ref) / _rms(a), 0.25, 4.0)
        s = int(at * SR)
        track[s: s + len(a)] += a[: len(track) - s]
        end = at + len(a) / SR
        report.append({"id": l["id"], "slot": [l["start"], round(nxt, 2)], "placed": round(at, 2),
                       "end": round(end, 2), "tempo": round(tempo, 3),
                       "late_by": round(max(0.0, at - l["start"]), 2),
                       "overrun": round(max(0.0, end - nxt), 2)})
        cursor = end + GAP
    track = np.clip(track, -1, 1)
    out = work / f"dub_vocals.{lang}.wav"
    sf.write(out, track[: int(total * SR)], SR)
    (work / f"timing.{lang}.json").write_text(json.dumps(report, indent=1))
    return out, report


def mix(src, bed, dub_vocals, work, lang, out_mp4):
    audio = work / f"audio.{lang}.m4a"
    ffmpeg("-i", bed, "-i", dub_vocals, "-filter_complex",
           "[1:a]aformat=channel_layouts=stereo[v];[0:a][v]amix=inputs=2:normalize=0,"
           "alimiter=limit=0.97[m]", "-map", "[m]", "-c:a", "aac", "-b:a", "192k", audio)
    ffmpeg("-i", src, "-i", audio, "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "copy",
           "-shortest", "-metadata:s:a:0", f"language={lang}", out_mp4)
    return audio


def qa(lines, dub_vocals, report, lang, work):
    import jiwer
    from faster_whisper import WhisperModel
    model = WhisperModel("large-v3", device="auto", compute_type="int8")
    segs, _ = model.transcribe(str(dub_vocals), language=lang, condition_on_previous_text=False)
    norm = lambda t: re.sub(r"[^\w ]", "", t.lower())
    hyp = " ".join(s.text for s in segs)
    ref = " ".join(l["text"] for l in lines)
    res = {"wer": round(jiwer.wer(norm(ref), norm(hyp)), 3),
           "lines": len(lines),
           "sped_up": sum(r["tempo"] > 1.0 for r in report),
           "max_tempo": max(r["tempo"] for r in report),
           "overruns": [r["id"] for r in report if r["overrun"] > 0.05],
           "max_late": max(r["late_by"] for r in report)}
    (work / f"qa.{lang}.json").write_text(json.dumps(res, indent=1))
    return res


# --------------------------------------------------------------------------- ElevenLabs Dubbing Studio

def eleven_dub(src, lang, out, voice_clone=True):
    """End-to-end ElevenLabs dubbing, for comparison. Background audio kept, no watermark."""
    import requests
    key = os.environ["ELEVENLABS_API_KEY"]
    h = {"xi-api-key": key}
    with open(src, "rb") as f:
        r = requests.post("https://api.elevenlabs.io/v1/dubbing", headers=h,
                          files={"file": (Path(src).name, f, "video/mp4")},
                          data={"target_lang": lang, "source_lang": "en", "num_speakers": 1,
                                "watermark": "false", "drop_background_audio": "false",
                                "highest_resolution": "true",
                                "disable_voice_cloning": "false" if voice_clone else "true"},
                          timeout=600)
    r.raise_for_status()
    dub_id, eta = r.json()["dubbing_id"], r.json().get("expected_duration_sec", 60)
    print(f"dubbing_id={dub_id} eta={eta}s")
    while True:
        time.sleep(15)
        st = requests.get(f"https://api.elevenlabs.io/v1/dubbing/{dub_id}", headers=h).json()
        if st["status"] == "dubbed":
            break
        if st["status"] == "failed":
            raise RuntimeError(st)
    r = requests.get(f"https://api.elevenlabs.io/v1/dubbing/{dub_id}/audio/{lang}", headers=h)
    r.raise_for_status()
    Path(out).write_bytes(r.content)
    return dub_id


# --------------------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("src")
    r.add_argument("--lang", required=True)
    r.add_argument("--work", required=True)
    r.add_argument("--glossary", default=None)
    r.add_argument("--script", default=None, help="pre-translated lines JSON [[start,end,text],...]")
    r.add_argument("--clips-dir", default=None, help="pre-rendered TTS clips, one per line, in order")
    r.add_argument("--voice-id", default=os.environ.get("ELEVENLABS_VOICE_ID"))
    r.add_argument("--out", default=None)
    e = sub.add_parser("eleven-dub")
    e.add_argument("src")
    e.add_argument("--lang", required=True)
    e.add_argument("--out", required=True)
    a = ap.parse_args()

    if a.cmd == "eleven-dub":
        eleven_dub(a.src, a.lang, a.out)
        return

    work = Path(a.work)
    work.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    vocals, bed = separate(a.src, work)
    total = duration(a.src)
    if a.script:
        lines = [{"id": i, "start": s, "end": e, "text": t}
                 for i, (s, e, t) in enumerate(json.loads(Path(a.script).read_text()))]
    else:
        words = transcribe(vocals, work)
        lines = segment(words, work)
        glossary = json.loads(Path(a.glossary).read_text()) if a.glossary else {}
        lines = translate(lines, a.lang, glossary, work)
    clip_dir = a.clips_dir or tts_elevenlabs(lines, a.lang, a.voice_id, work)
    dub_vocals, report = retime(lines, clip_dir, vocals, total, work, a.lang)
    out = a.out or work / f"dub.{a.lang}.mp4"
    mix(a.src, bed, dub_vocals, work, a.lang, out)
    res = qa(lines, dub_vocals, report, a.lang, work)
    print(json.dumps(res, indent=1))
    print(f"done in {time.time() - t0:.0f}s -> {out}")


if __name__ == "__main__":
    main()
