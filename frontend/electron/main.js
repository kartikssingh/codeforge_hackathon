// frontend/electron/main.js
// Electron main process.
//
// Responsibilities:
//   - spawn the FastAPI backend (unless one is already running)
//   - poll /health until it answers, then show the window
//   - show a readable error page instead of a blank window when boot fails
//   - shut the backend down cleanly on quit (SIGTERM, then SIGKILL)
//
// Security: contextIsolation on, nodeIntegration off, no remote content, and
// external links are refused rather than opened.

'use strict';

const { app, BrowserWindow, Menu, shell, dialog } = require('electron');
const path = require('path');
const fs = require('fs');
const http = require('http');
const { spawn } = require('child_process');

// Electron's GPU compositor commonly fails under WSL2/Wayland and renders a
// blank window with no error. Software rendering is fast enough for this UI.
app.disableHardwareAcceleration();
app.commandLine.appendSwitch('disable-gpu');
app.commandLine.appendSwitch('disable-gpu-compositing');
if (process.platform === 'linux') {
  app.commandLine.appendSwitch('no-sandbox');
}

const API_HOST = process.env.ARIA_API_HOST || process.env.DL_API_HOST || '127.0.0.1';
const API_PORT = Number(process.env.ARIA_API_PORT || process.env.DL_API_PORT || 8000);
const PROJECT_ROOT = path.join(__dirname, '..', '..');
const BACKEND_ENTRY = path.join(PROJECT_ROOT, 'backend', 'main.py');

const BOOT_TIMEOUT_MS = 120000; // first run builds the vector index
const HEALTH_INTERVAL_MS = 500;

let mainWindow = null;
let backendProcess = null;
let quitting = false;
const backendLog = [];

/** Prefer a project venv, then an active one, then whatever is on PATH. */
function resolvePython() {
  const isWindows = process.platform === 'win32';
  const binDir = isWindows ? 'Scripts' : 'bin';
  const exe = isWindows ? 'python.exe' : 'python';

  const candidates = [
    path.join(PROJECT_ROOT, 'backend', 'venv', binDir, exe),
    path.join(PROJECT_ROOT, 'venv', binDir, exe),
    path.join(PROJECT_ROOT, '.venv', binDir, exe),
  ];
  if (process.env.VIRTUAL_ENV) {
    candidates.unshift(path.join(process.env.VIRTUAL_ENV, binDir, exe));
  }
  if (process.env.ARIA_PYTHON) {
    candidates.unshift(process.env.ARIA_PYTHON);
  }

  const found = candidates.find((candidate) => fs.existsSync(candidate));
  return found || (isWindows ? 'python' : 'python3');
}

function recordBackendOutput(chunk, stream) {
  const text = chunk.toString();
  process[stream].write(`[backend] ${text}`);
  backendLog.push(text);
  if (backendLog.length > 200) backendLog.shift();
}

function startBackend() {
  if (backendProcess) return backendProcess;

  const python = resolvePython();
  console.log(`[aria] starting backend: ${python} ${BACKEND_ENTRY}`);

  backendProcess = spawn(python, [BACKEND_ENTRY], {
    cwd: PROJECT_ROOT,
    env: {
      ...process.env,
      ARIA_API_HOST: API_HOST,
      ARIA_API_PORT: String(API_PORT),
      PYTHONUNBUFFERED: '1',
    },
    stdio: 'pipe',
  });

  backendProcess.stdout?.on('data', (chunk) => recordBackendOutput(chunk, 'stdout'));
  backendProcess.stderr?.on('data', (chunk) => recordBackendOutput(chunk, 'stderr'));

  backendProcess.on('error', (err) => {
    backendLog.push(`Failed to spawn ${python}: ${err.message}\n`);
  });

  backendProcess.on('exit', (code, signal) => {
    backendProcess = null;
    if (quitting) return;
    console.error(`[aria] backend exited (code=${code}, signal=${signal})`);
    if (mainWindow && !mainWindow.isDestroyed()) {
      dialog.showMessageBox(mainWindow, {
        type: 'error',
        title: 'Backend stopped',
        message: 'The ARIA backend process exited.',
        detail: backendLog.slice(-15).join('') || 'No output was captured.',
        buttons: ['Quit'],
      }).then(() => app.quit());
    } else {
      app.quit();
    }
  });

  return backendProcess;
}

