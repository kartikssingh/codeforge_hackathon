// frontend/js/view-analysis.js
// The human-in-the-loop panel: review, detail and agent log.
//
// Review mode is the heart of the product. The old UI silently approved
// situation 0 and hid the rest, which made the multi-situation differential
// pointless. Here every hypothesis is shown with its evidence, its citations
// and its supply list; the manager ticks the ones they accept and adjusts the
// quantities, and only then does anything get reserved or dispatched.
'use strict';

window.ARIA = window.ARIA || {};

ARIA.viewAnalysis = (function () {
  const { esc, clock, statusLabel, severityClass, duration, secondsSince } = ARIA.util;

  const ORIGIN_LABEL = {
    llm: 'MODEL',
    rules: 'PROTOCOL RULE',
    'llm+rules': 'MODEL + RULE',
    manual: 'MANAGER',
    fallback: 'FALLBACK',
  };

  // ── Shared fragments ───────────────────────────────────────────────────────

  function instructionsHtml(instructions) {
    if (!instructions || !instructions.length) return '<p class="muted">No steps supplied.</p>';
    return `<ol class="steps-list">${instructions
      .map((step) => `<li class="step-row">${esc(step)}</li>`)
      .join('')}</ol>`;
  }

  function sourcesHtml(sources) {
    if (!sources || !sources.length) return '<p class="muted">No protocol citation.</p>';
    return `<ul class="sources-list">${sources
      .map(
        (source) =>
          `<li class="source-item"><span class="source-name">${esc(source.source)}</span>` +
          `<span class="source-page">${esc(
            source.page && !['?', '—', ''].includes(String(source.page)) ? `p.${source.page}` : '',
          )}</span></li>`,
      )
      .join('')}</ul>`;
  }

  function materialLine(material, { editable, quantity }) {
    const stock = ARIA.store.findInventory(material.matched_item || material.item);
    const available = stock ? stock.available : material.available_qty;
    const total = stock ? stock.total : null;
    const short = quantity > available;
    const missing = !stock && !material.available_qty;

    const stepper = editable
      ? `<div class="qty-stepper">
           <button class="btn btn-qty" type="button" data-step="-1" data-item="${esc(material.item)}" aria-label="One fewer">−</button>
           <input class="qty-input" type="number" min="0" max="99" value="${quantity}"
                  data-item="${esc(material.item)}" aria-label="Quantity of ${esc(material.item)}" />
           <button class="btn btn-qty" type="button" data-step="1" data-item="${esc(material.item)}" aria-label="One more">+</button>
         </div>`
      : `<span class="material-qty-static">×${quantity}</span>`;

    return `
      <div class="material-row${missing ? ' material-missing' : short ? ' material-short' : ''}">
        <span class="material-name">
          ${esc(material.item)}
          ${material.bin && material.bin !== '?' ? `<span class="material-bin">${esc(material.bin)}</span>` : ''}
        </span>
        ${stepper}
        <span class="material-stock${short ? ' critical-text' : ''}">
          ${missing ? 'not stocked' : `${available}${total !== null ? `/${total}` : ''} in stock`}
        </span>
      </div>`;
  }

  function situationCard(situation, index, { editable, selected }) {
    const materials = situation.materials || [];
    const rows = materials
      .map((material) =>
        materialLine(material, {
          editable,
          quantity: editable ? ARIA.store.materialQuantity(material) : material.quantity,
        }),
      )
      .join('');

    const confidence = Math.round((situation.confidence || 0) * 100);
    const origin = ORIGIN_LABEL[situation.origin] || String(situation.origin || '').toUpperCase();

    return `
      <section class="situation-card${selected ? ' situation-selected' : ''} ${severityClass(situation.severity)}"
               data-index="${index}">
        <header class="situation-head">
          <label class="situation-check">
            ${
              editable
                ? `<input type="checkbox" data-situation="${index}" ${selected ? 'checked' : ''}
                          aria-label="Confirm ${esc(situation.label)}" />`
                : ''
            }
            <span class="situation-label">${esc(situation.label)}</span>
          </label>
          <span class="severity-badge ${severityClass(situation.severity)}">${esc(situation.severity)}</span>
        </header>

        <div class="situation-meta">
          <span class="chip">${confidence}% confidence</span>
          <span class="chip">${esc(origin)}</span>
          <span class="chip">${situation.travel_time_min} min travel</span>
          <span class="chip">${situation.resolution_time_min} min on site</span>
        </div>

        ${situation.reasoning ? `<p class="situation-reasoning">${esc(situation.reasoning)}</p>` : ''}

        <div class="situation-section">
          <div class="section-label">STEPS TO TAKE</div>
          ${instructionsHtml(situation.instructions)}
        </div>

        <div class="situation-section">
          <div class="section-label">MATERIALS</div>
          ${rows || '<p class="muted">No supplies required.</p>'}
        </div>

        <div class="situation-section">
          <div class="section-label">SOURCES</div>
          ${sourcesHtml(situation.source_chunks)}
        </div>
      </section>`;
  }

  function notesHtml(notes) {
    if (!notes || !notes.length) return '';
    return `<div class="note-block">${notes
      .map((note) => `<div class="note-row">${esc(note)}</div>`)
      .join('')}</div>`;
  }

  function requestHeader(request, extraChips = '') {
    return `
      <div class="analysis-head">
        <div class="analysis-id">${esc(request.request_id)}</div>
        <div class="analysis-title">${esc(request.summary || (request.situations || [])[0]?.label || 'Emergency request')}</div>
        <div class="analysis-chips">
          <span class="severity-badge ${severityClass(request.severity)}">${esc(request.severity)}</span>
          <span class="chip">${esc(statusLabel(request.status))}</span>
          <span class="chip">${esc(request.intake_mode)} intake</span>
          <span class="chip">${esc(clock(request.created_at))}</span>
          ${request.is_vague ? '<span class="chip chip-warn">VAGUE REPORT</span>' : ''}
          ${request.degraded ? '<span class="chip chip-warn">DEGRADED</span>' : ''}
          ${extraChips}
        </div>
      </div>
      <div class="analysis-section">
        <div class="section-label">REPORT</div>
        <blockquote class="transcript">${esc(request.transcript || '')}</blockquote>
      </div>
      ${notesHtml(request.notes)}`;
  }

  // ── Modes ──────────────────────────────────────────────────────────────────

  function renderReview(state) {
    const request = state.incoming;
    if (!request) {
      return `<div class="empty-hint">
        No report waiting for review.<br />Submit audio or type a report to begin.
      </div>`;
    }
    const situations = request.situations || [];
    return `
      ${requestHeader(request)}
      <div class="analysis-section">
        <div class="section-label">
          DIFFERENTIAL — TICK WHAT YOU ACCEPT (${situations.length})
        </div>
        <p class="hint">Only ticked situations reserve stock and dispatch a volunteer.</p>
        ${situations
          .map((situation, index) =>
            situationCard(situation, index, {
              editable: true,
              selected: state.selectedSituations.has(index),
            }),
          )
          .join('')}
      </div>`;
  }

  function renderDetail(state) {
    const request = ARIA.store.findRequest(state.selectedRequestId);
    if (!request) {
      return '<div class="empty-hint">Select an incident on the board to see its analysis.</div>';
    }

    const chips = [];
    if (request.assigned_volunteer) {
      chips.push(`<span class="chip">${esc(request.assigned_volunteer)}</span>`);
    }
    if (request.escalation_stage) {
      chips.push(`<span class="chip chip-warn">ESCALATED ×${request.escalation_stage}</span>`);
    }

    const chosen = (request.situations || []).filter((situation) => situation.selected);
    const shown = chosen.length ? chosen : request.situations || [];

    const timeline = (request.handoff_logs || [])
      .map(
        (entry) => `
        <li class="handoff-entry">
          <div class="handoff-dot"></div>
          <div class="handoff-content">
            <div class="handoff-agent">${esc(entry.from_agent || '')} → ${esc(entry.to_agent || '')}</div>
            <div class="handoff-note">${esc(entry.reason || entry.step || '')}</div>
            <div class="handoff-time">${esc(clock(entry.at))}${
              entry.duration_ms ? ` · ${entry.duration_ms} ms` : ''
            }</div>
          </div>
        </li>`,
      )
      .join('');

    const taken = request.items_taken || [];

    return `
      ${requestHeader(request, chips.join(''))}

      <div class="analysis-section">
        <div class="section-label">TIMING</div>
        <div class="kv-grid">
          <span>Reported</span><span>${esc(clock(request.created_at))}</span>
          <span>Approved</span><span>${esc(clock(request.approved_at))}</span>
          <span>Dispatched</span><span>${esc(clock(request.assigned_at))}</span>
          <span>Expected back</span><span>${esc(clock(request.expected_return))}</span>
          ${
            request.actual_return
              ? `<span>Returned</span><span>${esc(clock(request.actual_return))}</span>`
              : `<span>Open for</span><span>${esc(duration(secondsSince(request.created_at)))}</span>`
          }
        </div>
      </div>

      ${
        taken.length
          ? `<div class="analysis-section">
               <div class="section-label">SIGNED OUT</div>
               <div class="chip-row">${taken
                 .map((item) => `<span class="chip">${esc(item.item)} ×${item.quantity}</span>`)
                 .join('')}</div>
             </div>`
          : ''
      }

      <div class="analysis-section">
        <div class="section-label">${chosen.length ? 'CONFIRMED SITUATIONS' : 'SITUATIONS'}</div>
        ${shown
          .map((situation, index) => situationCard(situation, index, { editable: false, selected: situation.selected }))
          .join('')}
      </div>

      <div class="analysis-section">
        <div class="section-label">AGENT HANDOFF</div>
        <ul class="handoff-timeline">${timeline || '<li class="muted">No handoffs recorded.</li>'}</ul>
      </div>`;
  }

  function renderLog(state) {
    if (!state.logs.length) {
      return '<div class="empty-hint">No agent activity recorded yet.</div>';
    }
    return `
      <div class="analysis-section">
        <div class="section-label">AGENT ACTIVITY — NEWEST FIRST</div>
        <ul class="log-list">
          ${state.logs
            .map(
              (entry) => `
            <li class="log-entry">
              <span class="log-time">${esc(clock(entry.at))}</span>
              <span class="log-agents">${esc(entry.from_agent)} → ${esc(entry.to_agent)}</span>
              <span class="log-reason">${esc(entry.reason || '')}</span>
              ${entry.request_id ? `<span class="log-req">${esc(entry.request_id)}</span>` : ''}
            </li>`,
            )
            .join('')}
        </ul>
      </div>`;
  }

  // ── Entry point ────────────────────────────────────────────────────────────

  function render(state) {
    const content = document.getElementById('analysis-content');
    if (!content) return;

    document.querySelectorAll('.analysis-tab').forEach((tab) => {
      const active = tab.dataset.mode === state.analysisMode;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', String(active));
      if (tab.dataset.mode === 'review') {
        tab.classList.toggle('tab-attention', !!state.incoming);
      }
    });

    if (state.analysisMode === 'review') content.innerHTML = renderReview(state);
    else if (state.analysisMode === 'detail') content.innerHTML = renderDetail(state);
    else content.innerHTML = renderLog(state);

    const actions = document.getElementById('analysis-actions');
    if (actions) {
      const showApproval = state.analysisMode === 'review' && !!state.incoming;
      actions.classList.toggle('hidden', !showApproval);
      const approve = document.getElementById('btn-approve');
      if (approve) {
        const count = state.selectedSituations.size;
        approve.disabled = count === 0 || state.busy;
        approve.textContent = count > 1 ? `APPROVE ${count} SITUATIONS` : 'APPROVE & DISPATCH';
      }
    }
  }

  function mount() {
    document.querySelectorAll('.analysis-tab').forEach((tab) => {
      tab.addEventListener('click', () => ARIA.store.setAnalysisMode(tab.dataset.mode));
    });

    const content = document.getElementById('analysis-content');

    ARIA.util.delegate(content, 'change', 'input[data-situation]', (_event, input) => {
      ARIA.store.toggleSituation(Number(input.dataset.situation));
    });

    ARIA.util.delegate(content, 'click', '.btn-qty', (_event, button) => {
      const item = button.dataset.item;
      const input = content.querySelector(`.qty-input[data-item="${CSS.escape(item)}"]`);
      const current = input ? Number(input.value) || 0 : 0;
      ARIA.store.setMaterialQuantity(item, ARIA.util.clamp(current + Number(button.dataset.step), 0, 99));
    });

    ARIA.util.delegate(content, 'change', '.qty-input', (_event, input) => {
      ARIA.store.setMaterialQuantity(input.dataset.item, ARIA.util.clamp(Number(input.value) || 0, 0, 99));
    });
  }

  return { render, mount };
})();
