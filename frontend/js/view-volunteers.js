// frontend/js/view-volunteers.js
// The volunteer activity board.
//
// This panel is new. The old dashboard tracked volunteers server-side and never
// showed them, so the shelter head could not see who was out, on what, carrying
// which supplies, or how overdue they were — the single most important thing to
// know when deciding whether to send someone else.
'use strict';

window.ARIA = window.ARIA || {};

ARIA.viewVolunteers = (function () {
  const { esc, countdown, clock, delegate } = ARIA.util;

  function itemsSummary(items) {
    if (!items || !items.length) return '—';
    return items.map((item) => `${item.item}×${item.quantity}`).join(', ');
  }

  function rowHtml(volunteer) {
    const busy = volunteer.status === 'BUSY';
    const timer = busy ? countdown(volunteer.expected_return) : null;
    const timerClass = timer && timer.overdue ? 'vol-timer overdue' : timer && timer.urgent ? 'vol-timer urgent' : 'vol-timer';

    const actions = busy
      ? `<button class="btn btn-sm btn-complete" data-action="return"
                 data-volunteer="${esc(volunteer.volunteer_id)}">BACK AT BASE</button>`
      : `<button class="btn btn-sm btn-ghost" data-action="toggle-duty"
                 data-volunteer="${esc(volunteer.volunteer_id)}"
                 data-status="${volunteer.status === 'OFF_DUTY' ? 'AVAILABLE' : 'OFF_DUTY'}">
           ${volunteer.status === 'OFF_DUTY' ? 'ON SHIFT' : 'REST'}
         </button>
         <button class="btn btn-sm btn-ghost" data-action="remove"
                 data-volunteer="${esc(volunteer.volunteer_id)}" title="Remove from roster">×</button>`;

    return `
      <div class="vol-row vol-${volunteer.status.toLowerCase()}">
        <div class="vol-identity">
          <span class="vol-dot"></span>
          <span class="vol-id">${esc(volunteer.volunteer_id)}</span>
          ${
            volunteer.name && volunteer.name !== volunteer.volunteer_id
              ? `<span class="vol-name">${esc(volunteer.name)}</span>`
              : ''
          }
        </div>
        <div class="vol-task">
          ${
            busy
              ? `<span class="vol-task-title">${esc(volunteer.request_summary || volunteer.request_id)}</span>
                 <span class="vol-task-meta">${esc(volunteer.request_id)} · out since ${esc(clock(volunteer.assigned_at))}</span>
                 <span class="vol-task-items">${esc(itemsSummary(volunteer.items_taken))}</span>`
              : `<span class="vol-task-meta">${
                  volunteer.status === 'OFF_DUTY' ? 'Resting' : 'Ready for tasking'
                } · ${ARIA.util.pluralise(volunteer.missions_completed || 0, 'mission')} completed</span>`
          }
        </div>
        <div class="vol-right">
          ${
            busy
              ? `<span class="${timerClass}" data-countdown="${esc(volunteer.expected_return || '')}">${esc(timer.text)}</span>`
              : `<span class="vol-status-label">${esc(volunteer.status.replace('_', ' '))}</span>`
          }
          <div class="vol-actions">${actions}</div>
        </div>
      </div>`;
  }

  function render(state) {
    const list = document.getElementById('volunteer-list');
    if (!list) return;

    const volunteers = state.board.volunteers || [];
    list.innerHTML = volunteers.length
      ? volunteers.map(rowHtml).join('')
      : '<div class="empty-hint">No volunteers on the roster. Add one to start dispatching.</div>';

    const summary = document.getElementById('volunteer-summary');
    if (summary) {
      const stats = state.board.metrics.volunteers || {};
      summary.textContent = `${stats.available ?? 0} free · ${stats.busy ?? 0} out${
        stats.overdue ? ` · ${stats.overdue} overdue` : ''
      }`;
    }

    const countInput = document.getElementById('volunteer-count-input');
    if (countInput && document.activeElement !== countInput) {
      countInput.value = volunteers.length;
    }
  }

  function mount() {
    const list = document.getElementById('volunteer-list');

    delegate(list, 'click', '[data-action]', (event, button) => {
      const { action, volunteer, status } = button.dataset;
      if (action === 'return') ARIA.modals.openReturn(null, volunteer);
      if (action === 'toggle-duty') ARIA.actions.setVolunteerStatus(volunteer, status);
      if (action === 'remove') ARIA.actions.removeVolunteer(volunteer);
    });
  }

  return { render, mount };
})();
