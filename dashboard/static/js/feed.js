"use strict";

/* feed.js — every non-map surface: the event log, the per-channel lists,
 * the capture gallery and the beacon table.
 *
 * Holds the last MAX_BUFFER events so switching the device filter can
 * re-render from memory instead of waiting for new traffic.
 */

MT.feed = (function () {
  const { esc, el, prepend, fmtTime, relTime, shortUuid } = MT.util;

  const MAX_BUFFER = 300;  // events retained for re-render
  const MAX_NODES = 60;    // DOM cap per list

  const TYPE_LABEL = {
    image: "capture",
    generated: "answer",
    transcript: "speech",
    ble: "beacon",
    audio: "audio",
  };

  const buffer = [];
  const beacons = new Map();   // uuid -> registry entry + live hit stats
  let filter = "";             // "" = all devices
  let onEvent = () => {};      // app.js hook, fires for events passing the filter

  const dom = {};

  function init(hooks) {
    onEvent = (hooks && hooks.onEvent) || onEvent;
    dom.feed = document.getElementById("feed");
    dom.gallery = document.getElementById("gallery");
    dom.transcript = document.getElementById("list-transcript");
    dom.generated = document.getElementById("list-generated");
    dom.beaconRows = document.getElementById("beacon-rows");
    dom.feedCount = document.getElementById("feed-count");
    dom.lightbox = document.getElementById("lightbox");
    dom.lightboxImg = document.getElementById("lightbox-img");

    dom.lightbox.addEventListener("click", closeLightbox);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeLightbox();
    });

    // One delegated handler beats an inline onclick per thumbnail, and keeps
    // the open function out of the global scope.
    document.addEventListener("click", (e) => {
      const trigger = e.target.closest("[data-full]");
      if (trigger) openLightbox(trigger.dataset.full);
    });

    setInterval(refreshBeaconTable, 5000);  // keep "last seen" honest
  }

  /* ── Beacon registry ─────────────────────────────────────────────────── */

  function seedBeacons(list) {
    for (const b of list || []) {
      const prev = beacons.get(b.beacon_uuid) || { hits: 0, rssi: null, last: null };
      beacons.set(b.beacon_uuid, Object.assign({}, b, prev));
    }
    refreshBeaconTable();
  }

  function noteBeaconHit(ev) {
    const entry = beacons.get(ev.beacon_uuid) || {
      beacon_uuid: ev.beacon_uuid,
      title: ev.title,
      location: ev.location,
      hits: 0,
    };
    entry.title = entry.title || ev.title;
    entry.location = entry.location || ev.location;
    entry.hits = (entry.hits || 0) + 1;
    entry.rssi = ev.rssi;
    entry.last = ev.ts;
    beacons.set(ev.beacon_uuid, entry);
  }

  /** RSSI to a 0-3 bar count. -55 and up is "full"; below -85 is one bar. */
  function barsFor(rssi) {
    if (rssi == null) return 0;
    if (rssi >= -60) return 3;
    if (rssi >= -75) return 2;
    return 1;
  }

  function refreshBeaconTable() {
    if (!dom.beaconRows) return;
    const rows = [...beacons.values()].sort((a, b) => {
      if (!!b.last !== !!a.last) return b.last ? 1 : -1;      // seen first
      if (a.last && b.last && a.last !== b.last) return a.last < b.last ? 1 : -1;
      return String(a.location || "").localeCompare(String(b.location || ""));
    });

    const active = MT.map && MT.map.activeBeacon();
    dom.beaconRows.innerHTML = rows.map((b) => {
      const n = barsFor(b.rssi);
      const bars = [1, 2, 3].map((i) => `<i class="${i <= n ? "on" : ""}"></i>`).join("");
      return `<tr class="${b.beacon_uuid === active ? "is-active" : ""}">
        <td>${esc(b.title || "unknown beacon")}</td>
        <td>${esc(b.location || "—")}</td>
        <td class="mono">${esc(shortUuid(b.beacon_uuid))}</td>
        <td class="num">${b.rssi != null ? esc(b.rssi) + " dBm" : "—"}</td>
        <td><span class="bars">${bars}</span></td>
        <td class="num">${b.hits || 0}</td>
        <td class="num">${b.last ? esc(relTime(b.last)) : "—"}</td>
      </tr>`;
    }).join("");
  }

  /* ── Item rendering ──────────────────────────────────────────────────── */

  function bodyFor(ev) {
    switch (ev.type) {
      case "image":
        return `<img class="thumb" src="/image/${encodeURIComponent(ev.image_id)}"
                     alt="Camera capture" data-full="/image/${encodeURIComponent(ev.image_id)}">`;
      case "generated": {
        const tag = ev.response_type === "error"
          ? `<span class="tag is-error">error</span>` : "";
        return tag + esc(ev.text);
      }
      case "transcript":
        return `<span class="tag">${esc(ev.source || "")}</span>${esc(ev.text)}`;
      case "ble": {
        const title = ev.title ? esc(ev.title) : "<i>unknown beacon</i>";
        const rssi = ev.rssi != null ? `<span class="rssi">${esc(ev.rssi)} dBm</span>` : "";
        const where = ev.location ? ` · ${esc(ev.location)}` : "";
        return `${title} ${rssi}<div class="mono">${esc(shortUuid(ev.beacon_uuid))}${where}</div>`;
      }
      case "audio":
        return `<audio controls preload="none" src="/audio/${encodeURIComponent(ev.audio_id)}"></audio>`;
      default:
        return esc(JSON.stringify(ev));
    }
  }

  function makeItem(ev) {
    const item = el("div", "item t-" + ev.type);
    const meta = el("div", "meta");
    meta.innerHTML =
      `<span class="type">${esc(TYPE_LABEL[ev.type] || ev.type)}</span>` +
      `<span class="mono">${esc(ev.device_id || "?")}</span>` +
      `<span class="when">${esc(fmtTime(ev.ts))}</span>`;
    item.appendChild(meta);
    item.appendChild(el("div", "body", bodyFor(ev)));
    return item;
  }

  function makeShot(ev) {
    const fig = el("figure", "shot");
    const src = "/image/" + encodeURIComponent(ev.image_id);
    fig.dataset.full = src;
    fig.setAttribute("role", "button");
    fig.setAttribute("tabindex", "0");
    fig.innerHTML = `<img src="${src}" alt="Capture from ${esc(ev.device_id || "device")}">
                     <figcaption>${esc(fmtTime(ev.ts))}</figcaption>`;
    return fig;
  }

  /* ── Pipeline ────────────────────────────────────────────────────────── */

  function passes(ev) { return !filter || ev.device_id === filter; }

  function paint(ev) {
    prepend(dom.feed, makeItem(ev), MAX_NODES);
    if (ev.type === "image") {
      prepend(dom.gallery, makeShot(ev), 40);
    } else if (ev.type === "transcript") {
      prepend(dom.transcript, makeItem(ev), MAX_NODES);
    } else if (ev.type === "generated") {
      prepend(dom.generated, makeItem(ev), MAX_NODES);
    }
  }

  /** Take one live event: buffer it, update registries, paint if it passes. */
  function ingest(ev) {
    buffer.push(ev);
    while (buffer.length > MAX_BUFFER) buffer.shift();
    if (ev.type === "ble") noteBeaconHit(ev);
    if (!passes(ev)) return;
    paint(ev);
    updateEmpties();
    dom.feedCount.textContent = dom.feed.children.length;
    onEvent(ev);
  }

  /** Replace the buffer wholesale (initial snapshot) and redraw everything. */
  function load(events) {
    buffer.length = 0;
    for (const ev of events || []) {
      buffer.push(ev);
      if (ev.type === "ble") noteBeaconHit(ev);
    }
    rerender();
  }

  function rerender() {
    for (const node of [dom.feed, dom.gallery, dom.transcript, dom.generated]) {
      node.innerHTML = "";
    }
    // Oldest first, so prepend() leaves the newest on top.
    for (const ev of buffer) {
      if (passes(ev)) paint(ev);
    }
    dom.feedCount.textContent = dom.feed.children.length;
    updateEmpties();
    refreshBeaconTable();
  }

  function updateEmpties() {
    toggleEmpty("feed-empty", dom.feed);
    toggleEmpty("gallery-empty", dom.gallery);
    toggleEmpty("transcript-empty", dom.transcript);
    toggleEmpty("generated-empty", dom.generated);
  }

  function toggleEmpty(id, list) {
    const node = document.getElementById(id);
    if (node) node.hidden = list.children.length > 0;
  }

  function setFilter(deviceId) {
    filter = deviceId || "";
    rerender();
  }

  function visibleEvents() { return buffer.filter(passes); }

  /* ── Lightbox ────────────────────────────────────────────────────────── */

  function openLightbox(src) {
    dom.lightboxImg.src = src;
    dom.lightbox.hidden = false;
  }
  function closeLightbox() {
    dom.lightbox.hidden = true;
    dom.lightboxImg.removeAttribute("src");
  }

  return {
    init, ingest, load, setFilter, visibleEvents,
    seedBeacons, refreshBeaconTable, beacons,
  };
})();
