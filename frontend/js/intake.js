// frontend/js/intake.js
// Report intake: an audio file, or typed text.
//
// The text tab is not a fallback bolted on the side — it is how a dispatcher
// logs radio traffic or a runner's message, and it is the path that keeps the
// shelter working on a machine where Whisper was never installed.
'use strict';

window.ARIA = window.ARIA || {};

ARIA.intake = (function () {
  let selectedFile = null;
  let stepTimer = null;

  function config() {
    const settings = ARIA.store.state.settings || {};
    return {
      extensions: settings.audio?.accepted_extensions || ['.wav', '.mp3', '.flac', '.ogg', '.m4a'],
      maxBytes: settings.audio?.max_upload_bytes || 64 * 1024 * 1024,
      text: settings.ui_text?.processing || {
        starting: 'Denoising audio…',
        steps: ['Transcribing speech…', 'Searching protocols…', 'Running triage…'],
        done: 'Triage complete — review and approve.',
      },
    };
  }

  function setStatus(message, kind = '') {
    const status = document.getElementById('process-status');
    if (!status) return;
    status.textContent = message || '';
    status.className = `process-status${kind ? ` ${kind}` : ''}${message ? '' : ' hidden'}`;
  }

  function setBusy(busy, button, label) {
    if (!button) return;
    button.disabled = busy;
    button.textContent = busy ? 'WORKING…' : label;
    const waveform = document.getElementById('waveform');
    if (waveform) waveform.classList.toggle('active', busy);
  }

  /** Walk the status line through the pipeline stages while we wait. */
  function startStepTicker() {
    const { text } = config();
    setStatus(text.starting, 'working');
    let index = 0;
    stopStepTicker();
    stepTimer = setInterval(() => {
      const steps = text.steps || [];
      if (index >= steps.length) return;
      setStatus(steps[index], 'working');
      index += 1;
    }, 2500);
  }

  function stopStepTicker() {
    if (stepTimer) clearInterval(stepTimer);
    stepTimer = null;
  }

  function acceptFile(file) {
    if (!file) return false;
    const { extensions, maxBytes } = config();
    const name = file.name.toLowerCase();
    if (!extensions.some((extension) => name.endsWith(extension))) {
      ARIA.toast.warn(`Unsupported file type. Accepted: ${extensions.join(', ')}`);
      return false;
    }
    if (file.size > maxBytes) {
      ARIA.toast.warn(`That recording is ${Math.round(file.size / 1024 / 1024)} MB; the limit is ${Math.round(maxBytes / 1024 / 1024)} MB.`);
      return false;
    }
    selectedFile = file;
    const label = document.getElementById('upload-label');
    if (label) label.textContent = `${file.name} · ${(file.size / 1024).toFixed(0)} KB`;
    document.getElementById('upload-zone')?.classList.add('has-file');
    return true;
  }

  function clearFile() {
    selectedFile = null;
    const label = document.getElementById('upload-label');
    const settings = ARIA.store.state.settings || {};
    if (label) {
      label.textContent = settings.ui_text?.upload?.drop_hint || 'Click to upload or drag & drop';
    }
    document.getElementById('upload-zone')?.classList.remove('has-file');
    const input = document.getElementById('audio-file');
    if (input) input.value = '';
  }

  function readAsBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(new Error('The file could not be read.'));
      reader.onload = (event) => {
        try {
          resolve(ARIA.util.bufferToBase64(event.target.result));
        } catch (error) {
          reject(error);
        }
      };
      reader.readAsArrayBuffer(file);
    });
  }

  function handleResult(payload) {
    const request = payload.request;
    ARIA.store.setIncoming(request);
    ARIA.app.refreshBoard();

    const timing = payload.timings_ms?.total;
    ARIA.toast.success(
      `${request.request_id}: ${request.severity} — ${request.situations?.[0]?.label || 'assessed'}`,
      timing ? { detail: `Triaged in ${(timing / 1000).toFixed(1)}s` } : undefined,
    );
    if (payload.degraded) {
      ARIA.toast.warn('Triage ran in degraded mode.', {
        detail: (payload.notes || []).join(' · ').slice(0, 300),
      });
    }
  }

  async function submitAudio() {
    const button = document.getElementById('btn-process');
    if (!selectedFile) {
      ARIA.toast.warn('Select an audio file first, or use the TEXT tab.');
      return;
    }

    setBusy(true, button, 'PROCESS REPORT');
    startStepTicker();
    try {
      const encoded = await readAsBase64(selectedFile);
      const payload = await ARIA.api.intakeAudio(encoded, selectedFile.name, ARIA.store.state.npuMode);
      stopStepTicker();
      setStatus(config().text.done, 'ok');
      handleResult(payload);
      clearFile();
    } catch (error) {
      stopStepTicker();
      setStatus('', '');
      const hint = error?.detail?.hint;
      ARIA.toast.error(error?.message || 'The pipeline failed.', hint ? { detail: hint } : undefined);
      if (error?.code === 'agent_unavailable') switchTab('text');
    } finally {
      setBusy(false, button, 'PROCESS REPORT');
    }
  }

  async function submitText() {
    const field = document.getElementById('text-report');
    const button = document.getElementById('btn-process-text');
    const text = (field?.value || '').trim();
    if (text.length < 3) {
      ARIA.toast.warn('Type what was reported before submitting.');
      field?.focus();
      return;
    }

    setBusy(true, button, 'TRIAGE REPORT');
    startStepTicker();
    try {
      const payload = await ARIA.api.intakeText(text, ARIA.store.state.npuMode);
      stopStepTicker();
      setStatus(config().text.done, 'ok');
      handleResult(payload);
      if (field) field.value = '';
    } catch (error) {
      stopStepTicker();
      setStatus('', '');
      ARIA.toast.error(error?.message || 'The pipeline failed.');
    } finally {
      setBusy(false, button, 'TRIAGE REPORT');
    }
  }

  function switchTab(mode) {
    document.querySelectorAll('.intake-tab').forEach((tab) => {
      const active = tab.dataset.mode === mode;
      tab.classList.toggle('active', active);
      tab.setAttribute('aria-selected', String(active));
    });
    document.querySelectorAll('.intake-pane').forEach((pane) => {
      pane.classList.toggle('hidden', pane.dataset.mode !== mode);
    });
    if (mode === 'text') document.getElementById('text-report')?.focus();
  }

  function mount() {
    document.querySelectorAll('.intake-tab').forEach((tab) => {
      tab.addEventListener('click', () => switchTab(tab.dataset.mode));
    });

    const zone = document.getElementById('upload-zone');
    const input = document.getElementById('audio-file');

    if (zone) {
      zone.addEventListener('click', () => input?.click());
      zone.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          input?.click();
        }
      });
      ['dragover', 'dragenter'].forEach((name) =>
        zone.addEventListener(name, (event) => {
          event.preventDefault();
          zone.classList.add('dragging');
        }),
      );
      ['dragleave', 'dragend'].forEach((name) =>
        zone.addEventListener(name, () => zone.classList.remove('dragging')),
      );
      zone.addEventListener('drop', (event) => {
        event.preventDefault();
        zone.classList.remove('dragging');
        acceptFile(event.dataTransfer?.files?.[0]);
      });
    }

    if (input) {
      input.addEventListener('change', (event) => acceptFile(event.target.files?.[0]));
    }

    document.getElementById('btn-process')?.addEventListener('click', submitAudio);
    document.getElementById('btn-process-text')?.addEventListener('click', submitText);

    // Ctrl/Cmd+Enter submits the typed report.
    document.getElementById('text-report')?.addEventListener('keydown', (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
        event.preventDefault();
        submitText();
      }
    });
  }

  function applySettings() {
    const input = document.getElementById('audio-file');
    const { extensions } = config();
    if (input) input.accept = extensions.join(',');
    clearFile();
  }

  return { mount, applySettings, switchTab };
})();
