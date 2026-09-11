# Displays & Presenting v2 — design

Date: 2026-09-10. Scope: `tools/display-profiles/displays.py`, `home/shells/plugins/displays/`, `home/desktop/displays.nix`, docs.

## Problems being solved

1. **Mirror silently fails when the source is off.** Hyprland 0.56 applies every `hl.monitor` rule of one `hyprctl eval` in a single pass and only enables outputs after that pass, so a mirror of a currently disabled output resolves to "no mirror". Verified live from external-only mode.
2. **The panel drops clicks.** Commands and the once-per-second status poll share one `busy` flag; ~9% of clicks (including "Keep") are ignored.
3. **The controller polls.** The watcher forks `hyprctl` every second for the whole session; reaction lag is up to 1 s.
4. **Presenting is manual.** Layout, keep-awake, Do Not Disturb and audio are four independent steps, and none are undone automatically.
5. **Recurring rooms need Nix edits.** A kept layout is forgotten on the next unplug.

## Design

### Controller (`hypr-display`, Python, tested)

**Two-phase apply.** `apply(specs)` detects mirror specs whose source is currently disabled. It first evaluates all specs with `mirror=""`, polls `hyprctl -j monitors all` (100 ms, up to 5 s) until each source reports `disabled=false`, then evaluates the full specs. Otherwise one eval. Timeout raises a clear error. The sort by (disabled, mirror) stays but its comment is corrected: order inside one eval does not control application order.

**Placement.** `extend --place right|left|above|below` (default `right`) maps to Hyprland `auto-right|auto-left|auto-up|auto-down` on the target; the internal panel stays at `0x0`.

**Mirror direction.** `mirror --source internal|external` (default `internal`). `external`: target is the source at `0x0` with the chosen mode/scale; internal gets `mirror=<target>`, mode `preferred`. Any mirror with the lid closed is rejected with a message that suggests External only.

**Remembered layouts.** `remember [--name NAME]` stores the kept (non-automatic, no pending deadline) `active` specs in `$XDG_STATE_HOME/display-profiles/learned.json`, keyed by the topology signature (sorted descriptions + lid state). Each spec carries `description`; on apply the connector name is resolved from the description, so `DP-9` becoming `DP-10` still matches. `auto_plan` prefers an exact learned match over Nix profiles and reports the profile name as `remembered:<name>`. `forget` deletes the entry and re-applies auto. `export` prints a Nix attrset for `services.displayProfiles.profiles` built from the current learned entry (or the active layout when none). Nothing in the Nix store is written.

**Present bundle.** `present --layout extend|mirror|external [--target --mode --scale --place --source] [--sink auto|none|NAME]` applies the layout with the normal 20 s preview and additionally: `noctalia msg caffeine-enable`; records `notification-dnd-status` then `notification-dnd-set true`; when `--sink auto`, picks the PipeWire sink whose description mentions HDMI/DisplayPort or the target's description (`pw-dump` JSON), records the current default (`wpctl inspect @DEFAULT_AUDIO_SINK@`), and `wpctl set-default`. State gains `present = {dnd_before, sink_before, sink}`. `done` undoes the bundle and runs `auto`. The bundle is also undone whenever the layout is reverted (revert, preview expiry, topology change, service start). Bundle failures are non-fatal: reported on stderr, layout still applied.

**Status.** Adds `lid` (`open|closed`), `internal` (name or null), `externals` (names), `remembered` (bool for the current topology), `learned` (list of `{name, topology}`), `present` (object or null), `place`/`source` of the active manual layout when known.

**Event-driven watcher.** `watch` connects to `$XDG_RUNTIME_DIR/hypr/$HYPRLAND_INSTANCE_SIGNATURE/.socket2.sock` and `select()`s with a timeout of `min(2 s, time to deadline)`. `monitoradded*`/`monitorremoved*` lines, a lid change (read from `/proc/acpi/button/lid/*/state` each wake, no fork), or an expired deadline trigger `tick()`. Heartbeat is written each wake, so the existing 5 s liveness check in the CLI still holds. If the socket cannot be opened it falls back to the current 1 s polling loop and logs once. Reconnects with backoff on socket errors.

### Panel (`panel.luau`)

- Separate `polling` and `busy` flags. Commands are never dropped; while one runs the action buttons are disabled and a "Working…" line shows.
- Controls: display, mode, scale, placement (Right/Left/Above/Below), mirror direction (Mirror laptop on external / Mirror external on laptop).
- Rows: Extend · Mirror · External only / Laptop only · Automatic / Present · Done (Done only while `present` is set) / Remember · Forget (Forget only when `remembered`) · Export (copies the Nix snippet with `wl-copy` and shows a notice).
- Lid-aware: Mirror and Laptop only are disabled with a hint when `lid == "closed"`.
- Keep awake / DND / HDMI audio toggles stay for manual use.
- Current line shows mode, profile (including `remembered:` names) and a "presenting" marker.

### Nix

`home/desktop/displays.nix` adds `wireplumber`, `pipewire` (pw-dump), `wl-clipboard` and the Noctalia CLI to the wrapper's `runtimeInputs`. Service definition unchanged.

### Testing

Unit tests (unittest, no compositor): two-phase apply (source off → two evals; source on → one eval; timeout), placement mapping, mirror source external, lid rejection, learned match by description with connector rename, learned entry ignored when a description is missing, export snippet shape, present bundle undo on restore, socket2 line parsing and wake decision. Live: build home config, switch, restart the service, then mirror from external-only and docked states, present/done, remember/forget, and confirm rollback.
