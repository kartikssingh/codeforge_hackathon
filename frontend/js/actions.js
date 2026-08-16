// frontend/js/actions.js
// Every state-changing operation the operator can trigger.
//
// One pattern throughout: call the backend, take the board out of the response,
// hand it to the store. Because each mutating endpoint returns the whole board,
// the UI is never assembled from several requests taken at different moments.
'use strict';

window.ARIA = window.ARIA || {};

ARIA.actions = (function () {
  const store = ARIA.store;

  function applyBoard(response) {
    if (response && response.board) store.setBoard(response.board);
    return response;
  }

  // ── Review and approval ────────────────────────────────────────────────────

  async function approveIncoming() {
    const request = store.state.incoming;
    if (!request) return;

    const indices = Array.from(store.state.selectedSituations).sort((a, b) => a - b);
    if (!indices.length) {
      ARIA.toast.warn('Tick at least one situation before approving.');
      return;
    }

    // Only send overrides for materials that belong to the chosen situations.
    const relevant = new Set();
    indices.forEach((index) => {
      (request.situations[index]?.materials || []).forEach((material) => relevant.add(material.item));
    });
    const material_overrides = Array.from(store.state.materialOverrides.entries())
      .filter(([item]) => relevant.has(item))
      .map(([item, quantity]) => ({ item, quantity }));

    store.setBusy(true);
    const response = await ARIA.api.approve(request.request_id, {
      selected_indices: indices,
      material_overrides,
      note: '',
    });
    store.setBusy(false);
    if (!response) return;

    applyBoard(response);
    store.clearIncoming();

    const approved = response.request || {};
    const shortfalls = (response.detail?.reservation?.lines || []).filter((line) => !line.ok);
    if (approved.assigned_volunteer) {
      ARIA.toast.success(`${approved.request_id} dispatched to ${approved.assigned_volunteer}.`);
    } else {
      ARIA.toast.info(`${approved.request_id} queued — no volunteer is free yet.`);
    }
    if (shortfalls.length) {
      ARIA.toast.warn('Some supplies could not be reserved in full.', {
        detail: shortfalls
          .map((line) => `${line.item}: ${line.reserved}/${line.requested}`)
          .join(' · '),
      });
    }
    store.selectRequest(approved.request_id);
  }

  async function discardIncoming() {
    const request = store.state.incoming;
    if (!request) return;
    const ok = await ARIA.modals.confirm(
      'Discard this report?',
      `${request.request_id} will be cancelled and removed from the board. Nothing has been reserved yet.`,
      'DISCARD',
    );
    if (!ok) return;

    const response = await ARIA.api.cancel(request.request_id, 'Discarded by the shelter manager');
    if (!response) return;
    applyBoard(response);
    store.clearIncoming();
    ARIA.toast.info(`${request.request_id} discarded.`);
  }

  /** Put an already-stored request back into the review panel. */
  function reviewExisting(requestId) {
    const request = store.findRequest(requestId);
    if (!request) return;
    if (request.status !== 'AWAITING_REVIEW') {
      store.selectRequest(requestId);
      return;
    }
    store.setIncoming(request);
  }

  async function applyOverride(payload) {
    const source = store.state.incoming || store.findRequest(store.state.selectedRequestId);
    if (!source) return;

    store.setBusy(true);
    const response = await ARIA.api.override(source.request_id, payload);
    store.setBusy(false);
    if (!response) return;

    applyBoard(response);
    store.clearIncoming();
    const created = response.request || {};
    ARIA.toast.success(`Override ${created.request_id} created and queued.`);
    store.selectRequest(created.request_id);
  }

  async function cancelRequest(requestId) {
    const request = store.findRequest(requestId);
    if (!request) return;
    const ok = await ARIA.modals.confirm(
      `Cancel ${requestId}?`,
      'Any supplies held for this request go straight back to available stock.',
      'CANCEL REQUEST',
    );
    if (!ok) return;

    const response = await ARIA.api.cancel(requestId, 'Cancelled by the shelter manager');
    if (!response) return;
    applyBoard(response);
    if (store.state.incoming && store.state.incoming.request_id === requestId) store.clearIncoming();
    ARIA.toast.info(`${requestId} cancelled.`);
  }

  // ── Volunteers ─────────────────────────────────────────────────────────────

  async function volunteerReturn(volunteerId, items, note) {
    const response = await ARIA.api.volunteerReturn(volunteerId, items, note || '');
    if (!response) return;
    applyBoard(response);

    const settlement = response.detail?.settlement || {};
    const consumed = (settlement.consumed || []).reduce((sum, entry) => sum + entry.quantity, 0);
    const assignments = response.detail?.new_assignments || [];
    ARIA.toast.success(
      `${volunteerId} is back at base.`,
      consumed
        ? { detail: `${consumed} item(s) used on site and written off stock.` }
        : undefined,
    );
    assignments.forEach((assignment) => {
      ARIA.toast.info(`${assignment.volunteer_id} dispatched to ${assignment.request_id}.`);
    });
  }

  async function setVolunteerCount(count) {
    const response = await ARIA.api.setVolunteerCount(count);
    if (!response) return;
    applyBoard(response);
    ARIA.toast.success(`Roster set to ${response.detail?.count ?? count}.`);
  }

  async function addVolunteer(name) {
    const response = await ARIA.api.addVolunteer(name || '');
    if (!response) return;
    applyBoard(response);
    const volunteer = response.detail?.volunteer;
    ARIA.toast.success(`${volunteer?.name || 'Volunteer'} added to the roster.`);
  }

  async function removeVolunteer(volunteerId) {
    const ok = await ARIA.modals.confirm(
      `Remove ${volunteerId}?`,
      'They will no longer be considered for dispatch. Mission history is kept in the log.',
      'REMOVE',
    );
    if (!ok) return;
    const response = await ARIA.api.removeVolunteer(volunteerId);
    if (!response) return;
    applyBoard(response);
    ARIA.toast.info(`${volunteerId} removed from the roster.`);
  }

  async function setVolunteerStatus(volunteerId, status) {
    const response = await ARIA.api.setVolunteerStatus(volunteerId, status);
    if (!response) return;
    applyBoard(response);
  }

  // ── Inventory ──────────────────────────────────────────────────────────────

  async function addStock(item, quantity) {
    const response = await ARIA.api.addStock(item, quantity);
    if (!response) return false;
    applyBoard(response);
    ARIA.toast.success(`Added ${quantity} × ${item}.`);
    return true;
  }

  async function createItem(item, capacity, bin, category) {
    const response = await ARIA.api.createItem({
      item,
      capacity,
      bin: bin || 'NEW',
      category: category || 'General',
    });
    if (!response) return false;
    applyBoard(response);
    ARIA.toast.success(`Created ${item} with capacity ${capacity}.`);
    return true;
  }

  async function refill(mode) {
    const ok =
      mode !== 'daily' ||
      (await ARIA.modals.confirm(
        'Run the daily resupply?',
        'Every item returns to full capacity and all holds are cleared. Use this after a real resupply, not during a shift.',
        'RESUPPLY',
      ));
    if (!ok) return;

    const response = await ARIA.api.refill(mode);
    if (!response) return;
    applyBoard(response);
    ARIA.toast.success(
      mode === 'daily'
        ? 'Daily resupply applied.'
        : `Topped up ${response.detail?.items_refilled ?? 0} low item(s).`,
    );
  }

  return {
    addStock,
    addVolunteer,
    applyBoard,
    applyOverride,
    approveIncoming,
    cancelRequest,
    createItem,
    discardIncoming,
    refill,
    removeVolunteer,
    reviewExisting,
    setVolunteerCount,
    setVolunteerStatus,
    volunteerReturn,
  };
})();
