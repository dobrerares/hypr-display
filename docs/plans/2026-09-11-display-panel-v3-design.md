# Displays panel v3 — one control per capability, live preview

Date: 2026-09-11. Builds on `2026-09-10-display-presenting-design.md`. Scope: `home/shells/plugins/displays/` (panel, bar, manifest, README), small controller changes in `tools/display-profiles/displays.py`, docs.

## Problem

The v2 panel exposes every capability twice. Extend/Mirror/External/Laptop/Automatic buttons compete with a "Present: layout" select plus a Present button; Keep awake / Do Not Disturb / HDMI audio toggles duplicate the Present bundle; Remember and Forget are two buttons for one bit of state; placement and mirror-direction selects are visible even when they do not apply. Nothing shows what a layout will look like before or after it is applied, and the countdown is a line of text.

## Design

### Model: every change applies live, one Keep confirms

There is no "configure, then press a layout button". Selecting a layout chip or changing an option applies it immediately (the controller already keeps the first `before` snapshot across re-previews, so Undo always returns to the layout from before the editing session). The 20 s preview with Keep/Undo is the only confirmation. State shown in the panel is always the live compositor state from `hypr-display status`; local selections are cleared once the status reflects them.

### Panel layout (620 × 600)

1. **Header**: title, then a one-line summary derived from the live monitors ("Dell AW3423DWF to the right of the laptop · docked").
2. **Schematic**: two tiles (laptop, external) drawn with `ui.column` boxes sized proportionally to logical resolution and arranged per the live positions: side by side, stacked, overlapping for mirror (two tiles joined by a mirror glyph), a dimmed outline tile for a disabled output. Hovering a layout or placement chip redraws the schematic as a prediction with a "preview" caption; leaving restores the live view.
3. **Layout chips** (segmented row, `selected` reflects the live mode): Auto · Laptop · Extend · Mirror · External. Disabled with tooltips when not applicable (no external, lid closed).
4. **Contextual options**, only for the selected layout: placement chips (←→↑↓) under Extend; direction chips under Mirror; a compact resolution/scale row under Extend/Mirror/External. Multiple externals: a display select appears above the chips; otherwise the target is implicit.
5. **Countdown**: while a preview is pending, a progress bar plus **Keep** (primary) and **Undo**; the bar drains over the 20 s.
6. **Present card**: one toggle, "Presenting". On: enables keep-awake, Do Not Disturb, routes audio to the external display; chips show what is active ("Awake", "Quiet", "Audio → <sink>"). Off: undoes those, layout untouched. The bundle is still undone automatically on Undo, expiry, unplug and service restart.
7. **Remember card**: one toggle, "Remember for this display setup" (checked when a learned entry matches; enabled when the current layout is kept or already remembered), and a ghost "Copy Nix" button that uses `noctalia.copyToClipboard`.
8. **Messages**: an error line with an alert glyph; a one-line notice; "Applying…" while a command runs, with chips disabled.

Removed: the manual Keep awake / Do Not Disturb / HDMI audio buttons (the control center has them), the Present layout select, separate Forget and Export rows, `wl-copy` in the export pipeline.

### Bar widget

Renders a glyph that reflects state: `device-desktop` when docked or automatic, `presentation` while presenting, `device-laptop` for laptop only, and shows the remaining seconds while a preview is pending. Polls `hypr-display status` every 5 s, every 1 s while a preview is pending.

### Controller

- `present` no longer requires a layout: without `--layout` it starts the bundle on the current layout; with `--layout` it applies that layout first (existing behaviour).
- `done` stops the bundle and leaves the layout in place (previously it also reset to automatic; `auto` still does that).
- The bundle records `sink_label` (the sink description) so the panel can show a readable name.
- Status is otherwise unchanged; the schematic derives everything from `monitors` (x, y, width, height, scale, disabled, mirrorOf).

### Testing

Unit tests: `present` without layout starts the bundle and keeps mode; `done` keeps the layout; `sink_label` recorded. Live: build, switch, then from the current docked state with the laptop backlight at minimum: Extend with each placement, Mirror both directions, External, Laptop, Auto, Present on/off, Remember/Forget, Copy Nix, Undo and expiry; verify the schematic matches `hyprctl monitors`. Restore the original layout afterwards.
