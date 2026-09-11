# Displays & Presenting

Enable `rdobre/displays`, then add `rdobre/displays:bar` to a Noctalia bar.
Open the panel with `noctalia msg panel-toggle rdobre/displays:panel`
(`Super+Shift+F8` in this configuration).

Requires this repository's `hypr-display` command and `display-profiles` user
service, plus Hyprland's Lua configuration/IPC.

The panel shows the live layout as a schematic: every connected display is a
tile drawn to scale from `hyprctl monitors`, mirror copies hang off their
source with a ⇄ glyph, and displays that are off sit in the **Off** tray.
Hovering a chip previews what it would do. Every change applies immediately
and starts a 20-second countdown: **Keep** confirms, **Undo** returns to the
layout from before the edit; the systemd service owns the timer, so closing
the panel is safe.

**Drag to rearrange.** Drag a tile onto one of the thin strips beside another
tile to put it on that side (neighbours slide out of the way and the gap it
leaves closes), onto the tile itself to mirror it, or into the Off tray to
turn it off. Drag a tile out of the tray onto a strip to turn it back on. Each
drop is one `hypr-display place` step with the same countdown and Undo. The
chips remain for keyboard use; Noctalia has no keyboard drag.

- **Shape chips**: Laptop, Extend, Mirror, External describe what is on
  screen, whatever produced it (Nix profile, chip or drag), so their options
  are always available: placement arrows for Extend, direction for Mirror,
  resolution and scale whenever the external is on. **Auto** in the header
  is the origin: highlighted while the automatic profile is active, otherwise
  the way back to it. With several externals, the
  chips act on the display chosen in the select that appears above them, the
  others keep Hyprland's automatic placement, and dragging arranges them all;
  the summary then reads "Custom arrangement".
- **Present** is one switch: keeps the session awake, enables Do Not Disturb
  and routes audio to the external display (a sink that names it, else the
  first HDMI/DisplayPort sink); the row lists what is active.
  Turning it off undoes those and leaves the layout alone. It is also undone
  when the layout is reverted, expires, or the display is unplugged.
- **Remember for this setup** is one switch: on stores the kept layout for
  this set of displays (reapplied on reconnect, even if the connector name
  changes); off forgets it and re-applies the automatic profile. The clipboard
  button copies a Nix snippet for `hosts/<host>/monitors.nix`.
- The bar glyph follows the state (laptop, mirror, presenting, controller
  offline) and shows the countdown while a preview is pending.

Configuration, recovery, Nix persistence and limitations are documented in
`docs/DESKTOP_WORKFLOW.md` at the repository root.
