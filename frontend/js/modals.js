// frontend/js/modals.js
// Dialogs: volunteer return, manual override, and a generic confirm.
//
// All three trap focus, close on Escape and on backdrop click, and restore
// focus to whatever opened them.
'use strict';

window.ARIA = window.ARIA || {};

ARIA.modals = (function () {
  const { esc, delegate } = ARIA.util;

  let lastFocused = null;
  let activeOverlay = null;
  let returnContext = null;

  // ── Open / close plumbing ──────────────────────────────────────────────────

  function open(overlayId) {
    const overlay = document.getElementById(overlayId);
    if (!overlay) return null;
    lastFocused = document.activeElement;
    overlay.classList.remove('hidden');
    activeOverlay = overlay;
    const focusable = overlay.querySelector(
      'input:not([type=hidden]), textarea, select, button:not(.btn-qty)',
    );
    if (focusable) focusable.focus();
    return overlay;
  }

  function close(overlayId) {
    const overlay = overlayId ? document.getElementById(overlayId) : activeOverlay;
    if (!overlay) return;
    overlay.classList.add('hidden');
    if (overlay === activeOverlay) activeOverlay = null;
    if (lastFocused && lastFocused.focus) lastFocused.focus();
  }

  function closeAll() {
    document.querySelectorAll('.modal-overlay').forEach((overlay) => overlay.classList.add('hidden'));
    activeOverlay = null;
    returnContext = null;
  }

  function trapFocus(event) {
    if (!activeOverlay || event.key !== 'Tab') return;
    const focusable = Array.from(
      activeOverlay.querySelectorAll('button, input, textarea, select, [tabindex]:not([tabindex="-1"])'),
    ).filter((node) => !node.disabled && node.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  // ── Return modal ───────────────────────────────────────────────────────────

  function itemRow(item) {
    return `
      <div class="modal-item-row">
        <span class="modal-item-name">${esc(item.item)}</span>
        <div class="qty-stepper">
          <button class="btn btn-qty" type="button" data-step="-1" data-item="${esc(item.item)}" aria-label="One fewer">−</button>
          <input class="qty-input return-qty" type="number" min="0" max="${item.quantity}"
                 value="${item.quantity}" data-item="${esc(item.item)}"
                 aria-label="${esc(item.item)} returned" />
          <button class="btn btn-qty" type="button" data-step="1" data-item="${esc(item.item)}" aria-label="One more">+</button>
        </div>
        <span class="modal-item-taken">of ${item.quantity} taken</span>
      </div>`;
  }

  /**
   * Open the "back at base" checklist.
   * Either identifier is enough — the board card knows the request, the
   * volunteer row knows the volunteer.
   */
  function openReturn(requestId, volunteerId) {
    const state = ARIA.store.state;
    let volunteer = volunteerId ? ARIA.store.findVolunteer(volunteerId) : null;
    let request = requestId ? ARIA.store.findRequest(requestId) : null;

    if (!volunteer && request) volunteer = ARIA.store.findVolunteer(request.assigned_volunteer);
    if (!request && volunteer) request = ARIA.store.findRequest(volunteer.request_id);

    if (!volunteer) {
      ARIA.toast.error('That volunteer is no longer on an active mission.');
      return;
    }

    const items = volunteer.items_taken && volunteer.items_taken.length
      ? volunteer.items_taken
      : (request && request.items_taken) || [];

    returnContext = { volunteerId: volunteer.volunteer_id, items };

    const subtitle = document.getElementById('return-subtitle');
    if (subtitle) {
      subtitle.textContent = request
        ? `${volunteer.volunteer_id} returning from ${request.request_id} — ${request.situations?.[0]?.label || ''}`
        : `${volunteer.volunteer_id} returning to base`;
    }

    const checklist = document.getElementById('return-checklist');
    if (checklist) {
      checklist.innerHTML = items.length
        ? items.map(itemRow).join('')
        : '<p class="muted">No supplies were signed out for this mission.</p>';
    }

    const note = document.getElementById('return-note');
    if (note) note.value = '';

    open('modal-return');
  }

  function collectReturnedItems() {
    return Array.from(document.querySelectorAll('#return-checklist .return-qty'))
      .map((input) => ({ item: input.dataset.item, quantity: Number(input.value) || 0 }))
      .filter((entry) => entry.quantity > 0);
  }

  // ── Override modal ─────────────────────────────────────────────────────────

  function resourceRow(row) {
    return `
      <div class="modal-item-row" data-item="${esc(row.item)}">
        <span class="modal-item-name">${esc(row.item)}</span>
        <div class="qty-stepper">
          <button class="btn btn-qty" type="button" data-step="-1" data-item="${esc(row.item)}" aria-label="One fewer">−</button>
          <input class="qty-input override-qty" type="number" min="0" max="${row.available}"
                 value="0" data-item="${esc(row.item)}" aria-label="${esc(row.item)} quantity" />
          <button class="btn btn-qty" type="button" data-step="1" data-item="${esc(row.item)}" aria-label="One more">+</button>
        </div>
        <span class="modal-item-taken">${row.available} available</span>
      </div>`;
  }

  function renderOverrideResources(query = '') {
    const container = document.getElementById('override-items');
    if (!container) return;
    const needle = query.trim().toLowerCase();
    const rows = ARIA.store.state.board.inventory.filter(
      (row) => !needle || String(row.item).toLowerCase().includes(needle),
    );
    container.innerHTML = rows.length
      ? rows.map(resourceRow).join('')
      : '<p class="muted">No item matches that search.</p>';
  }

  function openOverride() {
    const source = ARIA.store.state.incoming || ARIA.store.findRequest(ARIA.store.state.selectedRequestId);
    if (!source) {
      ARIA.toast.warn('Open a report first — an override replaces an existing assessment.');
      return;
    }

    const subtitle = document.getElementById('override-subtitle');
    if (subtitle) {
      subtitle.textContent = `Replaces ${source.request_id}. The original stays in the audit trail.`;
    }

    document.getElementById('override-condition').value = '';
    document.getElementById('override-steps').value = '';
    document.getElementById('override-severity').value = 'HIGH';
    document.getElementById('override-travel').value = 10;
    document.getElementById('override-resolution').value = 20;
    const search = document.getElementById('override-search');
    if (search) search.value = '';
    renderOverrideResources();

    open('modal-override');
  }

  function collectOverride() {
    const condition = document.getElementById('override-condition').value.trim();
    const stepsRaw = document.getElementById('override-steps').value.trim();
    return {
      condition,
      severity: document.getElementById('override-severity').value,
      travel_time_min: Number(document.getElementById('override-travel').value) || 10,
      resolution_time_min: Number(document.getElementById('override-resolution').value) || 20,
      instructions: stepsRaw ? stepsRaw.split('\n').map((line) => line.trim()).filter(Boolean) : [],
      resources: Array.from(document.querySelectorAll('#override-items .override-qty'))
        .map((input) => ({ item: input.dataset.item, quantity: Number(input.value) || 0 }))
        .filter((entry) => entry.quantity > 0),
      notes: '',
    };
  }

  // ── Confirm dialog ─────────────────────────────────────────────────────────

  let confirmResolver = null;

  function confirm(title, message, confirmLabel = 'CONFIRM') {
    document.getElementById('confirm-title').textContent = title;
    document.getElementById('confirm-message').textContent = message;
    document.getElementById('confirm-accept').textContent = confirmLabel;
    open('modal-confirm');
    return new Promise((resolve) => {
      confirmResolver = resolve;
    });
  }

  function settleConfirm(result) {
    close('modal-confirm');
    if (confirmResolver) {
      confirmResolver(result);
      confirmResolver = null;
    }
  }

  // ── Wiring ─────────────────────────────────────────────────────────────────

  function mount() {
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && activeOverlay) {
        if (activeOverlay.id === 'modal-confirm') settleConfirm(false);
        else closeAll();
      }
      trapFocus(event);
    });

    document.querySelectorAll('.modal-overlay').forEach((overlay) => {
      overlay.addEventListener('click', (event) => {
        if (event.target === overlay) {
          if (overlay.id === 'modal-confirm') settleConfirm(false);
          else closeAll();
        }
      });
    });

    document.querySelectorAll('[data-close-modal]').forEach((button) => {
      button.addEventListener('click', () => closeAll());
    });

    // Quantity steppers inside any modal.
    document.querySelectorAll('.modal-overlay').forEach((overlay) => {
      delegate(overlay, 'click', '.btn-qty', (_event, button) => {
        const input = overlay.querySelector(`.qty-input[data-item="${CSS.escape(button.dataset.item)}"]`);
        if (!input) return;
        const max = Number(input.max) || 99;
        input.value = ARIA.util.clamp((Number(input.value) || 0) + Number(button.dataset.step), 0, max);
      });
    });

    const returnConfirm = document.getElementById('return-confirm');
    if (returnConfirm) {
      returnConfirm.addEventListener('click', async () => {
        if (!returnContext) return closeAll();
        const note = document.getElementById('return-note').value.trim();
        const items = collectReturnedItems();
        const context = returnContext;
        closeAll();
        await ARIA.actions.volunteerReturn(context.volunteerId, items, note);
      });
    }

    const overrideSubmit = document.getElementById('override-submit');
    if (overrideSubmit) {
      overrideSubmit.addEventListener('click', async () => {
        const payload = collectOverride();
        if (!payload.condition) {
          ARIA.toast.warn('Describe the situation before applying an override.');
          document.getElementById('override-condition').focus();
          return;
        }
        closeAll();
        await ARIA.actions.applyOverride(payload);
      });
    }

    const overrideSearch = document.getElementById('override-search');
    if (overrideSearch) {
      overrideSearch.addEventListener(
        'input',
        ARIA.util.debounce((event) => renderOverrideResources(event.target.value), 120),
      );
    }

    const accept = document.getElementById('confirm-accept');
    const reject = document.getElementById('confirm-cancel');
    if (accept) accept.addEventListener('click', () => settleConfirm(true));
    if (reject) reject.addEventListener('click', () => settleConfirm(false));
  }

  return { mount, openReturn, openOverride, confirm, closeAll };
})();
