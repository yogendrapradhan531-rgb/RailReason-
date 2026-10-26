"""
Two ways in: a chooser, and a plain view for people who want one answer.

The dashboard answers a lot of questions at once. Most people arriving at
this app have exactly one: how late is my train, and why. Showing them a
map, a connection calculator, a sharing panel and a calibration report to
get to that answer is a cost, not a feature.

So the chooser asks once, and Basic is deliberately small: a search box,
the delay, the reason, and the next few stops. No map, no forecasting
range, no sharing, no watching. Anyone who wants those is one click away.

Both screens use the same palette as the dashboard so the three do not
feel like three different products.
"""

import json

# shared shell so the chooser and the basic view cannot drift apart visually
_HEAD = """<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>__TITLE__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
 :root{--ink:#0B1218;--panel:#131E27;--lift:#1A2832;--line:#23343F;--text:#EDE6D6;
       --dim:#93A8B4;--faint:#5F7482;--lamp:#5B9DD9;--green:#4FAE73;--amber:#E7A33E;--red:#E35D42;}
 *{box-sizing:border-box}
 body{margin:0;min-height:100vh;background:var(--ink);color:var(--text);
      font-family:'IBM Plex Sans',system-ui,sans-serif;-webkit-font-smoothing:antialiased}
 .num{font-family:'Space Grotesk',sans-serif;font-variant-numeric:tabular-nums}
 button,input{font-family:inherit}
 :focus-visible{outline:2px solid var(--lamp);outline-offset:3px}
 a{color:var(--lamp)}
 .card{background:var(--panel);border:1px solid var(--line);border-radius:14px}
</style>"""


CHOOSER = """<!DOCTYPE html>
<html lang="en"><head>__HEAD__
<style>
 body{display:grid;place-items:center;padding:28px 18px}
 .box{width:100%;max-width:700px;text-align:center}
 .mark{width:54px;height:54px;border-radius:15px;background:var(--lift);border:1px solid var(--line);
       display:grid;place-items:center;margin:0 auto 18px;font-size:26px}
 h1{font-family:'Space Grotesk',sans-serif;font-size:31px;margin:0 0 8px;letter-spacing:-.01em}
 .sub{color:var(--dim);font-size:16px;margin:0 0 30px;line-height:1.5}
 .choices{display:grid;grid-template-columns:1fr 1fr;gap:15px;text-align:left}
 .choice{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:24px 22px;
         cursor:pointer;color:inherit;font:inherit;display:flex;flex-direction:column;gap:9px}
 .choice:hover{border-color:#2F5F84}
 .choice b{font-family:'Space Grotesk',sans-serif;font-size:19px;font-weight:600}
 .choice p{margin:0;color:var(--dim);line-height:1.55;font-size:14.5px}
 .choice ul{margin:3px 0 0;padding-left:17px;color:var(--dim);font-size:13.5px;line-height:1.75}
 .choice .go{margin-top:auto;padding-top:11px;font-weight:600;color:var(--lamp);font-size:14px}
 .remember{display:inline-flex;gap:8px;align-items:center;margin-top:24px;color:var(--dim);
           font-size:13.5px;cursor:pointer}
 .remember input{width:16px;height:16px;accent-color:var(--lamp)}
 .hint{color:var(--faint);font-size:12.5px;margin-top:8px}
 @media (max-width:620px){.choices{grid-template-columns:1fr}h1{font-size:26px}}
</style></head>
<body>
<div class="box">
  <div class="mark">&#128646;</div>
  <h1>RailReason</h1>
  <p class="sub">Find out how late your train is, and why.<br>How would you like to use it?</p>

  <div class="choices">
    <button class="choice" data-view="basic">
      <b>Just the basics</b>
      <p>One screen. Search a train and read the answer.</p>
      <ul><li>How late it is</li><li>Why it is late</li><li>When it reaches the next stops</li></ul>
      <div class="go">Use the simple view</div>
    </button>
    <button class="choice" data-view="advanced">
      <b>Everything</b>
      <p>The full dashboard, for planning around a delay.</p>
      <ul><li>Live map and arrival ranges</li><li>Connection risk and when to leave</li><li>Share the journey with someone</li></ul>
      <div class="go">Use the full dashboard</div>
    </button>
  </div>

  <label class="remember"><input type="checkbox" id="remember"> Remember this on this device</label>
  <p class="hint">You can switch at any time from the top of either screen.</p>
</div>
<script>
 // Honour a remembered choice, unless the person came here deliberately.
 try{
   const saved = localStorage.getItem('rr-view');
   if (saved && !location.search.includes('choose')) location.replace(saved === 'advanced' ? '/advanced' : '/basic');
 }catch(e){}

 document.querySelectorAll('.choice').forEach(b => b.addEventListener('click', () => {
   const v = b.dataset.view;
   try{
     if (document.getElementById('remember').checked) localStorage.setItem('rr-view', v);
     else localStorage.removeItem('rr-view');
   }catch(e){}
   location.href = v === 'advanced' ? '/advanced' : '/basic';
 }));
</script>
</body></html>"""


