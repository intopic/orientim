# -*- coding: utf-8 -*-
"""A scrubbable timeline, borrowed from video editing.

Generates a self-contained HTML file from a recording and opens it. Nothing
leaves the machine: the page is read from local disk, or straight from the
user's own bucket through a signed URL.
"""
import datetime as dt
import html
import json
import os
import webbrowser

from . import store

TPL = r"""<!doctype html><html lang="sq"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title><style>
:root{--bg:#EFF0F1;--panel:#F9FAFA;--sunk:#E4E6E8;--ink:#16191C;--ink2:#4C5359;
--ink3:#7E868C;--rule:#D4D8DB;--accent:#0A7484;--force:#B3341F;--ok:#0ca30c;
--track:#D8DCDF;--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
@media(prefers-color-scheme:dark){:root{--bg:#0E1114;--panel:#161A1E;--sunk:#0A0D10;
--ink:#E2E6E9;--ink2:#9AA4AB;--ink3:#6A747B;--rule:#262C32;--accent:#4FC2D4;
--force:#F0714F;--ok:#3fbf5f;--track:#232A31}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:26px 22px 60px}
header{display:flex;flex-wrap:wrap;gap:10px 22px;align-items:baseline;
padding-bottom:14px;border-bottom:1px solid var(--rule)}
h1{font:600 17px/1.2 var(--mono);margin:0;letter-spacing:-.2px}
.badge{font:600 10.5px/1 var(--mono);letter-spacing:.12em;text-transform:uppercase;
padding:4px 8px;border-radius:2px;border:1px solid currentColor}
.b-fail{color:var(--force)}.b-ok{color:var(--ok)}
.meta{font:11.5px/1.5 var(--mono);color:var(--ink3)}
.meta b{color:var(--ink2);font-weight:500}
.stage{margin-top:22px;background:var(--panel);border:1px solid var(--rule);
border-radius:4px 4px 0 0;padding:18px 20px 14px}
.controls{display:flex;align-items:center;gap:14px;margin-bottom:14px;flex-wrap:wrap}
button{font:600 12px/1 var(--mono);cursor:pointer;background:transparent;
color:var(--ink2);border:1px solid var(--rule);border-radius:2px;padding:7px 12px}
button:hover{border-color:var(--accent);color:var(--ink)}
button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.pos{font:600 12px/1 var(--mono);color:var(--ink);font-variant-numeric:tabular-nums}
.pos span{color:var(--ink3);font-weight:400}
.hint{margin-left:auto;font:10.5px/1 var(--mono);color:var(--ink3);letter-spacing:.06em}
.track{position:relative;height:64px;cursor:pointer;user-select:none;touch-action:none}
.rail{position:absolute;top:32px;left:0;right:0;height:3px;background:var(--track);
border-radius:2px}
.seg{position:absolute;top:26px;height:15px;border-radius:2px;background:var(--accent);
opacity:.4}
.seg.bad{background:var(--force);opacity:.6}
.seg.sel{opacity:1}
.seg.live{opacity:1;box-shadow:0 0 0 2px var(--ok)}
.seg.diverged{opacity:1;background:var(--force);box-shadow:0 0 0 2px var(--force)}
.live-bar{display:flex;align-items:center;gap:12px;margin:0 0 14px;padding:9px 12px;
border-radius:3px;background:var(--sunk);font:12px/1.4 var(--mono);color:var(--ink2);
min-height:38px}
.live-bar.on{box-shadow:inset 3px 0 0 var(--accent)}
.live-bar.good{box-shadow:inset 3px 0 0 var(--ok);color:var(--ink)}
.live-bar.bad{box-shadow:inset 3px 0 0 var(--force);color:var(--ink)}
.live-bar b{color:var(--ink)}
.notice{margin-top:18px;padding:11px 14px;border-left:3px solid var(--force);
background:var(--panel);border-radius:3px;font-size:13px;color:var(--ink2)}
.notice b{color:var(--ink)}
.notice ul{margin:7px 0 0;padding-left:18px;font:11.5px/1.7 var(--mono)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--ink3);flex:none}
.dot.on{background:var(--accent);animation:pulse 1s infinite}
.dot.good{background:var(--ok)}.dot.bad{background:var(--force)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
@media(prefers-reduced-motion:reduce){.dot.on{animation:none}}
label.sw{display:flex;align-items:center;gap:6px;font:11px/1 var(--mono);color:var(--ink3);
cursor:pointer}
.tick{position:absolute;top:48px;width:1px;height:7px;background:var(--rule)}
.head{position:absolute;top:10px;width:2px;height:46px;background:var(--ink);
pointer-events:none;transition:left .09s linear}
.head::after{content:"";position:absolute;top:-6px;left:-5px;width:12px;height:12px;
border-radius:50%;background:var(--ink)}
.marker{position:absolute;top:2px;font:10px/1 var(--mono);color:var(--force);
transform:translateX(-50%);pointer-events:none}
.panes{display:grid;grid-template-columns:220px minmax(0,1fr);gap:1px;
background:var(--rule);border:1px solid var(--rule);border-top:0;
border-radius:0 0 4px 4px;overflow:hidden}
.list{background:var(--panel);max-height:360px;overflow-y:auto}
.row{padding:8px 12px;border-bottom:1px solid var(--rule);cursor:pointer;
font:11.5px/1.4 var(--mono);color:var(--ink2);display:flex;gap:8px}
.row:hover{background:var(--sunk)}
.row.sel{background:var(--sunk);color:var(--ink);box-shadow:inset 3px 0 0 var(--accent)}
.row.bad{color:var(--force)}
.row .n{color:var(--ink3);font-variant-numeric:tabular-nums}
.detail{background:var(--panel);padding:16px 18px;min-height:360px}
.dh{display:flex;flex-wrap:wrap;gap:8px 14px;align-items:baseline;margin-bottom:12px}
.dh .m{font:600 12px/1 var(--mono);color:var(--ink)}
.dh .u{font:12px/1.4 var(--mono);color:var(--ink2);word-break:break-all}
.st{font:600 11px/1 var(--mono);padding:3px 7px;border-radius:2px;
border:1px solid currentColor}
.st.ok{color:var(--ok)}.st.bad{color:var(--force)}
.warn{margin:10px 0 0;padding:9px 12px;border-left:2px solid var(--force);
background:var(--sunk);font-size:12.5px;color:var(--ink2)}
.warn b{color:var(--ink)}
.kv{margin-top:14px}
.kv h4{font:600 10px/1 var(--mono);letter-spacing:.14em;text-transform:uppercase;
color:var(--ink3);margin:0 0 6px}
pre{margin:0;background:var(--sunk);border-radius:3px;padding:11px 13px;
font:12px/1.55 var(--mono);color:var(--ink);overflow-x:auto;white-space:pre-wrap;
word-break:break-word}
</style></head><body><div class="wrap">
<header>
  <h1>__RUNID__</h1>
  <span class="badge __BCLS__">__STATUS__</span>
  <span class="meta">__WHEN__ &middot; <b>__NSTEPS__</b> steps &middot; <b>__DUR__</b></span>
  <span class="meta">__TAGS__</span>
</header>
__NOTICE__
<div class="stage">
  <div class="controls">
    <button id="play">&#9654; Play</button>
    <button id="prev">&larr;</button><button id="next">&rarr;</button>
    <span class="pos" id="pos"></span>
    <span class="hint">drag the track &middot; &larr; &rarr; step &middot; space to play</span>
  </div>
  __LIVE_CONTROLS__
  <div class="track" id="track">
    <div class="rail"></div><div class="head" id="head" style="left:0"></div>
  </div>
</div>
<div class="panes">
  <div class="list" id="list"></div>
  <div class="detail" id="detail"></div>
</div>
</div>
<script>
var S = __DATA__;
var track=document.getElementById('track'), head=document.getElementById('head'),
    list=document.getElementById('list'), detail=document.getElementById('detail'),
    pos=document.getElementById('pos'), playBtn=document.getElementById('play');
var total = 1; S.forEach(function(s){ total = Math.max(total, s.t0 + s.ms); });
var cur = 0, timer = null;

S.forEach(function(s, i){
  var seg=document.createElement('div');
  seg.className='seg'+(s.bad?' bad':''); seg.dataset.i=i;
  seg.style.left=(s.t0/total*100)+'%';
  seg.style.width=Math.max(s.ms/total*100, 0.9)+'%';
  seg.title=s.method+' '+s.short+' · '+s.ms.toFixed(0)+'ms';
  track.appendChild(seg);
  var t=document.createElement('div');
  t.className='tick'; t.style.left=(s.t0/total*100)+'%'; track.appendChild(t);
  if(s.bad || s.note){
    var m=document.createElement('div'); m.className='marker';
    m.style.left=(s.t0/total*100)+'%'; m.textContent='▲'; track.appendChild(m);
  }
  var r=document.createElement('div');
  r.className='row'+(s.bad?' bad':''); r.dataset.i=i;
  r.innerHTML='<span class="n">'+String(i).padStart(2,'0')+'</span><span>'
    +s.method+' '+s.short+'</span>';
  r.onclick=function(){ stop(); go(i); };
  list.appendChild(r);
});

function esc(x){
  return String(x).replace(/[&<>]/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c];
  });
}

function go(i){
  cur = Math.max(0, Math.min(S.length-1, i));
  var s = S[cur];
  head.style.left = ((s.t0 + s.ms/2)/total*100)+'%';
  pos.innerHTML = 'step '+String(cur).padStart(2,'0')
    +' <span>/ '+(S.length-1)+' · '+s.t0.toFixed(0)+'ms</span>';
  var segs=document.querySelectorAll('.seg');
  for(var a=0;a<segs.length;a++) segs[a].classList.toggle('sel', +segs[a].dataset.i===cur);
  var rows=document.querySelectorAll('.row');
  for(var b=0;b<rows.length;b++){
    var on = +rows[b].dataset.i===cur;
    rows[b].classList.toggle('sel', on);
    if(on) rows[b].scrollIntoView({block:'nearest'});
  }
  var h = '<div class="dh"><span class="m">'+s.method+'</span>'
    +'<span class="u">'+esc(s.url)+'</span>'
    +'<span class="st '+(s.bad?'bad':'ok')+'">'+(s.status||'GABIM')+'</span>'
    +'<span class="meta">'+s.ms.toFixed(0)+' ms</span></div>';
  if(s.side) h += '<div class="warn"><b>Side effect.</b> Replay forwards nothing '
    +'anywhere, so this did not fire a second time.</div>';
  if(s.note) h += '<div class="warn"><b>This is where it went wrong.</b> '+esc(s.note)+'</div>';
  h += '<div class="kv"><h4>Request</h4><pre>'+(esc(s.req)||'—')+'</pre></div>';
  h += '<div class="kv"><h4>Response</h4><pre>'+(esc(s.body)||'—')+'</pre></div>';
  detail.innerHTML = h;
}

function seek(ev){
  var r = track.getBoundingClientRect();
  var cx = ev.clientX;
  var x = Math.max(0, Math.min(1, (cx - r.left)/r.width));
  var t = x * total, best = 0, bd = 1e12;
  S.forEach(function(s, i){
    var d = Math.abs((s.t0 + s.ms/2) - t);
    if(d < bd){ bd = d; best = i; }
  });
  go(best);
}
var dragging = false;
track.addEventListener('pointerdown', function(e){
  dragging = true; stop(); track.setPointerCapture(e.pointerId); seek(e);
});
track.addEventListener('pointermove', function(e){ if(dragging) seek(e); });
track.addEventListener('pointerup', function(){ dragging = false; });

function stop(){
  if(timer){ clearInterval(timer); timer = null; }
  playBtn.innerHTML = '&#9654; Play';
}
playBtn.onclick = function(){
  if(timer){ stop(); return; }
  if(cur >= S.length-1) go(0);
  playBtn.innerHTML = '&#10073;&#10073; Pause';
  timer = setInterval(function(){
    if(cur >= S.length-1){ stop(); return; }
    go(cur+1);
  }, 700);
};
document.getElementById('prev').onclick = function(){ stop(); go(cur-1); };
document.getElementById('next').onclick = function(){ stop(); go(cur+1); };
addEventListener('keydown', function(e){
  if(e.key === 'ArrowLeft'){ stop(); go(cur-1); e.preventDefault(); }
  if(e.key === 'ArrowRight'){ stop(); go(cur+1); e.preventDefault(); }
  if(e.key === ' '){ playBtn.click(); e.preventDefault(); }
});
go(0);
__LIVE_JS__
</script></body></html>"""

