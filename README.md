# hypr-display

Automatic, remembered and drag-and-drop display layouts for [Hyprland](https://hyprland.org) 0.56+, with a [Noctalia](https://noctalia.dev) panel. Built for a laptop that moves between a desk, a projector and a lid-closed dock, and for the moment before a talk when the layout has to be right in one click.

- **Automatic profiles** from Nix: which displays are connected plus the lid state select a profile; it is applied on login, on hotplug and on every lid change.
- **Remembered layouts**: keep a layout once for a set of displays and it comes back on its own the next time those displays are connected, even if the connector name changes.
- **Live layouts with a safety net**: every change applies immediately and starts a 20 s countdown. Keep confirms, Undo returns to the layout from before the edit, and a service outside the UI owns the timer, so a crashed shell or a wrong click on a screen you cannot see cannot leave you stranded.
- **A schematic you can drag**: every connected display is a tile drawn to scale. Drag a tile beside another, onto it to mirror, or into the Off tray.
- **Present**: one switch that keeps the session awake, turns on Do Not Disturb and routes audio to the external display. Off puts everything back.

Everything is driven through Hyprland's Lua IPC (`hyprctl eval 'hl.monitor(...)'`). Nothing writes compositor configuration files.

## Contents

- [How it fits together](#how-it-fits-together)
- [Installation](#installation)
- [Configuration reference](#configuration-reference)
- [Automatic profiles](#automatic-profiles)
- [The panel](#the-panel)
- [Remember: the learned layouts](#remember-the-learned-layouts)
- [Present](#present)
- [Rules the controller always enforces](#rules-the-controller-always-enforces)
- [Command line reference](#command-line-reference)
- [Files and state](#files-and-state)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)
- [Development](#development)

## How it fits together

| Part | What it is | Where |
|---|---|---|
| `hypr-display` | The controller. A Python command that plans a layout from the connected outputs and the profiles, applies it through `hyprctl eval`, and keeps the state file. Every panel action is one call to it. | `controller/displays.py`, built by `nix/controller.nix` |
| `display-profiles.service` | A user service running `hypr-display watch`. It listens on Hyprland's event socket for monitor hotplug, watches the lid, re-applies the automatic profile when either changes, and reverts expired previews. | `nix/module.nix` |
| Noctalia panel `rdobre/displays:panel` | The UI: schematic, layout chips, options, countdown, Present, Remember. It never keeps its own idea of the layout; it renders `hypr-display status` and remembers only hover and the chosen display. | `plugin/displays/panel.luau` |
| Noctalia bar widget `rdobre/displays:bar` | A glyph that follows the state and shows the countdown while a preview is pending. Click opens the panel. | `plugin/displays/bar.luau` |

The controller is the only component that decides anything. The panel could disappear and `hypr-display` on the command line would still do the same things.

## Installation

The repository is a flake. It provides `homeManagerModules.default` (the controller, the service, Hyprland hooks and a desktop entry) and `homeManagerModules.noctalia` (registers the plugin with Noctalia).

```nix
# flake.nix
inputs.hypr-display = {
  url = "github:dobrerares/hypr-display";
  inputs.nixpkgs.follows = "nixpkgs";
};
```

```nix
# home-manager configuration
{ inputs, config, ... }: {
  imports = [
    inputs.hypr-display.homeManagerModules.default
    inputs.hypr-display.homeManagerModules.noctalia   # only with Noctalia's own module imported
  ];

  services.displayProfiles = {
    enable = true;
    noctaliaPackage = config.programs.noctalia.package;  # for Present's keep-awake and Do Not Disturb
    profiles = { ... };                                   # see below
  };

  # Put the bar widget somewhere and give the panel a key.
  programs.noctalia.settings.widget.displays.type = "rdobre/displays:bar";
  programs.noctalia.settings.bar.end = [ "displays" "network" "volume" ];  # or wherever you keep widgets
  wayland.windowManager.hyprland.settings.bind = [{
    _args = [ "SUPER + SHIFT + F8"
              (lib.generators.mkLuaInline ''hl.dsp.exec_cmd("noctalia msg panel-toggle rdobre/displays:panel")'')
              { description = "Displays and presenting"; } ];
  }];
}
```

Requirements:

- Hyprland 0.56 or newer with Home Manager's `wayland.windowManager.hyprland.configType = "lua"` if `services.displayProfiles.hyprland.enable` stays on (it adds a Lua `config.reloaded` hook). Set it to `false` if you manage that yourself.
- Noctalia v5 with plugin API level 21 or higher for the panel and bar widget. The controller works without Noctalia.
- PipeWire with WirePlumber for the audio part of Present.

Without the `noctalia` module you can register the plugin by hand: add a plugin source with `kind = "path"` and `location = "${inputs.hypr-display}/plugin"`, then enable `rdobre/displays`.

## Configuration reference

All options live under `services.displayProfiles`.

| Option | Default | Meaning |
|---|---|---|
| `enable` | `false` | Install the command, the service, the Hyprland hooks and the desktop entry. |
| `profiles` | `{}` | Named automatic profiles, see the next section. Baked into the command at build time. |
| `noctaliaPackage` | `null` | Noctalia package for `noctalia msg caffeine-*` and `notification-dnd-*`. With `null`, `noctalia` is taken from PATH if it exists; otherwise those two Present steps are reported and skipped. |
| `hyprlandPackage` | `pkgs.hyprland` | Provides `hyprctl`. Keep it the same version as the running compositor. |
| `package` | built from `profiles` | The command the service runs. Override to change runtime inputs. |
| `desktopEntry` | `true` | A "Displays & Presenting" launcher entry that opens the panel. |
| `hyprland.enable` | `true` | Add the catch-all `monitor` rule and the `config.reloaded` hook to the Lua configuration. |

### Profile schema

```nix
services.displayProfiles.profiles = {
  <name> = {
    conditions = {
      requiredMonitors = [ { description = "<exact description>"; } ... ];  # every entry must be connected
      lidState = "closed";                                                  # optional: "open" or "closed"
    };
    monitors = {
      "<connector>"           = { ... };   # e.g. "eDP-1"
      "desc:<description>"    = { ... };   # matched by description, wins over the connector entry
    };
  };
};
```

Per-monitor settings:

| Key | Type | Meaning |
|---|---|---|
| `disabled` | bool | Turn the output off. All other keys are ignored. |
| `resolution` | `"WxH"` | Mode. Omit for the preferred mode. |
| `refreshRate` | number | Appended to the mode as `@rate`; only with `resolution`. |
| `position` | `"XxY"` or `"auto"` / `"auto-right"` / `"auto-left"` / `"auto-up"` / `"auto-down"` | Position in logical pixels (after scaling). Default `auto`. |
| `scale` | number | Default `1`. |
| `vrr` | bool | Variable refresh rate. Default `false`. |
| `transform` | int | Hyprland transform id. Default `0`. |
| `mirror` | `"<connector>"` or `"desc:<description>"` | Show a copy of that output instead of a desktop. |

Descriptions must match `hyprctl monitors` exactly. Use `hyprctl monitors all -j` to list connectors, descriptions, modes and scales. Prefer `desc:` keys for external displays: connector names such as `DP-9` change between docks and ports, descriptions do not.

## Automatic profiles

The automatic profile is applied at service start, whenever the set of connected outputs changes, whenever the lid opens or closes, and when a preview expires or is undone after a hotplug. Choosing it works like this:

1. If a **remembered layout** exists for this exact set of display descriptions and lid state, it wins. See [Remember](#remember-the-learned-layouts).
2. Otherwise every profile whose `requiredMonitors` are all connected and whose `lidState` (if set) matches is a candidate.
3. The most specific candidate wins: two points per required monitor, one point for a `lidState` condition. Ties are broken by name, so name the more specific profile so it sorts later if you need to force one.
4. If nothing matches, the fallback profile is applied: every output enabled at its preferred mode, automatic placement, scale 1.

Outputs that a profile does not mention are still enabled with those same defaults, so an unknown projector plugged into a docked laptop gets a readable scale-1 desktop rather than nothing.

Two safety rules apply to the result: a profile can never disable every connected output (the first one is forced back on at 0x0), and a `mirror` whose source is missing becomes a normal desktop.

A typical laptop set from `hosts/laptop/monitors.nix` in the author's configuration: `laptop-only` (panel), `laptop-lid-closed` (panel disabled, kept on by the safety rule when alone), `docked` (panel plus the Dell to its right), `docked-lid-closed` (Dell alone). The lid variants score higher than their open counterparts because of the extra condition.

## The panel

Open it from the bar widget, the desktop entry, or your keybinding. Everything in it applies immediately; the countdown is the only confirmation.

### Schematic

The top card draws the live layout from `hyprctl monitors`: each display that shows its own desktop is a tile with a proportional size and position, grouped into horizontal bands. A display that mirrors another hangs off its source with a ⇄ glyph. Displays that are off sit in the **Off** tray under the map. The caption summarises the state ("Automatic · docked", "Dell AW3423DWF to the right of the laptop", "Custom arrangement · 3 displays on", "Previewing · …").

Hovering a layout chip redraws the schematic as a prediction of what that click would do; leaving restores the live view. When a layout is blocked (Mirror or Laptop with the lid closed) the caption says why instead.

### Layout chips

| Chip | Effect |
|---|---|
| **Auto** | Apply the automatic profile (Nix profile or remembered layout). If you are already on it with no preview pending, it simply re-applies. If you are on a temporary layout it previews the automatic profile with the same countdown, so Auto is undoable too. |
| **Laptop** | Laptop panel only, every external off. |
| **Extend** | Both on with separate desktops. The external goes to the placement chosen below (right by default). |
| **Mirror** | Both show the same picture; direction chosen below. |
| **External** | External only, laptop panel off. |

With one external display it is the implicit target. With several, a select appears above the chips; the chips act on the selected display and leave the others to Hyprland's automatic placement (appended to the right). Use dragging to arrange all of them.

### Options

Options appear only for the live layout:

- **Placement** (Extend): left, right, above, below. Positions use Hyprland's `auto-*` placements relative to the laptop.
- **Direction** (Mirror): *Laptop → External* copies the laptop panel onto the external at the external's mode; *External → Laptop* makes the external the source at its own mode and copies it onto the laptop. The second is what you want when the external is the bigger screen.
- **Resolution** and **Scale** (any layout that uses the external): only modes the display advertises are offered; scale from a short list plus the live value.

Changing an option re-applies the layout and restarts the countdown. The first snapshot of an editing session is kept across re-previews, so Undo always returns to the layout from before you started, not to the previous variant.

### Drag to rearrange

Noctalia's plugin drag and drop reports only which zone received a drop, not pointer coordinates, so the editor is snap-to-slot. Each tile is a drag source, and around every tile there are thin strips:

| Drop a tile … | Result |
|---|---|
| on the strip left/right/above/below another tile | It moves to that side of the other display. Whatever was already on that side slides out by the moved display's size, and the hole it left behind closes. Positions are normalised so the top-left display sits at 0x0. |
| onto another tile | It mirrors that display. |
| into the **Off** tray | It turns off. The tray refuses the drop while only one display is on. |
| from the tray onto a strip | It turns on at its preferred mode, in that place. |

Each drop is one `hypr-display place` step with the usual countdown, Keep and Undo. The mode becomes `custom`: no chip is selected, the summary reads "Custom arrangement", and Remember and the Nix snippet work on the result. Copies of a display that is turned off or turned into a mirror are turned off rather than left blank. Dragging is enabled while the controller is idle and more than one display is connected; the chips remain for keyboard use because the shell has no keyboard drag.

### Countdown, Keep, Undo

Every change arms a 20 s rollback in the service before anything is sent to the compositor. The panel shows the remaining seconds with a draining bar and two buttons. **Keep** clears the timer and the layout becomes the kept temporary layout. **Undo** restores the layout from before the editing session. If the panel is closed, the bar widget shows the seconds; if nothing is clicked, the service reverts on its own. The rollback compares the topology recorded at preview time with the current one: if a display was plugged or unplugged in between, it applies the automatic profile instead of a stale snapshot.

A kept temporary layout lasts until an output is connected or disconnected, the lid changes, the service restarts, or a new session starts. Then the automatic profile returns. To make a layout permanent, use Remember or the Nix snippet.

## Remember: the learned layouts

"Remember for this setup" stores the kept layout so it is reapplied automatically whenever the same displays are connected again. This is the "auto remember" feature.

- **Key**: the sorted list of connected display descriptions plus the lid state, for example `Dell Inc. AW3423DWF BTW82S3; Samsung Display Corp. ATNA40CU05-0 [lid open]`. Connector names are not part of the key, so the layout still matches when `DP-9` becomes `DP-10` on another dock or port. When it is applied, each stored spec is mapped onto the current connector with the same description.
- **Precedence**: a remembered layout beats every Nix profile for its key. Nix remains the fallback for setups you have not remembered and for a fresh state directory.
- **What is stored**: the full monitor specs of the kept layout (mode, position, scale, transform, VRR, disabled, mirror by description), the layout mode, placement and direction, and a name. The default name is the mode (`extend`, `mirror`, `custom`); `hypr-display remember --name "Room 4"` gives it a label that the summary then shows as "Remembered layout: Room 4".
- **When it is available**: only after Keep. The switch is disabled during a preview and on an automatic layout; the hint under it says what to do.
- **Forget**: switching it off deletes the entry for the current key and re-applies the automatic profile.
- **Where**: `~/.local/state/display-profiles/learned.json`, plain JSON you can edit or delete.
- **Panel state**: the switch reads on only while the live layout is the remembered one for this key. If you keep a different temporary layout on a remembered setup, the switch turns off and offers to replace the entry.

The clipboard button next to the switch runs `hypr-display export` and copies a Nix snippet for `services.displayProfiles.profiles`, built from the remembered entry if one exists or from the live layout otherwise. Paste it into your configuration when a layout should survive a state reset or move to another machine.

## Present

One switch for the things you do before a talk:

1. Keep the session awake (`noctalia msg caffeine-enable`).
2. Record the current Do Not Disturb state and turn it on.
3. Record the current default audio sink and switch to the external display's audio. With `--sink auto` a sink whose name or description contains a word from the target display's description wins; otherwise the first HDMI or DisplayPort sink. `--sink none` skips audio; a sink name or description selects one explicitly.

The row lists what is active (Awake, Quiet, the sink). Switching it off restores Do Not Disturb and the previous sink and stops keep-awake, leaving the layout alone. The bundle is also undone when the layout is reverted or expires, when a display is unplugged, and when the service restarts. Each step is attempted independently: a missing HDMI sink is printed to stderr but does not stop the layout or the other steps.

From the command line `hypr-display present --layout extend --target HDMI-A-1 --sink auto` applies a layout and the bundle together; without `--layout` it only starts the bundle for the current layout.

## Rules the controller always enforces

- Never disable every connected output. `apply` refuses, and profile planning forces the first output back on.
- Never turn the laptop panel into a mirror, or turn it on from off, while the lid is closed. Use External only instead.
- Mirror sources must be on before the mirror rule is sent. Hyprland applies all `hl.monitor` rules of one `eval` in one pass and a mirror of an output that is off at that moment silently becomes "no mirror", so the controller enables the source first, waits for the compositor to report it on, then sends the mirror rule.
- Only modes advertised by the target display are accepted.
- The rollback is armed and saved before any rule is sent, and a failed apply restores the previous layout immediately.
- After a compositor configuration reload the kept layout is re-applied (`hypr-display reload`), because a reload resets runtime monitor rules to the configuration file's catch-all.
- Hyprland only applies queued monitor rules when a frame renders. While every output is off (DPMS) a change is accepted and applied at the first frame after wake; this is not an error.

## Command line reference

```
hypr-display status                      # JSON: mode, profile, monitors, remaining, watcher, lid, present, remembered…
hypr-display auto                        # automatic profile (reset, or preview with Undo from a temporary layout)
hypr-display internal                    # laptop only
hypr-display extend  --target DP-9 [--mode 3440x1440@59.97] [--scale 1] [--place right|left|above|below]
hypr-display mirror  --target DP-9 [--mode …] [--scale …] [--source internal|external]
hypr-display external --target DP-9 [--mode …] [--scale …]
hypr-display place <display> <left|right|above|below|mirror|off> [anchor]   # one drag step
hypr-display confirm                     # Keep
hypr-display revert                      # Undo
hypr-display present [--layout extend|mirror|external --target …] [--sink auto|none|<name>]
hypr-display done                        # undo the bundle, keep the layout
hypr-display remember [--name "Room 4"]  # store the kept layout for these displays
hypr-display forget                      # delete the entry and re-apply the automatic profile
hypr-display export                      # Nix snippet for services.displayProfiles.profiles
hypr-display reload                      # re-apply the kept layout after a compositor reload
hypr-display watch                       # what the service runs
```

`mode` in `status` is one of `automatic`, `internal`, `extend`, `mirror`, `external`, `custom`. `profile` is the automatic profile name, `remembered:<name>` for a learned layout, or `fallback`. `remaining` is the countdown in seconds. `watcher` is false when the service has not written a heartbeat in the last 5 s; every layout command refuses to run in that case because nothing would revert it.

## Files and state

| Path | Content |
|---|---|
| `$XDG_RUNTIME_DIR/display-profiles-<HYPRLAND_INSTANCE_SIGNATURE>/state.json` | Live state: mode, active specs, `before` snapshot and deadline during a preview, present bundle, topology, heartbeat. Per session; gone after logout. |
| `~/.local/state/display-profiles/learned.json` | Remembered layouts keyed by descriptions and lid state. |
| `<store>/display-profiles.json` | Your Nix profiles as the command sees them. |

## Troubleshooting

- **"Display rollback service is not running"**: `systemctl --user status display-profiles`. Every change needs the service because it owns the timer.
- **Nothing changes but the command says ok**: check `hyprctl -j monitors all` and whether the outputs are DPMS-off or the session is locked. Rules apply on the next rendered frame.
- **Mirror shows two desktops**: the source was off when the mirror rule arrived. The controller handles this for its own layouts; if you script `hyprctl eval` yourself, enable the source first.
- **A profile is not chosen**: compare `requiredMonitors` descriptions with `hyprctl monitors all -j` character by character, and check that a remembered layout is not shadowing it (`hypr-display status` → `remembered: true`, or `profile: "remembered:…"`).
- **The panel shows "Controller offline"**: `hypr-display status` failed or timed out. Run it in a terminal for the error.
- **After a Home Manager switch the layout is wrong for a moment**: the compositor reload applied the catch-all rule; the `config.reloaded` hook re-applies the kept layout on the next frame.

## Limitations

- Snap-to-slot only. The shell's drag and drop has no coordinates, so free positioning and gaps between displays are not expressible from the panel; the Nix snippet plus a `position` edit covers those.
- Laptop-centric chips. Laptop, Mirror and External assume a built-in panel (`eDP-*`, `LVDS-*`, `DSI-*`). On a desktop with two externals use Extend, dragging, and profiles. Dragging works on any set of displays.
- Chip layouts handle one target external; other externals get automatic placement. Dragging arranges all of them.
- No keyboard drag, which is a shell limitation; the chips and the command line cover keyboard use.
- The lid state comes from `/proc/acpi/button/lid`.

## Development

```bash
nix flake check                              # runs the controller tests in a sandbox
python3 -m unittest controller/test_displays # same, locally
noctalia plugins lint plugin/displays        # Noctalia's plugin linter
luau-analyze plugin/displays/panel.luau      # host globals (ui, panel, noctalia) are reported as unknown; ignore those
```

The controller has no dependencies beyond the Python standard library. Tests stub `hyprctl`, `wpctl`, `pw-dump` and `noctalia` through a fake `run`. The panel can be exercised headless: stub `ui.*`, `noctalia.runAsync`, `panel.render` with plain tables, feed `hypr-display status` fixtures, call the global callbacks (`onOpen`, `onChipHover`, `onDrop`, …) and inspect the rendered tree and the commands it emitted. `docs/plans/` holds the design notes for each iteration.

## License

MIT, see `LICENSE`.
