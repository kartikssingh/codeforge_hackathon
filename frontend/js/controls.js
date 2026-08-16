// frontend/js/controls.js
// The small forms: roster size, add volunteer, add stock, create item, refill.
'use strict';

window.ARIA = window.ARIA || {};

ARIA.controls = (function () {
  function onEnter(node, handler) {
    if (!node) return;
    node.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        handler();
      }
    });
  }

  function mount() {
    // ── Roster ───────────────────────────────────────────────────────────────
    const countInput = document.getElementById('volunteer-count-input');
    const setCount = () => {
      const count = Number(countInput?.value);
      if (!Number.isInteger(count) || count < 0) {
        ARIA.toast.warn('Enter a whole number of volunteers.');
        return;
      }
      ARIA.actions.setVolunteerCount(count);
    };
    document.getElementById('btn-set-volunteers')?.addEventListener('click', setCount);
    onEnter(countInput, setCount);

    const nameInput = document.getElementById('new-volunteer-name');
    const addVolunteer = () => {
      ARIA.actions.addVolunteer((nameInput?.value || '').trim());
      if (nameInput) nameInput.value = '';
    };
    document.getElementById('btn-add-volunteer')?.addEventListener('click', addVolunteer);
    onEnter(nameInput, addVolunteer);

    // ── Stock ────────────────────────────────────────────────────────────────
    const stockItem = document.getElementById('stock-item');
    const stockQty = document.getElementById('stock-qty');
    const addStock = async () => {
      const item = (stockItem?.value || '').trim();
      const quantity = Number(stockQty?.value);
      if (!item || !Number.isInteger(quantity) || quantity < 1) {
        ARIA.toast.warn('Enter an item name and a quantity of at least 1.');
        return;
      }
      if (await ARIA.actions.addStock(item, quantity)) {
        stockItem.value = '';
        stockQty.value = '';
      }
    };
    document.getElementById('btn-add-stock')?.addEventListener('click', addStock);
    onEnter(stockItem, addStock);
    onEnter(stockQty, addStock);

    // ── New item ─────────────────────────────────────────────────────────────
    const newItem = document.getElementById('new-item');
    const newCapacity = document.getElementById('new-capacity');
    const newBin = document.getElementById('new-bin');
    const createItem = async () => {
      const item = (newItem?.value || '').trim();
      const capacity = Number(newCapacity?.value);
      if (!item || !Number.isInteger(capacity) || capacity < 1) {
        ARIA.toast.warn('Enter a name and a capacity of at least 1.');
        return;
      }
      if (await ARIA.actions.createItem(item, capacity, (newBin?.value || '').trim())) {
        newItem.value = '';
        newCapacity.value = '';
        if (newBin) newBin.value = '';
      }
    };
    document.getElementById('btn-create-item')?.addEventListener('click', createItem);
    onEnter(newItem, createItem);
    onEnter(newCapacity, createItem);
    onEnter(newBin, createItem);

    // ── Refill ───────────────────────────────────────────────────────────────
    document
      .getElementById('btn-refill-partial')
      ?.addEventListener('click', () => ARIA.actions.refill('partial'));
    document
      .getElementById('btn-refill-daily')
      ?.addEventListener('click', () => ARIA.actions.refill('daily'));

    // ── Analysis panel actions ───────────────────────────────────────────────
    document.getElementById('btn-approve')?.addEventListener('click', () => ARIA.actions.approveIncoming());
    document.getElementById('btn-override')?.addEventListener('click', () => ARIA.modals.openOverride());
    document.getElementById('btn-discard')?.addEventListener('click', () => ARIA.actions.discardIncoming());
  }

  return { mount };
})();
