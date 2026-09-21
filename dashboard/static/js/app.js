"use strict";

/* app.js — bootstrap: theme, navigation, the SSE connection, and the
 * per-device rollup that feeds the context card and the map label. */

(function () {
  const { esc, relTime, fmtTime } = MT.util;

  const VIEW_LABEL = {
    overview: "Overview",
    map: "Live map",
    media: "Captures",
    voice: "Voice & answers",
    beacons: "Beacons",
    feed: "Event log",
  };

  const seen = new Set();        // event ids already applied (SSE replays the backlog)
  const devices = new Map();     // device_id -> rollup
  let totalBeacons = 0;
  let filter = "";

  /* ── Theme ───────────────────────────────────────────────────────────── */

  const MOON = "M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z";
  const SUN = "M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10zM12 1v2M12 21v2M4.2 4.2l1.4 1.4" +
              "M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4";

  function paintThemeIcon() {
    const dark = document.documentElement.dataset.theme !== "light";
    document.querySelector("#theme-icon path").setAttribute("d", dark ? MOON : SUN);
  }

  document.getElementById("theme-btn").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("mt.theme", next); } catch (e) { /* private mode */ }
    paintThemeIcon();
  });

  /* ── Navigation ──────────────────────────────────────────────────────── */

  const workspace = document.getElementById("workspace");

  document.getElementById("nav").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-nav]");
    if (!btn) return;
    const view = btn.dataset.nav;
    workspace.dataset.view = view;
    document.getElementById("crumb-view").textContent = VIEW_LABEL[view] || view;
    document.querySelectorAll("[data-nav]").forEach((b) => {
      if (b === btn) b.setAttribute("aria-current", "page");
      else b.removeAttribute("aria-current");
    });
  });

  /* ── Connection pill ─────────────────────────────────────────────────── */

  function setConnected(ok, broker) {
    const pill = document.getElementById("conn-pill");
    const dot = document.getElementById("conn-dot");
    pill.className = "pill " + (ok ? "is-ok" : "is-bad");
    dot.className = "dot " + (ok ? "is-ok" : "is-bad");
    document.getElementById("conn-label").textContent = ok ? "broker connected" : "broker offline";
    if (broker) document.getElementById("conn-broker").textContent = broker;
  }

  /* ── Device rollup ───────────────────────────────────────────────────── */

  function deviceFor(id) {
    let d = devices.get(id);
    if (!d) {
      d = { device_id: id, visited: new Set(), counts: {} };
      devices.set(id, d);
      addPickerOption(id);
    }
    return d;
  }

  function addPickerOption(id) {
    const picker = document.getElementById("device-picker");
    if ([...picker.options].some((o) => o.value === id)) return;
    const opt = document.createElement("option");
    opt.value = id;
    opt.textContent = id;
    picker.appendChild(opt);
  }

  function applyToDevice(ev) {
    const d = deviceFor(ev.device_id || "unknown");
    d.last_seen = ev.ts;
    d.counts[ev.type] = (d.counts[ev.type] || 0) + 1;
    if (ev.type === "ble") {
      d.last_beacon_uuid = ev.beacon_uuid;
      d.last_rssi = ev.rssi;
      d.last_title = ev.title;
      d.last_location = ev.location;
      d.last_fix = ev.ts;
      if (ev.exhibit_id) d.visited.add(ev.exhibit_id);
    }
  }

  /** The device the header cards describe: the filtered one, else the freshest. */
  function focused() {
    if (filter) return devices.get(filter);
    let best = null;
    devices.forEach((d) => {
      if (!best || String(d.last_seen) > String(best.last_seen)) best = d;
    });
    return best;
  }

  function paintContext() {
    const d = focused();
    const set = (id, text) => { document.getElementById(id).textContent = text; };

    if (!d) {
      set("now-title", "—");
      set("now-by", "waiting for a beacon activation");
      set("fact-rssi", "—");
      set("fact-loc", "—");
      set("fact-visited", "0 / " + totalBeacons);
      set("fact-seen", "—");
      set("dev-name", "no device");
      set("dev-sub", "awaiting telemetry");
      document.getElementById("dev-dot").className = "dot";
      return;
    }

    set("now-title", d.last_title || "no beacon yet");
    set("now-by", d.last_location
      ? "nearest beacon on " + d.last_location
      : "no beacon activation recorded");
    set("fact-rssi", d.last_rssi != null ? d.last_rssi + " dBm" : "—");
    set("fact-loc", d.last_location || "—");
    set("fact-visited", d.visited.size + " / " + totalBeacons);
    set("fact-seen", d.last_seen ? relTime(d.last_seen) : "—");

    set("dev-name", d.device_id);
    const fresh = d.last_seen && (Date.now() - new Date(d.last_seen).getTime()) < 120000;
    set("dev-sub", d.last_seen ? "active " + relTime(d.last_seen) : "no telemetry");
    document.getElementById("dev-dot").className = "dot " + (fresh ? "is-ok" : "");
    MT.map.setLabel(d.device_id);
  }

  function paintCounts() {
    const totals = {};
    for (const ev of MT.feed.visibleEvents()) {
      totals[ev.type] = (totals[ev.type] || 0) + 1;
    }
    document.querySelectorAll("[data-count]").forEach((node) => {
      node.textContent = totals[node.dataset.count] || 0;
    });
  }

  /* ── Event intake ────────────────────────────────────────────────────── */

  function handle(ev, live) {
    if (!ev || seen.has(ev.id)) return;
    seen.add(ev.id);
    applyToDevice(ev);

    const passes = !filter || ev.device_id === filter;
    if (live) {
      MT.feed.ingest(ev);
      if (passes) MT.stats.bump(ev.type);
    }
    if (passes && ev.type === "ble") MT.map.onBeacon(ev);
  }

  /* ── Boot ────────────────────────────────────────────────────────────── */

  async function boot() {
    paintThemeIcon();
    MT.stats.init();
    MT.feed.init({
      onEvent: () => {
        paintCounts();
        paintContext();
      },
    });

    // Floor plan first: the map must stand up even with the broker down.
    let plan = null;
    try {
      plan = await (await fetch("/api/floorplan")).json();
      totalBeacons = (plan.beacons || []).length;
      MT.feed.seedBeacons(plan.beacons);
      if (plan.venue) document.getElementById("crumb-venue").textContent = plan.venue;
    } catch (e) {
      console.warn("floor plan unavailable", e);
    }
    await MT.map.init(plan);

    // Initial paint from the snapshot. SSE replays the same backlog on
    // connect, so every id is recorded as seen here to avoid double-counting.
    try {
      const snap = await (await fetch("/api/snapshot")).json();
      setConnected(snap.connected, snap.broker);
      for (const d of snap.devices || []) {
        const dev = deviceFor(d.device_id);
        dev.last_seen = d.last_seen;
        dev.last_rssi = d.last_rssi;
        dev.last_title = d.last_exhibit_title;
        dev.last_location = d.last_location;
        dev.last_beacon_uuid = d.last_beacon_uuid;
        (d.visited || []).forEach((v) => dev.visited.add(v));
      }
      for (const ev of snap.events || []) {
        seen.add(ev.id);
        applyToDevice(ev);
      }
      MT.feed.load(snap.events);
      MT.stats.rebuild(MT.feed.visibleEvents());
      // Restore the last known position, stamped with when it really happened.
      const lastBle = [...(snap.events || [])].reverse().find((e) => e.type === "ble");
      if (lastBle) MT.map.onBeacon(lastBle, Date.parse(lastBle.ts) || Date.now());
    } catch (e) {
      console.warn("snapshot unavailable", e);
    }

    paintCounts();
    paintContext();
    connect();

    document.getElementById("device-picker").addEventListener("change", (e) => {
      filter = e.target.value;
      MT.feed.setFilter(filter);
      MT.stats.rebuild(MT.feed.visibleEvents());
      paintCounts();
      paintContext();
    });

    // Relative stamps ("active 12s ago") go stale on their own.
    setInterval(paintContext, 5000);
  }

  function connect() {
    const es = new EventSource("/events");
    es.onopen = () => setConnected(true);
    es.onerror = () => setConnected(false);
    es.onmessage = (e) => {
      if (!e.data) return;
      try {
        handle(JSON.parse(e.data), true);
      } catch (err) { /* ignore a malformed frame */ }
    };
  }

  boot();
})();
