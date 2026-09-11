# Displays panel: drag-and-drop arrangement (v3.1)

Date: 2026-09-11. Extends the v3 design (`2026-09-11-display-panel-v3-design.md`).

## Goal

Rearrange displays by dragging tiles in the schematic, and make the panel and
controller honest about more than one external display.

## Constraint that shaped the design

Noctalia's plugin drag and drop (API level 5) is declarative: `ui.dragSource`
carries an opaque payload, `ui.dropZone` carries an opaque value, and the only
event a plugin receives is `onDrop(payload, value)`. There are no pointer
coordinates, so a free-form canvas is impossible. The editor is therefore
snap-to-slot: drop targets that each mean one relation.

## Interaction

- Every tile that shows its own desktop is a drag source and has four thin
  drop strips (left, right, above, below). The tile itself is a drop zone that
  means "mirror this display". An Off tray under the map turns a display off
  and holds the tiles that are off; those can be dragged back onto a strip.
- Zones accept every tile type except their own, so a display cannot be
  dropped beside itself. The tray accepts nothing while only one display is
  on. Copies and their strips accept nothing because they have no geometry.
- Each drop runs `hypr-display place <display> <relation> [anchor]`, which is
  previewed like any layout: 20 s countdown, Keep, Undo. The live mode becomes
  `custom`; no chip is selected and the summary reads "Custom arrangement".
- Dragging is enabled only while the controller is idle and more than one
  display is connected; the strips keep their size when disabled so the map
  does not jump.

## Controller (`place_plan`)

Geometry is computed in Python so it is unit-tested. The moved display's
logical rectangle is removed from the layout and the hole collapses (anything
beyond it in the same band slides back); then for a side relation the tile is
inserted next to the anchor and whatever was on that side is pushed out by the
tile's size; finally the origin is normalised to 0x0. Mirror sets the mirror
source and clears the position; off disables. Copies of a display that stops
showing its own desktop are turned off rather than left blank. A display that
is off comes back at its preferred mode. Errors: unknown display or anchor,
anchor equal to the display, anchor off or a copy, last display turned off,
mirror or turning the laptop on with the lid closed.

## Panel map model

`liveMap()` groups displays that show their own desktop into horizontal bands
by their y ranges, attaches mirror copies to their source, and lists disabled
displays for the tray. `predictedMap()` produces the same shape for chip
hovers; extra externals are appended to the first row because Hyprland's
automatic placement puts them to the right.

## Verified

50 controller tests; headless Luau harness renders three-display, custom
with a mirror copy and laptop-only states and replays drops into the expected
commands; `noctalia plugins lint` and `luau-analyze` clean.
