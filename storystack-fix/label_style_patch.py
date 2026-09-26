#!/usr/bin/env python3
"""Rounder, lighter "STORY N" label.

- rounded font (Montserrat) instead of DejaVu Sans
- pill-shaped plate with true round corners (the old corner curve was nearly square)
- more transparent plate (35% instead of 72%)
- plate width tuned to the font
Usage: python3 label_style_patch.py [/opt/storystack/bin/storystack.py]
"""
import re, sys
p = sys.argv[1] if len(sys.argv) > 1 else "/opt/storystack/bin/storystack.py"
s = open(p).read()
if "text_width_scale" in s:
    sys.exit("already patched")

start = s.index('    return (\n        f"m {r:.0f} 0 "')
end = s.index("    )", start) + 5
s = s[:start] + '''    k = r * 0.5523  # control-point offset for a true circular quarter arc
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
    )''' + s[end:]

old = "        text_w = estimate_text_width(body_text, font_size)"
assert s.count(old) == 1, "text width line not found"
s = s.replace(old, "        # fonts differ in width; text_width_scale tunes the plate to the font\n"
                   '        text_w = estimate_text_width(body_text, font_size) * float(lab.get("text_width_scale", 1.0))')

defaults = [
    (r'"font": "DejaVu Sans",', '"font": "Montserrat",'),
    (r'"font_size_pct": 4\.2,', '"font_size_pct": 5.4,'),
    (r'"box_opacity": 0\.72,', '"box_opacity": 0.35,'),
    (r'"box_radius_pct": 22\.0,', '"box_radius_pct": 50.0,'),
    (r'"box_pad_x_em": 0\.75,', '"box_pad_x_em": 0.55,'),
    (r'"box_pad_y_em": 0\.42,', '"box_pad_y_em": 0.28,\n        "text_width_scale": 0.88,'),
]
for pat, rep in defaults:
    s, n = re.subn(pat, rep, s, count=1)
    assert n == 1, f"default not found: {pat}"
open(p, "w").write(s)
print("label style patched")
