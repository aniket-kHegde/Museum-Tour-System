"use strict";

/* stats.js — KPI tiles.
 *
 * Each tile is a single-series sparkline of one MQTT channel over the last
 * 60 seconds, in that channel's color. Per the dataviz rules a lone series
 * needs no legend (the tile's heading names it) and identity never rests on
 * color alone: the heading, the running total and the delta are all text.
 */

MT.stats = (function () {
  const { svg, clamp } = MT.util;

  const BUCKETS = 60;          // one per second
  const VB_W = 240, VB_H = 34; // sparkline viewBox
  const PAD = 3;               // keeps the 2px stroke off the tile edges

  const tiles = new Map();     // channel -> tile state

  function build(article) {
    const channel = article.dataset.stat;
    const spark = article.querySelector("[data-spark]");

    // area under the line, then the line, then the leading "now" dot
    const area = svg("path", { fill: "var(--ch)", "fill-opacity": "0.14", d: "" });
    const line = svg("path", {
      fill: "none",
      stroke: "var(--ch)",
      "stroke-width": "2",
      "stroke-linejoin": "round",
      "stroke-linecap": "round",
      "vector-effect": "non-scaling-stroke", // survives the horizontal stretch
      d: "",
    });
    const base = svg("line", {
      x1: 0, y1: VB_H - PAD, x2: VB_W, y2: VB_H - PAD,
      stroke: "var(--border)", "stroke-width": "1",
      "vector-effect": "non-scaling-stroke",
    });
    const head = svg("circle", { r: "2.6", fill: "var(--ch)", cx: -10, cy: -10 });
    // The head dot would smear under preserveAspectRatio="none"; keeping it in
    // its own un-stretched overlay is not worth a second SVG, so it rides the
    // same space and is deliberately small.
    spark.append(base, area, line, head);

    tiles.set(channel, {
      channel,
      counts: new Array(BUCKETS).fill(0),
      total: 0,
      valueEl: article.querySelector("[data-value]"),
      deltaEl: article.querySelector("[data-delta]"),
      area, line, head,
    });
  }

  function bump(channel) {
    const t = tiles.get(channel);
    if (!t) return;
    t.total += 1;
    t.counts[BUCKETS - 1] += 1;
    t.valueEl.textContent = t.total;
  }

  /** Rebuild every tile from a list of events.
   *
   * Used for the initial paint from /api/snapshot and whenever the device
   * filter changes. Real timestamps are bucketed by age, so the 60s window is
   * genuine history rather than a flat line that only starts filling on load.
   */
  function rebuild(events) {
    const now = Date.now();
    tiles.forEach((t) => {
      t.counts.fill(0);
      t.total = 0;
    });
    for (const ev of events || []) {
      const t = tiles.get(ev.type);
      if (!t) continue;
      t.total += 1;
      const age = (now - new Date(ev.ts).getTime()) / 1000;
      if (age >= 0 && age < BUCKETS) {
        t.counts[BUCKETS - 1 - Math.floor(age)] += 1;
      }
    }
    tiles.forEach((t) => {
      t.valueEl.textContent = t.total;
      draw(t);
    });
  }

  /** Slide every window one second into the past. Called once a second. */
  function tick() {
    tiles.forEach((t) => {
      t.counts.shift();
      t.counts.push(0);
      draw(t);
    });
  }

  function draw(t) {
    const recent = t.counts.reduce((a, b) => a + b, 0);
    t.deltaEl.textContent = recent ? "+" + recent + " · 60s" : "none in 60s";
    t.deltaEl.classList.toggle("is-live", recent > 0);

    // Floor the scale at 3 so one lone event doesn't spike to full height.
    const peak = Math.max(3, ...t.counts);
    const step = VB_W / (BUCKETS - 1);
    const y = (n) => VB_H - PAD - (n / peak) * (VB_H - PAD * 2);

    let d = "";
    for (let i = 0; i < BUCKETS; i++) {
      d += (i ? "L" : "M") + (i * step).toFixed(2) + " " + y(t.counts[i]).toFixed(2);
    }
    t.line.setAttribute("d", d);
    t.area.setAttribute("d", d + `L${VB_W} ${VB_H - PAD}L0 ${VB_H - PAD}Z`);

    const last = t.counts[BUCKETS - 1];
    t.head.setAttribute("cx", VB_W - 1.5);
    t.head.setAttribute("cy", y(last).toFixed(2));
    t.head.setAttribute("opacity", last > 0 ? "1" : "0.35");
  }

  function init() {
    document.querySelectorAll("[data-stat]").forEach(build);
    tiles.forEach(draw);
    setInterval(tick, 1000);
  }

  return { init, bump, rebuild };
})();
