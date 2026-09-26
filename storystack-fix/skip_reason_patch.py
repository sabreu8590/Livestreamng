import sys
p = sys.argv[1] if len(sys.argv) > 1 else "/opt/storystack/server/static/app.js"
s = open(p).read()
old = "failed.slice(0, 5).map(f => esc(f.video_title || f.video_id)).join('<br>')"
new = ("failed.slice(0, 5).map(f => esc(f.video_title || f.video_id) + "
       "(skipWhy(b, f) ? ' <span style=\"opacity:.75\">— ' + esc(skipWhy(b, f)) + '</span>' : '')).join('<br>')")
if "skipWhy(" in s: sys.exit("already patched")
if s.count(old) != 1: sys.exit("PATCH FAILED: line not found")
s = s.replace(old, new) + r'''

// StoryStack fix: say WHY a clip was skipped (reason is stored on the build)
function skipWhy(b, f) {
  let why = f.reason || f.error || f.message || '';
  if (!why) {
    try { why = (Object.fromEntries(JSON.parse(b.error || '[]'))[f.video_id]) || ''; } catch (e) { why = ''; }
  }
  why = String(why).replace(/^(download|encode) failed:\s*/i, '').replace(/^ERROR:\s*/i, '');
  return why.length > 160 ? why.slice(0, 157) + '…' : why;
}
'''
open(p, "w").write(s); print("patched app.js")
