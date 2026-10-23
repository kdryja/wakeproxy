# Changelog

## 1.6.0

- A wake that is safe to repeat is sent several times: three times at the start, 2 s apart, then
  again every 30 s until the machine answers. A single lost magic packet used to mean waiting out
  `wake_timeout` and failing the request that asked for the wake; extra packets do nothing to a
  machine that is already booting. This is the default for `wol` and for `homeassistant` sending
  `wake_on_lan.send_magic_packet`; other wakes (a switch, a script, an HTTP call) are still sent
  once. New `machine.wake_packets` and `machine.wake_resend` change it, for any wake.
- Wake plugins say whether they can be repeated with `repeat_safe`.
- History: `wake`, `wake_ok` and `wake_failed` events record how many wakes were sent (`packets`).

## 1.5.2

- Fix: paths and query strings are passed on exactly as the client sent them. They were decoded
  first, so an encoded character could change the request: ComfyUI couldn't open saved
  workflows (it asks for `/api/userdata/workflows%2F<name>`, which became another path and a
  404), and `%26` or `%2B` in a query value turned into a parameter separator or a plus sign.

## 1.5.1

- The widget can be dragged elsewhere when it covers part of the app. The browser remembers the
  spot for each app (and keeps it on the same side when the window is resized); **Reset
  position** in its menu puts it back where `position` says. A click without dragging still
  opens the menu, which now opens toward the middle of the window wherever the badge is.

## 1.5.0

- `browser.widget`: a small badge on a hostname route's pages with the shutdown countdown,
  +30 min / +1 h, a Wake button if the machine went off (the page reloads once it's back), and a
  link to the status page (`status_page.public_url`). Shadow DOM, served from the app's own
  address, so it works under strict content security policies.
- Presence: while the tab is visible and someone used mouse or keyboard in the last 2 minutes,
  the widget holds the machine for `presence_minutes` (default 3). A forgotten tab doesn't.
- Fix (since 1.3.0): a client that sent no `Accept-Encoding` could get a compressed body, because
  the forwarding client asked for gzip on its behalf. The service is now asked for `identity`.

## 1.4.0

- Routes can answer whole hostnames (`hosts`), for web apps with absolute paths: point the
  reverse proxy's router for the hostname at wakeproxy, and the path is forwarded as it is.
- `while_off.wake_page`: while the machine is off, a browser opening the app gets a page with a
  Wake button, the progress (with the usual wake time from the history), and a reload once the
  service answers. Scripts and API calls still get the offline answer.
- Websockets opened through a hostname never count as work (browser tabs keep theirs open).
- Requests that do no work trust the background service check (every 15 s) while it is fresh,
  instead of checking the service before each request.

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
