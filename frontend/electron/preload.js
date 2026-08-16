// frontend/electron/preload.js
// The renderer's only bridge to the backend.
//
// Every HTTP call is made here with Node's http module, never from the page:
// the renderer keeps contextIsolation on, no Node access, and a strict CSP with
// no connect-src at all. Nothing in the UI can reach the network directly, and
// there is no CORS surface to widen.
//
// Exposed as window.aria:
//   aria.env                              → { baseUrl, host, port }
//   aria.intakeAudio(b64, filename, npu)  → POST /pipeline
//   aria.intakeText(text, npu)            → POST /pipeline/text
//   aria.getBoard() / getQueue() / getInventory() / getVolunteers()
//   aria.getRequest(id) / getHistory() / getLogs(opts) / getMetrics()
//   aria.approve(id, body) / override(id, body) / cancel(id, reason)
//   aria.volunteerReturn(id, items, note)
//   aria.setVolunteerCount(n) / addVolunteer(name) / removeVolunteer(id)
//   aria.setVolunteerStatus(id, status)
//   aria.addStock(item, qty) / createItem(body) / deleteItem(item) / refill(mode)
//   aria.getSettings() / getHealth()
//   aria.onEvent(handler)                 → live SSE stream, returns unsubscribe

'use strict';

const { contextBridge } = require('electron');
const http = require('http');

const API_HOST = process.env.ARIA_API_HOST || process.env.DL_API_HOST || '127.0.0.1';
const API_PORT = Number(process.env.ARIA_API_PORT || process.env.DL_API_PORT || 8000);
const BASE_URL = `http://${API_HOST}:${API_PORT}`;

const REQUEST_TIMEOUT_MS = 300000; // a cold Whisper + LLM run can take minutes

/**
 * One JSON request. Rejects with an Error carrying { status, code, detail }
 * taken from the backend's error envelope so the UI can show a real message
 * instead of "HTTP 409".
 */
function request(method, path, body = null) {
  return new Promise((resolve, reject) => {
    const payload = body === null ? null : JSON.stringify(body);
    const req = http.request(
      {
        hostname: API_HOST,
        port: API_PORT,
        path,
        method,
        headers: {
          Accept: 'application/json',
          ...(payload
            ? {
                'Content-Type': 'application/json',
                'Content-Length': Buffer.byteLength(payload),
              }
            : {}),
        },
        timeout: REQUEST_TIMEOUT_MS,
      },
      (res) => {
        const chunks = [];
        res.on('data', (chunk) => chunks.push(chunk));
        res.on('end', () => {
          const raw = Buffer.concat(chunks).toString('utf8');
          let parsed = null;
          try {
            parsed = raw ? JSON.parse(raw) : {};
          } catch (err) {
            reject(Object.assign(new Error('The backend sent a malformed response.'), {
              status: res.statusCode,
            }));
            return;
          }
          if (res.statusCode >= 400) {
            const envelope = parsed && parsed.error ? parsed.error : {};
            const detail = parsed && parsed.detail ? parsed.detail : {};
            const message =
              envelope.message ||
              detail.message ||
              (typeof parsed.detail === 'string' ? parsed.detail : null) ||
              `Request failed (HTTP ${res.statusCode})`;
            reject(Object.assign(new Error(message), {
              status: res.statusCode,
              code: envelope.code || detail.code || 'http_error',
              detail: envelope.detail || detail,
            }));
            return;
          }
          resolve(parsed);
        });
      },
    );

    req.on('error', (err) => {
      reject(Object.assign(new Error(`Cannot reach the backend at ${BASE_URL}.`), {
        status: 0,
        code: 'offline',
        cause: err.message,
      }));
    });
    req.on('timeout', () => {
      req.destroy();
      reject(Object.assign(new Error('The backend took too long to answer.'), {
        status: 0,
        code: 'timeout',
      }));
    });

    if (payload) req.write(payload);
    req.end();
  });
}

const encodePath = (value) => encodeURIComponent(String(value));

/**
 * Server-Sent Events subscription with automatic reconnection.
 * The handler is called as handler(eventName, payload); connection state is
 * reported through the synthetic 'connection' event so the UI can show a dot.
 */
