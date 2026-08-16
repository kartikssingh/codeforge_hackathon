// frontend/js/toast.js
// Transient notifications.
//
// Errors used to go to console.error only, so a failed approval looked
// identical to a successful one from the operator's chair. Every failure now
// surfaces where the person can see it.
'use strict';

window.ARIA = window.ARIA || {};

ARIA.toast = (function () {
  const DURATIONS = { info: 4000, success: 4000, warn: 7000, error: 10000 };
  let container = null;

  function ensureContainer() {
    if (!container) {
      container = document.getElementById('toast-stack');
    }
    return container;
  }

  function show(message, kind = 'info', { detail = '' } = {}) {
    const stack = ensureContainer();
    if (!stack) {
      console[kind === 'error' ? 'error' : 'log'](`[aria] ${message}`);
      return () => {};
    }

    const node = document.createElement('div');
    node.className = `toast toast-${kind}`;
    node.setAttribute('role', kind === 'error' ? 'alert' : 'status');

    const title = document.createElement('div');
    title.className = 'toast-message';
    title.textContent = message;
    node.appendChild(title);

    if (detail) {
      const sub = document.createElement('div');
      sub.className = 'toast-detail';
      sub.textContent = detail;
      node.appendChild(sub);
    }

    const close = document.createElement('button');
    close.className = 'toast-close';
    close.type = 'button';
    close.setAttribute('aria-label', 'Dismiss');
    close.textContent = '×';
    node.appendChild(close);

    const dismiss = () => {
      node.classList.add('toast-leaving');
      setTimeout(() => node.remove(), 180);
    };
    close.addEventListener('click', dismiss);

    stack.appendChild(node);
    const timer = setTimeout(dismiss, DURATIONS[kind] || 4000);
    return () => {
      clearTimeout(timer);
      dismiss();
    };
  }

  return {
    show,
    info: (message, options) => show(message, 'info', options),
    success: (message, options) => show(message, 'success', options),
    warn: (message, options) => show(message, 'warn', options),
    error: (message, options) => show(message, 'error', options),
  };
})();
