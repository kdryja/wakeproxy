# Changelog

## 1.3.0

- Answers are streamed back as they arrive instead of after the whole body: token streams
  (`"stream": true` chat completions) reach the client word by word, and large downloads start
  at once. Before, a streamed chat answer arrived all at the end.
- The upstream's headers are passed on (redirects with `Location`, every `Set-Cookie`,
  caching headers), redirects are no longer followed by wakeproxy, and compressed bodies are
  passed on as they are. Before, only the content type survived.

## 1.2.0

- A source can name a `route` in its config: the status page then shows it on that route's line
  ("comfyui · requests · … · 1 running") instead of as a second entry for the same service.

## 1.1.1

- `comfyui` source: shown as "ComfyUI queue", so it isn't mistaken for a second copy of a route
  named comfyui. No error while the machine shuts down, and ComfyUI not answering only counts as
  a problem after `patience_minutes` (default 5), since it starts a while after the machine.

## 1.1.0

- Websockets are passed through the proxy (before, the upgrade was dropped). A route's new
  `work.websockets` setting makes opening one wake the machine and hold it until the connection
  closes.
- New source `comfyui`: running and waiting ComfyUI jobs keep the machine on, and
  `free_after_minutes` unloads ComfyUI's models after the queue has been empty that long.

## 1.0.0

First release.

- Holds: everything that needs the machine keeps it on; it shuts down `idle_minutes` after the
  last hold ends, and only if wakeproxy woke it.
- Reverse proxy with per-route rules for what does work, and answers while the machine is off.
- Plugins: wake (`wol`, `homeassistant`, `http`, `command`), shutdown (`ssh`, `homeassistant`,
  `http`, `command`), up checks (`tcp`, `http`, `ssh`, `command`), sources (`tdarr`,
  `windows_session`), plus your own via `plugin_paths`.
- Hold API for scripts, status page with timer controls and history, one YAML config with
  `${ENV}` substitution, checked at startup.
