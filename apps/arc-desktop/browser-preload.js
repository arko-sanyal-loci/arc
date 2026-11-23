const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("arcBrowser", {
  navigate: (url) => ipcRenderer.invoke("browser-navigate", url),
  back: () => ipcRenderer.invoke("browser-back"),
  forward: () => ipcRenderer.invoke("browser-forward"),
  reload: () => ipcRenderer.invoke("browser-reload"),
  onState: (callback) => {
    ipcRenderer.on("browser-state", (_event, state) => callback(state));
  },
});
