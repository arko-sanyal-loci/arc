const {
  app,
  BrowserView,
  BrowserWindow,
  ipcMain,
  Menu,
  session,
} = require("electron");
const { spawn } = require("child_process");
const path = require("path");

const port = Number(process.env.ARC_PORT || 7860);
const host = process.env.ARC_HOST || "127.0.0.1";
const runtimeUrl = `http://${host}:${port}`;
const mediaUrl = process.env.ARC_MEDIA_URL || "http://127.0.0.1:11500";
const repoRoot = path.resolve(__dirname, "../..");
let runtimeProcess = null;
let browserWindow = null;
let browserView = null;

function isBrowserUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

function normalizeBrowserUrl(value) {
  const trimmed = String(value || "").trim();
  if (!trimmed) {
    return "https://www.google.com";
  }
  if (isBrowserUrl(trimmed)) {
    return trimmed;
  }
  return `https://www.google.com/search?q=${encodeURIComponent(trimmed)}`;
}

function updateBrowserToolbar() {
  if (!browserWindow || browserWindow.isDestroyed() || !browserView) {
    return;
  }
  browserWindow.webContents.send("browser-state", {
    url: browserView.webContents.getURL(),
    title: browserView.webContents.getTitle(),
    canGoBack: browserView.webContents.canGoBack(),
    canGoForward: browserView.webContents.canGoForward(),
  });
}

function createBrowserWindow() {
  if (browserWindow && !browserWindow.isDestroyed()) {
    browserWindow.focus();
    return;
  }

  browserWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 960,
    minHeight: 640,
    title: "Arc Browser",
    backgroundColor: "#111318",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      preload: path.join(__dirname, "browser-preload.js"),
    },
  });

  const browserSession = session.fromPartition("persist:arc-browser");
  browserView = new BrowserView({
    webPreferences: {
      session: browserSession,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  browserWindow.setBrowserView(browserView);

  const resizeBrowserView = () => {
    if (!browserWindow.isDestroyed()) {
      const [width, height] = browserWindow.getContentSize();
      browserView.setBounds({ x: 0, y: 58, width, height: Math.max(0, height - 58) });
      browserView.setAutoResize({ width: true, height: true });
    }
  };
  browserWindow.on("resize", resizeBrowserView);
  browserWindow.on("closed", () => {
    browserWindow = null;
    browserView = null;
  });

  browserView.webContents.on("did-navigate", updateBrowserToolbar);
  browserView.webContents.on("did-navigate-in-page", updateBrowserToolbar);
  browserView.webContents.on("page-title-updated", updateBrowserToolbar);
  browserView.webContents.setWindowOpenHandler(({ url }) => {
    if (isBrowserUrl(url)) {
      browserView.webContents.loadURL(url);
    }
    return { action: "deny" };
  });
  browserView.webContents.on("will-navigate", (event, url) => {
    if (!isBrowserUrl(url)) {
      event.preventDefault();
    }
  });
  browserSession.setPermissionRequestHandler((_webContents, _permission, callback) => {
    callback(false);
  });

  browserWindow.loadFile(path.join(__dirname, "browser.html"));
  browserView.webContents.loadURL("https://www.google.com");
  resizeBrowserView();
}

async function runtimeReady() {
  try {
    const response = await fetch(`${runtimeUrl}/api/status`, {
      signal: AbortSignal.timeout(800),
    });
    return response.ok;
  } catch {
    return false;
  }
}

function runtimeCommand() {
  if (process.env.ARC_PYTHON) {
    return { command: process.env.ARC_PYTHON, args: [] };
  }
  return process.platform === "win32"
    ? { command: "python", args: [] }
    : { command: "python3", args: [] };
}

function startRuntime() {
  if (process.env.ARC_DESKTOP_MANAGE_RUNTIME === "0") {
    return;
  }
  const runtime = runtimeCommand();
  runtimeProcess = spawn(
    runtime.command,
    [...runtime.args, path.join(repoRoot, "start.py"), "--host", host, "--port", String(port)],
    {
      cwd: repoRoot,
      env: { ...process.env, ARC_HOST: host, ARC_PORT: String(port) },
      stdio: "inherit",
      windowsHide: true,
    },
  );
  runtimeProcess.on("error", (error) => {
    console.error(`Arc runtime failed to start: ${error.message}`);
  });
}

async function waitForRuntime(timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await runtimeReady()) {
      return true;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  return false;
}

function createWindow() {
  const window = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 960,
    minHeight: 640,
    backgroundColor: "#0f0f10",
    title: "Arc",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      preload: path.join(__dirname, "preload.js"),
    },
  });

  window.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith(runtimeUrl) || url.startsWith(mediaUrl)) {
      return { action: "allow" };
    }
    return { action: "deny" };
  });
  window.loadURL(runtimeUrl);
}

app.whenReady().then(async () => {
  session.defaultSession.webRequest.onBeforeRequest(
    { urls: ["*://*/*"] },
    (details, callback) => {
      const allowed =
        details.url.startsWith(runtimeUrl) ||
        details.url.startsWith(mediaUrl) ||
        details.url.startsWith("devtools://");
      callback({ cancel: !allowed });
    },
  );

  if (!(await runtimeReady())) {
    startRuntime();
  }
  if (await waitForRuntime()) {
    createWindow();
    const menu = Menu.buildFromTemplate([
      {
        label: "Arc",
        submenu: [
          { label: "Open Chromium Browser", accelerator: "CmdOrCtrl+Shift+B", click: createBrowserWindow },
          { role: "quit" },
        ],
      },
    ]);
    Menu.setApplicationMenu(menu);
  } else {
    app.exit(1);
  }
});

ipcMain.handle("browser-open", (_event, value) => {
  createBrowserWindow();
  if (browserView) {
    browserView.webContents.loadURL(normalizeBrowserUrl(value));
  }
});
ipcMain.handle("browser-navigate", (_event, value) => {
  if (browserView) {
    browserView.webContents.loadURL(normalizeBrowserUrl(value));
  }
});
ipcMain.handle("browser-back", () => browserView?.webContents.goBack());
ipcMain.handle("browser-forward", () => browserView?.webContents.goForward());
ipcMain.handle("browser-reload", () => browserView?.webContents.reload());

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.on("before-quit", () => {
  if (runtimeProcess && !runtimeProcess.killed) {
    runtimeProcess.kill();
  }
});

