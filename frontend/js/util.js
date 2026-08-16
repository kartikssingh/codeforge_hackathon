// frontend/js/util.js
// Formatting and DOM helpers shared by every view.
//
// esc() is the important one: transcripts and situation labels come from a
// language model chewing on a recording of a stranger's voice. The previous
// build interpolated them straight into innerHTML, so a transcript containing
// markup executed as markup. Everything user- or model-derived goes through
// esc() before it reaches the DOM.
'use strict';

window.ARIA = window.ARIA || {};

ARIA.util = (function () {
  const HTML_ESCAPES = {
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
    '`': '&#96;',
  };

  /** Escape a value for safe interpolation into an HTML template. */
  function esc(value) {
    if (value === null || value === undefined) return '';
    return String(value).replace(/[&<>"'`]/g, (char) => HTML_ESCAPES[char]);
  }

  /** Escape a value for use inside a double-quoted HTML attribute. */
  const attr = esc;

  function $(selector, root = document) {
    return root.querySelector(selector);
  }

  function $$(selector, root = document) {
    return Array.from(root.querySelectorAll(selector));
  }

  /** Delegated listener: survives re-renders of the container's children. */
  function delegate(root, eventName, selector, handler) {
    if (!root) return;
    root.addEventListener(eventName, (event) => {
      const target = event.target.closest(selector);
      if (target && root.contains(target)) handler(event, target);
    });
  }

  function setText(node, text) {
    if (node) node.textContent = text === null || text === undefined ? '' : String(text);
  }

  function toggle(node, className, on) {
    if (node) node.classList.toggle(className, !!on);
  }

  // ── Time ───────────────────────────────────────────────────────────────────

  function parseDate(value) {
    if (!value) return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function clock(value) {
    const date = parseDate(value);
    if (!date) return '—';
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  function shortClock(value) {
    const date = parseDate(value);
    if (!date) return '—';
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  /** "4m 12s" / "1h 03m" — for elapsed and remaining durations. */
  function duration(seconds) {
    const total = Math.max(0, Math.round(Math.abs(seconds)));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    if (hours) return `${hours}h ${String(minutes).padStart(2, '0')}m`;
    if (minutes) return `${minutes}m ${String(secs).padStart(2, '0')}s`;
    return `${secs}s`;
  }

  function secondsSince(value) {
    const date = parseDate(value);
    return date ? (Date.now() - date.getTime()) / 1000 : 0;
  }

  function secondsUntil(value) {
    const date = parseDate(value);
    return date ? (date.getTime() - Date.now()) / 1000 : 0;
  }

  /**
   * Countdown to an expected return. Past due counts up as overdue rather than
   * disappearing: the shelter head needs to see how late a volunteer is.
   */
  function countdown(expectedReturn) {
    const remaining = secondsUntil(expectedReturn);
    if (!parseDate(expectedReturn)) return { text: '—', overdue: false, urgent: false };
    if (remaining <= 0) {
      return { text: `+${duration(-remaining)} overdue`, overdue: true, urgent: true };
    }
    return { text: duration(remaining), overdue: false, urgent: remaining < 300 };
  }

  // ── Misc ───────────────────────────────────────────────────────────────────

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  function debounce(fn, wait = 200) {
    let timer = null;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), wait);
    };
  }

  function pluralise(count, singular, plural) {
    return `${count} ${count === 1 ? singular : plural || `${singular}s`}`;
  }

  /** Base64-encode an ArrayBuffer without blowing the call stack on big files. */
  function bufferToBase64(buffer) {
    const bytes = new Uint8Array(buffer);
    const CHUNK = 0x8000;
    let binary = '';
    for (let i = 0; i < bytes.length; i += CHUNK) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
    }
    return btoa(binary);
  }

  const SEVERITY_ORDER = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'];

  function severityClass(severity) {
    return `sev-${String(severity || 'HIGH').toLowerCase()}`;
  }

  function statusClass(status) {
    return `status-${String(status || 'QUEUED').toLowerCase()}`;
  }

  function statusLabel(status) {
    return String(status || '').replace(/_/g, ' ');
  }

  return {
    $,
    $$,
    attr,
    bufferToBase64,
    clamp,
    clock,
    countdown,
    debounce,
    delegate,
    duration,
    esc,
    parseDate,
    pluralise,
    secondsSince,
    secondsUntil,
    setText,
    severityClass,
    shortClock,
    statusClass,
    statusLabel,
    toggle,
    SEVERITY_ORDER,
  };
})();
