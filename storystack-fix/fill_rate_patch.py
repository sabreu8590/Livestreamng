import re, sys
p = sys.argv[1] if len(sys.argv) > 1 else "server/jobs.py"
s = open(p).read()
if "_fill_rate(" in s:
    sys.exit("already patched")
a = "total, done = len(pending), 0\n"
if s.count(a) != 1:
    sys.exit("PATCH FAILED: start line not found")
indent = re.search(r"\n([ \t]*)" + re.escape(a), s).group(1)
s = s.replace(a, a + indent + "_t0 = time.time()\n")
s, n = re.subn(r'note\(f"details \{(\w+)\}/\{([^}]+)\}', r'note(f"details {\1}/{\2}{_fill_rate(_t0, \1, \2)}', s)
if n == 0:
    sys.exit("PATCH FAILED: no progress lines found")
s = s + '''\nimport time

def _fill_rate(t0, done, total):
    """' · 52/s · ~24s left' for the fill-in progress message."""
    secs = max(time.time() - t0, 0.001)
    rate = done / secs
    left = (total - done) / rate if rate else 0
    r = f"{rate:.0f}/s" if rate >= 1 else f"{rate * 60:.1f}/min"
    eta = f"{left:.0f}s" if left < 90 else f"{left / 60:.0f} min"
    return f" · {r} · ~{eta} left · {secs:.0f}s elapsed"
'''
open(p, "w").write(s)
print(f"patched {n} progress line(s)")