LIVE_CONTROLS = """<div class="controls">
    <button id="rerun">&#9679; Replay for real</button>
    <label class="sw"><input type="checkbox" id="strict" checked> strict matching</label>
  </div>
  <div class="live-bar" id="live"><span class="dot" id="dot"></span>
    <span id="livetxt">Press <b>Replay for real</b> — the agent runs again, fed by the
    recording. No network call leaves this machine.</span></div>"""

LIVE_JS = r"""
// Only this page knows the token, and a custom header cannot be sent
// cross-origin without a preflight this server never answers. Without it any
// site the developer visits could POST to 127.0.0.1 and run their agent.
var TOKEN = "__TOKEN__";
var rerun=document.getElementById('rerun'), live=document.getElementById('live'),
    dot=document.getElementById('dot'), livetxt=document.getElementById('livetxt'),
    strictBox=document.getElementById('strict'), poller=null;

function setBar(cls, dcls, html){
  live.className='live-bar'+(cls?' '+cls:'');
  dot.className='dot'+(dcls?' '+dcls:'');
  livetxt.innerHTML=html;
}
function clearLive(){
  var segs=document.querySelectorAll('.seg');
  for(var i=0;i<segs.length;i++) segs[i].classList.remove('live','diverged');
}
function paint(events){
  for(var i=0;i<events.length;i++){
    var e=events[i];
    var seg=document.querySelector('.seg[data-i="'+(e.orig_i!==undefined&&e.orig_i!==null?e.orig_i:e.i)+'"]');
    if(!seg) seg=document.querySelectorAll('.seg')[Math.min(e.i,S.length-1)];
    if(seg) seg.classList.add(e.kind==='divergence'?'diverged':'live');
  }
}
rerun.onclick=function(){
  stop(); clearLive();
  setBar('on','on','Running the agent...');
  rerun.disabled=true;
  fetch('/api/replay',{method:'POST',
    headers:{'Content-Type':'application/json','X-Orientim-Token':TOKEN},
    body:JSON.stringify({strict:strictBox.checked})})
  .then(function(){ if(poller) clearInterval(poller); poller=setInterval(tick,120); })
  .catch(function(err){ setBar('bad','bad','Could not connect: '+err); rerun.disabled=false; });
};
function tick(){
  fetch('/api/progress',{headers:{'X-Orientim-Token':TOKEN}}).then(function(r){return r.json();}).then(function(p){
    paint(p.events);
    if(p.running){
      setBar('on','on','Running — <b>'+p.events.length+'</b> steps so far');
      if(p.events.length) go(Math.min(p.events.length-1, S.length-1));
      return;
    }
    if(!p.done) return;
    clearInterval(poller); poller=null; rerun.disabled=false;
    var r=p.result||{};
    if(r.ok){
      setBar('good','good','<b>'+(r.title||'Identical')+'.</b> '+(r.message||'')
        +' Hash <b>'+r.root_replay+'</b>'
        +(r.blocked?' &middot; <b>'+r.blocked+'</b> side effect blocked':''));
    } else {
      var h='<b>'+(r.title||'Diverged')+'.</b> '+(r.message||'');
      if(r.action) h+=' <span style="opacity:.75">&rarr; '+r.action+'</span>';
      setBar('bad','bad',h);
      if(r.index!==null&&r.index!==undefined) go(Math.min(r.index,S.length-1));
    }
  }).catch(function(){ clearInterval(poller); poller=null; rerun.disabled=false; });
}
"""


