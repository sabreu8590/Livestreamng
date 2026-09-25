
# --- StoryStack fix: always pick the ORIGINAL audio track -------------------
# Shorts with YouTube auto-dubs carry several audio tracks (Spanish, Hindi, ...).
# Plain "bestaudio" can land on a dub, so try "original" first and fall back
# to the unchanged string for videos that have no dubs.
import re as _re
_orig_format_string = format_string
_AUDIO = _re.compile(r"\+(bestaudio|ba)(?![\w\[])")

def format_string(*args, **kwargs):
    base = _orig_format_string(*args, **kwargs)
    pref = [_AUDIO.sub(lambda m: "+" + m.group(1) + "[format_note*=original]", p)
            for p in base.split("/") if _AUDIO.search(p)]
    return "/".join(pref + [base])
