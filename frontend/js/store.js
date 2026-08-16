// frontend/js/store.js
// Single source of truth for the renderer.
//
// Views never fetch and never talk to each other: they read store.state and
// re-render when notified. That is what stopped the three panels from showing
// three different moments in time — the board now arrives as one snapshot from
// one response, and every view redraws from that same object.
'use strict';

window.ARIA = window.ARIA || {};

ARIA.store = (function () {
  const EMPTY_BOARD = { queue: [], volunteers: [], inventory: [], buffer: [], metrics: {} };

  const state = {
    board: EMPTY_BOARD,
    settings: null,

    /** Request returned by the last intake, waiting for a human decision. */
    incoming: null,
    /** Situation indices the manager has ticked in the review panel. */
    selectedSituations: new Set(),
    /** item name → quantity override for the approval. */
    materialOverrides: new Map(),

    selectedRequestId: null,
    analysisMode: 'review', // review | detail | log
    boardFilter: 'ALL', // ALL | AWAITING_REVIEW | QUEUED | ASSIGNED
    inventoryQuery: '',
    logs: [],

    npuMode: false,
    connection: { live: false, reason: 'connecting', lastUpdate: null },
    busy: false,
  };

  const listeners = new Set();

  function notify(reason = 'update') {
    listeners.forEach((listener) => {
      try {
        listener(state, reason);
      } catch (error) {
        console.error('[aria] view failed to render', error);
      }
    });
  }

  function subscribe(listener) {
    listeners.add(listener);
    return () => listeners.delete(listener);
  }

  function update(patch, reason = 'update') {
    Object.assign(state, patch);
    notify(reason);
  }

  function setBoard(board) {
    if (!board) return;
    state.board = {
      queue: board.queue || [],
      volunteers: board.volunteers || [],
      inventory: board.inventory || [],
      buffer: board.buffer || [],
      metrics: board.metrics || state.board.metrics || {},
    };
    state.connection.lastUpdate = new Date();

    // Drop a selection that no longer exists (resolved, cancelled, superseded).
    if (state.selectedRequestId && !findRequest(state.selectedRequestId)) {
      state.selectedRequestId = null;
      if (state.analysisMode === 'detail') {
        state.analysisMode = state.incoming ? 'review' : 'log';
      }
    }
    notify('board');
  }

  function findRequest(requestId) {
    return state.board.queue.find((request) => request.request_id === requestId) || null;
  }

  function findVolunteer(volunteerId) {
    return (
      state.board.volunteers.find((volunteer) => volunteer.volunteer_id === volunteerId) || null
    );
  }

  function findInventory(itemName) {
    if (!itemName) return null;
    const needle = String(itemName).toLowerCase();
    return (
      state.board.inventory.find((row) => String(row.item).toLowerCase() === needle) || null
    );
  }

  /** Put a freshly triaged request into the review panel with sane defaults. */
  function setIncoming(request) {
    state.incoming = request;
    state.selectedSituations = new Set(request ? [0] : []);
    state.materialOverrides = new Map();
    state.analysisMode = request ? 'review' : 'log';
    notify('incoming');
  }

  function clearIncoming() {
    state.incoming = null;
    state.selectedSituations = new Set();
    state.materialOverrides = new Map();
    notify('incoming');
  }

  function toggleSituation(index) {
    if (state.selectedSituations.has(index)) state.selectedSituations.delete(index);
    else state.selectedSituations.add(index);
    notify('selection');
  }

  function setMaterialQuantity(item, quantity) {
    state.materialOverrides.set(item, Math.max(0, quantity));
    notify('selection');
  }

  /** Effective quantity for a material line, honouring any manager edit. */
  function materialQuantity(material) {
    if (state.materialOverrides.has(material.item)) {
      return state.materialOverrides.get(material.item);
    }
    return material.quantity;
  }

  function selectRequest(requestId) {
    state.selectedRequestId = requestId;
    state.analysisMode = 'detail';
    notify('selection');
  }

  function setAnalysisMode(mode) {
    state.analysisMode = mode;
    notify('mode');
  }

  function setConnection(live, reason = '') {
    state.connection = { ...state.connection, live, reason };
    notify('connection');
  }

  function setBusy(busy) {
    state.busy = busy;
    notify('busy');
  }

  return {
    state,
    subscribe,
    notify,
    update,
    setBoard,
    setIncoming,
    clearIncoming,
    toggleSituation,
    setMaterialQuantity,
    materialQuantity,
    selectRequest,
    setAnalysisMode,
    setConnection,
    setBusy,
    findRequest,
    findVolunteer,
    findInventory,
  };
})();
