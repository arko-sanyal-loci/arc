# Arc Desktop

Arc is the main runtime; Arc Desktop is its thin, cross-platform shell.
Python, LOCI/Glassbox, the GPU inference services, MCP, and the media monitor
remain independent components that power Arc. The desktop shell presents them
through one Claude-shaped workspace and coordinates startup only when needed.

The shell:

- reuses the existing Arc web UI;
- provides an independent Chromium browser workspace from the `Arc` menu or
  `Ctrl/Cmd+Shift+B`;
- connects to an already-running loopback Python runtime when available;
- starts the existing `start.py` component launcher only when the runtime is not already available;
- blocks navigation away from the local Arc origin;
- uses Electron context isolation, sandboxing, and disabled Node integration.
- keeps browser storage in Electron's persistent `arc-browser` partition;
- allows only HTTP(S) navigation and denies browser permission requests by
  default.
- exposes visible browser-only input primitives (mouse move, click, type, and
  scroll) through the isolated preload bridge;
- blocks configured adult domains and requires a one-shot confirmation for
  sensitive browser actions.

The control surface is intentionally limited to the Arc Chromium window. It
does not move the operating-system cursor or control unrelated desktop
applications.

## Development

```bash
npm install
npm start
```

## Packaging

```bash
npm run dist:linux
npm run dist:mac
npm run dist:win
```

Build on the target operating system for the most reliable native installer.
No provider credentials are bundled into the desktop package.

