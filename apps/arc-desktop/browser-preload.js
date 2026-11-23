const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("arcBrowser", {
  navigate: (url) => ipcRenderer.invoke("browser-navigate", url),
  back: () => ipcRenderer.invoke("browser-back"),
  forward: () => ipcRenderer.invoke("browser-forward"),
  reload: () => ipcRenderer.invoke("browser-reload"),
  mouseMove: (x, y) => ipcRenderer.invoke("browser-mouse-move", x, y),
  click: (x, y, button) => ipcRenderer.invoke("browser-click", x, y, button),
  type: (text) => ipcRenderer.invoke("browser-type", text),
  scroll: (x, y, deltaY) => ipcRenderer.invoke("browser-scroll", x, y, deltaY),
  confirmSensitiveAction: (action) =>
    ipcRenderer.invoke("browser-sensitive-action", action),
  onState: (callback) => {
    ipcRenderer.on("browser-state", (_event, state) => callback(state));
  },
});
