// wakeproxy's widget, added to the pages of web apps on a hostname route (browser.widget).
// Shows when the machine shuts down, can extend that and wake it, links to the status page,
// and reports that someone is using the app (tab visible, recent input) so the machine stays on.
// It can be dragged elsewhere when it covers something; the spot is remembered per app.
// Everything lives in a shadow DOM: the app's styles don't reach it and it doesn't touch the app.
(() => {
  if (window.__wakeproxyWidget) return;
  window.__wakeproxyWidget = true;

  const params = new URL(document.currentScript.src).searchParams;
  const ROUTE = params.get("route"), POSITION = params.get("position") || "bottom-right";
  const API = "/_wakeproxy/page/";
  const ACTIVE_MS = 2 * 60 * 1000;           // input this recent counts as "using it"
  const HIDDEN_KEY = "wakeproxy-widget-collapsed";
  const MOVED_KEY = "wakeproxy-widget-moved:" + ROUTE;

  const host = document.createElement("div");
  const [v, h] = POSITION.split("-");
  const FIXED = "position:fixed;z-index:2147483647;";
  const CONFIGURED = (v === "top" ? "top:10px;" : "bottom:10px;") +
    (h === "left" ? "left:10px;" : h === "right" ? "right:10px;" : "left:50%;transform:translateX(-50%);");
  host.style.cssText = FIXED + CONFIGURED;
  const root = host.attachShadow({ mode: "open" });
  root.innerHTML = `
<style>
  :host { all: initial; }
  * { box-sizing: border-box; font: 12.5px/1.35 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  .w { --bg: rgba(23,27,35,.92); --text: #e6e9ef; --muted: #8b93a5; --line: #303746; --accent: #22b8d6;
       --on: #34d399; --warn: #f59e0b; --bad: #f87171; color: var(--text); }
  .pill { display: flex; align-items: center; gap: 7px; background: var(--bg); border: 1px solid var(--line);
          border-radius: 99px; padding: 5px 11px 5px 9px; cursor: pointer; color: var(--text);
          box-shadow: 0 2px 10px rgba(0,0,0,.25); backdrop-filter: blur(6px); white-space: nowrap;
          touch-action: none; user-select: none; -webkit-user-select: none; }
  .pill.dragging { cursor: grabbing; }
  .pill.mini { padding: 5px; }
  .pill.mini .t { display: none; }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); flex: none; }
  .dot.on { background: var(--on); } .dot.warn { background: var(--warn); } .dot.bad { background: var(--bad); }
  .t { font-variant-numeric: tabular-nums; }
  .menu { position: absolute; min-width: 230px; background: var(--bg); border: 1px solid var(--line); border-radius: 10px;
          padding: 10px; box-shadow: 0 4px 18px rgba(0,0,0,.3); backdrop-filter: blur(6px); }
  .menu[hidden] { display: none; }
  .line { color: var(--muted); margin: 0 0 8px; }
  .btns { display: flex; flex-wrap: wrap; gap: 6px; }
  button.b { background: transparent; color: var(--text); border: 1px solid var(--line); border-radius: 7px;
             padding: 5px 9px; cursor: pointer; font-weight: 600; }
  button.b:hover { border-color: var(--accent); }
  button.b.primary { background: var(--accent); border-color: var(--accent); color: #0b1016; }
  a { color: var(--accent); text-decoration: none; display: inline-block; margin-top: 9px; }
  .err { color: var(--bad); margin-top: 6px; }
</style>
<div class="w">
  <div class="pill" id="pill"><span class="dot" id="dot"></span><span class="t" id="text">…</span></div>
  <div class="menu" id="menu" hidden>
    <p class="line" id="detail"></p>
    <div class="btns" id="btns"></div>
    <div class="err" id="err" hidden></div>
    <a id="status" target="_blank" rel="noopener" hidden>Open the wakeproxy page ↗</a>
  </div>
</div>`;
  (document.body || document.documentElement).appendChild(host);
  const $ = (id) => root.getElementById(id);

  let state = null, offset = 0, lastActive = Date.now(), lastSent = 0, wasUp = null, clicked = null;
  let collapsed = false;
  try { collapsed = localStorage.getItem(HIDDEN_KEY) === "1"; } catch (e) {}
  // Where the user dragged it: {x, y}, as fractions of the room left of and above the badge (so it
  // keeps its side of the window when the window or the badge changes size). null: configured spot.
  let moved = null, drag = null, droppedAt = 0;
  try {
    const m = JSON.parse(localStorage.getItem(MOVED_KEY));
    if (m && Number.isFinite(m.x) && Number.isFinite(m.y)) moved = m;
  } catch (e) {}

  const mmss = (s) => { s = Math.max(0, Math.round(s)); const m = Math.floor(s / 60);
    return m >= 60 ? `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min` : `${m}:${String(s % 60).padStart(2, "0")}`; };
  const now = () => Date.now() / 1000 + offset;     // the server's clock

  async function call(method, path, extra = "") {
    const r = await fetch(API + path + "?route=" + encodeURIComponent(ROUTE) + extra, { method, cache: "no-store" });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || `${r.status} ${r.statusText}`);
    return body;
  }

  function button(label, action, primary) {
    const b = document.createElement("button");
    b.className = "b" + (primary ? " primary" : "");
    b.textContent = label;
    b.addEventListener("click", async (e) => {
      e.stopPropagation();
      $("err").hidden = true;
      try { await action(); } catch (err) { $("err").textContent = err.message; $("err").hidden = false; }
    });
    return b;
  }

  function setState(s) {
    state = s;
    offset = s.now - Date.now() / 1000;
    if (wasUp === false && s.service_up) location.reload();   // woken from here: load the app again
    wasUp = s.service_up;
    render();
  }

  function render() {
    const s = state;
    if (!s) return;
    const M = s.machine;
    let text, dot = "", detail, btns = [];
    if (s.service_up) {
      if (s.holds.length) {
        text = `${M} · on`; dot = "on";
        detail = "Kept on by: " + s.holds.map((x) => x.key === "presence" ? "someone using this page" :
          `${x.source} (${x.label})`).join(", ") + ". The timer starts when that ends.";
      } else if (s.deadline) {
        const left = s.deadline - now();
        text = `${M} · off in ${mmss(left)}`; dot = left < 120 ? "warn" : "on";
        detail = `${M} shuts down ${left < 60 ? "in under a minute" : "at " + new Date((Date.now() / 1000 + left) * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })} unless something uses it.`;
      } else {
        text = `${M} · on`; dot = "on";
        detail = `${M} wasn't woken by wakeproxy (or the timer is off), so it stays on.`;
      }
      if (s.managed) btns = [button("+30 min", () => call("POST", "extend", "&minutes=30").then(setState)),
                             button("+1 h", () => call("POST", "extend", "&minutes=60").then(setState))];
    } else if (s.waking || (clicked && Date.now() - clicked < 20000 && !s.up)) {
      const took = clicked ? (Date.now() - clicked) / 1000 : now() - s.waking;
      text = `Waking ${M}… ${mmss(took)}`; dot = "warn";
      detail = `This page reloads once it's up.${s.typical_wake ? ` Usually takes about ${mmss(s.typical_wake)}.` : ""}`;
    } else if (s.shutting_down) {
      text = `${M} is shutting down`; dot = "bad";
      detail = `You can wake it again once it is off.`;
    } else if (s.up) {
      text = `${M} on, app not answering`; dot = "warn";
      detail = `${M} is on, but this app isn't answering.`;
    } else {
      text = `${M} is off`; dot = "bad";
      detail = `${M} has been switched off. Wake it to keep using this page.`;
      btns = [button(`Wake ${M}`, () => { clicked = Date.now(); return call("POST", "wake").then(setState); }, true)];
    }
    $("text").textContent = text;
    $("dot").className = "dot " + dot;
    $("pill").classList.toggle("mini", collapsed);
    $("pill").title = (collapsed ? text : "wakeproxy") + " · drag to move";
    $("detail").textContent = detail;
    const box = $("btns");
    box.replaceChildren(...btns, button(collapsed ? "Expand" : "Minimise", () => {
      collapsed = !collapsed;
      try { localStorage.setItem(HIDDEN_KEY, collapsed ? "1" : "0"); } catch (e) {}
      $("menu").hidden = true; render();
    }), ...(moved ? [button("Reset position", () => {
      moved = null;
      try { localStorage.removeItem(MOVED_KEY); } catch (e) {}
      $("menu").hidden = true; render();
    })] : []));
    if (s.status_url) { $("status").href = s.status_url; $("status").hidden = false; }
    place();
  }

  const clamp = (n, lo, hi) => Math.min(Math.max(n, lo), Math.max(lo, hi));
  function place() {
    if (drag && drag.on) return;
    if (!moved) { host.style.cssText = FIXED + CONFIGURED; return; }
    const r = host.getBoundingClientRect();
    const left = Math.round(clamp(moved.x, 0, 1) * Math.max(0, innerWidth - r.width));
    const top = Math.round(clamp(moved.y, 0, 1) * Math.max(0, innerHeight - r.height));
    host.style.cssText = FIXED + `left:${left}px;top:${top}px;`;
  }

  // The menu opens toward the middle of the window, wherever the badge is.
  function placeMenu() {
    const r = host.getBoundingClientRect(), m = $("menu").style, cx = r.left + r.width / 2;
    const below = r.top + r.height / 2 < innerHeight / 2;
    m.top = below ? "calc(100% + 6px)" : ""; m.bottom = below ? "" : "calc(100% + 6px)";
    m.left = m.right = m.transform = "";
    if (cx < innerWidth / 3) m.left = "0";
    else if (cx > innerWidth * 2 / 3) m.right = "0";
    else { m.left = "50%"; m.transform = "translateX(-50%)"; }
  }

  // Dragging: it starts once the pointer has moved a few pixels, so a plain click still opens the menu.
  const pill = $("pill");
  pill.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    const r = host.getBoundingClientRect();
    drag = { id: e.pointerId, dx: e.clientX - r.left, dy: e.clientY - r.top, x0: e.clientX, y0: e.clientY, on: false };
    pill.setPointerCapture(e.pointerId);
  });
  pill.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    if (!drag.on) {
      if (Math.hypot(e.clientX - drag.x0, e.clientY - drag.y0) < 5) return;
      drag.on = true; pill.classList.add("dragging"); $("menu").hidden = true;
    }
    const r = host.getBoundingClientRect();
    host.style.cssText = FIXED + `left:${clamp(e.clientX - drag.dx, 0, innerWidth - r.width)}px;` +
      `top:${clamp(e.clientY - drag.dy, 0, innerHeight - r.height)}px;`;
  });
  const drop = (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const was = drag.on;
    drag = null;
    if (!was) return;
    pill.classList.remove("dragging");
    const r = host.getBoundingClientRect();
    moved = { x: r.left / Math.max(1, innerWidth - r.width), y: r.top / Math.max(1, innerHeight - r.height) };
    try { localStorage.setItem(MOVED_KEY, JSON.stringify(moved)); } catch (err) {}
    if (e.type === "pointerup") droppedAt = Date.now();   // the click this drop makes doesn't open the menu
    render();
  };
  pill.addEventListener("pointerup", drop);
  pill.addEventListener("pointercancel", drop);
  pill.addEventListener("click", () => {
    if (Date.now() - droppedAt < 400) { droppedAt = 0; return; }
    placeMenu();
    $("menu").hidden = !$("menu").hidden;
  });
  addEventListener("resize", place);
  document.addEventListener("click", (e) => { if (!e.composedPath().includes(host)) $("menu").hidden = true; });

  // Presence: someone moved, clicked, typed or scrolled recently, and the tab is visible.
  const active = () => { lastActive = Date.now(); maybeReport(); };
  for (const ev of ["pointerdown", "keydown", "wheel", "touchstart"]) addEventListener(ev, active, { passive: true, capture: true });
  let moveSeen = 0;
  addEventListener("pointermove", () => { if (Date.now() - moveSeen > 5000) { moveSeen = Date.now(); active(); } },
                   { passive: true, capture: true });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) active(); });

  async function maybeReport() {
    if (!state || !state.presence_minutes || document.hidden || !state.service_up) return;
    if (Date.now() - lastActive > ACTIVE_MS || Date.now() - lastSent < 60000) return;
    lastSent = Date.now();
    try { setState(await call("POST", "presence")); } catch (e) {}
  }

  async function poll() {
    try { setState(await call("GET", "state")); } catch (e) {}
    maybeReport();
  }
  place();
  poll();
  setInterval(poll, 15000);
  setInterval(() => { if (state && (state.deadline || state.waking || clicked)) render(); }, 1000);
})();
