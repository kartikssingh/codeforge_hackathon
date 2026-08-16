// frontend/js/view-board.js
// The incident board: metric strip, filter chips, incident cards.
//
// Card timers count *down* to the volunteer's expected return and keep counting
// once they are late. The previous build counted up from zero from the moment
// the card appeared, which told the shelter head nothing about whether anyone
// was overdue.
'use strict';

window.ARIA = window.ARIA || {};

ARIA.viewBoard = (function () {
  const { esc, severityClass, statusClass, statusLabel, countdown, duration, secondsSince } =
    ARIA.util;

  const FILTERS = ['ALL', 'AWAITING_REVIEW', 'QUEUED', 'ASSIGNED'];

  function passesFilter(request, filter) {
    return filter === 'ALL' || request.status === filter;
  }

  function metricStrip(state) {
    const metrics = state.board.metrics || {};
    const requests = metrics.requests || {};
    const volunteers = metrics.volunteers || {};
    const inventory = metrics.inventory || {};
    const sla = metrics.sla || {};
    const bySeverity = requests.open_by_severity || {};

    const tiles = [
      { id: 'open', label: 'OPEN', value: requests.open ?? 0 },
      {
        id: 'critical',
        label: 'CRITICAL',
        value: bySeverity.CRITICAL ?? 0,
        alert: (bySeverity.CRITICAL ?? 0) > 0,
      },
      {
        id: 'breach',
        label: 'OVER SLA',
        value: sla.breaching_now ?? 0,
        alert: (sla.breaching_now ?? 0) > 0,
      },
      {
        id: 'volunteers',
        label: 'VOLUNTEERS',
        value: `${volunteers.busy ?? 0}/${volunteers.total ?? 0}`,
        alert: (volunteers.total ?? 0) > 0 && volunteers.available === 0,
      },
      {
        id: 'stock',
        label: 'STOCK',
        value: `${inventory.fill_pct ?? 0}%`,
        alert: (inventory.low_stock_items ?? 0) > 0,
      },
    ];

    return tiles
      .map(
        (tile) => `
      <div class="metric-tile${tile.alert ? ' metric-alert' : ''}">
        <div class="metric-value">${esc(tile.value)}</div>
        <div class="metric-label">${esc(tile.label)}</div>
      </div>`,
      )
      .join('');
  }

  function timerHtml(request) {
    if (request.status === 'ASSIGNED' && request.expected_return) {
      const timer = countdown(request.expected_return);
      const cls = timer.overdue ? 'card-timer overdue' : timer.urgent ? 'card-timer urgent' : 'card-timer';
      return `<span class="${cls}" data-countdown="${esc(request.expected_return)}"
                    title="Expected back at ${esc(ARIA.util.clock(request.expected_return))}">${esc(timer.text)}</span>`;
    }
    const waited = secondsSince(request.created_at);
    return `<span class="card-timer waiting" data-elapsed="${esc(request.created_at)}"
                  title="Waiting since ${esc(ARIA.util.clock(request.created_at))}">waiting ${esc(duration(waited))}</span>`;
  }

  function cardHtml(request, state) {
    const severity = request.severity || 'HIGH';
    const primary =
      (request.situations || []).find((situation) => situation.selected) ||
      (request.situations || [])[0] ||
      {};
    const selected = request.request_id === state.selectedRequestId;
    const escalated = (request.escalation_stage || 0) > 0;
    const degraded = request.degraded;

    const actions = [];
    if (request.status === 'ASSIGNED') {
      actions.push(
        `<button class="btn btn-sm btn-complete" data-action="return" data-request="${esc(request.request_id)}"
                 data-volunteer="${esc(request.assigned_volunteer || '')}">BACK AT BASE</button>`,
      );
    }
    if (request.status === 'AWAITING_REVIEW') {
      actions.push(
        `<button class="btn btn-sm btn-review" data-action="review" data-request="${esc(request.request_id)}">REVIEW</button>`,
      );
    }
    if (request.status !== 'ASSIGNED') {
      actions.push(
        `<button class="btn btn-sm btn-ghost" data-action="cancel" data-request="${esc(request.request_id)}">CANCEL</button>`,
      );
    }

    return `
      <article class="task-card ${severityClass(severity)} ${statusClass(request.status)}${selected ? ' card-selected' : ''}"
               data-request="${esc(request.request_id)}" tabindex="0" role="button"
               aria-pressed="${selected}" aria-label="${esc(request.request_id)} ${esc(severity)} ${esc(primary.label || '')}">
        <div class="card-top">
          <div class="card-badges">
            <span class="severity-badge ${severityClass(severity)}">${esc(severity)}</span>
            <span class="status-badge ${statusClass(request.status)}">${esc(statusLabel(request.status))}</span>
            ${escalated ? '<span class="chip chip-warn" title="Priority escalated while waiting">ESCALATED</span>' : ''}
            ${request.is_vague ? '<span class="chip" title="Report was ambiguous and was expanded into hypotheses">VAGUE</span>' : ''}
            ${degraded ? '<span class="chip" title="Some AI capability was unavailable during triage">DEGRADED</span>' : ''}
          </div>
          ${timerHtml(request)}
        </div>

        <h3 class="card-title">${esc(primary.label || 'Emergency request')}</h3>
        <p class="card-transcript">${esc(request.transcript || '')}</p>

        <div class="card-footer">
          <div class="card-meta">
            <span class="card-id">${esc(request.request_id)}</span>
            ${
              request.assigned_volunteer
                ? `<span class="card-volunteer">${esc(request.assigned_volunteer)}</span>`
                : ''
            }
            ${
              (request.items_taken || []).length
                ? `<span class="card-items">${ARIA.util.pluralise(request.items_taken.length, 'item')}</span>`
                : ''
            }
          </div>
          <div class="card-actions">${actions.join('')}</div>
        </div>
      </article>`;
  }

  function render(state) {
    const strip = document.getElementById('metric-strip');
    if (strip) strip.innerHTML = metricStrip(state);

    document.querySelectorAll('.filter-chip').forEach((chip) => {
      chip.classList.toggle('active', chip.dataset.filter === state.boardFilter);
    });

    const list = document.getElementById('task-list');
    if (!list) return;

    const visible = state.board.queue.filter((request) => passesFilter(request, state.boardFilter));
    const count = document.getElementById('incident-count');
    if (count) count.textContent = String(visible.length);

    list.innerHTML = visible.length
      ? visible.map((request) => cardHtml(request, state)).join('')
      : `<div class="empty-hint">${
          state.boardFilter === 'ALL'
            ? 'No active incidents. Submit a report to begin.'
            : 'Nothing in this state right now.'
        }</div>`;
  }

  /** Update only the timer text — called once a second, never re-renders cards. */
  function tick() {
    document.querySelectorAll('[data-countdown]').forEach((node) => {
      const timer = countdown(node.dataset.countdown);
      node.textContent = timer.text;
      node.classList.toggle('overdue', timer.overdue);
      node.classList.toggle('urgent', timer.urgent && !timer.overdue);
    });
    document.querySelectorAll('[data-elapsed]').forEach((node) => {
      node.textContent = `waiting ${duration(secondsSince(node.dataset.elapsed))}`;
    });
  }

  function mount() {
    const list = document.getElementById('task-list');

    ARIA.util.delegate(list, 'click', '.task-card', (event, card) => {
      if (event.target.closest('[data-action]')) return; // buttons handle themselves
      ARIA.store.selectRequest(card.dataset.request);
    });

    ARIA.util.delegate(list, 'keydown', '.task-card', (event, card) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        ARIA.store.selectRequest(card.dataset.request);
      }
    });

    ARIA.util.delegate(list, 'click', '[data-action]', (event, button) => {
      event.stopPropagation();
      const { action, request, volunteer } = button.dataset;
      if (action === 'return') ARIA.modals.openReturn(request, volunteer);
      if (action === 'cancel') ARIA.actions.cancelRequest(request);
      if (action === 'review') ARIA.actions.reviewExisting(request);
    });

    document.querySelectorAll('.filter-chip').forEach((chip) => {
      chip.addEventListener('click', () => {
        ARIA.store.update({ boardFilter: chip.dataset.filter }, 'filter');
      });
    });
  }

  return { render, tick, mount, FILTERS };
})();
