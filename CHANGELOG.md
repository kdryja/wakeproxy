# Changelog

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
