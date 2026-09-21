"use strict";

/* Shared helpers. Everything hangs off one global so the files below can stay
   plain <script> tags — no bundler in this project by design. */
window.MT = window.MT || {};

MT.util = (function () {
  const SVG_NS = "http://www.w3.org/2000/svg";

  /** Escape untrusted MQTT text before it ever reaches innerHTML. */
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function el(tag, cls, html) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (html !== undefined) node.innerHTML = html;
    return node;
  }

  /** SVG element with attributes, since createElement() won't do namespaces. */
  function svg(tag, attrs) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const k in attrs) {
      if (attrs[k] !== undefined && attrs[k] !== null) {
        node.setAttribute(k, attrs[k]);
      }
    }
    return node;
  }

  function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }

  /** Map v from one range to another, clamped to the output range. */
  function remap(v, inLo, inHi, outLo, outHi) {
    if (inHi === inLo) return outLo;
    const t = clamp((v - inLo) / (inHi - inLo), 0, 1);
    return outLo + t * (outHi - outLo);
  }

  function fmtTime(iso) {
    const d = new Date(iso);
    return isNaN(d) ? String(iso || "") : d.toLocaleTimeString();
  }

  /** "just now" / "12s ago" / "4m ago" — for last-seen stamps. */
  function relTime(iso) {
    const d = new Date(iso);
    if (isNaN(d)) return "—";
    const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
    if (s < 2) return "just now";
    if (s < 60) return Math.floor(s) + "s ago";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    return Math.floor(s / 3600) + "h ago";
  }

  /** Cap a list container at n nodes, oldest (last) first. */
  function prepend(container, node, max) {
    container.insertBefore(node, container.firstChild);
    while (container.children.length > (max || 60)) {
      container.removeChild(container.lastChild);
    }
  }

  function shortUuid(u) {
    const s = String(u || "");
    return s.length > 13 ? s.slice(0, 8) + "…" + s.slice(-4) : s;
  }

  return { SVG_NS, esc, el, svg, clamp, remap, fmtTime, relTime, prepend, shortUuid };
})();