function stopBackend() {
  if (!backendProcess) return;
  const child = backendProcess;
  backendProcess = null;
  // Give the backend a moment to flush its state snapshot before it dies.
  child.kill('SIGTERM');
  setTimeout(() => {
    if (!child.killed) child.kill('SIGKILL');
  }, 4000).unref?.();
}

function checkHealth(timeoutMs = 2000) {
  return new Promise((resolve) => {
    const req = http.get(
      { host: API_HOST, port: API_PORT, path: '/health', timeout: timeoutMs },
      (res) => {
        res.resume();
        resolve(res.statusCode === 200);
      },
    );
    req.on('error', () => resolve(false));
    req.on('timeout', () => {
      req.destroy();
      resolve(false);
    });
  });
}

async function waitForHealth(maxWaitMs, intervalMs = HEALTH_INTERVAL_MS) {
  const deadline = Date.now() + maxWaitMs;
  while (Date.now() < deadline) {
    if (await checkHealth()) return true;
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  return false;
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1500,
    height: 950,
    minWidth: 1120,
    minHeight: 720,
    title: 'ARIA — Autonomous Relief Intelligence Agent',
    backgroundColor: '#0d0d0d',
    show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false, // the preload needs Node's http module
      spellcheck: false,
    },
  });

  mainWindow.once('ready-to-show', () => mainWindow.show());
  mainWindow.loadFile(path.join(__dirname, '..', 'index.html'));
  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  // This app is offline by definition: never navigate or open anything remote.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('http://127.0.0.1') || url.startsWith('http://localhost')) {
      shell.openExternal(url);
    }
    return { action: 'deny' };
  });
  mainWindow.webContents.on('will-navigate', (event) => event.preventDefault());
}

function showBootFailure() {
  const detail = backendLog.slice(-20).join('') || 'The backend produced no output.';
  dialog.showErrorBox(
    'ARIA could not start its backend',
    [
      `No response from http://${API_HOST}:${API_PORT}/health.`,
      '',
      'Common causes:',
      `  • dependencies not installed → pip install -r backend/requirements.txt`,
      `  • port ${API_PORT} already in use → pkill -f "backend/main.py"`,
      '  • no Python found → set ARIA_PYTHON=/path/to/python',
      '',
      'Last backend output:',
      detail,
    ].join('\n'),
  );
}

function buildMenu() {
  const template = [
    {
      label: 'ARIA',
      submenu: [
        { role: 'reload' },
        { role: 'forceReload' },
        { role: 'toggleDevTools' },
        { type: 'separator' },
        { role: 'resetZoom' },
        { role: 'zoomIn' },
        { role: 'zoomOut' },
        { type: 'separator' },
        { role: 'quit' },
      ],
    },
    {
      label: 'Edit',
      submenu: [
        { role: 'undo' },
        { role: 'redo' },
        { type: 'separator' },
        { role: 'cut' },
        { role: 'copy' },
        { role: 'paste' },
        { role: 'selectAll' },
      ],
    },
    {
      label: 'Help',
      submenu: [
        {
          label: 'Backend health',
          click: () => shell.openExternal(`http://${API_HOST}:${API_PORT}/health/detail`),
        },
        {
          label: 'API documentation',
          click: () => shell.openExternal(`http://${API_HOST}:${API_PORT}/docs`),
        },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

async function boot() {
  buildMenu();

  // ARIA_SKIP_BACKEND=1 (npm run ui-only) works on the UI against a backend
  // someone else started, without this process owning its lifecycle.
  if (process.env.ARIA_SKIP_BACKEND === '1') {
    await waitForHealth(10000);
    createWindow();
    return;
  }

  // Attach to an already-running backend (developer restarting the UI alone).
  const alreadyUp = await checkHealth(1000);
  if (!alreadyUp) {
    startBackend();
    const ready = await waitForHealth(BOOT_TIMEOUT_MS);
    if (!ready) {
      showBootFailure();
      app.quit();
      return;
    }
  } else {
    console.log('[aria] reusing the backend already listening on this port');
  }

  createWindow();
}

// One window per machine: a second instance would fight over port 8000.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(boot);
}

app.on('window-all-closed', () => {
  quitting = true;
  stopBackend();
  app.quit();
});

app.on('before-quit', () => {
  quitting = true;
  stopBackend();
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});