BASIC = """<!DOCTYPE html>
<html lang="en"><head>__HEAD__
<style>
 body{display:flex;justify-content:center;padding:20px 16px 60px}
 .wrap{width:100%;max-width:560px}
 .top{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:18px}
 .brand{font-family:'Space Grotesk',sans-serif;font-size:18px;font-weight:700}
 .switch{font-size:12.5px;color:var(--dim);text-decoration:none;border:1px solid var(--line);
         padding:6px 11px;border-radius:20px;background:var(--panel)}
 .switch:hover{border-color:#2F5F84;color:var(--text)}
 .find{display:flex;gap:8px;margin-bottom:16px}
 .find input{flex:1;min-width:0;padding:12px 14px;border-radius:10px;border:1px solid var(--line);
             background:var(--panel);color:var(--text);font-size:15px}
 .find input::placeholder{color:var(--faint)}
 .find button{padding:12px 18px;border-radius:10px;border:1px solid #2F5F84;background:#1B3A4F;
              color:var(--text);font-family:'Space Grotesk',sans-serif;font-size:14px;font-weight:600;cursor:pointer}
 .note{font-size:12.5px;color:var(--dim);margin-bottom:14px;min-height:18px}
 .pills{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:16px}
 .pill{font-family:'Space Grotesk',sans-serif;font-size:12.5px;font-weight:600;padding:6px 13px;
       border-radius:20px;border:1px solid var(--line);background:transparent;color:var(--dim);cursor:pointer}
 .pill.on{background:var(--lift);color:var(--text);border-color:#3A4F6B}
 .pad{padding:18px}
 .tno{font-size:12.5px;color:var(--faint)}
 .tname{font-family:'Space Grotesk',sans-serif;font-size:19px;font-weight:600;margin:3px 0 4px;line-height:1.3}
 .troute{font-size:13px;color:var(--dim)}
 .big{font-family:'Space Grotesk',sans-serif;font-size:46px;font-weight:700;line-height:1.05}
 .lab{font-size:12.5px;color:var(--faint);margin-top:5px}
 .why{font-size:15px;line-height:1.55;margin-top:4px}
 .stop{display:flex;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid #1B2A34}
 .stop:last-child{border-bottom:none}
 .sname{font-size:14.5px;font-weight:500}
 .smeta{font-size:12px;color:var(--faint);margin-top:2px}
 .sdel{font-family:'Space Grotesk',sans-serif;font-size:14px;font-weight:600;white-space:nowrap}
 .head{font-size:12.5px;color:var(--dim);font-weight:600;margin-bottom:6px}
 .foot{font-size:12px;color:var(--faint);text-align:center;margin-top:20px;line-height:1.6}
 .empty{padding:40px 20px;text-align:center;color:var(--dim);font-size:14.5px;line-height:1.6}
 #m{height:220px;width:100%;background:var(--panel);border-radius:14px 14px 0 0}
 .mapwrap{border:1px solid var(--line);border-radius:14px;overflow:hidden;margin-bottom:12px}
 .mapfoot{font-size:10.5px;color:var(--faint);padding:7px 12px;border-top:1px solid var(--line)}
 .leaflet-tile-pane{filter:invert(1) hue-rotate(180deg) brightness(.92) contrast(.86) saturate(.85)}
 .leaflet-popup-content-wrapper,.leaflet-popup-tip{background:#16242E;color:#EDE6D6}
 .leaflet-popup-content{font-family:'IBM Plex Sans',sans-serif;font-size:12.5px}
 .leaflet-container a.leaflet-popup-close-button{color:#93A8B4}
</style></head>
<body>
<div class="wrap">
  <div class="top">
    <div class="brand">&#128646; RailReason</div>
    <a class="switch" href="/advanced">Full dashboard</a>
  </div>

  <div class="find">
    <input id="q" inputmode="numeric" placeholder="Train number, e.g. 12951"
           onkeydown="if(event.key==='Enter') lookup()">
    <button onclick="lookup()">Check</button>
  </div>
  <div class="note" id="note"></div>
  <div class="pills" id="pills"></div>
  <div id="out"></div>

  <div class="foot">
    Times are estimates from live data, not promises.<br>
    <a href="/?choose=1">Change how you use this app</a>
  </div>
</div>
<script>
const CAUSES = __CAUSES__;
let DATA = {}, current = null;

function colour(m){ return m < 10 ? 'var(--green)' : (m < 45 ? 'var(--amber)' : 'var(--red)'); }

async function refresh(){
  try{
    const r = await fetch('/api/live');
    if(!r.ok) return;
    DATA = await r.json();
    if(!current || !DATA[current]) current = Object.keys(DATA)[0];
    draw();
  }catch(e){}
}

async function lookup(){
  const input = document.getElementById('q');
  const note = document.getElementById('note');
  const no = input.value.trim();
  if(!no) return;
  if(DATA[no]){ current = no; input.value=''; note.textContent=''; draw(); return; }

  note.textContent = 'Looking for train ' + no + '.';
  note.style.color = 'var(--dim)';
  try{
    const r = await fetch('/api/track', {method:'POST', headers:{'Content-Type':'application/json'},
                                          body: JSON.stringify({train_no: no})});
    const b = await r.json();
    if(!r.ok){ note.textContent = b.error || 'That train could not be found.'; note.style.color='var(--red)'; return; }
    note.textContent = '';
    input.value = '';
    current = b.train_no;
    await refresh();
  }catch(e){ note.textContent = 'The server did not respond.'; note.style.color='var(--red)'; }
}

function draw(){
  const pills = document.getElementById('pills');
  pills.innerHTML = Object.keys(DATA).map(k =>
    `<button class="pill ${k===current?'on':''}" onclick="current='${k}';draw()">${k}</button>`).join('');

  const out = document.getElementById('out');
  const t = DATA[current];
  if(!t){ out.innerHTML = '<div class="card empty">Search a train number above to see how late it is.</div>'; return; }

  const done = t.stations.filter(s => s.status === 'completed');
  const ahead = t.stations.filter(s => s.status === 'predicted');
  const last = done[done.length-1];
  const notStarted = t.run_status === 'not-started';
  const d = t.current_delay || 0;

  // The reason, in one line. This is the whole point of the simple view, so
  // it gets its own card rather than being buried in a list.
  let why = 'Nothing notable has happened so far.';
  const withCause = done.filter(s => s.cause && s.cause !== 'NONE');
  if(notStarted) why = 'It has not left yet, so there is nothing to explain.';
  else if(withCause.length){
    const latest = withCause[withCause.length-1];
    why = latest.explanation || (CAUSES[latest.cause] || {}).label || why;
  }

  const hasMap = t.stations.some(s => s.lat !== undefined && s.lng !== undefined);

  const stops = ahead.slice(0,5).map(s => `
    <div class="stop">
      <div><div class="sname">${s.station_name}</div>
           <div class="smeta">due ${s.scheduled_clock}</div></div>
      <div style="text-align:right">
        <div class="sdel" style="color:${colour(s.predicted_delay_p50||0)}">${s.predicted_clock_p50}</div>
        <div class="smeta">${(s.predicted_delay_p50||0) > 0.5 ? 'about ' + Math.round(s.predicted_delay_p50) + ' min late' : 'about on time'}</div>
      </div>
    </div>`).join('');

  out.innerHTML = `
    <div class="card pad" style="margin-bottom:12px">
      <div class="tno">Train ${t.train_no}</div>
      <div class="tname">${t.train_name}</div>
      <div class="troute">${t.stations[0].station_name} to ${t.stations[t.stations.length-1].station_name}</div>
    </div>

    <div class="card pad" style="margin-bottom:12px">
      <div class="big" style="color:${notStarted ? 'var(--dim)' : colour(d)}">
        ${notStarted ? 'Not started' : (d > 0.5 ? '+' + Math.round(d) + '<span style="font-size:17px;font-weight:500"> min late</span>' : 'On time')}
      </div>
      <div class="lab">${notStarted ? 'waiting to depart' : 'last seen at ' + (last ? last.station_name : '')}</div>
    </div>

    ${hasMap ? `<div class="mapwrap"><div id="m"></div>
      <div class="mapfoot">Map by OpenStreetMap. The marker is an estimate along a straight line
      between stations, not the real track.</div></div>` : ''}

    <div class="card pad" style="margin-bottom:12px">
      <div class="head">Why</div>
      <div class="why">${why}</div>
    </div>

    ${stops ? `<div class="card pad"><div class="head">Next stops</div>${stops}</div>` : ''}`;

  if (hasMap) drawMap(t);
}

// Rebuilt on each draw rather than updated in place. The simple view has one
// train on screen at a time and redraws about once a minute, so the extra work
// is not worth the bookkeeping of keeping a map instance in sync.
let map = null;
function drawMap(t){
  const el = document.getElementById('m');
  if (map) { map.remove(); map = null; }
  if (!el) return;

  const pts = t.stations.filter(s => s.lat !== undefined && s.lng !== undefined);
  if (!pts.length) return;

  map = L.map('m', {zoomControl:false, attributionControl:false});
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom:15}).addTo(map);

  const line = pts.map(s => [s.lat, s.lng]);
  L.polyline(line, {color:'#5B9DD9', weight:3, opacity:.75}).addTo(map);
  pts.forEach(s => {
    const c = s.status === 'completed' ? '#4FAE73' : '#5F7482';
    L.circleMarker([s.lat, s.lng], {radius:4, weight:1, color:c, fillColor:c, fillOpacity:1})
     .bindPopup('<b>' + s.station_name + '</b>').addTo(map);
  });

  let bounds = L.latLngBounds(line);
  if (t.live_position) {
    const icon = L.divIcon({className:'', iconSize:[16,16], iconAnchor:[8,8],
      html:'<div style="position:relative;width:16px;height:16px;">' +
           '<div style="width:16px;height:16px;border-radius:50%;background:#4FAE73;opacity:.5;position:absolute;"></div>' +
           '<div style="width:12px;height:12px;border-radius:50%;background:#4FAE73;border:2px solid #0B1218;position:absolute;top:2px;left:2px;"></div></div>'});
    L.marker([t.live_position.lat, t.live_position.lng], {icon}).addTo(map)
     .bindPopup('Where the train is now');
    bounds.extend([t.live_position.lat, t.live_position.lng]);
  }
  map.fitBounds(bounds, {padding:[22,22]});
}

refresh();
setInterval(refresh, 60000);
</script>
</body></html>"""


def render_chooser():
    return CHOOSER.replace("__HEAD__", _HEAD.replace("__TITLE__", "RailReason"))


def render_basic(cause_meta):
    page = BASIC.replace("__HEAD__", _HEAD.replace("__TITLE__", "RailReason"))
    return page.replace("__CAUSES__", json.dumps(cause_meta))
