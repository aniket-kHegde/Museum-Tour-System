"use strict";

/* map.js — the live floor plan.
 *
 * The tour system has no positioning system: the only spatial signal is
 * "which beacon is nearest, and how strong". This module turns that into a
 * believable moving fix.
 *
 *   LIVE      a beacon_enter arrives -> the device glides to that exhibit and
 *             the accuracy ring is sized from the real RSSI.
 *   SIMULATED nothing live for IDLE_MS -> an autopilot walks the aisles,
 *             dwelling at shelves, so the map is never a dead screen.
 *
 * Both modes only ever set a *target*; one rAF loop does the moving. That is
 * why the handover between them is a glide and never a jump.
 */

MT.map = (function () {
  const { svg, clamp, remap, relTime } = MT.util;

  const IDLE_MS = 20000;      // no beacon for this long -> autopilot
  const REACH = 7;            // px, "arrived at target"
  const TRAIL_MAX = 44;
  const TRAIL_MIN_STEP = 9;   // px between breadcrumbs
  const LIVE_SPEED = 260;     // px/s cap when chasing a real fix
  const SIM_SPEED = 62;       // px/s cap for a walking pace
  const JITTER_PX = 2.6;      // restlessness — a perfectly still dot reads dead

  let plan = null;
  let stage, root, camera, layers = {};
  let devNode, devAccuracy, devHeading, devGlow, devCore, devTag, devText, trailNode;

  const beaconNodes = new Map();  // uuid -> {g, x, y}
  let anchors = [];               // [{uuid, x, y, wp}] autopilot dwell points

  // ── Motion state ──────────────────────────────────────────────────────
  const pos = { x: 500, y: 560 };
  const target = { x: 500, y: 560 };
  const heading = { x: 0, y: -1 };
  let trail = [];
  let mode = "sim";
  let lastFix = 0;            // ms epoch of the last real beacon event
  let rssi = null;            // last RSSI (real in live, synthesised in sim)
  let activeUuid = null;
  let deviceLabel = "device";
  let lastFrame = 0;
  let started = false;

  // ── Autopilot state ───────────────────────────────────────────────────
  // `done` holds the beacons already visited this lap. A single "last visited"
  // slot is not enough: a gallery can serve two exhibits off one waypoint, and
  // the autopilot would ping-pong between that pair forever.
  const auto = { phase: "walk", leg: 0, dwellUntil: 0, detour: null, done: new Set() };

  // ── View state ────────────────────────────────────────────────────────
  const view = { k: 1, tx: 0, ty: 0, follow: true, trail: true };
  let dragging = null;

  /* ── Build ───────────────────────────────────────────────────────────── */

  async function init(prefetched) {
    stage = document.getElementById("map-stage");
    root = document.getElementById("floorplan");
    camera = document.getElementById("map-camera");
    for (const id of ["floor", "zones", "walk", "trail", "beacons", "device"]) {
      layers[id] = document.getElementById("layer-" + id);
    }

    plan = prefetched || await (await fetch("/api/floorplan")).json();

    const sub = document.getElementById("venue-sub");
    if (sub) sub.textContent = `${plan.venue} · ${plan.floor}`;

    drawFloor();
    drawZones();
    drawWalkways();
    drawBeacons();
    drawDevice();
    drawChrome();
    wireControls();

    // Start at the entrance, which is where a visitor actually would.
    const start = (plan.entrance_path && plan.entrance_path[0]) || [500, 560];
    pos.x = target.x = start[0];
    pos.y = target.y = start[1];

    applyView(pos.x, pos.y);
    started = true;
    requestAnimationFrame(frame);
    setInterval(updateReadout, 250);
  }

  function drawFloor() {
    const w = plan.walls;
    layers.floor.appendChild(svg("rect", {
      class: "fp-wall-fill", x: w.x, y: w.y, width: w.w, height: w.h, rx: 6,
    }));

    // 1m grid, with a heavier line every 5m so the scale is legible at a glance.
    const g = svg("g", { class: "fp-grid" });
    const m = plan.px_per_meter;
    for (let x = w.x + m; x < w.x + w.w; x += m) {
      const major = Math.round((x - w.x) / m) % 5 === 0;
      g.appendChild(svg("line", { x1: x, y1: w.y, x2: x, y2: w.y + w.h, class: major ? "major" : "" }));
    }
    for (let y = w.y + m; y < w.y + w.h; y += m) {
      const major = Math.round((y - w.y) / m) % 5 === 0;
      g.appendChild(svg("line", { x1: w.x, y1: y, x2: w.x + w.w, y2: y, class: major ? "major" : "" }));
    }
    layers.floor.appendChild(g);

    // Outer wall drawn as one path with a gap where the entrance is.
    const ex = w.entrance.x, ew = w.entrance.w, x2 = w.x + w.w, y2 = w.y + w.h;
    layers.floor.appendChild(svg("path", {
      class: "fp-wall",
      d: `M${w.x} ${w.y} L${x2} ${w.y} L${x2} ${y2} L${ex + ew} ${y2}` +
         ` M${ex} ${y2} L${w.x} ${y2} L${w.x} ${w.y}`,
    }));

    layers.floor.appendChild(svg("line", {
      class: "fp-entrance", x1: ex + 6, y1: y2, x2: ex + ew - 6, y2: y2,
    }));
    const lbl = svg("text", { class: "fp-entrance-label", x: ex - 14, y: y2 + 16,
                              "text-anchor": "end" });
    lbl.textContent = "Entrance";
    layers.floor.appendChild(lbl);
  }

  function drawZones() {
    for (const z of plan.zones) {
      const g = svg("g", {});
      if (z.kind === "shelf") {
        g.appendChild(svg("rect", { class: "fp-shelf", x: z.x, y: z.y, width: z.w, height: z.h }));
        g.appendChild(svg("rect", { class: "fp-shelf-hatch", x: z.x, y: z.y, width: z.w, height: z.h }));
        const t = svg("text", { class: "fp-zone-label", x: z.x + z.w / 2, y: z.y + z.h / 2 });
        t.textContent = z.label;
        g.appendChild(t);
        if (z.sub) {
          const s = svg("text", { class: "fp-zone-sub", x: z.x + z.w / 2, y: z.y + z.h / 2 + 17 });
          s.textContent = z.sub;
          g.appendChild(s);
        }
      } else if (z.kind === "room") {
        g.appendChild(svg("rect", { class: "fp-room", x: z.x, y: z.y, width: z.w, height: z.h }));
        // Two reading tables, so the space reads as furniture not a void.
        g.appendChild(svg("circle", { class: "fp-table", cx: z.x + z.w * 0.3, cy: z.y + z.h / 2, r: 22 }));
        g.appendChild(svg("circle", { class: "fp-table", cx: z.x + z.w * 0.72, cy: z.y + z.h / 2, r: 22 }));
        const t = svg("text", { class: "fp-room-label", x: z.x + z.w / 2, y: z.y - 8 });
        t.textContent = z.label;
        g.appendChild(t);
      } else {
        g.appendChild(svg("rect", { class: "fp-desk", x: z.x, y: z.y, width: z.w, height: z.h }));
        const t = svg("text", { class: "fp-room-label", x: z.x + z.w / 2, y: z.y + z.h / 2 + 4 });
        t.textContent = z.label;
        g.appendChild(t);
      }
      layers.zones.appendChild(g);
    }
  }

  function drawWalkways() {
    const pts = plan.waypoints.map((p) => p.join(",")).join(" ");
    layers.walk.appendChild(svg("polygon", { class: "fp-walk", points: pts }));
    if (plan.entrance_path) {
      layers.walk.appendChild(svg("polyline", {
        class: "fp-walk", points: plan.entrance_path.map((p) => p.join(",")).join(" "),
      }));
    }
  }

  function drawBeacons() {
    for (const b of plan.beacons || []) {
      const g = svg("g", { class: "bc", transform: `translate(${b.x} ${b.y})` });
      g.dataset.uuid = b.beacon_uuid;
      for (let i = 0; i < 3; i++) g.appendChild(svg("circle", { class: "bc-ping", r: 9 }));
      g.appendChild(svg("circle", { class: "bc-halo", r: 27 }));
      g.appendChild(svg("circle", { class: "bc-core", r: 7 }));
      g.appendChild(svg("circle", { class: "bc-glyph", r: 2.6 }));

      // Markers hang off the aisle-facing edge of a gallery, so the label goes
      // further into the aisle — centring it would run the text over the display.
      const toLeft = b.x > plan.width / 2;
      const name = svg("text", {
        class: "bc-name",
        x: toLeft ? -13 : 13,
        y: 3.5,
        "text-anchor": toLeft ? "end" : "start",
      });
      const title = String(b.title || "beacon");
      name.textContent = title.length > 22 ? title.slice(0, 21) + "…" : title;
      g.appendChild(name);

      const tip = svg("title", {});
      tip.textContent = `${b.title || "beacon"} — ${b.location || "unknown location"}`;
      g.appendChild(tip);

      layers.beacons.appendChild(g);
      beaconNodes.set(b.beacon_uuid, { g, x: b.x, y: b.y });
    }

    // Precompute the waypoint each beacon hangs off, so the autopilot knows
    // where to peel out of the loop for a dwell.
    anchors = (plan.beacons || []).map((b) => ({
      uuid: b.beacon_uuid, x: b.x, y: b.y, wp: nearestWaypoint(b.x, b.y),
    }));
  }

  function nearestWaypoint(x, y) {
    let best = 0, bestD = Infinity;
    plan.waypoints.forEach((p, i) => {
      const d = (p[0] - x) ** 2 + (p[1] - y) ** 2;
      if (d < bestD) { bestD = d; best = i; }
    });
    return best;
  }

  function drawDevice() {
    trailNode = svg("polyline", { class: "dev-trail", points: "" });
    layers.trail.appendChild(trailNode);

    devNode = svg("g", { transform: "translate(500 560)" });
    devAccuracy = svg("circle", { class: "dev-accuracy", r: 44 });
    devHeading = svg("path", { class: "dev-heading", d: "M10 0 L24 -8 L24 8 Z" });
    devGlow = svg("circle", { class: "dev-glow", r: 16 });
    devCore = svg("circle", { class: "dev-core", r: 6 });

    devTag = svg("g", { transform: "translate(0 -30)" });
    const bg = svg("rect", { class: "dev-label-bg", x: -30, y: -11, width: 60, height: 17 });
    devText = svg("text", { class: "dev-label", y: 1.5 });
    devTag.append(bg, devText);

    devNode.append(devAccuracy, devHeading, devGlow, devCore, devTag);
    layers.device.appendChild(devNode);
    setLabel(deviceLabel);
  }

  function setLabel(text) {
    deviceLabel = text || "device";
    if (!devText) return;
    devText.textContent = deviceLabel;
    // Size the chip to the text so long device ids don't overflow the plate.
    const w = Math.max(46, deviceLabel.length * 6.6 + 14);
    const bg = devTag.querySelector(".dev-label-bg");
    bg.setAttribute("width", w);
    bg.setAttribute("x", -w / 2);
  }

  function drawChrome() {
    const chrome = document.getElementById("layer-chrome");
    const m = plan.px_per_meter;

    // Scale bar: 5 m, bottom centre, clear of both HTML overlays.
    const g = svg("g", { class: "fp-scale", transform: "translate(400 80)" });
    g.appendChild(svg("path", { d: `M0 -4 L0 4 M0 0 L${5 * m} 0 M${5 * m} -4 L${5 * m} 4` }));
    const t = svg("text", { x: 5 * m / 2, y: -8, "text-anchor": "middle" });
    t.textContent = "5 m";
    g.appendChild(t);
    chrome.appendChild(g);

    // Compass, tucked in the margin between the wall and Section A.
    const c = svg("g", { class: "fp-compass", transform: "translate(72 78)" });
    c.appendChild(svg("circle", { r: 19 }));
    c.appendChild(svg("path", { d: "M0 -13 L5 3 L0 -1 L-5 3 Z" }));
    const n = svg("text", { y: 14, "text-anchor": "middle" });
    n.textContent = "N";
    c.appendChild(n);
    chrome.appendChild(c);
  }

  /* ── Live input ──────────────────────────────────────────────────────── */

  /** A real beacon_enter. Preempts the autopilot immediately.
   *
   * `atMs` lets the initial page paint restore the last known fix at the time
   * it actually happened — replaying a ten-minute-old event must not light up
   * the LIVE badge and claim the fix is current.
   */
  function onBeacon(ev, atMs) {
    const node = beaconNodes.get(ev.beacon_uuid);
    const x = ev.x != null ? ev.x : node && node.x;
    const y = ev.y != null ? ev.y : node && node.y;
    if (x == null || y == null) return;

    // A real fix is never dead-centre on the beacon; offsetting a little sells
    // it as a measurement rather than a lookup.
    target.x = x + (Math.random() - 0.5) * 16;
    target.y = y + 12 + (Math.random() - 0.5) * 10;

    rssi = ev.rssi != null ? ev.rssi : rssi;
    lastFix = atMs || Date.now();
    mode = Date.now() - lastFix < IDLE_MS ? "live" : "sim";
    setActive(ev.beacon_uuid);
    auto.done.add(ev.beacon_uuid);
    auto.leg = nearestWaypoint(x, y);
    auto.phase = "walk";
    if (mode === "sim") {
      // Restored a stale fix: place the device there, then resume walking.
      pos.x = target.x;
      pos.y = target.y;
      trail = [];
    }
  }

  function setActive(uuid) {
    if (activeUuid === uuid) return;
    if (activeUuid && beaconNodes.has(activeUuid)) {
      beaconNodes.get(activeUuid).g.classList.remove("is-active");
    }
    activeUuid = uuid;
    if (uuid && beaconNodes.has(uuid)) {
      beaconNodes.get(uuid).g.classList.add("is-active");
    }
    if (MT.feed) MT.feed.refreshBeaconTable();
  }

  /* ── Autopilot ───────────────────────────────────────────────────────── */

  function autopilot(now) {
    const wp = plan.waypoints;

    if (auto.phase === "dwell") {
      if (now >= auto.dwellUntil) {
        if (auto.detour) auto.done.add(auto.detour.uuid);
        auto.phase = "walk";
        setTargetTo(wp[auto.leg]);   // back to the aisle, then re-evaluate
      } else {
        // Keep the synthesised signal drifting so the ring and bars stay alive.
        rssi = Math.round(clamp(-52 + Math.sin(now / 900) * 3.5 + (Math.random() - 0.5) * 2, -70, -44));
      }
      return;
    }

    if (dist(pos, target) > REACH) return;

    if (auto.phase === "detour") {
      auto.phase = "dwell";
      auto.dwellUntil = now + 6000 + Math.random() * 4000;
      setActive(auto.detour && auto.detour.uuid);
      return;
    }

    // Walking the loop: peel off if this waypoint serves a beacon we have not
    // stopped at yet this lap. Several beacons can share one waypoint.
    const candidate = anchors.find((a) => a.wp === auto.leg && !auto.done.has(a.uuid));
    if (candidate) {
      auto.phase = "detour";
      auto.detour = candidate;
      target.x = candidate.x + (Math.random() - 0.5) * 12;
      target.y = candidate.y + 14;
      return;
    }

    auto.leg = (auto.leg + 1) % wp.length;
    if (auto.leg === 0) auto.done.clear();   // new lap, visit everything again
    setTargetTo(wp[auto.leg]);
    setActive(null);
    // Between shelves the nearest beacon is far: weaken the fix accordingly.
    rssi = Math.round(clamp(-74 + (Math.random() - 0.5) * 6, -88, -66));
  }

  function setTargetTo(p) {
    target.x = p[0] + (Math.random() - 0.5) * 8;
    target.y = p[1] + (Math.random() - 0.5) * 8;
  }

  function dist(a, b) { return Math.hypot(a.x - b.x, a.y - b.y); }

  /* ── Frame loop ──────────────────────────────────────────────────────── */

  function frame(ts) {
    requestAnimationFrame(frame);
    if (!started) return;

    const dt = lastFrame ? Math.min(0.05, (ts - lastFrame) / 1000) : 0.016;
    lastFrame = ts;
    const now = Date.now();

    if (mode === "live" && now - lastFix > IDLE_MS) {
      mode = "sim";
      auto.phase = "walk";
      auto.leg = nearestWaypoint(pos.x, pos.y);
      // The beacon we were just parked at is already in `done`, so the
      // autopilot walks on instead of re-dwelling where it already stands.
    }
    if (mode === "sim") autopilot(now);

    // Move toward the target: near-constant speed over distance, easing out
    // at the end so arrivals settle instead of stopping dead.
    const d = dist(pos, target);
    if (d > 0.4) {
      const cap = mode === "live" ? LIVE_SPEED : SIM_SPEED;
      const step = Math.min(d, clamp(d * 2.4, 0, cap) * dt);
      const ux = (target.x - pos.x) / d, uy = (target.y - pos.y) / d;
      pos.x += ux * step;
      pos.y += uy * step;

      const hs = clamp(step * 6, 0, 1);       // smooth the heading, don't snap it
      heading.x += (ux - heading.x) * hs;
      heading.y += (uy - heading.y) * hs;
      pushTrail();
    }

    render(ts, now);
  }

  function pushTrail() {
    const last = trail[trail.length - 1];
    if (last && Math.hypot(last.x - pos.x, last.y - pos.y) < TRAIL_MIN_STEP) return;
    trail.push({ x: pos.x, y: pos.y });
    if (trail.length > TRAIL_MAX) trail.shift();
    trailNode.setAttribute("points", trail.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(" "));
  }

  function render(ts, now) {
    // Two out-of-phase sines: cheap, seamless, and never lands on a fixed point.
    const jx = Math.sin(ts / 730) * JITTER_PX + Math.sin(ts / 310) * (JITTER_PX * 0.4);
    const jy = Math.cos(ts / 610) * JITTER_PX + Math.cos(ts / 270) * (JITTER_PX * 0.4);
    const rx = pos.x + jx, ry = pos.y + jy;

    devNode.setAttribute("transform", `translate(${rx.toFixed(2)} ${ry.toFixed(2)})`);

    const ang = Math.atan2(heading.y, heading.x) * 180 / Math.PI;
    devHeading.setAttribute("transform", `rotate(${ang.toFixed(1)})`);

    // Accuracy ring from RSSI: -40 dBm is a tight fix, -90 is a vague one.
    devAccuracy.setAttribute("r", rssi == null ? 70 : remap(rssi, -90, -40, 92, 18).toFixed(1));

    const sim = mode !== "live";
    for (const n of [devAccuracy, devHeading, devGlow, devCore, trailNode]) {
      n.classList.toggle("is-sim", sim);
    }

    applyView(rx, ry);
  }

  function applyView(dx, dy) {
    if (view.follow && view.k > 1.02) {
      view.tx = plan.width / 2 - dx * view.k;
      view.ty = plan.height / 2 - dy * view.k;
    }
    clampView();
    camera.setAttribute("transform", `translate(${view.tx.toFixed(2)} ${view.ty.toFixed(2)}) scale(${view.k.toFixed(3)})`);
  }

  /** Keep the plan pinned to the stage: no panning it into a corner, and no
   *  floating it off to one side when zoomed out below 1. */
  function clampView() {
    const fit = (span, t) => {
      const slack = span * (1 - view.k);
      return view.k >= 1 ? clamp(t, slack, 0) : slack / 2;
    };
    view.tx = fit(plan.width, view.tx);
    view.ty = fit(plan.height, view.ty);
  }

  /* ── Overlays ────────────────────────────────────────────────────────── */

  function updateReadout(){
    if (!started) return;
    const live = mode === "live";

    const acc = (rssi == null ? 70 : remap(rssi, -90, -40, 92, 18)) / plan.px_per_meter;
    document.getElementById("map-readout").innerHTML =
      `<span class="k">x</span> <span class="v">${pos.x.toFixed(0)}</span>` +
      ` <span class="k">y</span> <span class="v">${pos.y.toFixed(0)}</span>` +
      ` <span class="k">·</span> <span class="v">±${acc.toFixed(1)} m</span>` +
      ` <span class="k">·</span> <span class="v">${rssi == null ? "—" : rssi + " dBm"}</span>`;

    document.getElementById("map-stamp").textContent =
      live ? "fix " + relTime(new Date(lastFix).toISOString())
           : (lastFix ? "no beacon since " + relTime(new Date(lastFix).toISOString())
                      : "no beacon fix yet");
  }

  /* ── Controls ────────────────────────────────────────────────────────── */

  function toViewBox(evt) {
    const pt = root.createSVGPoint();
    pt.x = evt.clientX;
    pt.y = evt.clientY;
    return pt.matrixTransform(root.getScreenCTM().inverse());
  }

  function zoomAt(vx, vy, k2) {
    k2 = clamp(k2, 0.6, 3);
    // Keep the point under the cursor fixed while the scale changes.
    view.tx = vx - (vx - view.tx) * (k2 / view.k);
    view.ty = vy - (vy - view.ty) * (k2 / view.k);
    view.k = k2;
    applyView(pos.x, pos.y);
  }

  function wireControls() {
    // Plain wheel belongs to the page — hijacking it would trap the scroll
    // whenever the cursor crossed the map. Zoom is ctrl/cmd + wheel, plus the
    // buttons, which is what every map UI trains people to expect.
    stage.addEventListener("wheel", (e) => {
      if (!e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      const p = toViewBox(e);
      zoomAt(p.x, p.y, view.k * (e.deltaY < 0 ? 1.12 : 1 / 1.12));
    }, { passive: false });

    stage.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      dragging = { x: e.clientX, y: e.clientY, tx: view.tx, ty: view.ty };
      setFollow(false);
      stage.classList.add("is-panning");
      stage.setPointerCapture(e.pointerId);
    });
    stage.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      const ctm = root.getScreenCTM();
      view.tx = dragging.tx + (e.clientX - dragging.x) / ctm.a;
      view.ty = dragging.ty + (e.clientY - dragging.y) / ctm.d;
      applyView(pos.x, pos.y);
    });
    const endDrag = () => { dragging = null; stage.classList.remove("is-panning"); };
    stage.addEventListener("pointerup", endDrag);
    stage.addEventListener("pointercancel", endDrag);

    const mid = () => ({ x: plan.width / 2, y: plan.height / 2 });
    document.getElementById("map-in").onclick = () => { const c = mid(); zoomAt(c.x, c.y, view.k * 1.25); };
    document.getElementById("map-out").onclick = () => { const c = mid(); zoomAt(c.x, c.y, view.k / 1.25); };
    document.getElementById("map-reset").onclick = () => {
      view.k = 1; view.tx = 0; view.ty = 0;
      setFollow(true);
      applyView(pos.x, pos.y);
    };

    const followBtn = document.getElementById("map-follow");
    followBtn.onclick = () => setFollow(!view.follow);

    const trailBtn = document.getElementById("map-trail");
    trailBtn.onclick = () => {
      view.trail = !view.trail;
      trailBtn.setAttribute("aria-pressed", String(view.trail));
      stage.classList.toggle("no-trail", !view.trail);
    };
  }

  function setFollow(on) {
    view.follow = on;
    document.getElementById("map-follow").setAttribute("aria-pressed", String(on));
  }

  return {
    init,
    onBeacon,
    setLabel,
    activeBeacon: () => activeUuid,
    isLive: () => mode === "live",
  };
})();