_JS_ESCAPES = (("&", "u0026"), ("<", "u003c"), (">", "u003e"),
               (" ", "u2028"), (" ", "u2029"))


def _embed(obj):
    """JSON safe to drop inside a <script> block.

    A recorded response can contain the literal string "</script>" — any
    agent that reads a web page will produce one sooner or later — and it
    closes the tag early, turning the rest of the recording into markup the
    browser runs. U+2028 and U+2029 are valid JSON but break a JavaScript
    string literal, so they go too.
    """
    raw = json.dumps(obj, ensure_ascii=False)
    for ch, code in _JS_ESCAPES:
        raw = raw.replace(ch, "\\" + code)
    return raw


def _notice(meta):
    """Say it on the page, not only in the replay report.

    A timeline that shows six steps when the run made eight is a lie told by
    omission. If part of the run went out through a library we do not
    intercept, the page says so above the steps it does have.
    """
    unseen = meta.get("unseen") or []
    if not unseen:
        return ""
    n = meta.get("unseen_n") or len(unseen)
    items = "".join("<li>%s</li>" % html.escape(u.get("detail", "?"))
                    for u in unseen[:6])
    more = ("<li>... and %d more</li>" % (n - 6)) if n > 6 else ""
    return ('<div class="notice"><b>This timeline is not the whole run.</b> '
            '%d call(s) left through a library Orientim does not intercept, so '
            'they are not below and they are not in the recording.'
            '<ul>%s%s</ul></div>' % (n, items, more))