function subscribeEvents(handler) {
  let closed = false;
  let activeRequest = null;
  let retryTimer = null;
  let attempt = 0;

  const emit = (type, payload) => {
    try {
      handler(type, payload);
    } catch (err) {
      console.error('[aria] event handler threw', err);
    }
  };

  function scheduleReconnect() {
    if (closed) return;
    attempt += 1;
    const delay = Math.min(30000, 1000 * 2 ** Math.min(attempt, 5));
    retryTimer = setTimeout(connect, delay);
  }

  function connect() {
    if (closed) return;
    activeRequest = http.get(
      {
        hostname: API_HOST,
        port: API_PORT,
        path: '/events',
        headers: { Accept: 'text/event-stream' },
      },
      (res) => {
        if (res.statusCode !== 200) {
          res.resume();
          emit('connection', { connected: false, reason: `HTTP ${res.statusCode}` });
          scheduleReconnect();
          return;
        }
        attempt = 0;
        emit('connection', { connected: true });

        let buffer = '';
        res.setEncoding('utf8');
        res.on('data', (chunk) => {
          buffer += chunk;
          // SSE frames are separated by a blank line.
          let split = buffer.indexOf('\n\n');
          while (split !== -1) {
            parseFrame(buffer.slice(0, split));
            buffer = buffer.slice(split + 2);
            split = buffer.indexOf('\n\n');
          }
        });
        res.on('end', () => {
          emit('connection', { connected: false, reason: 'stream ended' });
          scheduleReconnect();
        });
      },
    );
    activeRequest.on('error', (err) => {
      emit('connection', { connected: false, reason: err.message });
      scheduleReconnect();
    });
  }

  function parseFrame(frame) {
    let eventName = 'message';
    const dataLines = [];
    for (const line of frame.split('\n')) {
      if (line.startsWith('event:')) eventName = line.slice(6).trim();
      else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
    }
    if (!dataLines.length) return;
    try {
      emit(eventName, JSON.parse(dataLines.join('\n')));
    } catch (err) {
      console.warn('[aria] unparseable SSE frame', err);
    }
  }

  connect();

  return () => {
    closed = true;
    if (retryTimer) clearTimeout(retryTimer);
    if (activeRequest) activeRequest.destroy();
  };
}

contextBridge.exposeInMainWorld('aria', {
  env: { baseUrl: BASE_URL, host: API_HOST, port: API_PORT },

  // Intake
  intakeAudio: (audio_b64, filename, npu_mode) =>
    request('POST', '/pipeline', { audio_b64, filename, npu_mode: !!npu_mode }),
  intakeText: (text, npu_mode) =>
    request('POST', '/pipeline/text', { text, npu_mode: !!npu_mode }),

  // Reads
  getBoard: () => request('GET', '/board'),
  getQueue: () => request('GET', '/queue'),
  getRequest: (id) => request('GET', `/requests/${encodePath(id)}`),
  getHistory: (limit = 50) => request('GET', `/requests/history?limit=${limit}`),
  getInventory: () => request('GET', '/inventory'),
  getVolunteers: () => request('GET', '/volunteers'),
  getMetrics: () => request('GET', '/metrics'),
  getLogs: ({ limit = 100, requestId = null } = {}) =>
    request(
      'GET',
      `/logs?limit=${limit}${requestId ? `&request_id=${encodePath(requestId)}` : ''}`,
    ),
  getSettings: () => request('GET', '/settings/frontend'),
  getHealth: () => request('GET', '/health/detail'),

  // Request lifecycle
  approve: (id, body) => request('POST', `/requests/${encodePath(id)}/approve`, body),
  override: (id, body) => request('POST', `/requests/${encodePath(id)}/override`, body),
  cancel: (id, reason) => request('POST', `/requests/${encodePath(id)}/cancel`, { reason }),

  // Volunteers
  volunteerReturn: (id, returned_items, note = '') =>
    request('POST', `/volunteers/${encodePath(id)}/return`, { returned_items, note }),
  setVolunteerCount: (count) => request('POST', '/volunteers/count', { count }),
  addVolunteer: (name = '') => request('POST', '/volunteers', { name }),
  removeVolunteer: (id) => request('DELETE', `/volunteers/${encodePath(id)}`),
  setVolunteerStatus: (id, status) => request('PATCH', `/volunteers/${encodePath(id)}`, { status }),

  // Inventory
  addStock: (item, quantity) =>
    request('POST', `/inventory/${encodePath(item)}/stock`, { quantity }),
  createItem: (body) => request('POST', '/inventory', body),
  deleteItem: (item) => request('DELETE', `/inventory/${encodePath(item)}`),
  refill: (mode) => request('POST', '/inventory/refill', { mode }),

  // Live updates
  onEvent: (handler) => subscribeEvents(handler),
});
