import yt_dlp
def f(fid, ext, w, h, vc, ac='none', tbr=1000):
    d = dict(format_id=fid, ext=ext, width=w, height=h, vcodec=vc, acodec=ac, tbr=tbr,
             url=f'https://x/{fid}', protocol='https')
    if vc == 'none': d.update(width=None, height=None)
    return d
short = [  # vertical Short as YouTube serves it
  f('18','mp4',360,640,'avc1.42001E','mp4a.40.2',500),
  f('134','mp4',360,640,'avc1.4d401e',tbr=300), f('136','mp4',406,720,'avc1.4d401f',tbr=700),
  f('137','mp4',608,1080,'avc1.640028',tbr=1500),
  f('247','webm',720,1280,'vp9',tbr=1000), f('248','webm',1080,1920,'vp9',tbr=2500),
  f('399','mp4',1080,1920,'av01.0.08M.08',tbr=2000),
  f('140','m4a',None,None,'none','mp4a.40.2',128), f('251','webm',None,None,'none','opus',140)]
landscape = [
  f('137','mp4',1920,1080,'avc1.640028',tbr=4000), f('248','webm',1920,1080,'vp9',tbr=3000),
  f('271','webm',2560,1440,'vp9',tbr=8000), f('313','webm',3840,2160,'vp9',tbr=16000),
  f('140','m4a',None,None,'none','mp4a.40.2',128), f('251','webm',None,None,'none','opus',140)]
OLD = 'bv*[ext=mp4][height<=1080]+ba[ext=m4a]/b[ext=mp4]/b'
NEW = 'bv*+ba/b'
for name, fmt, sort in [('OLD (mp4, height<=1080)', OLD, []), ('NEW (-S res:1080)', NEW, ['res:1080'])]:
    for label, fmts in [('Short', short), ('Landscape', landscape)]:
        info = dict(id='t', title='t', extractor='youtube', extractor_key='Youtube', webpage_url='x', formats=[dict(x) for x in fmts])
        y = yt_dlp.YoutubeDL(dict(format=fmt, format_sort=sort, simulate=True, quiet=True, merge_output_format='mp4'))
        y.params['forceprint']={}; r = y.process_ie_result(info, download=False)
        v = next(x for x in r.get('requested_formats',[r]) if x.get('vcodec')!='none')
        print(f"{name:26} {label:9} -> {r['format_id']:8} {v['width']}x{v['height']} {v['vcodec']}")
