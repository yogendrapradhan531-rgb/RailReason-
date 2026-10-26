"""
Shared HTML/CSS/JS shell for the railway ETA dashboard, used by both:
  - build_dashboard.py  (static file, data embedded, demo mode)
  - server.py            (live file, data polled from /api/live)

Only the small "bootstrap" script differs between the two — everything
else (styling, rendering, map, charts) is shared so the two modes never
visually drift apart.

Design note: the palette is railway signalling (green/amber/red aspects)
on ink-dark ground with warm parchment type, rather than the blue-violet
gradient common to dashboard templates. The cause colours ARE the signal
aspects, so colour carries meaning everywhere it appears.
"""

import json

CAUSE_META = {
    "NONE":          {"label": "On schedule",     "color": "#55606C", "short": "on time"},
    "HALT_OVERRUN":  {"label": "Halt overrun",     "color": "#E7A33E", "short": "long halt"},
    "CROSSING_WAIT": {"label": "Crossing wait",    "color": "#E35D42", "short": "held for crossing"},
    "SECTION_SLOW":  {"label": "Section slowdown", "color": "#C97A3D", "short": "slow section"},
    "FOG":           {"label": "Fog",              "color": "#7FA3B8", "short": "fog"},
    "TECHNICAL":     {"label": "Technical fault",  "color": "#C43B3B", "short": "technical fault"},
    "CASCADING":     {"label": "Late start",       "color": "#8B6FD1", "short": "late start"},
    "RECOVERY":      {"label": "Time recovered",   "color": "#4FAE73", "short": "recovered time"},
}

