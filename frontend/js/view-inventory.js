// frontend/js/view-inventory.js
// Stock panel: capacity bars, reserved holds, low-stock warnings.
//
// The bar shows three quantities at once — available, reserved and empty —
// because "5 of 20" means something very different when 14 of the missing 15
// are reserved for a volunteer who is already on the way.
'use strict';

window.ARIA = window.ARIA || {};

ARIA.viewInventory = (function () {
  const { esc, delegate } = ARIA.util;

  const STATUS_LABEL = {
    OK: '',
    LOW: 'LOW',
    OUT_OF_STOCK: 'OUT',
    ALL_RESERVED: 'ALL RESERVED',
    UNTRACKED: 'UNTRACKED',
  };

  function matches(row, query) {
    if (!query) return true;
    const needle = query.toLowerCase();
    return (
      String(row.item).toLowerCase().includes(needle) ||
      String(row.category || '').toLowerCase().includes(needle) ||
      String(row.bin || '').toLowerCase().includes(needle)
    );
  }

  function rowHtml(row) {
    const total = row.total || 0;
    const availablePct = total ? Math.round((row.available / total) * 100) : 0;
    const reservedPct = total ? Math.round((row.reserved / total) * 100) : 0;
    const badge = STATUS_LABEL[row.status] || '';
    const isLow = row.status !== 'OK' && row.status !== 'UNTRACKED';

    return `
      <div class="inv-item${isLow ? ' inv-item-low' : ''}" data-item="${esc(row.item)}">
        <div class="inv-item-header">
          <span class="inv-name" title="${esc(row.category || '')} · bin ${esc(row.bin || '?')}">${esc(row.item)}</span>
          <span class="inv-count${isLow ? ' critical-text' : ''}">${row.available}/${total}</span>
        </div>
        <div class="inv-bar-track" role="img"
             aria-label="${row.available} available, ${row.reserved} reserved, of ${total}">
          <div class="inv-bar-fill${isLow ? ' inv-bar-low' : ''}" style="width:${availablePct}%"></div>
          <div class="inv-bar-reserved" style="width:${reservedPct}%"></div>
        </div>
        <div class="inv-meta-row">
          <span class="inv-meta">${esc(row.bin || '—')}</span>
          ${row.reserved ? `<span class="inv-meta inv-reserved">${row.reserved} held</span>` : ''}
          ${badge ? `<span class="inv-badge">${esc(badge)}</span>` : ''}
        </div>
      </div>`;
  }

  function render(state) {
    const list = document.getElementById('inventory-list');
    if (!list) return;

    const query = state.inventoryQuery;
    const rows = state.board.inventory.filter((row) => matches(row, query));
    // Anything needing attention floats to the top of a long list.
    rows.sort((a, b) => {
      const rank = (row) => (row.status === 'OK' ? 1 : 0);
      return rank(a) - rank(b) || a.item.localeCompare(b.item);
    });

    list.innerHTML = rows.length
      ? rows.map(rowHtml).join('')
      : `<div class="empty-hint">${query ? 'No item matches that search.' : 'No inventory loaded.'}</div>`;

    const summary = document.getElementById('inventory-summary');
    if (summary) {
      const stats = state.board.metrics.inventory;
      const buffered = state.board.buffer.reduce((sum, entry) => sum + entry.quantity, 0);
      summary.textContent = stats
        ? `${stats.units_available} available · ${stats.units_reserved} held · ${stats.low_stock_items} low` +
          (buffered ? ` · ${buffered} in buffer` : '')
        : `${rows.length} item(s)`;
    }
  }

  function mount() {
    const search = document.getElementById('inventory-search');
    if (search) {
      search.addEventListener(
        'input',
        ARIA.util.debounce((event) => {
          ARIA.store.update({ inventoryQuery: event.target.value.trim() }, 'filter');
        }, 120),
      );
    }

    // Click an item to prefill the "add stock" form with its name.
    delegate(document.getElementById('inventory-list'), 'click', '.inv-item', (_event, node) => {
      const field = document.getElementById('stock-item');
      if (field) {
        field.value = node.dataset.item;
        field.focus();
      }
    });
  }

  return { render, mount };
})();