def _note(step):
    """The one annotation: where an empty result was turned into an action."""
    try:
        j = json.loads(step.get("body") or "{}")
    except Exception:
        return ""
    if not isinstance(j, dict):
        return ""
    if "search" in step.get("url", "") and not j.get("hits"):
        return "The search returned nothing. Everything after this was built on it."
    return ""


def build(path, out=None, open_browser=True, live=False, token=""):
    meta, steps = store.load(path)
    if out is None:
        # The recording may live in a bucket; the page it renders always
        # lands on this machine.
        base = os.path.splitext(os.path.basename(path))[0]
        local_root = os.path.dirname(path)
        if not local_root or "://" in local_root:
            local_root = os.path.join(os.path.expanduser("~"), ".orientim", "views")
        os.makedirs(local_root, exist_ok=True)
        out = os.path.join(local_root, base + ".html")
    http = [s for s in steps if s.get("t") == "http"]
    data = []
    for s in http:
        url = s.get("url", "")
        status = s.get("status", 0)
        data.append({
            "method": s.get("method", ""),
            "url": url,
            "short": (url.rstrip("/").split("/")[-1] or "/")[:26],
            "status": status,
            "bad": status >= 400 or status == 0,
            "side": bool(s.get("side_effect")),
            "t0": float(s.get("t0", 0.0)) * 1000.0,
            "ms": float(s.get("ms", 0.0)),
            "body": ("<%d bytes, not text>" % len(s.get("body") or "")
                     if s.get("b64") else (s.get("body") or "")[:4000]),
            "req": (s.get("req") or "")[:4000],
            "note": _note(s),
        })
    dur = max((d["t0"] + d["ms"]) for d in data) if data else 0.0
    when = dt.datetime.fromtimestamp(meta["started_at"]).strftime("%d.%m.%Y %H:%M:%S")
    tags = " &middot; ".join(
        "<b>{}</b>={}".format(html.escape(k), html.escape(str(v)))
        for k, v in (meta.get("tags") or {}).items()
    )
    failed = meta.get("status") != "ok"
    doc = (TPL
           .replace("__TITLE__", "Orientim — " + html.escape(meta["run_id"]))
           .replace("__RUNID__", html.escape(meta["run_id"]))
           .replace("__STATUS__", "failed" if failed else "ok")
           .replace("__BCLS__", "b-fail" if failed else "b-ok")
           .replace("__WHEN__", when)
           .replace("__NSTEPS__", str(len(data)))
           .replace("__DUR__", "{:.0f} ms".format(dur))
           .replace("__TAGS__", tags)
           .replace("__NOTICE__", _notice(meta))
           .replace("__LIVE_CONTROLS__", LIVE_CONTROLS if live else "")
           .replace("__LIVE_JS__", LIVE_JS if live else "")
           .replace("__TOKEN__", token)
           .replace("__DATA__", _embed(data)))
    with open(out, "w", encoding="utf-8") as f:
        f.write(doc)
    if open_browser:
        webbrowser.open("file://" + os.path.abspath(out))
    return out
