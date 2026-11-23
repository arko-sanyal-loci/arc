# Arc licensing and attribution

Arc Desktop is an independent implementation. It does not copy Claude
Desktop source code, artwork, proprietary prompts, or bundled assets, and it
is not affiliated with Anthropic. “Claude-compatible” describes the runtime
tool and protocol surface only.

## Components

- Arc application code is released under the MIT license in `LICENSE`.
- Electron distributes Chromium and Node.js components under their respective
  open-source licenses. The packaged application must retain the notices
  supplied by Electron for the selected release.
- Third-party npm packages remain under their own licenses. The lockfile and
  package metadata are the source of truth for the dependency set.
- Provider APIs, model weights, and model outputs are governed by their
  respective provider or model licenses and terms; Arc does not relicense
  them.

Before publishing a package, run `npm ls --omit=dev` and retain the Electron
distribution notices alongside the release artifacts. Do not bundle provider
credentials, private calendar data, or raw media.
