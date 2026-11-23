const { contextBridge } = require("electron");

contextBridge.exposeInMainWorld("arcDesktop", {
  platform: process.platform,
  version: process.versions.electron,
  clientCompatibility: "claude",
  runtimeLabel: "plaude bonnet",
});
