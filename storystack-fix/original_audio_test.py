import re, yt_dlp
def patch(base):
    pat = re.compile(r"\+(bestaudio|ba)(?![\w\[])")
    pref = [pat.sub(lambda m: "+" + m.group(1) + "[format_note*=original]", p) for p in base.split("/") if pat.search(p)]
    return "/".join(pref + [base])
def f(fid, w, h, vc, ac='none', note='', tbr=1000, lang=None):
    return dict(format_id=fid, ext='mp4', width=w, height=h, vcodec=vc, acodec=ac, tbr=tbr,
                format_note=note, language=lang, url='https://x/'+fid, protocol='https')
V = [f('137',1080,1920,'avc1.640028',tbr=3000), f('399',1080,1920,'av01',tbr=2000)]
dubbed = V + [f('140-0',None,None,'none','mp4a.40.2','Spanish (Latin America), medium',130,'es'),
              f('140-1',None,None,'none','mp4a.40.2','English (US) original (default), medium',129,'en'),
              f('140-2',None,None,'none','mp4a.40.2','Hindi, medium',131,'hi')]
for a in dubbed[2:]: a.update(width=None, height=None)
plain = V + [dict(dubbed[3], format_id='140', format_note='medium', language='en')]
BASE = 'bestvideo[height<=1920]+bestaudio/best[height<=1920]/bestvideo+bestaudio/best'
for name, fmt in [('old', BASE), ('new', patch(BASE))]:
    for label, fmts in [('dubbed', dubbed), ('no-dubs', plain)]:
        info = dict(id='t', title='t', extractor='youtube', extractor_key='Youtube', webpage_url='x', formats=[dict(x) for x in fmts])
        y = yt_dlp.YoutubeDL(dict(format=fmt, format_sort=['res:1080','vcodec:h264','abr'], simulate=True, quiet=True))
        r = y.process_ie_result(info, download=False)
        a = [x for x in r['requested_formats'] if x['vcodec']=='none'][0]
        print(f"{name:4} {label:8} -> {r['format_id']:10} audio: {a['format_note']}")
print(patch(BASE))