_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Live train tracking and delay forecasting</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  :root {
    --ink: #0B1218;
    --panel: #131E27;
    --panel-lift: #1A2832;
    --line: #23343F;
    --line-soft: #1B2A34;
    --text: #EDE6D6;
    --dim: #93A8B4;
    --faint: #5F7482;
    --lamp: #5B9DD9;
    --green: #4FAE73;
    --amber: #E7A33E;
    --red: #E35D42;
    --radius: 12px;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0; background: var(--ink); color: var(--text);
    font-family: 'IBM Plex Sans', system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .num { font-family: 'Space Grotesk', system-ui, sans-serif; font-variant-numeric: tabular-nums; }
  button, input, select { font-family: inherit; }
  :focus-visible { outline: 2px solid var(--lamp); outline-offset: 2px; }
  @media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }

  /* ---------- shell ---------- */
  .shell { display: grid; grid-template-columns: 216px 1fr; min-height: 100vh; }

  .rail {
    background: #0E1820; border-right: 1px solid var(--line-soft);
    padding: 20px 14px; display: flex; flex-direction: column; gap: 26px;
  }
  .brand { display: flex; align-items: center; gap: 10px; padding: 0 6px; }
  .brand-mark {
    width: 34px; height: 34px; border-radius: 9px; flex-shrink: 0;
    background: #16303F; border: 1px solid #274657;
    display: grid; place-items: center; font-size: 17px;
  }
  .brand-name { font-family: 'Space Grotesk', sans-serif; font-size: 16px; font-weight: 700; letter-spacing: -0.01em; }
  .brand-sub { font-size: 11px; color: var(--faint); margin-top: 1px; }

  .nav { display: flex; flex-direction: column; gap: 2px; }
  .nav-item {
    display: flex; align-items: center; gap: 10px; width: 100%;
    padding: 9px 10px; border-radius: 8px; border: 1px solid transparent;
    background: transparent; color: var(--dim); font-size: 13.5px; cursor: pointer; text-align: left;
  }
  .nav-item:hover { color: var(--text); background: var(--panel); }
  .nav-item.on { color: var(--text); background: var(--panel-lift); border-color: var(--line); }
  .nav-glyph { width: 16px; text-align: center; opacity: .9; }

  .rail-foot { margin-top: auto; font-size: 11px; color: var(--faint); line-height: 1.5; padding: 0 6px; }

  /* ---------- top bar ---------- */
  .main { display: flex; flex-direction: column; min-width: 0; }
  .topbar {
    display: flex; align-items: center; gap: 16px; flex-wrap: wrap;
    padding: 14px 20px; border-bottom: 1px solid var(--line-soft); background: #0E1820;
    position: sticky; top: 0; z-index: 30;
  }
  .finder { display: flex; align-items: center; gap: 8px; flex: 1; min-width: 260px; }
  .finder input {
    flex: 1; min-width: 0; padding: 9px 13px; border-radius: 9px;
    border: 1px solid var(--line); background: var(--panel); color: var(--text); font-size: 13.5px;
  }
  .finder input::placeholder { color: var(--faint); }
  .finder button {
    padding: 9px 16px; border-radius: 9px; border: 1px solid #2F5F84;
    background: #1B3A4F; color: var(--text);
    font-family: 'Space Grotesk', sans-serif; font-size: 13px; font-weight: 600; cursor: pointer;
  }
  .finder button:hover { background: #224961; }
  .finder-note { font-size: 12px; }
  .topbar-meta { display: flex; align-items: center; gap: 18px; font-size: 12.5px; color: var(--dim); }
  .topbar-meta .num { font-size: 15px; color: var(--text); }

  /* ---------- content grid ---------- */
  .content { padding: 18px 20px 40px; display: flex; flex-direction: column; gap: 16px; }
  .cols { display: grid; grid-template-columns: 290px minmax(0,1fr) 320px; gap: 16px; align-items: start; }
  @media (max-width: 1240px) { .cols { grid-template-columns: 1fr 320px; } .col-spine { grid-column: 1 / -1; } }
  @media (max-width: 900px)  { .shell { grid-template-columns: 1fr; } .rail { flex-direction: row; align-items: center; gap: 16px; overflow-x: auto; }
                               .nav { flex-direction: row; } .rail-foot { display: none; } .cols { grid-template-columns: 1fr; } }

  .card { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); }
  .card-pad { padding: 15px 16px; }
  .card-head { font-size: 12.5px; color: var(--dim); font-weight: 600; margin-bottom: 12px; }
  .card-head span { color: var(--faint); font-weight: 400; }

  /* ---------- headline strip ---------- */
  .headline { display: grid; grid-template-columns: minmax(240px,1.4fr) repeat(3, minmax(120px,1fr)); gap: 0; overflow: hidden; }
  @media (max-width: 900px) { .headline { grid-template-columns: 1fr 1fr; } }
  .headline > div { padding: 16px 18px; border-right: 1px solid var(--line-soft); }
  .headline > div:last-child { border-right: none; }
  .train-no { font-size: 12.5px; color: var(--faint); }
  .train-name { font-family: 'Space Grotesk', sans-serif; font-size: 19px; font-weight: 600; margin: 2px 0 4px; line-height: 1.25; }
  .train-od { font-size: 12.5px; color: var(--dim); }
  .pill { display: inline-flex; align-items: center; gap: 6px; font-size: 11.5px; padding: 3px 9px; border-radius: 20px; margin-top: 9px; }
  .metric-label { font-size: 11.5px; color: var(--faint); }
  .metric-value { font-family: 'Space Grotesk', sans-serif; font-size: 26px; font-weight: 700; line-height: 1.15; margin-top: 3px; }
  .metric-sub { font-size: 11.5px; color: var(--dim); margin-top: 2px; }

  /* ---------- route spine (the bold element) ---------- */
  .spine { position: relative; padding-left: 4px; }
  .spine-stop { position: relative; padding: 0 0 0 26px; cursor: pointer; }
  .spine-stop:not(:last-child)::before {
    content: ''; position: absolute; left: 6px; top: 16px; bottom: -4px; width: 2px; background: var(--line);
  }
  .spine-stop.ahead:not(:last-child)::before { background: repeating-linear-gradient(to bottom, var(--line) 0 4px, transparent 4px 8px); }
  .spine-dot { position: absolute; left: 0; top: 9px; width: 14px; height: 14px; border-radius: 50%; border: 2px solid var(--panel); z-index: 2; }
  .spine-stop.ahead .spine-dot { background: var(--ink); border: 2px solid var(--faint); }
  .spine-row { display: flex; justify-content: space-between; align-items: baseline; gap: 10px; padding: 7px 0; }
  .spine-name { font-size: 13.5px; font-weight: 500; }
  .spine-meta { font-size: 11.5px; color: var(--faint); margin-top: 1px; }
  .spine-delay { font-family: 'Space Grotesk', sans-serif; font-size: 13px; font-weight: 600; white-space: nowrap; }
  .spine-stop.quiet .spine-name { color: var(--dim); }
  .now-band {
    display: flex; align-items: center; gap: 9px; margin: 4px 0 4px -4px; padding: 7px 10px;
    background: var(--panel-lift); border: 1px solid var(--line); border-radius: 8px; font-size: 11.5px; color: var(--text);
  }
  .now-lamp { width: 9px; height: 9px; border-radius: 50%; background: var(--green); box-shadow: 0 0 0 4px rgba(79,174,115,.16); }
  .detail { max-height: 0; overflow: hidden; transition: max-height .2s ease; font-size: 12.5px; color: var(--dim); line-height: 1.55; }
  .spine-stop.open .detail { max-height: 260px; padding-bottom: 10px; }
  .detail-in { border-top: 1px dashed var(--line); margin-top: 2px; padding-top: 8px; }
  .tag { display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 5px; margin-bottom: 6px; font-weight: 600; }
  .band-row { display: flex; justify-content: space-between; font-size: 10.5px; color: var(--faint); }
  .band { position: relative; height: 5px; border-radius: 3px; background: var(--panel-lift); margin: 5px 0 4px; }
  .band .fill { position: absolute; height: 100%; border-radius: 3px; background: #2F5165; }
  .band .mid { position: absolute; top: -3px; width: 2px; height: 11px; background: var(--text); border-radius: 1px; }

  /* ---------- map ---------- */
  #live-map { height: 300px; width: 100%; background: var(--panel); border-radius: var(--radius) var(--radius) 0 0; }
  /* OpenStreetMap tiles need no API key; this filter gives them a dark cast
     to match the UI. Applied to the tile pane only, so route lines, station
     dots and popups keep their true signal colours. */
  .leaflet-tile-pane { filter: invert(1) hue-rotate(180deg) brightness(0.92) contrast(0.86) saturate(0.85); }
  .leaflet-popup-content-wrapper, .leaflet-popup-tip { background: #16242E; color: #EDE6D6; }
  .leaflet-popup-content { font-family: 'IBM Plex Sans', sans-serif; font-size: 12.5px; }
  .leaflet-container a.leaflet-popup-close-button { color: #93A8B4; }
  .leaflet-control-zoom a { background: #16242E !important; color: #EDE6D6 !important; border-color: #23343F !important; }
  .map-foot { font-size: 10.5px; color: var(--faint); padding: 7px 14px; border-top: 1px solid var(--line-soft); }
  .train-marker { position: relative; width: 16px; height: 16px; }
  .train-marker .dot { width: 12px; height: 12px; border-radius: 50%; background: var(--green); border: 2px solid var(--ink); position: absolute; top: 2px; left: 2px; z-index: 2; }
  .train-marker .pulse { width: 16px; height: 16px; border-radius: 50%; background: var(--green); position: absolute; top: 0; left: 0; opacity: .5; animation: ping 1.8s ease-out infinite; }
  @keyframes ping { 0% { transform: scale(.6); opacity: .5; } 70%,100% { transform: scale(2.2); opacity: 0; } }

  /* ---------- tables ---------- */
  table.grid { width: 100%; border-collapse: collapse; font-size: 13px; }
  table.grid th { text-align: left; font-size: 11.5px; color: var(--faint); font-weight: 500; padding: 0 10px 8px 0; border-bottom: 1px solid var(--line-soft); }
  table.grid td { padding: 9px 10px 9px 0; border-bottom: 1px solid var(--line-soft); }
  table.grid tr:last-child td { border-bottom: none; }
  .chip { display: inline-block; font-family: 'Space Grotesk', sans-serif; font-size: 12px; font-weight: 600; padding: 2px 8px; border-radius: 5px; }
  .range-txt { font-size: 12px; color: var(--dim); }

  /* ---------- right column ---------- */
  .quant { display: grid; grid-template-columns: repeat(3,1fr); gap: 8px; }
  .quant div { padding: 11px 10px; border-radius: 9px; border: 1px solid var(--line); background: var(--panel-lift); }
  .quant .q-num { font-family: 'Space Grotesk', sans-serif; font-size: 19px; font-weight: 700; }
  .quant .q-lab { font-size: 10.5px; color: var(--faint); margin-top: 2px; }
  .wx { display: flex; align-items: center; gap: 14px; }
  .wx-temp { font-family: 'Space Grotesk', sans-serif; font-size: 27px; font-weight: 700; }
  .wx-desc { font-size: 12.5px; color: var(--dim); }
  .wx-list { margin-left: auto; font-size: 11.5px; color: var(--dim); text-align: right; line-height: 1.7; }
  .cause-row { display: grid; grid-template-columns: 1fr 78px 34px; align-items: center; gap: 9px; margin-bottom: 8px; font-size: 12.5px; }
  .cause-bar { height: 6px; border-radius: 3px; background: var(--panel-lift); overflow: hidden; }
  .cause-bar i { display: block; height: 100%; border-radius: 3px; }
  .hist-chips { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 11px; }
  .hist-chip {
    display: inline-flex; align-items: center; gap: 6px;
    font-size: 12px; padding: 4px 9px; border-radius: 7px;
    background: var(--panel-lift); border: 1px solid var(--line); color: var(--dim);
    white-space: nowrap;
  }
  .hist-chip b { color: var(--text); font-weight: 600; }
  .cause-pct { font-family: 'Space Grotesk', sans-serif; font-size: 12px; color: var(--dim); text-align: right; }
  .risk-head { display: flex; align-items: center; gap: 8px; font-family: 'Space Grotesk', sans-serif; font-size: 15px; font-weight: 600; }
  .advisor select, .advisor button {
    width: 100%; padding: 9px 11px; border-radius: 9px; border: 1px solid var(--line);
    background: var(--panel-lift); color: var(--text); font-size: 13px; margin-bottom: 8px;
  }
  .advisor button { cursor: pointer; font-family: 'Space Grotesk', sans-serif; font-weight: 600; }
  .advisor button:hover { border-color: #2F5F84; }
  .advisor input {
    width: 100%; padding: 9px 11px; border-radius: 9px; border: 1px solid var(--line);
    background: var(--panel-lift); color: var(--text); font-size: 13px; margin-bottom: 8px;
  }
  .advisor input::placeholder { color: var(--faint); }

  .watch-banner {
    display: flex; align-items: center; gap: 14px;
    padding: 14px 16px; margin-bottom: 4px;
    background: var(--panel); border: 1px solid var(--line);
    border-left-width: 4px; border-radius: var(--radius);
  }
  .watch-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
  .watch-stop {
    flex-shrink: 0; padding: 6px 12px; border-radius: 8px; cursor: pointer;
    border: 1px solid var(--line); background: var(--panel-lift); color: var(--dim); font-size: 12px;
  }
  .watch-stop:hover { color: var(--text); }
  @media (max-width: 700px) { .watch-banner { flex-wrap: wrap; } }

  .net-row { display: flex; justify-content: space-between; align-items: center; gap: 10px; padding: 11px 13px; margin-bottom: 8px; border-radius: 10px; background: var(--panel); border: 1px solid var(--line); cursor: pointer; }
  .net-row:hover { border-color: #2F5F84; }
  .empty { font-size: 13px; color: var(--dim); padding: 20px; text-align: center; }
</style>
</head>
<body>
<div id="app"></div>

<script>
const CAUSE_META = __CAUSE_META_JSON__;

__BOOTSTRAP__

let networkViewActive = false;
let leafletMap = null;
let ACCURACY = null;
// train_no (string) -> { token, url, expires_at }. Kept in JS state, not just
// in the DOM, because render() rebuilds #app from scratch on every poll —
// without this, a "Stop sharing" button reverts to "Create a link" the
// moment the next refresh() cycle runs (most visibly right after switching
// back to the tab, when throttled timers fire in a catch-up burst).
let activeShares = {};

/* ============ helpers ============ */
function clock(mins) {
  mins = Math.round(mins);
  const day = Math.floor(mins / 1440);
  const h = Math.floor((mins % 1440) / 60);
  const m = mins % 60;
  const hh = String(h).padStart(2, '0');
  const mm = String(m).padStart(2, '0');
  return day > 0 ? `Day ${day+1} ${hh}:${mm}` : `${hh}:${mm}`;
}
function delayColor(min) { return min < 10 ? 'var(--green)' : (min < 45 ? 'var(--amber)' : 'var(--red)'); }
function signed(min) { return min > 0.5 ? '+' + Math.round(min) : (min < -0.5 ? Math.round(min) : '0'); }
function completedStops(trip) { return trip.stations.filter(s => s.status === 'completed'); }
function aheadStops(trip) { return trip.stations.filter(s => s.status === 'predicted'); }

function narrative(trip) {
  const origin = trip.stations[0];
  const originDelay = (origin && origin.status === 'completed') ? origin.actual_delay_min : 0;
  const withCause = completedStops(trip).filter(s => s.cause !== 'NONE');
  const counts = {};
  withCause.forEach(s => { counts[s.cause] = (counts[s.cause] || 0) + 1; });
  const ranked = Object.entries(counts).sort((a,b) => b[1]-a[1]);

  if (!ranked.length && originDelay <= 0.5) return 'Running close to schedule, with no notable delay events so far.';
  if (!ranked.length) return `Started ${Math.round(originDelay)} minutes late and has held that gap since, with no new delay events.`;
  const parts = ranked.slice(0,2).map(([c,n]) => `${CAUSE_META[c].short} at ${n} stop${n>1?'s':''}`);
  const lead = originDelay > 0.5 ? `Started ${Math.round(originDelay)} minutes late. Since then, ` : 'So far the delay comes ';
  return `${lead}most of the change comes from ${parts.join(' and ')}.`;
}

/* ============ route spine ============ */
function renderStop(s) {
  if (s.status === 'completed') {
    const m = CAUSE_META[s.cause];
    const quiet = s.cause === 'NONE';
    return `
      <div class="spine-stop ${quiet?'quiet':''}" onclick="toggleStop(this)">
        <div class="spine-dot" style="background:${m.color}"></div>
        <div class="spine-row">
          <div>
            <div class="spine-name">${s.station_name}</div>
            <div class="spine-meta">${s.scheduled_clock} scheduled${s.actual_clock && s.actual_clock !== s.scheduled_clock ? ', ' + s.actual_clock + ' actual' : ''}</div>
          </div>
          <div class="spine-delay" style="color:${quiet?'var(--faint)':m.color}">${s.actual_delay_min > 0.5 ? '+'+Math.round(s.actual_delay_min)+'m' : 'on time'}</div>
        </div>
        <div class="detail"><div class="detail-in">
          <span class="tag" style="background:${m.color}22;color:${m.color}">${m.label}</span>
          <div>${s.explanation}</div>
        </div></div>
      </div>`;
  }
  const risky = s.network_risk && s.network_risk.length;
  const scale = 180;
  return `
    <div class="spine-stop ahead" onclick="toggleStop(this)">
      <div class="spine-dot"></div>
      <div class="spine-row">
        <div>
          <div class="spine-name">${s.station_name}${risky?' <span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--red);vertical-align:middle;margin-left:4px;"></span>':''}</div>
          <div class="spine-meta">${s.scheduled_clock} scheduled, ${s.predicted_clock_p50} expected</div>
        </div>
        <div class="spine-delay" style="color:var(--dim)">~${signed(s.predicted_delay_p50)}m</div>
      </div>
      <div class="detail"><div class="detail-in">
        <div class="band-row"><span>best ${signed(s.predicted_delay_p10)}m</span><span>likely ${signed(s.predicted_delay_p50)}m</span><span>worst ${signed(s.predicted_delay_p90)}m</span></div>
        <div class="band">
          <div class="fill" style="left:${(s.predicted_delay_p10/scale*100).toFixed(1)}%;width:${((s.predicted_delay_p90-s.predicted_delay_p10)/scale*100).toFixed(1)}%"></div>
          <div class="mid" style="left:${(s.predicted_delay_p50/scale*100).toFixed(1)}%"></div>
        </div>
        <div style="margin-top:6px;color:var(--faint)">Range from 300 simulated runs of this stretch.</div>
        ${risky ? `<div style="margin-top:9px;padding-top:8px;border-top:1px dashed var(--line);">
          <div style="color:var(--text);font-weight:600;margin-bottom:4px;">Crossing conflict possible</div>
          ${s.network_risk.map(r => `<div>Train ${r.train_no}${r.train_name?' ('+r.train_name+')':''} is due here around the same time, running about ${Math.round(r.delay_min)} minutes late.</div>`).join('')}
        </div>` : ''}
      </div></div>
    </div>`;
}
function toggleStop(el) { el.classList.toggle('open'); }

function buildSpine(trip) {
  const done = completedStops(trip).length;
  const last = completedStops(trip).slice(-1)[0];
  return trip.stations.map((s, i) => {
    const html = renderStop(s);
    if (s.status === 'completed' && i === done - 1 && last) {
      const waiting = trip.run_status === 'not-started';
      return html + `<div class="now-band"><span class="now-lamp" style="${waiting?'background:var(--faint);box-shadow:none;':''}"></span>${waiting ? 'Waiting to depart from ' + last.station_name : 'Train is here now, at ' + last.station_name}</div>`;
    }
    return html;
  }).join('');
}

/* ============ charts ============ */
function buildTrendChart(trip) {
  const done = completedStops(trip);
  const ahead = aheadStops(trip);
  if (!ahead.length) return '<div class="empty">No upcoming stations to forecast.</div>';

  const anchor = done.length ? done[done.length-1].actual_delay_min : 0;
  const p10 = [anchor].concat(ahead.map(s => s.predicted_delay_p10));
  const p50 = [anchor].concat(ahead.map(s => s.predicted_delay_p50));
  const p90 = [anchor].concat(ahead.map(s => s.predicted_delay_p90));
  const labels = [done.length ? done[done.length-1].station_name : 'Origin'].concat(ahead.map(s => s.station_name));

  const w = 720, h = 190, padL = 34, padR = 12, padT = 12, padB = 30;
  const maxV = Math.max(...p90, 10);
  const stepX = (w - padL - padR) / Math.max(p50.length - 1, 1);
  const X = i => padL + i * stepX;
  const Y = v => h - padB - (v / maxV) * (h - padT - padB);
  const path = arr => arr.map((v,i) => `${i?'L':'M'}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(' ');
  const areaPath = `${path(p90)} L${X(p10.length-1).toFixed(1)},${Y(p10[p10.length-1]).toFixed(1)} ${p10.slice().reverse().map((v,i)=>`L${X(p10.length-1-i).toFixed(1)},${Y(v).toFixed(1)}`).join(' ')} Z`;

  const gridY = [0, maxV/2, maxV].map(v =>
    `<line x1="${padL}" y1="${Y(v).toFixed(1)}" x2="${w-padR}" y2="${Y(v).toFixed(1)}" stroke="#1B2A34" stroke-width="1"/>
     <text x="4" y="${(Y(v)+4).toFixed(1)}" fill="#5F7482" font-size="10">${Math.round(v)}</text>`).join('');

  const showEvery = Math.ceil(labels.length / 6);
  const xLabels = labels.map((l,i) => (i % showEvery === 0 || i === labels.length-1)
    ? `<text x="${X(i).toFixed(1)}" y="${h-10}" fill="#5F7482" font-size="10" text-anchor="middle">${l.length>12?l.slice(0,11)+'\u2026':l}</text>` : '').join('');

  return `
    <svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" preserveAspectRatio="none" style="display:block">
      ${gridY}
      <path d="${areaPath}" fill="#5B9DD9" opacity="0.10"/>
      <path d="${path(p90)}" fill="none" stroke="#8B6FD1" stroke-width="1.5" stroke-dasharray="4 3"/>
      <path d="${path(p10)}" fill="none" stroke="#4FAE73" stroke-width="1.5" stroke-dasharray="4 3"/>
      <path d="${path(p50)}" fill="none" stroke="#5B9DD9" stroke-width="2.5"/>
      ${p50.map((v,i)=>`<circle cx="${X(i).toFixed(1)}" cy="${Y(v).toFixed(1)}" r="3" fill="#5B9DD9"/>`).join('')}
      ${xLabels}
    </svg>
    <div style="display:flex;gap:16px;font-size:11.5px;color:var(--dim);padding:8px 2px 0;">
      <span><i style="display:inline-block;width:14px;height:2px;background:#4FAE73;vertical-align:middle;"></i> best case</span>
      <span><i style="display:inline-block;width:14px;height:3px;background:#5B9DD9;vertical-align:middle;"></i> most likely</span>
      <span><i style="display:inline-block;width:14px;height:2px;background:#8B6FD1;vertical-align:middle;"></i> worst case</span>
    </div>`;
}

function buildCauseBars(trip) {
  const risk = trip.next_leg_risk || {};
  const ranked = Object.entries(risk).filter(([c]) => c !== 'NONE').sort((a,b) => b[1]-a[1]).slice(0,6);
  if (!ranked.length) return '<div class="empty">No forecast available yet.</div>';
  const total = ranked.reduce((a,[,p]) => a+p, 0) || 1;
  return ranked.map(([c,p]) => {
    const m = CAUSE_META[c]; const pct = Math.round(p/total*100);
    return `<div class="cause-row">
      <span>${m.label}</span>
      <span class="cause-bar"><i style="width:${pct}%;background:${m.color}"></i></span>
      <span class="cause-pct">${pct}%</span>
    </div>`;
  }).join('');
}

function buildUpcomingTable(trip) {
  const ahead = aheadStops(trip).slice(0, 7);
  if (!ahead.length) return '<div class="empty">This train has reached its final listed stop.</div>';
  return `<table class="grid">
    <thead><tr><th>Station</th><th>Scheduled</th><th>Expected</th><th>Delay</th><th>Range</th></tr></thead>
    <tbody>${ahead.map(s => `
      <tr>
        <td>${s.station_name}</td>
        <td class="num" style="color:var(--dim)">${s.scheduled_clock}</td>
        <td class="num">${s.predicted_clock_p50}</td>
        <td><span class="chip" style="background:${delayColor(s.predicted_delay_p50)}22;color:${delayColor(s.predicted_delay_p50)}">${signed(s.predicted_delay_p50)}m</span></td>
        <td class="range-txt">${signed(s.predicted_delay_p10)} to ${signed(s.predicted_delay_p90)}m</td>
      </tr>`).join('')}</tbody></table>`;
}

function buildForecastCard(trip) {
  const ahead = aheadStops(trip);
  if (!ahead.length) return '';
  const f = ahead[ahead.length-1];
  return `<div class="card card-pad">
    <div class="card-head">Arrival forecast <span>at ${f.station_name}</span></div>
    <div class="quant">
      <div><div class="q-num" style="color:var(--green)">${signed(f.predicted_delay_p10)}m</div><div class="q-lab">best case</div></div>
      <div><div class="q-num" style="color:var(--lamp)">${signed(f.predicted_delay_p50)}m</div><div class="q-lab">most likely</div></div>
      <div><div class="q-num" style="color:#8B6FD1">${signed(f.predicted_delay_p90)}m</div><div class="q-lab">worst case</div></div>
    </div>
    <div style="font-size:12.5px;color:var(--dim);margin-top:11px;line-height:1.5;">
      Expected around <b class="num" style="color:var(--text)">${f.predicted_clock_p50}</b>, most likely between ${clock(f.scheduled_arr_min + f.predicted_delay_p10)} and ${clock(f.scheduled_arr_min + f.predicted_delay_p90)}.
    </div>
  </div>`;
}

function buildWeatherCard(trip) {
  const w = trip.current_weather;
  if (!w) return '';
  return `<div class="card card-pad">
    <div class="card-head">Weather <span>near ${w.station_code}</span></div>
    <div class="wx">
      <div>
        <div class="wx-temp">${w.temp_c !== undefined && w.temp_c !== null ? Math.round(w.temp_c)+'\u00B0' : w.condition}</div>
        <div class="wx-desc">${w.description}</div>
      </div>
      <div class="wx-list">
        ${w.visibility_km !== null && w.visibility_km !== undefined ? `Visibility ${w.visibility_km} km<br>` : ''}
        ${w.is_foggy ? 'Fog risk active' : 'No fog reported'}
      </div>
    </div>
  </div>`;
}

function buildNetworkRiskCard(trip) {
  const risky = aheadStops(trip).filter(s => s.network_risk && s.network_risk.length);
  const level = !risky.length ? 'Clear' : (risky.length > 1 ? 'Elevated' : 'Watch');
  const colour = !risky.length ? 'var(--green)' : (risky.length > 1 ? 'var(--red)' : 'var(--amber)');
  const body = !risky.length
    ? 'No other delayed trains are due at the junctions ahead around the same time.'
    : risky.map(s => `${s.network_risk.length} delayed train${s.network_risk.length>1?'s':''} due at ${s.station_name} near ${s.predicted_clock_p50}.`).join('<br>');
  return `<div class="card card-pad">
    <div class="card-head">Junction conflict</div>
    <div class="risk-head" style="color:${colour}"><span style="width:9px;height:9px;border-radius:50%;background:${colour};display:inline-block;"></span>${level}</div>
    <div style="font-size:12.5px;color:var(--dim);margin-top:8px;line-height:1.55;">${body}</div>
  </div>`;
}

function buildAccuracyCard() {
  if (!ACCURACY) return '';
  if (!ACCURACY.count) {
    return `<div class="card card-pad">
      <div class="card-head">Forecast accuracy</div>
      <div style="font-size:12.5px;color:var(--dim);line-height:1.5;">${ACCURACY.message || 'Checking predictions against outcomes as trains arrive.'}</div>
    </div>`;
  }
  return `<div class="card card-pad">
    <div class="card-head">Forecast accuracy <span>${ACCURACY.count} checked</span></div>
    <div class="quant" style="grid-template-columns:repeat(2,1fr)">
      <div><div class="q-num">${ACCURACY.mean_absolute_error_min}m</div><div class="q-lab">average error</div></div>
      <div><div class="q-num">${ACCURACY.within_p10_p90_band_pct}%</div><div class="q-lab">inside our range</div></div>
    </div>
    <div style="font-size:12px;color:var(--dim);margin-top:10px;">Calibration reads as ${ACCURACY.calibration}.${ACCURACY.pending ? ' Another ' + ACCURACY.pending + ' prediction' + (ACCURACY.pending>1?'s are':' is') + ' still being watched.' : ''}</div>
  </div>`;
}

/* ============ departure advisor ============ */
function buildDepartureAdvisorHtml(trip) {
  const opts = aheadStops(trip).filter(s => s.lat !== undefined)
    .map(s => `<option value="${s.station_code}">${s.station_name}, around ${s.predicted_clock_p50}</option>`).join('');
  if (!opts) return '';
  return `<div class="card card-pad advisor">
    <div class="card-head">When to leave</div>
    <select id="advisor-station-select">${opts}</select>
    <button onclick="getDepartureAdvice()">Use my location</button>
    <div id="advisor-result" style="font-size:12.5px;line-height:1.55;color:var(--dim);"></div>
  </div>`;
}

/* ---------------- watched connections ----------------
   A connection risk you have to ask for is a lookup. A connection risk
   that watches the train and tells you when it turns bad is the actual
   product. The maths is identical; what changes is who starts the
   conversation.

   The watch is kept per train in the browser, so a refresh or a tab
   reopen does not lose it. Nothing is sent anywhere.                  */

const WATCH_KEY = 'railreason.watch';

function loadWatches() {
  try { return JSON.parse(localStorage.getItem(WATCH_KEY) || '{}'); }
  catch (e) { return {}; }
}
function saveWatches(w) {
  try { localStorage.setItem(WATCH_KEY, JSON.stringify(w)); } catch (e) {}
}
function getWatch(trainKey) { return loadWatches()[trainKey] || null; }
function setWatch(trainKey, watch) {
  const w = loadWatches();
  if (watch) w[trainKey] = watch; else delete w[trainKey];
  saveWatches(w);
}

function stopWatching() {
  setWatch(currentTrain, null);
  render();
}

async function refreshWatchBanner() {
  const holder = document.getElementById('watch-banner');
  if (!holder) return;
  const watch = getWatch(currentTrain);
  if (!watch) { holder.innerHTML = ''; return; }

  try {
    const res = await fetch('/api/connection', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ train_no: String(trip_no_of(currentTrain)),
                             station_code: watch.station_code,
                             onward_departure: watch.onward,
                             min_transfer_min: watch.transfer }),
    });
    const b = await res.json();
    if (!res.ok) { holder.innerHTML = ''; return; }

    const tone = b.tone === 'good' ? 'var(--green)' : (b.tone === 'warn' ? 'var(--amber)' : 'var(--red)');
    const shownRisk = b.already_departed ? '&mdash;' : b.risk_pct + '%';
    // Movement matters more than the level. A steady 30% is background
    // noise; 12% climbing to 30% is the moment worth interrupting someone.
    let move = '';
    if (watch.last_pct !== undefined && watch.last_pct !== null) {
      const d = b.risk_pct - watch.last_pct;
      if (d >= 8) move = `<span style="color:var(--red);font-weight:600;">up ${d} points since the last check</span>`;
      else if (d <= -8) move = `<span style="color:var(--green);font-weight:600;">down ${Math.abs(d)} points since the last check</span>`;
    }
    watch.last_pct = b.risk_pct;
    setWatch(currentTrain, watch);

    holder.innerHTML = `
      <div class="watch-banner" style="border-color:${tone};">
        <div class="watch-dot" style="background:${tone};"></div>
        <div style="flex:1;min-width:0;">
          <div style="font-size:13px;color:var(--dim);">Watching your change at
            <b style="color:var(--text);">${b.station_name}</b> for the ${b.onward_departure} departure</div>
          <div style="margin-top:3px;font-size:14.5px;color:var(--text);">${b.summary} ${move}</div>
        </div>
        <div style="text-align:right;flex-shrink:0;">
          <div class="num" style="font-size:30px;font-weight:700;color:${tone};line-height:1;">${shownRisk}</div>
          <div style="font-size:11.5px;color:${tone};">${b.verdict}</div>
        </div>
        <button class="watch-stop" onclick="stopWatching()">Stop</button>
      </div>`;
  } catch (e) { holder.innerHTML = ''; }
}

function renderShareBlock(share) {
  const until = new Date(share.expires_at).toLocaleString([], { hour: '2-digit', minute: '2-digit',
                                                               day: 'numeric', month: 'short' });
  return `
    <div style="margin-top:6px;padding:9px 11px;border-radius:9px;background:var(--panel-lift);
                border:1px solid var(--line);word-break:break-all;font-size:12px;color:var(--text);">${share.url}</div>
    <div style="display:flex;gap:8px;margin-top:8px;">
      <button onclick="copyShare('${share.url}')" style="flex:1;margin:0;">Copy</button>
      <button onclick="revokeShare('${share.token}')" style="flex:1;margin:0;">Stop sharing</button>
    </div>
    <div style="margin-top:8px;color:var(--faint);font-size:11.5px;">
      Works until ${until}. Anyone holding the link can see the train's progress until then.
    </div>`;
}

/* ---------------- what we have actually seen ----------------
   Every other panel forecasts. This one reports: arrivals we recorded
   ourselves, with the sample size attached. It stays quiet about
   anything thin, because an average of two numbers is not a pattern and
   presenting it as one would undo the honesty the rest of the app is
   built on.                                                          */

let HISTORY = {};

async function loadHistory(trainKey) {
  const no = String(trip_no_of(trainKey));
  if (HISTORY[no] !== undefined) return;
  HISTORY[no] = null;
  try {
    const r = await fetch('/api/history/' + no);
    HISTORY[no] = r.ok ? await r.json() : null;
    if (currentTrain === trainKey) render();
  } catch (e) { HISTORY[no] = null; }
}

function buildHistoryCard(trip) {
  const h = HISTORY[String(trip.train_no)];
  if (h === undefined || h === null) return '';

  if (!h.observations || h.typical_delay_min === undefined) {
    return `<div class="card card-pad">
      <div class="card-head">What we have seen</div>
      <div style="font-size:12.5px;color:var(--dim);line-height:1.55;">${h.summary || h.message || ''}</div>
    </div>`;
  }

  const typical = h.typical_delay_min;
  const colour = typical <= 5 ? 'var(--green)' : (typical < 45 ? 'var(--amber)' : 'var(--red)');
  const causes = (h.causes || []).slice(0, 3).map(c => {
    const m = CAUSE_META[c.cause] || { label: c.cause, color: 'var(--faint)' };
    return `<span class="hist-chip"><span class="dot" style="background:${m.color}"></span>${m.label} <b>&times;${c.count}</b></span>`;
  }).join('');

  const stations = (h.stations || []).slice(0, 5).map(st => `
    <tr><td>${st.station_name}</td>
        <td class="num">${st.typical_delay_min > 0 ? '+' + st.typical_delay_min : st.typical_delay_min}m</td>
        <td style="color:var(--faint);font-size:12px;">seen ${st.times_seen}&times;</td></tr>`).join('');

  const withheld = h.stations_withheld
    ? `<div style="font-size:11.5px;color:var(--faint);margin-top:8px;">
         ${h.stations_withheld} more station${h.stations_withheld === 1 ? '' : 's'} seen too few times to summarise.
       </div>` : '';

  return `<div class="card card-pad">
    <div class="card-head">What we have seen <span>${h.observations} arrivals, ${h.runs_observed} run${h.runs_observed === 1 ? '' : 's'}</span></div>
    <div class="quant" style="grid-template-columns:repeat(3,1fr)">
      <div><div class="q-num" style="color:${colour}">${typical > 0 ? '+' + typical : typical}m</div><div class="q-lab">typical</div></div>
      <div><div class="q-num">${h.on_time_pct}%</div><div class="q-lab">within 5 min</div></div>
      <div><div class="q-num" style="font-size:16px;">+${h.worst_delay_min}m</div><div class="q-lab">worst seen</div></div>
    </div>
    <div style="font-size:12.5px;color:var(--dim);margin-top:11px;line-height:1.5;">${h.summary}</div>
    ${causes ? `<div class="hist-chips">${causes}</div>` : ''}
    ${stations ? `<table class="grid" style="margin-top:12px;"><tbody>${stations}</tbody></table>` : ''}
    ${withheld}
  </div>`;
}

function buildShareCard(trip) {
  const existing = activeShares[String(trip.train_no)];
  return `<div class="card card-pad advisor">
    <div class="card-head">Share this journey</div>
    <div style="font-size:12.5px;color:var(--dim);margin-bottom:9px;line-height:1.5;">
      A read-only page for whoever is meeting the train. No name, no account,
      and it stops working once the journey ends.
    </div>
    ${existing ? '' : '<button onclick="createShare()">Create a link</button>'}
    <div id="share-result" style="font-size:12.5px;line-height:1.55;color:var(--dim);">${existing ? renderShareBlock(existing) : ''}</div>
  </div>`;
}

async function createShare() {
  const out = document.getElementById('share-result');
  out.textContent = 'Creating.';
  const tno = String(trip_no_of(currentTrain));
  try {
    const res = await fetch('/api/share', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ train_no: tno }),
    });
    const b = await res.json();
    if (!res.ok) { out.textContent = b.error || 'Could not create a link.'; out.style.color = '#E35D42'; return; }

    const full = window.location.origin + b.url;
    // Stored in JS state (not just written into the DOM) so it survives the
    // next render() — including the one triggered by a background-tab
    // catch-up poll — instead of reverting to "Create a link".
    activeShares[tno] = { token: b.token, url: full, expires_at: b.expires_at };
    render();
  } catch (e) {
    out.textContent = 'The server did not respond.';
    out.style.color = '#E35D42';
  }
}

function copyShare(url) {
  if (!navigator.clipboard) return;
  navigator.clipboard.writeText(url).then(() => {
    const btn = event && event.target;
    if (btn) { const was = btn.textContent; btn.textContent = 'Copied';
               setTimeout(() => { btn.textContent = was; }, 1500); }
  });
}

async function revokeShare(token) {
  const out = document.getElementById('share-result');
  try {
    await fetch('/api/share/' + token, { method: 'DELETE' });
    for (const k in activeShares) { if (activeShares[k].token === token) delete activeShares[k]; }
    out.textContent = 'Sharing stopped. That link no longer works.';
    out.style.color = 'var(--green)';
    setTimeout(() => { if (currentTrain) render(); }, 1400);
  } catch (e) { out.textContent = 'Could not stop sharing.'; out.style.color = '#E35D42'; }
}

function buildConnectionCard(trip) {
  const opts = aheadStops(trip)
    .map(s => `<option value="${s.station_code}">${s.station_name}, around ${s.predicted_clock_p50}</option>`)
    .join('');
  if (!opts) return '';
  return `<div class="card card-pad advisor">
    <div class="card-head">Will I make my connection?</div>
    <div style="font-size:12.5px;color:var(--dim);margin-bottom:8px;">Where do you change trains?</div>
    <select id="conn-station">${opts}</select>
    <div style="display:flex;gap:8px;">
      <div style="flex:1;">
        <div style="font-size:11.5px;color:var(--faint);margin-bottom:4px;">Onward departs</div>
        <input id="conn-time" type="text" inputmode="numeric" placeholder="19:40"
               onkeydown="if(event.key==='Enter') checkConnection()" />
      </div>
      <div style="flex:1;">
        <div style="font-size:11.5px;color:var(--faint);margin-bottom:4px;">Minutes to change</div>
        <input id="conn-transfer" type="text" inputmode="numeric" value="15"
               onkeydown="if(event.key==='Enter') checkConnection()" />
      </div>
    </div>
    <button onclick="checkConnection()">Check the risk</button>
    <button onclick="watchThisConnection()" style="border-color:#2F5F84;">Watch it for me</button>
    <div id="conn-result" style="font-size:12.5px;line-height:1.55;color:var(--dim);"></div>
  </div>`;
}

async function checkConnection() {
  const out = document.getElementById('conn-result');
  const station = document.getElementById('conn-station').value;
  const onward = document.getElementById('conn-time').value.trim();
  const transfer = parseInt(document.getElementById('conn-transfer').value, 10) || 15;
  if (!onward) { out.textContent = 'Enter the onward departure time, for example 19:40.'; return; }

  out.textContent = 'Counting simulated arrivals.';
  out.style.color = 'var(--dim)';
  try {
    const res = await fetch('/api/connection', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ train_no: String(trip_no_of(currentTrain)), station_code: station,
                             onward_departure: onward, min_transfer_min: transfer }),
    });
    const b = await res.json();
    if (!res.ok) { out.textContent = b.error || 'Could not work that out.'; out.style.color = '#E35D42'; return; }

    const tone = b.tone === 'good' ? 'var(--green)' : (b.tone === 'warn' ? 'var(--amber)' : 'var(--red)');

    // When the onward train has already gone there is no probability to
    // show. Printing "100%" next to "Already departed" invites the reader to
    // study a number when what they need is to correct the time they typed.
    if (b.already_departed) {
      out.innerHTML = `
        <div style="display:flex;align-items:center;gap:9px;margin:10px 0 6px;">
          <span style="width:9px;height:9px;border-radius:50%;background:${tone};"></span>
          <span style="font-size:15px;font-weight:600;color:${tone}">${b.verdict}</span>
        </div>
        <div style="color:var(--text);">${b.summary}</div>`;
      out.style.color = 'var(--dim)';
      return;
    }

    out.innerHTML = `
      <div style="display:flex;align-items:baseline;gap:10px;margin:10px 0 6px;">
        <span class="num" style="font-size:30px;font-weight:700;color:${tone}">${b.risk_pct}%</span>
        <span style="font-size:14px;font-weight:600;color:${tone}">${b.verdict}</span>
      </div>
      <div style="color:var(--text);">${b.summary}</div>
      <div style="margin-top:8px;color:var(--faint);font-size:11.5px;">
        ${b.missed_runs} of ${b.total_runs} simulated arrivals left too little time to change.
      </div>`;
    out.style.color = 'var(--dim)';
  } catch (e) {
    out.textContent = 'The server did not respond.';
    out.style.color = '#E35D42';
  }
}

function trip_no_of(key) { return (DATA[key] && DATA[key].train_no) || key; }

function watchThisConnection() {
  const station = document.getElementById('conn-station').value;
  const onward = document.getElementById('conn-time').value.trim();
  const transfer = parseInt(document.getElementById('conn-transfer').value, 10) || 15;
  const out = document.getElementById('conn-result');
  if (!onward) { out.textContent = 'Enter the onward departure time first.'; return; }
  setWatch(currentTrain, { station_code: station, onward: onward, transfer: transfer, last_pct: null });
  render();
}

function estimateRealArrivalTime(trip, target) {
  const last = completedStops(trip).slice(-1)[0];
  if (!last || !trip.fetched_at) return null;
  const fetchedAt = new Date(trip.fetched_at);
  if (isNaN(fetchedAt.getTime())) return null;
  const lastTotal = last.scheduled_arr_min + last.actual_delay_min;
  return new Date(fetchedAt.getTime() + (target.predicted_eta_min_p50 - lastTotal) * 60000);
}

function getDepartureAdvice() {
  const trip = DATA[currentTrain];
  const sel = document.getElementById('advisor-station-select');
  const out = document.getElementById('advisor-result');
  if (!sel || !out) return;
  const target = trip.stations.find(s => s.station_code === sel.value);
  if (!target) { out.textContent = 'Choose a station first.'; return; }
  if (!navigator.geolocation) { out.textContent = 'This browser cannot share a location.'; return; }

  out.textContent = 'Finding your location.';
  navigator.geolocation.getCurrentPosition(async (pos) => {
    out.textContent = 'Working out travel time.';
    try {
      const { latitude, longitude } = pos.coords;
      const r = await fetch(`https://router.project-osrm.org/route/v1/driving/${longitude},${latitude};${target.lng},${target.lat}?overview=false`);
      const d = await r.json();
      if (!d.routes || !d.routes.length) throw new Error('no route');
      const driveMin = Math.ceil(d.routes[0].duration / 60);
      const driveKm = Math.round((d.routes[0].distance || 0) / 1000);
      const arrival = estimateRealArrivalTime(trip, target);
      const fmt = t => t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

      // This answers "when should I set off for the station", which only makes
      // sense for a station you could plausibly drive to. Picking one 1500 km
      // down the line produces an 18-hour figure that is not an answer to that
      // question, and the routing we use makes it worse: OSRM returns
      // free-flow driving time with no traffic, no breaks and no realistic
      // long-haul pacing, so over that distance it lands hours below what a
      // real journey takes. Say so rather than presenting the number plainly.
      if (driveMin > 240) {
        const hrs = (driveMin / 60).toFixed(1);
        out.innerHTML = `<b style="color:var(--amber)">That station is ${driveKm} km away</b>, about
          ${hrs} hours of driving. This is meant for a station you are setting off to shortly, so a
          leave-by time would not be meaningful here.
          <div style="margin-top:6px;color:var(--faint);font-size:11.5px;">
            The estimate also assumes clear roads with no stops, so a real journey of this length
            takes considerably longer than ${hrs} hours.
          </div>`;
        out.style.color = 'var(--dim)';
        return;
      }

      if (!arrival) { out.innerHTML = `The drive takes about <b style="color:var(--text)">${driveMin} minutes</b>. There is not enough live data to set a leave-by time.`; return; }
      // Thirty minutes, not ten. The two failure modes are not symmetric:
      // arriving early costs a wait on a platform, arriving late costs the
      // train. The drive estimate is optimistic anyway (OSRM assumes clear
      // roads), and nothing here accounts for parking, ticket queues or
      // finding the right platform at a large terminus.
      const buffer = 30;
      const leaveBy = new Date(arrival.getTime() - (driveMin + buffer) * 60000);
      const mins = Math.round((leaveBy - new Date()) / 60000);
      out.innerHTML = mins <= 0
        ? `<b style="color:var(--red)">Leave now.</b> The drive takes about ${driveMin} minutes and the train is expected at ${target.station_name} around ${fmt(arrival)}.`
        : `Leave by <b class="num" style="color:var(--text)">${fmt(leaveBy)}</b>, about ${mins} minutes from now. That allows ${driveMin} minutes of driving plus ${buffer} minutes at the station before the train reaches ${target.station_name} around ${fmt(arrival)}.`;
      out.innerHTML += `<div style="margin-top:6px;color:var(--faint);font-size:11.5px;">
        Driving time assumes clear roads, so allow more at busy times.</div>`;
    } catch (e) {
      out.textContent = 'The routing service did not respond. Try again in a moment.';
    }
  }, () => { out.textContent = 'Location access was declined, so the drive time cannot be worked out.'; });
}

/* ============ main render ============ */
function render() {
  if (networkViewActive) { renderNetworkView(); return; }
  const trip = DATA[currentTrain];
  if (!trip) return;

  const done = completedStops(trip);
  const last = done.slice(-1)[0] || { station_name: 'origin', actual_clock: '--:--' };
  const notStarted = trip.run_status === 'not-started';
  const next = aheadStops(trip)[0];
  const isLive = (typeof LIVE_MODE !== 'undefined' && LIVE_MODE);
  const origin = trip.stations[0], dest = trip.stations[trip.stations.length-1];

  const navTrains = Object.keys(DATA).map(tno =>
    `<button class="nav-item ${tno===currentTrain?'on':''}" onclick="switchTrain('${tno}')">
       <span class="nav-glyph">\u2022</span>${tno}
     </button>`).join('');

  document.getElementById('app').innerHTML = `
  <div class="shell">
    <aside class="rail">
      <div class="brand">
        <div class="brand-mark">\u{1F686}</div>
        <div>
          <div class="brand-name">RailReason</div>
          <div class="brand-sub">Why trains run late</div>
        </div>
      </div>
      <nav class="nav">
        <button class="nav-item on"><span class="nav-glyph">\u25C9</span>Live tracking</button>
        ${Object.keys(DATA).length > 1 ? `<button class="nav-item" onclick="toggleNetworkView()"><span class="nav-glyph">\u25CE</span>All trains</button>` : ''}
      </nav>
      <div class="nav">
        <div style="font-size:11.5px;color:var(--faint);padding:0 10px 6px;">Tracking now</div>
        ${navTrains}
      </div>
      <div class="nav" style="margin-top:14px;">
        <a class="nav-item" href="/basic" style="text-decoration:none;">
          <span class="nav-glyph">&#9678;</span>Simple view
        </a>
      </div>
      <div class="rail-foot">
        Live positions from RailRadar. Forecasts are simulated, so treat them as ranges rather than promises.
      </div>
    </aside>

    <div class="main">
      <div class="topbar">
        ${isLive ? `<div class="finder">
          <input id="track-train-input" type="text" inputmode="numeric" placeholder="Track another train by number"
                 onkeydown="if(event.key==='Enter') trackNewTrain()" />
          <button onclick="trackNewTrain()">Track</button>
          <span class="finder-note" id="track-train-status"></span>
        </div>` : `<div class="finder"><span style="font-size:12.5px;color:var(--faint)">Demo snapshot, not connected to live data.</span></div>`}
        <div class="topbar-meta">
          ${trip.current_weather ? `<span>${trip.current_weather.description}${trip.current_weather.temp_c!==undefined&&trip.current_weather.temp_c!==null?', <span class="num">'+Math.round(trip.current_weather.temp_c)+'\u00B0</span>':''}</span>` : ''}
          <span class="num" id="wall-clock"></span>
        </div>
      </div>

      <div class="content">
        <div id="watch-banner"></div>
        <div class="card headline">
          <div>
            <div class="train-no">Train ${trip.train_no}</div>
            <div class="train-name">${trip.train_name}</div>
            <div class="train-od">${origin.station_name} to ${dest.station_name}</div>
            <span class="pill" style="background:${notStarted ? 'var(--faint)2F' : delayColor(trip.current_delay)+'1F'};color:${notStarted ? 'var(--dim)' : delayColor(trip.current_delay)}">
              ${notStarted ? 'Not departed yet' : (trip.current_delay > 0.5 ? 'Running late' : 'On schedule')}
            </span>
          </div>
          <div>
            <div class="metric-label">Delay now</div>
            <div class="metric-value num" style="color:${notStarted ? 'var(--dim)' : delayColor(trip.current_delay)};${notStarted ? 'font-size:20px;' : ''}">${notStarted ? 'Not started' : signed(trip.current_delay) + '<span style="font-size:14px;font-weight:500;"> min</span>'}</div>
            <div class="metric-sub">${notStarted ? 'nothing to measure until it departs' : 'measured at ' + (trip.current_position_note ? trip.current_position_note.station_name : last.station_name)}</div>
          </div>
          <div>
            <div class="metric-label">${notStarted ? 'Scheduled departure' : 'Last listed stop'}</div>
            <div class="metric-value num" style="font-size:20px;">${notStarted ? trip.stations[0].scheduled_clock : last.actual_clock}</div>
            <div class="metric-sub">${last.station_name}${trip.current_position_note && !trip.current_position_note.is_listed_stop ? ', since passed ' + trip.current_position_note.station_name : ''}</div>
          </div>
          <div>
            <div class="metric-label">Next station</div>
            <div class="metric-value num" style="font-size:20px;">${next ? next.predicted_clock_p50 : '\u2014'}</div>
            <div class="metric-sub">${next ? next.station_name : 'final stop reached'}</div>
          </div>
        </div>

        <div class="cols">
          <div class="col-spine card card-pad">
            <div class="card-head">Route</div>
            <div class="spine">${buildSpine(trip)}</div>
          </div>

          <div style="display:flex;flex-direction:column;gap:16px;min-width:0;">
            ${trip.stations.some(s => s.lat !== undefined) ? `
            <div class="card" style="overflow:hidden;">
              <div id="live-map"></div>
              <div class="map-foot">Map tiles by OpenStreetMap. The moving marker is interpolated from RailRadar telemetry, along a straight line between stations rather than the real track.</div>
            </div>` : ''}

            <div class="card card-pad">
              <div class="card-head">How the delay is expected to develop</div>
              ${buildTrendChart(trip)}
              <div style="font-size:12.5px;color:var(--dim);margin-top:10px;line-height:1.55;">${narrative(trip)}</div>
            </div>

            <div class="card card-pad">
              <div class="card-head">Next stations</div>
              ${buildUpcomingTable(trip)}
            </div>
          </div>

          <div style="display:flex;flex-direction:column;gap:16px;">
            ${buildForecastCard(trip)}
            ${buildWeatherCard(trip)}
            ${buildNetworkRiskCard(trip)}
            <div class="card card-pad">
              <div class="card-head">What might cause more delay <span>next stretch</span></div>
              ${buildCauseBars(trip)}
            </div>
            ${buildHistoryCard(trip)}
            ${isLive ? buildConnectionCard(trip) : ''}
            ${isLive ? buildShareCard(trip) : ''}
            ${isLive ? buildDepartureAdvisorHtml(trip) : ''}
            ${buildAccuracyCard()}
          </div>
        </div>
      </div>
    </div>
  </div>`;

  tickClock();
  updateMap(trip);
  if (typeof LIVE_MODE !== 'undefined' && LIVE_MODE) { refreshWatchBanner(); loadHistory(currentTrain); }
}

function tickClock() {
  const el = document.getElementById('wall-clock');
  if (el) el.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

/* ============ network view ============ */
function toggleNetworkView() { networkViewActive = !networkViewActive; render(); }

function renderNetworkView() {
  const trains = Object.keys(DATA);
  const rows = trains.map(tno => {
    const trip = DATA[tno];
    const last = completedStops(trip).slice(-1)[0] || { station_name: 'origin', actual_clock: '--:--' };
    return `<div class="net-row" onclick="currentTrain='${tno}';networkViewActive=false;render();">
      <div>
        <div style="font-size:13.5px;font-weight:600;">${tno}, ${trip.train_name}</div>
        <div style="font-size:11.5px;color:var(--faint);margin-top:2px;">at ${last.station_name}, ${last.actual_clock}</div>
      </div>
      <div class="num" style="font-size:14px;font-weight:600;color:${delayColor(trip.current_delay)}">${signed(trip.current_delay)}m</div>
    </div>`;
  }).join('');

  document.getElementById('app').innerHTML = `
  <div class="shell">
    <aside class="rail">
      <div class="brand">
        <div class="brand-mark">\u{1F686}</div>
        <div><div class="brand-name">RailReason</div><div class="brand-sub">Why trains run late</div></div>
      </div>
      <nav class="nav">
        <button class="nav-item" onclick="toggleNetworkView()"><span class="nav-glyph">\u25C9</span>Live tracking</button>
        <button class="nav-item on"><span class="nav-glyph">\u25CE</span>All trains</button>
      </nav>
      <div class="rail-foot">Every tracked train, drawn on one map.</div>
    </aside>
    <div class="main">
      <div class="topbar"><div class="finder"><span style="font-size:13px;color:var(--dim)">${trains.length} train${trains.length===1?'':'s'} tracked right now</span></div></div>
      <div class="content">
        <div class="card" style="overflow:hidden;">
          <div id="live-map" style="height:380px;"></div>
          <div class="map-foot">Map tiles by OpenStreetMap. Each train keeps its own colour.</div>
        </div>
        <div class="card card-pad">
          <div class="card-head">Trains</div>
          ${rows}
        </div>
      </div>
    </div>
  </div>`;
  updateNetworkMap();
}

/* ============ maps ============ */
function updateMap(trip) {
  const el = document.getElementById('live-map');
  if (leafletMap) { leafletMap.remove(); leafletMap = null; }
  if (!el) return;
  const pts = trip.stations.filter(s => s.lat !== undefined && s.lng !== undefined);
  if (!pts.length) return;

  leafletMap = L.map('live-map', { zoomControl: true, attributionControl: false });
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 15 }).addTo(leafletMap);

  const line = pts.map(s => [s.lat, s.lng]);
  L.polyline(line, { color: '#5B9DD9', weight: 3, opacity: .75 }).addTo(leafletMap);
  pts.forEach(s => {
    const c = (s.status === 'completed' && CAUSE_META[s.cause]) ? CAUSE_META[s.cause].color : '#5F7482';
    L.circleMarker([s.lat, s.lng], { radius: 4, color: c, fillColor: c, fillOpacity: 1, weight: 1 })
      .bindPopup(`<b>${s.station_name}</b>`).addTo(leafletMap);
  });

  let bounds = L.latLngBounds(line);
  if (trip.live_position) {
    const icon = L.divIcon({ className: '', html: '<div class="train-marker"><div class="pulse"></div><div class="dot"></div></div>', iconSize: [16,16], iconAnchor: [8,8] });
    const speed = (trip.live_position.speed_kmh !== null && trip.live_position.speed_kmh !== undefined)
      ? Math.round(trip.live_position.speed_kmh) + ' km/h' : 'speed not reported';
    const pct = Math.round((trip.live_position.segment_progress || 0) * 100);
    L.marker([trip.live_position.lat, trip.live_position.lng], { icon }).addTo(leafletMap)
      .bindPopup(`Live position, ${speed}<br><span style="font-size:11px;color:#666;">${pct}% through this stretch, from RailRadar telemetry</span>`);
    bounds.extend([trip.live_position.lat, trip.live_position.lng]);
  }
  leafletMap.fitBounds(bounds, { padding: [26,26] });
}

const NETWORK_COLORS = ['#4FAE73','#E7A33E','#5B9DD9','#E35D42','#8B6FD1','#7FA3B8','#C97A3D'];

function updateNetworkMap() {
  const el = document.getElementById('live-map');
  if (leafletMap) { leafletMap.remove(); leafletMap = null; }
  if (!el) return;
  leafletMap = L.map('live-map', { zoomControl: true, attributionControl: false });
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 15 }).addTo(leafletMap);

  let all = [];
  Object.keys(DATA).forEach((tno, idx) => {
    const trip = DATA[tno];
    const colour = NETWORK_COLORS[idx % NETWORK_COLORS.length];
    const pts = trip.stations.filter(s => s.lat !== undefined && s.lng !== undefined);
    if (!pts.length) return;
    const line = pts.map(s => [s.lat, s.lng]);
    L.polyline(line, { color: colour, weight: 2, opacity: .5 }).addTo(leafletMap);
    all = all.concat(line);
    const pos = trip.live_position || pts.filter(s => s.status === 'completed').slice(-1)[0];
    if (!pos) return;
    const icon = L.divIcon({ className: '', html:
      `<div style="position:relative;width:16px;height:16px;">
         <div style="width:16px;height:16px;border-radius:50%;background:${colour};opacity:.45;position:absolute;animation:ping 1.8s ease-out infinite;"></div>
         <div style="width:12px;height:12px;border-radius:50%;background:${colour};border:2px solid var(--ink);position:absolute;top:2px;left:2px;"></div>
       </div>`, iconSize:[16,16], iconAnchor:[8,8] });
    L.marker([pos.lat, pos.lng], { icon }).addTo(leafletMap)
      .bindPopup(`<b>${tno}</b>, ${trip.train_name}<br>${trip.current_delay > 0.5 ? Math.round(trip.current_delay)+' minutes late' : 'on schedule'}`);
    all.push([pos.lat, pos.lng]);
  });
  if (all.length) leafletMap.fitBounds(L.latLngBounds(all), { padding: [26,26] });
  else leafletMap.setView([22.5, 78.9], 5);
}

function switchTrain(tno) { currentTrain = tno; networkViewActive = false; render(); }
</script>
</body>
</html>
"""


def render_html(bootstrap_js: str) -> str:
    """
    bootstrap_js must define `DATA` (object keyed by train number, same
    shape as live_trip_demo.json) and `currentTrain` (a key into DATA),
    and must call render() at least once.
    """
    html = _TEMPLATE.replace("__CAUSE_META_JSON__", json.dumps(CAUSE_META))
    return html.replace("__BOOTSTRAP__", bootstrap_js)


def static_bootstrap(data: dict) -> str:
    """Embeds a fixed snapshot directly (demo mode, no server)."""
    return f"""const DATA = {json.dumps(data)};
let currentTrain = Object.keys(DATA)[0];
setTimeout(render, 0);"""


def live_bootstrap(poll_seconds: int = 30) -> str:
    """Polls the local server on a timer (live mode)."""
    return f"""const LIVE_MODE = true;
let DATA = {{}};
let currentTrain = null;

async function refresh() {{
  try {{
    const res = await fetch('/api/live');
    if (!res.ok) throw new Error('the server has no data yet');
    DATA = await res.json();
    if (!currentTrain || !DATA[currentTrain]) currentTrain = Object.keys(DATA)[0];
    if (currentTrain) render();
  }} catch (e) {{
    document.getElementById('app').innerHTML =
      '<div style="padding:40px;color:#93A8B4;font-family:IBM Plex Sans,sans-serif;font-size:14px;">'
      + 'Waiting for the first live reading. ' + e.message + '.</div>';
  }}
}}

async function trackNewTrain() {{
  const input = document.getElementById('track-train-input');
  const note = document.getElementById('track-train-status');
  const trainNo = input.value.trim();
  if (!trainNo) return;
  note.textContent = 'Checking.';
  note.style.color = '#5F7482';
  try {{
    const res = await fetch('/api/track', {{
      method: 'POST', headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ train_no: trainNo }}),
    }});
    const body = await res.json();
    if (!res.ok) {{ note.textContent = body.error || 'That train could not be added.'; note.style.color = '#E35D42'; return; }}
    note.textContent = 'Added, running ' + body.current_delay + ' minutes late.';
    note.style.color = '#4FAE73';
    input.value = '';
    currentTrain = body.train_no;
    await refresh();
  }} catch (e) {{
    note.textContent = 'The server did not respond. Check it is still running.';
    note.style.color = '#E35D42';
  }}
}}

async function loadAccuracyStats() {{
  try {{
    const res = await fetch('/api/accuracy');
    ACCURACY = await res.json();
    if (currentTrain) render();
  }} catch (e) {{ /* non-critical */ }}
}}

setTimeout(() => {{ refresh(); loadAccuracyStats(); }}, 0);
setInterval(refresh, {poll_seconds * 1000});
setInterval(loadAccuracyStats, 60000);
setInterval(tickClock, 30000);"""