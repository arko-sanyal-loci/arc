const {
  app,
  BrowserView,
  BrowserWindow,
  dialog,
  ipcMain,
  Menu,
  session,
} = require("electron");
const { spawn } = require("child_process");
const { createServer } = require("http");
const path = require("path");

const port = Number(process.env.ARC_PORT || 7860);
const host = process.env.ARC_HOST || "127.0.0.1";
const runtimeUrl = `http://${host}:${port}`;
const mediaUrl = process.env.ARC_MEDIA_URL || "http://127.0.0.1:11500";
const browserControlPort = Number(process.env.ARC_BROWSER_CONTROL_PORT || 7861);
const repoRoot = path.resolve(__dirname, "../..");
let runtimeProcess = null;
let browserWindow = null;
let browserView = null;
let browserControlServer = null;
const blockedBrowserHosts = new Set([
  "porn.com",
  "pornhub.com",
  "xhamster.com",
  "xvideos.com",
  "xnxx.com",
  "redtube.com",
  "spankbang.com",
  "chaturbate.com",
  "stripchat.com",
  "onlyfans.com",
  "rule34.xxx",
  "nhentai.net",
]);

function isBrowserUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:";
  } catch {
    return false;
  }
}

function isBlockedBrowserUrl(value) {
  try {
    const hostname = new URL(value).hostname.toLowerCase().replace(/^www\./, "");
    return [...blockedBrowserHosts].some(
      (blocked) => hostname === blocked || hostname.endsWith(`.${blocked}`),
    );
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

function loadBrowserUrl(value) {
  const url = normalizeBrowserUrl(value);
  if (!browserView || isBlockedBrowserUrl(url)) {
    updateBrowserToolbar({ blocked: true, url });
    return false;
  }
  browserView.webContents.loadURL(url);
  return true;
}

function updateBrowserToolbar(extra = {}) {
  if (!browserWindow || browserWindow.isDestroyed() || !browserView) {
    return;
  }
  browserWindow.webContents.send("browser-state", {
    url: browserView.webContents.getURL(),
    title: browserView.webContents.getTitle(),
    canGoBack: browserView.webContents.canGoBack(),
    canGoForward: browserView.webContents.canGoForward(),
    blocked: Boolean(extra.blocked),
    blockedUrl: extra.url || "",
  });
}

function browserState() {
  return {
    open: Boolean(browserView),
    url: browserView?.webContents.getURL() || "",
    title: browserView?.webContents.getTitle() || "",
  };
}

function sendJson(response, status, value) {
  response.writeHead(status, { "content-type": "application/json" });
  response.end(JSON.stringify(value));
}

function startBrowserControlServer() {
  if (browserControlServer) return;
  browserControlServer = createServer(async (request, response) => {
    if (request.headers.host && !request.headers.host.startsWith("127.0.0.1:")) {
      sendJson(response, 403, { error: "loopback-only" });
      return;
    }

    let body = {};
    if (request.method === "POST") {
      try {
        const chunks = [];
        for await (const chunk of request) chunks.push(chunk);
        body = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
      } catch {
        sendJson(response, 400, { error: "invalid JSON" });
        return;
      }
    }

    try {
      if (request.method === "GET" && request.url === "/state") {
        sendJson(response, 200, browserState());
        return;
      }
      if (request.method !== "POST") {
        sendJson(response, 405, { error: "method not allowed" });
        return;
      }

      if (request.url === "/navigate") {
        createBrowserWindow();
        sendJson(response, loadBrowserUrl(body.url) ? 200 : 403, browserState());
        return;
      }
      if (request.url === "/mouse-move") {
        const { x, y } = body;
        if (!browserView || !Number.isFinite(x) || !Number.isFinite(y)) {
          sendJson(response, 400, { error: "browser or coordinates unavailable" });
          return;
        }
        browserView.webContents.sendInputEvent({ type: "mouseMove", x, y });
        sendJson(response, 200, { ok: true });
        return;
      }
      if (request.url === "/click") {
        const { x, y, button = "left" } = body;
        if (!browserView || !Number.isFinite(x) || !Number.isFinite(y)
          || !["left", "middle", "right"].includes(button)) {
          sendJson(response, 400, { error: "invalid browser click" });
          return;
        }
        browserView.webContents.sendInputEvent({ type: "mouseMove", x, y });
        browserView.webContents.sendInputEvent({ type: "mouseDown", button });
        browserView.webContents.sendInputEvent({ type: "mouseUp", button });
        sendJson(response, 200, { ok: true });
        return;
      }
      if (request.url === "/type") {
        if (!browserView || typeof body.text !== "string" || body.text.length > 20000) {
          sendJson(response, 400, { error: "invalid browser text" });
          return;
        }
        browserView.webContents.insertText(body.text);
        sendJson(response, 200, { ok: true });
        return;
      }
      if (request.url === "/scroll") {
        const { x, y, deltaY } = body;
        if (!browserView || ![x, y, deltaY].every(Number.isFinite)) {
          sendJson(response, 400, { error: "invalid browser scroll" });
          return;
        }
        browserView.webContents.sendInputEvent({ type: "mouseMove", x, y });
        browserView.webContents.sendInputEvent({ type: "mouseWheel", x, y, deltaY });
        sendJson(response, 200, { ok: true });
        return;
      }
      if (request.url === "/sensitive-action") {
        if (typeof body.action !== "string" || body.action.length > 200) {
          sendJson(response, 400, { error: "invalid sensitive action" });
          return;
        }
        const result = await confirmSensitiveAction(body.action);
        sendJson(response, 200, { allowed: result });
        return;
      }
      sendJson(response, 404, { error: "unknown browser route" });
    } catch (error) {
      sendJson(response, 500, { error: error.message });
    }
  });
  browserControlServer.listen(browserControlPort, "127.0.0.1");
}

function confirmSensitiveAction(action) {
  if (!browserWindow || browserWindow.isDestroyed()) return Promise.resolve(false);
  return dialog.showMessageBox(browserWindow, {
    type: "warning",
    buttons: ["Cancel", "Allow once"],
    defaultId: 0,
    cancelId: 0,
    title: "Arc browser confirmation",
    message: "Allow this sensitive browser action?",
    detail: action,
  }).then(({ response }) => response === 1);
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
    if (isBrowserUrl(url) && !isBlockedBrowserUrl(url)) {
      loadBrowserUrl(url);
    }
    return { action: "deny" };
  });
  browserView.webContents.on("will-navigate", (event, url) => {
    if (!isBrowserUrl(url) || isBlockedBrowserUrl(url)) {
      event.preventDefault();
      updateBrowserToolbar({ blocked: true, url });
    }
  });
  browserSession.setPermissionRequestHandler((_webContents, _permission, callback) => {
    callback(false);
  });

  browserWindow.loadFile(path.join(__dirname, "browser.html"));
  loadBrowserUrl("https://www.google.com");
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
  startBrowserControlServer();
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
  return loadBrowserUrl(value);
});
ipcMain.handle("browser-navigate", (_event, value) => {
  return loadBrowserUrl(value);
});
ipcMain.handle("browser-back", () => browserView?.webContents.goBack());
ipcMain.handle("browser-forward", () => browserView?.webContents.goForward());
ipcMain.handle("browser-reload", () => browserView?.webContents.reload());
ipcMain.handle("browser-mouse-move", (_event, x, y) => {
  if (!browserView || !Number.isFinite(x) || !Number.isFinite(y)) return false;
  browserView.webContents.sendInputEvent({ type: "mouseMove", x, y });
  return true;
});
ipcMain.handle("browser-click", (_event, x, y, button = "left") => {
  if (!browserView || !Number.isFinite(x) || !Number.isFinite(y)) return false;
  if (!["left", "middle", "right"].includes(button)) return false;
  browserView.webContents.sendInputEvent({ type: "mouseMove", x, y });
  browserView.webContents.sendInputEvent({ type: "mouseDown", button });
  browserView.webContents.sendInputEvent({ type: "mouseUp", button });
  return true;
});
ipcMain.handle("browser-type", (_event, text) => {
  if (!browserView || typeof text !== "string" || text.length > 20000) return false;
  browserView.webContents.insertText(text);
  return true;
});
ipcMain.handle("browser-scroll", (_event, x, y, deltaY) => {
  if (!browserView || ![x, y, deltaY].every(Number.isFinite)) return false;
  browserView.webContents.sendInputEvent({ type: "mouseMove", x, y });
  browserView.webContents.sendInputEvent({ type: "mouseWheel", x, y, deltaY });
  return true;
});
ipcMain.handle("browser-sensitive-action", async (_event, action) => {
  if (typeof action !== "string" || action.length > 200) return false;
  return confirmSensitiveAction(action);
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.on("before-quit", () => {
  browserControlServer?.close();
  if (runtimeProcess && !runtimeProcess.killed) {
    runtimeProcess.kill();
  }
});

