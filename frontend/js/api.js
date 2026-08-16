// frontend/js/api.js
// Thin wrapper over the preload bridge (window.aria).
//
// Adds three things the raw bridge does not have:
//   · a clear failure when the page is opened outside Electron
//   · one place where every backend error becomes a toast
//   · call() returning null on failure, so callers can write straight-line code
'use strict';

window.ARIA = window.ARIA || {};

ARIA.api = (function () {
  const bridge = window.aria || null;

  function available() {
    return bridge !== null;
  }

  function missingBridge() {
    const error = new Error(
      'This page is running outside the ARIA desktop app, so it cannot reach the backend.',
    );
    error.code = 'no_bridge';
    return error;
  }

  /**
   * Invoke a bridge method.
   * @param {string} method  name on window.aria
   * @param {Array}  args    arguments to pass
   * @param {object} options { quiet } to suppress the error toast
   * @returns {Promise<any|null>} null when the call failed
   */
  async function call(method, args = [], { quiet = false } = {}) {
    if (!bridge) {
      if (!quiet) ARIA.toast.error(missingBridge().message);
      return null;
    }
    const fn = bridge[method];
    if (typeof fn !== 'function') {
      if (!quiet) ARIA.toast.error(`The backend bridge has no "${method}" method.`);
      return null;
    }
    try {
      return await fn(...args);
    } catch (error) {
      if (!quiet) {
        const detail =
          error && error.code === 'offline'
            ? 'The backend is not answering. It may still be starting up.'
            : (error && error.detail && error.detail.hint) || '';
        ARIA.toast.error((error && error.message) || 'The request failed.', { detail });
      }
      console.warn(`[aria] ${method} failed`, error);
      return null;
    }
  }

  /** Like call(), but rethrows — for callers that must handle the failure. */
  async function callStrict(method, args = []) {
    if (!bridge) throw missingBridge();
    return bridge[method](...args);
  }

  return {
    available,
    call,
    callStrict,
    env: (bridge && bridge.env) || { baseUrl: 'http://127.0.0.1:8000' },
    onEvent: (handler) => (bridge && bridge.onEvent ? bridge.onEvent(handler) : () => {}),

    // Reads
    board: () => call('getBoard', [], { quiet: true }),
    settings: () => call('getSettings', [], { quiet: true }),
    health: () => call('getHealth', [], { quiet: true }),
    logs: (options) => call('getLogs', [options], { quiet: true }),
    history: (limit) => call('getHistory', [limit], { quiet: true }),

    // Intake
    intakeAudio: (b64, filename, npu) => callStrict('intakeAudio', [b64, filename, npu]),
    intakeText: (text, npu) => callStrict('intakeText', [text, npu]),

    // Lifecycle
    approve: (id, body) => call('approve', [id, body]),
    override: (id, body) => call('override', [id, body]),
    cancel: (id, reason) => call('cancel', [id, reason]),

    // Volunteers
    volunteerReturn: (id, items, note) => call('volunteerReturn', [id, items, note]),
    setVolunteerCount: (count) => call('setVolunteerCount', [count]),
    addVolunteer: (name) => call('addVolunteer', [name]),
    removeVolunteer: (id) => call('removeVolunteer', [id]),
    setVolunteerStatus: (id, status) => call('setVolunteerStatus', [id, status]),

    // Inventory
    addStock: (item, quantity) => call('addStock', [item, quantity]),
    createItem: (body) => call('createItem', [body]),
    deleteItem: (item) => call('deleteItem', [item]),
    refill: (mode) => call('refill', [mode]),
  };
})();
