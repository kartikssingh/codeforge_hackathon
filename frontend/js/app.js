// frontend/js/app.js
// Bootstrap: theme, settings, live stream, render loop, keyboard shortcuts.
//
// Live updates arrive over Server-Sent Events. Polling still exists, but only
// as a slow safety net for when the stream is down — the three independent
// three-second pollers of the old build are gone.
'use strict';

window.ARIA = window.ARIA || {};

ARIA.app = (function () {
  const store = ARIA.store;

  let pollTimer = null;
  let unsubscribeEvents = null;
  let refreshQueued = false;

  const VIEWS = [
    ARIA.viewInventory,
    ARIA.viewBoard,
    ARIA.viewVolunteers,
    ARIA.viewAnalysis,
  ];

  // ── Rendering ──────────────────────────────────────────────────────────────

  function renderAll(state) {
    VIEWS.forEach((view) => view.render(state));
    renderConnection(state);
  }

  function renderConnection(state) {
    const dot = document.getElementById('conn-dot');
    const label = document.getElementById('conn-label');
    if (!dot || !label) return;
    const { live, lastUpdate } = state.connection;
    dot.className = `conn-dot ${live ? 'conn-live' : 'conn-down'}`;
    label.textContent = live
      ? `LIVE${lastUpdate ? ` · ${ARIA.util.shortClock(lastUpdate)}` : ''}`
      : 'POLLING';
    label.title = live
      ? 'Streaming updates from the backend'
      : `No event stream — falling back to polling (${state.connection.reason || 'disconnected'})`;
  }

  // ── Data loading ───────────────────────────────────────────────────────────

  /** Fetch the board; coalesces bursts of events into one request. */
  async function refreshBoard() {
    if (refreshQueued) return;
    refreshQueued = true;
    try {
      const board = await ARIA.api.board();
      if (board) store.setBoard(board);
    } finally {
      refreshQueued = false;
    }
  }

  async function refreshLogs() {
    const payload = await ARIA.api.logs({ limit: 120 });
    if (payload) store.update({ logs: payload.logs || [] }, 'logs');
  }

  async function loadSettings() {
    const settings = await ARIA.api.settings();
    if (settings) {
      store.update({ settings }, 'settings');
      ARIA.intake.applySettings();
    }
  }

  async function reportHealth() {
    const health = await ARIA.api.health();
    if (!health) return;
    const missing = (health.components || []).filter((component) => !component.ok);
    if (health.status === 'degraded' && missing.length) {
      ARIA.toast.warn('ARIA is running with reduced capability.', {
        detail: missing.map((component) => `${component.name}: ${component.detail}`).join(' · '),
      });
    } else if (health.status === 'down') {
      ARIA.toast.error('Core services failed to start. Check backend/logs/aria.log.');
    }
  }

  // ── Live stream ────────────────────────────────────────────────────────────

  function startEventStream() {
    unsubscribeEvents = ARIA.api.onEvent((type, payload) => {
      if (type === 'connection') {
        store.setConnection(!!payload.connected, payload.reason || '');
        if (payload.connected) refreshBoard();
        return;
      }
      if (type === 'heartbeat' || type === 'ready') return;

      if (type === 'request.escalated') {
        ARIA.toast.warn(
          `${payload.payload?.request_id} escalated to ${payload.payload?.severity} while waiting.`,
        );
      }
      // Any other event means board state moved.
      refreshBoard();
      if (store.state.analysisMode === 'log') refreshLogs();
    });
  }

  function startPolling() {
    const interval = store.state.settings?.polling?.board_ms || 10000;
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(() => {
      // While the stream is healthy this is a cheap consistency check.
      refreshBoard();
      if (store.state.analysisMode === 'log') refreshLogs();
    }, store.state.connection.live ? interval * 3 : interval);
  }

  function startTimers() {
    setInterval(() => ARIA.viewBoard.tick(), 1000);
    // Re-pace polling when the connection state changes.
    let wasLive = store.state.connection.live;
    store.subscribe((state) => {
      if (state.connection.live !== wasLive) {
        wasLive = state.connection.live;
        startPolling();
      }
    });
  }

  // ── Chrome: theme, compute mode, shortcuts ─────────────────────────────────

  function initTheme() {
    const saved = localStorage.getItem('aria-theme') || 'dark';
    document.documentElement.setAttribute('data-theme', saved);
    document.querySelectorAll('.theme-btn').forEach((button) => {
      button.classList.toggle('active', button.dataset.theme === saved);
      button.addEventListener('click', () => {
        const theme = button.dataset.theme;
        document.documentElement.setAttribute('data-theme', theme);
        localStorage.setItem('aria-theme', theme);
        document.querySelectorAll('.theme-btn').forEach((other) => {
          other.classList.toggle('active', other === button);
        });
      });
    });
  }

  function initComputeMode() {
    const saved = localStorage.getItem('aria-compute') || 'cpu';
    store.state.npuMode = saved === 'npu';
    document.querySelectorAll('.compute-btn').forEach((button) => {
      button.classList.toggle('active', button.dataset.mode === saved);
      button.addEventListener('click', () => {
        const mode = button.dataset.mode;
        store.state.npuMode = mode === 'npu';
        localStorage.setItem('aria-compute', mode);
        document.querySelectorAll('.compute-btn').forEach((other) => {
          other.classList.toggle('active', other === button);
        });
        ARIA.toast.info(
          mode === 'npu'
            ? 'NPU mode: the next report will try the on-device accelerator.'
            : 'CPU mode: the next report will use the standard backend.',
        );
      });
    });
  }

  function initShortcuts() {
    document.addEventListener('keydown', (event) => {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '');
      if (event.key === '/' && !typing) {
        event.preventDefault();
        document.getElementById('inventory-search')?.focus();
        return;
      }
      if (typing || event.ctrlKey || event.metaKey || event.altKey) return;

      const shortcuts = {
        t: () => ARIA.intake.switchTab('text'),
        a: () => ARIA.intake.switchTab('audio'),
        r: () => store.setAnalysisMode('review'),
        d: () => store.setAnalysisMode('detail'),
        l: () => {
          store.setAnalysisMode('log');
          refreshLogs();
        },
        g: () => refreshBoard(),
      };
      const action = shortcuts[event.key.toLowerCase()];
      if (action) {
        event.preventDefault();
        action();
      }
    });
  }

  function showOfflineBanner() {
    const banner = document.getElementById('bridge-warning');
    if (banner) banner.classList.remove('hidden');
  }

  // ── Boot ───────────────────────────────────────────────────────────────────

  async function start() {
    initTheme();
    initComputeMode();
    initShortcuts();

    ARIA.viewInventory.mount();
    ARIA.viewBoard.mount();
    ARIA.viewVolunteers.mount();
    ARIA.viewAnalysis.mount();
    ARIA.modals.mount();
    ARIA.intake.mount();
    ARIA.controls.mount();

    store.subscribe(renderAll);
    renderAll(store.state);

    if (!ARIA.api.available()) {
      showOfflineBanner();
      return;
    }

    await loadSettings();
    await refreshBoard();
    await refreshLogs();
    startEventStream();
    startPolling();
    startTimers();
    reportHealth();
  }

  document.addEventListener('DOMContentLoaded', start);

  return { refreshBoard, refreshLogs, start };
})();
