-- Displays & Presenting panel. Every change applies live through hypr-display;
-- the systemd controller owns the 20 s rollback, so closing this panel is safe.
-- The panel never keeps its own idea of the layout: it renders `hypr-display
-- status` and only remembers transient UI state (hover, target choice).
local PREVIEW_SECONDS = 20
local snap = { monitors = {} }
local targetName = nil          -- chosen external when several are connected
local hover = nil               -- chip under the pointer, e.g. "layout:extend" or "place:left"
local errorText, noticeText = "", ""
local polling, wantRefresh, busy, opened, loaded = false, false, false, false, false
local render

local SCALES = { 1, 1.25, 1.333333, 1.5, 2 }
local LAYOUTS = {
  { id = "internal", text = "Laptop", glyph = "device-laptop", tip = "Laptop screen only" },
  { id = "extend", text = "Extend", glyph = "arrows-horizontal", tip = "Separate desktops on both screens" },
  { id = "mirror", text = "Mirror", glyph = "copy", tip = "Same picture on both screens" },
  { id = "external", text = "External", glyph = "device-desktop", tip = "External screen only, laptop off" },
}
local PLACES = {
  { id = "left", glyph = "arrow-left", tip = "External on the left" },
  { id = "right", glyph = "arrow-right", tip = "External on the right" },
  { id = "above", glyph = "arrow-up", tip = "External above" },
  { id = "below", glyph = "arrow-down", tip = "External below" },
}
local SOURCES = {
  { id = "internal", text = "Laptop → External", tip = "External shows the laptop screen" },
  { id = "external", text = "External → Laptop", tip = "Laptop shows the external screen at its own resolution" },
}

-- --- helpers ---------------------------------------------------------------

local function quote(value)
  return "'" .. tostring(value):gsub("'", "'\\''") .. "'"
end

local function message(result, fallback)
  local text = noctalia.string.trim(result.stderr or "")
  if result.timedOut then return "Timed out: " .. fallback end
  return text ~= "" and text or fallback
end

local function short(m)
  -- "Dell Inc. AW3423DWF BTW82S3" -> "Dell AW3423DWF"
  local desc = m and m.description or ""
  local make = (m and m.make or ""):gsub("%s+Inc%.?$", ""):gsub("%s+Corp%.?$", ""):gsub("%s+Display$", "")
  local model = noctalia.string.trim(m and m.model or "")
  if make ~= "" and model ~= "" then return make .. " " .. model end
  if desc ~= "" then return desc end
  return m and m.name or "Display"
end

local function isInternal(m)
  if snap.internal then return m.name == snap.internal end
  return m.name:match("^eDP%-") ~= nil or m.name:match("^LVDS%-") ~= nil or m.name:match("^DSI%-") ~= nil
end

local function logicalSize(m)
  local w, h, scale = m.width or 0, m.height or 0, m.scale or 1
  if w == 0 or h == 0 then
    local mode = m.availableModes and m.availableModes[1] or ""
    w, h = mode:match("^(%d+)x(%d+)")
    w, h = tonumber(w) or 1920, tonumber(h) or 1080
  end
  if scale <= 0 then scale = 1 end
  return w / scale, h / scale
end

local function modeLabel(m)
  if not m or (m.width or 0) == 0 then return nil end
  return string.format("%dx%d@%.2f", m.width, m.height, m.refreshRate or 60)
end

local function scaleLabel(value)
  if math.abs(value - math.floor(value + 0.5)) < 0.001 then return tostring(math.floor(value + 0.5)) end
  return (string.format("%.3f", value):gsub("0+$", ""))
end

-- Live monitors, split into the laptop panel, the chosen external and the rest.
local function outputs()
  local internal, externals = nil, {}
  for _, m in ipairs(snap.monitors or {}) do
    if isInternal(m) then internal = m else table.insert(externals, m) end
  end
  local target = nil
  targetName = targetName or snap.target
  for _, m in ipairs(externals) do if m.name == targetName then target = m end end
  if not target then
    for _, m in ipairs(externals) do if not m.disabled then target = m; break end end
    target = target or externals[1]
    targetName = target and target.name or nil
  end
  return internal, target, externals
end

-- --- arrangement: a pair summary for captions -------------------------------
-- { kind = "side"|"stack"|"mirror"|"single", first, second, off = { [name] = true } }

local function liveArrangement(internal, target)
  local off = {}
  for _, m in ipairs(snap.monitors or {}) do if m.disabled then off[m.name] = true end end
  if not internal or not target then
    return { kind = "single", first = internal or target, off = off }
  end
  if off[internal.name] or off[target.name] then
    return { kind = "side", first = internal, second = target, off = off }
  end
  if (internal.mirrorOf or "none") ~= "none" or (target.mirrorOf or "none") ~= "none" then
    local source = (internal.mirrorOf or "none") ~= "none" and target or internal
    local copy = source == target and internal or target
    return { kind = "mirror", first = source, second = copy, off = off }
  end
  local _, ih = logicalSize(internal)
  local tw, th = logicalSize(target)
  local ix, iy, tx, ty = internal.x or 0, internal.y or 0, target.x or 0, target.y or 0
  if tx + tw <= ix + 1 then return { kind = "side", first = target, second = internal, off = off } end
  if ty + th <= iy + 1 then return { kind = "stack", first = target, second = internal, off = off } end
  if ty >= iy + ih - 1 then return { kind = "stack", first = internal, second = target, off = off } end
  return { kind = "side", first = internal, second = target, off = off }
end

-- The shape of what is on screen, independent of how it got there (automatic
-- profile, chip, drag). Chips and options follow this, not the controller mode.
-- { id = "internal"|"extend"|"mirror"|"external"|"custom", place, source }
local function liveShape(internal, target, externals)
  local on = 0
  for _, m in ipairs(snap.monitors or {}) do if not m.disabled then on += 1 end end
  if not internal or #externals == 0 then return { id = on <= 1 and "internal" or "custom" } end
  for _, m in ipairs(externals) do
    if m ~= target and not m.disabled then return { id = "custom" } end
  end
  if internal.disabled and target.disabled then return { id = "custom" } end
  if internal.disabled then return { id = "external" } end
  if target.disabled then return { id = "internal" } end
  local arr = liveArrangement(internal, target)
  if arr.kind == "mirror" then return { id = "mirror", source = isInternal(arr.first) and "internal" or "external" } end
  local place = "right"
  if arr.kind == "stack" then place = isInternal(arr.first) and "below" or "above"
  elseif isInternal(arr.second) then place = "left" end
  return { id = "extend", place = place }
end

local function shapeLabel(shape)
  for _, l in ipairs(LAYOUTS) do if l.id == shape.id then return l.text end end
  return "Custom"
end

-- --- layout map: what the schematic draws -----------------------------------
-- { rows = { { m, ... }, ... }, copies = { [sourceName] = { m, ... } }, off = { m, ... } }
-- Rows are horizontal bands of displays that show their own desktop; mirror
-- copies hang off their source; displays that are off sit in the tray.

local function newMap() return { rows = {}, copies = {}, off = {} } end

local function liveMap()
  local names = {}
  for _, m in ipairs(snap.monitors or {}) do names[tostring(m.id)] = m.name end
  local map, shown = newMap(), {}
  for _, m in ipairs(snap.monitors or {}) do
    local source = m.mirrorOf or "none"
    if m.disabled then table.insert(map.off, m)
    elseif source ~= "none" then
      local key = names[tostring(source)] or tostring(source)
      map.copies[key] = map.copies[key] or {}
      table.insert(map.copies[key], m)
    else table.insert(shown, m) end
  end
  table.sort(shown, function(a, b)
    if (a.y or 0) ~= (b.y or 0) then return (a.y or 0) < (b.y or 0) end
    return (a.x or 0) < (b.x or 0)
  end)
  local row, bottom = nil, -math.huge
  for _, m in ipairs(shown) do
    local _, h = logicalSize(m)
    if not row or (m.y or 0) >= bottom - 1 then row = {}; table.insert(map.rows, row); bottom = (m.y or 0) + h end
    table.insert(row, m)
    bottom = math.max(bottom, (m.y or 0) + h)
  end
  for _, r in ipairs(map.rows) do table.sort(r, function(a, b) return (a.x or 0) < (b.x or 0) end) end
  return map
end

-- What a chip would do. Extra externals keep Hyprland's automatic placement
-- (appended to the right), which is what the controller sends for them.
local function predictedMap(layout, place, source, internal, target, externals)
  if not internal or not target then return nil end
  local map = newMap()
  local others = {}
  for _, m in ipairs(externals) do if m.name ~= target.name then table.insert(others, m) end end
  local function offAll() for _, m in ipairs(others) do table.insert(map.off, m) end end
  if layout == "internal" then
    map.rows = { { internal } }; table.insert(map.off, target); offAll()
  elseif layout == "external" then
    map.rows = { { target } }; table.insert(map.off, internal); offAll()
  elseif layout == "mirror" then
    local src, copy = internal, target
    if source == "external" then src, copy = target, internal end
    map.rows = { { src } }; map.copies[src.name] = { copy }
    for _, m in ipairs(others) do table.insert(map.rows[1], m) end
  else
    if place == "left" then map.rows = { { target, internal } }
    elseif place == "above" then map.rows = { { target }, { internal } }
    elseif place == "below" then map.rows = { { internal }, { target } }
    else map.rows = { { internal, target } } end
    for _, m in ipairs(others) do table.insert(map.rows[1], m) end
  end
  return map
end

local MAP_W, MAP_H, STRIP = 440, 108, 10  -- drawing budget in px; STRIP is a drop strip's thickness

local function mapScale(map)
  local widest, total = 1, 0
  for _, row in ipairs(map.rows) do
    local w, h = 0, 0
    for _, m in ipairs(row) do
      local mw, mh = logicalSize(m)
      w, h = w + mw, math.max(h, mh)
      for _, c in ipairs(map.copies[m.name] or {}) do local cw = logicalSize(c); w += cw end
    end
    widest, total = math.max(widest, w), total + h
  end
  return math.min(MAP_W / widest, MAP_H / math.max(total, 1), 0.08)
end

-- state: "on" | "predicted" | "copy" | "off"
local function tileBody(m, k, state)
  local w, h = logicalSize(m)
  local isOff = state == "off"
  local fills = { on = "primary/0.32", predicted = "primary/0.18", copy = "primary/0.22", off = "surface/0.4" }
  local color = isOff and "on_surface_variant/0.7" or "on_surface"
  local tw, th = math.max(56, math.floor(w * k)), math.max(36, math.floor(h * k))
  if isOff then tw, th = 68, 40 end
  local body = ui.column({
    key = "tile:" .. m.name, width = tw, height = th, fill = fills[state], border = isOff and "outline/0.6" or "primary", borderWidth = 1,
    radius = 6, align = "center", justify = "center", gap = 2, opacity = isOff and 0.7 or 1,
  }, {
    ui.glyph({ name = isInternal(m) and "device-laptop" or "device-desktop", size = isOff and 12 or 14, color = color }),
    ui.label({ text = isInternal(m) and "Laptop" or short(m), fontSize = isOff and 10 or 11, color = color, maxWidth = tw - 8, maxLines = 1 }),
  })
  return body, tw, th
end

-- Drag and drop is declarative: a tile is a drag source, the strips around a
-- tile (and the tile itself, for mirroring) are drop zones. The host only
-- reports the completed drop, so every zone encodes its meaning in `value`.
local function draggable(m, body, drag)
  if not drag then return body end
  return ui.dragSource({
    key = "drag:" .. m.name, dragType = "tile:" .. m.name, payload = m.name, previewAncestor = 0,
    tooltip = "Drag beside another display, onto it to mirror, or into Off",
  }, { body })
end

local function strip(anchor, side, w, h, accepts, drag)
  return ui.dropZone({
    key = "zone:" .. side .. ":" .. anchor, accepts = drag and accepts or {}, value = side .. ":" .. anchor, onDrop = "onDrop",
    width = w, height = h, hitSlop = drag and STRIP or 0, radius = 3, fill = drag and "primary/0.10" or nil,
  })
end

local function slot(m, k, map, drag, accepts, predicted)
  local body, tw, th = tileBody(m, k, predicted and "predicted" or "on")
  local ac = accepts(m.name)
  local kids = { ui.dropZone({ key = "zone:mirror:" .. m.name, accepts = drag and ac or {}, value = "mirror:" .. m.name, onDrop = "onDrop" }, { draggable(m, body, drag) }) }
  for _, c in ipairs(map.copies[m.name] or {}) do
    table.insert(kids, ui.glyph({ name = "arrows-horizontal", size = 16, color = "primary" }))
    table.insert(kids, draggable(c, (tileBody(c, k, "copy")), drag))
  end
  return ui.column({ key = "slot:" .. m.name, gap = 2, align = "center" }, {
    strip(m.name, "above", tw, STRIP, ac, drag),
    ui.row({ gap = 2, align = "center" }, { strip(m.name, "left", STRIP, th, ac, drag), ui.row({ gap = 4, align = "center" }, kids), strip(m.name, "right", STRIP, th, ac, drag) }),
    strip(m.name, "below", tw, STRIP, ac, drag),
  })
end

local function tray(map, k, drag, accepts)
  local kids = {
    ui.glyph({ name = "power", size = 14, color = "on_surface_variant" }),
    ui.label({ text = #map.off > 0 and "Off" or (drag and "Drag tiles to rearrange · drop one here to turn it off" or "Off"), fontSize = 11, color = "on_surface_variant" }),
  }
  for _, m in ipairs(map.off) do table.insert(kids, draggable(m, (tileBody(m, k, "off")), drag)) end
  return ui.dropZone({
    key = "zone:off", accepts = drag and accepts or {}, value = "off", onDrop = "onDrop", direction = "row",
    gap = 8, align = "center", padding = 6, minHeight = 40, radius = 8, fill = "surface/0.35", border = "outline/0.25", borderWidth = 1,
  }, kids)
end

local function schematic(map, caption, predicted, drag)
  -- `on` lists displays that are showing something (own desktop or a copy);
  -- strips and mirror zones also accept tiles from the tray so they can be turned back on.
  local body, on, all = nil, {}, {}
  if not map or (#map.rows == 0 and #map.off == 0) then
    body = ui.label({ text = "No displays reported", color = "on_surface_variant" })
  else
    for _, row in ipairs(map.rows) do
      for _, m in ipairs(row) do
        table.insert(on, "tile:" .. m.name)
        for _, c in ipairs(map.copies[m.name] or {}) do table.insert(on, "tile:" .. c.name) end
      end
    end
    for _, t in ipairs(on) do table.insert(all, t) end
    for _, m in ipairs(map.off) do table.insert(all, "tile:" .. m.name) end
    local function accepts(except)
      local out = {}
      for _, t in ipairs(all) do if t ~= "tile:" .. except then table.insert(out, t) end end
      return out
    end
    local k, rows = mapScale(map), {}
    for _, row in ipairs(map.rows) do
      local kids = {}
      for _, m in ipairs(row) do table.insert(kids, slot(m, k, map, drag, accepts, predicted)) end
      table.insert(rows, ui.row({ gap = 2, align = "end", justify = "center" }, kids))
    end
    body = ui.column({ gap = 0, align = "center" }, rows)
  end
  local kids = { body }
  if #all > 1 then
    table.insert(kids, tray(map, 0.02, drag, #on > 1 and on or {}))
  end
  table.insert(kids, ui.label({ text = caption, fontSize = 11, color = predicted and "primary" or "on_surface_variant", maxLines = 2, maxWidth = 540, textAlign = "center" }))
  -- The panel surface has a fixed height (no auto-size in the host), so this
  -- card grows to take whatever the rows below do not need.
  return ui.column({
    fill = "surface_variant/0.45", radius = 12, padding = 10, gap = 8, align = "center", justify = "center", minHeight = 150, flexGrow = 1,
    border = predicted and "primary/0.5" or "outline/0.25", borderWidth = 1,
  }, kids)
end

local function summary(shape, internal, target)
  local text
  if not target then
    text = internal and "Laptop only · no external display" or "No displays reported"
  elseif shape.id == "mirror" then
    text = shape.source == "internal" and ("Laptop mirrored on " .. short(target)) or (short(target) .. " mirrored on the laptop")
  elseif shape.id == "external" then
    text = short(target) .. " only · laptop off"
  elseif shape.id == "internal" then
    text = "Laptop only · " .. short(target) .. " off"
  elseif shape.id == "extend" then
    local words = { right = " to the right of the laptop", left = " to the left of the laptop", above = " above the laptop", below = " below the laptop" }
    text = short(target) .. (words[shape.place] or words.right)
  else
    local on = 0
    for _, m in ipairs(snap.monitors or {}) do if not m.disabled then on += 1 end end
    text = "Custom arrangement · " .. on .. " displays on"
  end
  if (snap.mode or "automatic") == "automatic" then
    local profile = snap.profile or ""
    local remembered = profile:match("^remembered:(.*)$")
    text = (remembered and ("Remembered · " .. remembered) or ("Auto · " .. (profile ~= "" and profile or "profile"))) .. " · " .. text
  end
  if type(snap.present) == "table" then text ..= " · presenting" end
  return text
end

-- --- commands --------------------------------------------------------------

local function refresh()
  if polling then wantRefresh = true; return end
  polling = true
  local accepted = noctalia.runAsync("hypr-display status", function(result)
    polling = false
    loaded = true
    if result.exitCode == 0 and not result.timedOut then
      local ok, value = pcall(noctalia.json.decode, result.stdout)
      if ok and type(value) == "table" then snap = value; errorText = "" end
    else
      errorText = message(result, "Display controller unavailable")
    end
    if opened then render() end
    if wantRefresh then wantRefresh = false; refresh() end
  end, 5000)
  if not accepted then polling = false end  -- the host refused to start the process; try again on the next tick
end

local function run(cmd, fallback, onSuccess)
  if busy then return end
  busy = true
  errorText, noticeText = "", ""
  if opened then render() end
  local accepted = noctalia.runAsync(cmd, function(result)
    busy = false
    if result.exitCode ~= 0 or result.timedOut then
      errorText = message(result, fallback)
    elseif onSuccess then
      onSuccess(result)
    end
    if opened then render() end
    refresh()
  end, 15000)
  if not accepted then
    busy = false
    errorText = fallback .. " (the shell could not start the command)"
    if opened then render() end
  end
end

-- Options for a layout come from the live state so a change re-applies exactly
-- what is on screen plus the one thing the user touched.
local function layoutCommand(layout, overrides)
  local internal, target, externals = outputs()
  if not target then return nil end
  local shape = liveShape(internal, target, externals)
  local o = overrides or {}
  local mode = o.mode or modeLabel(target) or "preferred"
  local scale = o.scale or scaleLabel(target.scale or 1)
  local cmd = "hypr-display " .. layout .. " --target " .. quote(target.name) .. " --mode " .. quote(mode) .. " --scale " .. quote(scale)
  if layout == "extend" then cmd ..= " --place " .. quote(o.place or shape.place or "right") end
  if layout == "mirror" then cmd ..= " --source " .. quote(o.source or shape.source or "internal") end
  return cmd
end

-- Resolution/scale of the target: re-apply the shape with the new value, or,
-- in a custom arrangement, change just that display in place.
local function tweak(overrides)
  local internal, target, externals = outputs()
  if not target then return end
  local shape = liveShape(internal, target, externals)
  if shape.id == "extend" or shape.id == "mirror" or shape.id == "external" then
    local cmd = layoutCommand(shape.id, overrides)
    if cmd then run(cmd, "Display change failed") end
  else
    local cmd = "hypr-display set " .. quote(target.name)
    if overrides.mode then cmd ..= " --mode " .. quote(overrides.mode) end
    if overrides.scale then cmd ..= " --scale " .. quote(overrides.scale) end
    run(cmd, "Could not change the display")
  end
end

local function applyLayout(layout, overrides)
  if layout == "auto" then
    run("hypr-display auto", "Could not apply the automatic profile")
  elseif layout == "internal" then
    run("hypr-display internal", "Could not switch to the laptop screen")
  else
    local cmd = layoutCommand(layout, overrides)
    if cmd then run(cmd, "Display change failed") end
  end
end

-- --- render ----------------------------------------------------------------

render = function()
  local internal, target, externals = outputs()
  local mode = snap.mode or "automatic"
  local watcher = snap.watcher == true
  local lidClosed = snap.lid == "closed"
  local presenting = type(snap.present) == "table"
  local remaining = snap.remaining or 0
  local kept = mode ~= "automatic" and remaining == 0
  local idle = watcher and not busy
  local hasExternal = target ~= nil
  local shape = liveShape(internal, target, externals)
  local drag = idle and #(snap.monitors or {}) > 1

  -- Hovering a chip previews what it would do; the live layout returns on leave.
  local map, caption, predicted = liveMap(), summary(shape, internal, target), false
  if hover and hasExternal then
    local kind, value = hover:match("^(%w+):(.+)$")
    local layoutId = shape.id
    local place, source = shape.place or "right", shape.source or "internal"
    if kind == "layout" then layoutId = value elseif kind == "place" then layoutId, place = "extend", value elseif kind == "source" then layoutId, source = "mirror", value end
    local blocked = lidClosed and (layoutId == "mirror" or layoutId == "internal")
    if blocked then
      caption = "Open the lid to use this layout"
    elseif layoutId ~= "auto" then
      local guess = predictedMap(layoutId, place, source, internal, target, externals)
      if guess then
        map, predicted = guess, true
        local names = { internal = "Laptop only", extend = "Extend", mirror = "Mirror", external = "External only" }
        caption = "Preview · " .. (names[layoutId] or layoutId) .. (kind == "place" and (" · " .. value) or "") .. (kind == "source" and (" · " .. value .. " is the source") or "")
      end
    else
      caption = "Preview · automatic profile from Nix or a remembered layout"
    end
  elseif remaining > 0 then
    caption = "Previewing · " .. caption
  end

  local rows = {
    ui.row({ gap = 8, align = "center" }, {
      ui.glyph({ name = "device-desktop", size = 20, color = "primary" }),
      ui.label({ text = "Displays", fontSize = 20, fontWeight = "bold", color = "primary" }),
      ui.spacer({ flexGrow = 1 }),
      ui.label({ text = busy and "Applying…" or (not loaded and "Loading…" or (watcher and "" or "Controller offline")), fontSize = 12, color = (busy or not loaded) and "primary" or "error" }),
      -- Auto is an origin, not a shape: back to the Nix profile or remembered layout.
      ui.button({
        key = "layout:auto", text = mode == "automatic" and "Automatic" or "Auto", glyph = "sparkles", glyphSize = 14,
        selected = mode == "automatic", variant = mode == "automatic" and "primary" or "outline", controlSize = "sm", enabled = not busy,
        tooltip = mode == "automatic" and "The automatic profile for these displays is active (Nix profile or remembered layout)" or "Back to the automatic profile for these displays",
        onClick = function() applyLayout("auto") end, onHover = "onChipHover",
      }),
    }),
    schematic(map, caption, predicted, drag and not predicted),
  }

  if #externals > 1 then
    local labels, index = {}, 0
    for i, m in ipairs(externals) do
      table.insert(labels, short(m) .. " (" .. m.name .. ")")
      if m.name == targetName then index = i - 1 end
    end
    table.insert(rows, ui.select({ options = labels, selectedIndex = index, onChange = "onTarget", controlSize = "sm" }))
  end

  -- Layout chips: the selected one is the live shape, however it came about.
  local chips = {}
  for _, l in ipairs(LAYOUTS) do
    local enabled = idle
    local tip = l.tip
    if (l.id == "extend" or l.id == "mirror" or l.id == "external") and not hasExternal then enabled, tip = false, "Connect an external display" end
    if (l.id == "mirror" or l.id == "internal") and lidClosed then enabled, tip = false, "Open the lid first" end
    local selected = shape.id == l.id
    table.insert(chips, ui.button({
      key = "layout:" .. l.id, text = l.text, glyph = l.glyph, glyphSize = 14, selected = selected,
      variant = selected and "primary" or "outline", controlSize = "sm", flexGrow = 1, enabled = enabled, tooltip = tip,
      onClick = function() applyLayout(l.id) end, onHover = "onChipHover",
    }))
  end
  table.insert(rows, ui.row({ gap = 6 }, chips))

  -- Contextual options for the live shape only.
  if shape.id == "extend" and hasExternal then
    local placeChips = { ui.label({ text = "Placement", color = "on_surface_variant", fontSize = 12, width = 84 }) }
    for _, p in ipairs(PLACES) do
      local selected = (shape.place or "right") == p.id
      table.insert(placeChips, ui.button({
        key = "place:" .. p.id, glyph = p.glyph, glyphSize = 14, selected = selected, variant = selected and "primary" or "outline",
        controlSize = "sm", width = 44, enabled = idle, tooltip = p.tip, onClick = function() applyLayout("extend", { place = p.id }) end, onHover = "onChipHover",
      }))
    end
    table.insert(rows, ui.row({ gap = 6, align = "center" }, placeChips))
  elseif shape.id == "mirror" and hasExternal then
    local sourceChips = { ui.label({ text = "Direction", color = "on_surface_variant", fontSize = 12, width = 84 }) }
    for _, s in ipairs(SOURCES) do
      local selected = (shape.source or "internal") == s.id
      table.insert(sourceChips, ui.button({
        key = "source:" .. s.id, text = s.text, selected = selected, variant = selected and "primary" or "outline",
        controlSize = "sm", flexGrow = 1, enabled = idle, tooltip = s.tip, onClick = function() applyLayout("mirror", { source = s.id }) end, onHover = "onChipHover",
      }))
    end
    table.insert(rows, ui.row({ gap = 6, align = "center" }, sourceChips))
  end

  -- Resolution and scale whenever the target shows something, whatever the origin.
  if hasExternal and not target.disabled then
    local modes, modeIndex = {}, -1
    local current = modeLabel(target)
    for i, m in ipairs(target.availableModes or {}) do
      local label = (m:gsub("Hz$", ""))
      table.insert(modes, label)
      if label == current then modeIndex = i - 1 end
    end
    local scales, scaleIndex = {}, -1
    local liveScale = target.scale or 1
    for i, s in ipairs(SCALES) do
      table.insert(scales, scaleLabel(s))
      if math.abs(s - liveScale) < 0.01 then scaleIndex = i - 1 end
    end
    if scaleIndex < 0 then table.insert(scales, 1, scaleLabel(liveScale)); scaleIndex = 0 end
    table.insert(rows, ui.row({ gap = 8, align = "center" }, {
      ui.label({ text = short(target), color = "on_surface_variant", fontSize = 12, width = 84, maxLines = 1 }),
      ui.select({ options = modes, selectedIndex = math.max(modeIndex, 0), onChange = "onMode", controlSize = "sm", flexGrow = 1, enabled = idle, placeholder = "Resolution" }),
      ui.label({ text = "Scale", color = "on_surface_variant", fontSize = 12 }),
      ui.select({ options = scales, selectedIndex = scaleIndex, onChange = "onScale", controlSize = "sm", width = 96, enabled = idle }),
    }))
  end

  if remaining > 0 then
    table.insert(rows, ui.column({ gap = 6, fill = "primary/0.08", radius = 10, padding = 10 }, {
      ui.row({ gap = 8, align = "center" }, {
        ui.glyph({ name = "history", size = 16, color = "primary" }),
        ui.label({ text = "Reverting in " .. remaining .. " s unless you keep it", color = "primary", flexGrow = 1 }),
        ui.button({ text = "Keep", glyph = "check", glyphSize = 14, onClick = "onConfirm", enabled = not busy, variant = "primary", controlSize = "sm", width = 96 }),
        ui.button({ text = "Undo", glyph = "arrow-back-up", glyphSize = 14, onClick = "onRevert", enabled = not busy, variant = "outline", controlSize = "sm", width = 96 }),
      }),
      ui.progress({ progress = remaining / PREVIEW_SECONDS, fill = "primary", track = "primary/0.15", height = 5, radius = 3 }),
    }))
  elseif lidClosed then
    table.insert(rows, ui.label({ text = "Lid closed: Mirror and Laptop are unavailable until you open it.", fontSize = 12, color = "on_surface_variant", maxLines = 1 }))
  end

  -- Messages sit under the action they belong to, above the two cards, so the
  -- fixed-height surface can only ever clip the cards, never the explanation.
  if noticeText ~= "" then
    table.insert(rows, ui.row({ gap = 6, align = "center" }, {
      ui.glyph({ name = "circle-check", size = 14, color = "primary" }),
      ui.label({ text = noticeText, color = "primary", maxLines = 1, maxWidth = 540 }),
    }))
  end
  if errorText ~= "" then
    table.insert(rows, ui.row({ gap = 6, align = "start" }, {
      ui.glyph({ name = "alert-triangle", size = 14, color = "error" }),
      ui.label({ text = errorText, color = "error", maxLines = 2, maxWidth = 540 }),
    }))
  end

  -- Present: one switch for keep-awake + Do Not Disturb + audio on the external.
  local presentDetail
  if presenting then
    local active = {
      ui.glyph({ name = "coffee", size = 13, color = "primary" }), ui.label({ text = "Awake", fontSize = 12 }),
      ui.glyph({ name = "bell-off", size = 13, color = "primary" }), ui.label({ text = "Quiet", fontSize = 12 }),
    }
    if snap.present.sink then
      table.insert(active, ui.glyph({ name = "volume", size = 13, color = "primary" }))
      table.insert(active, ui.label({ text = snap.present.sink_label or snap.present.sink, fontSize = 12, maxLines = 1, maxWidth = 260 }))
    end
    presentDetail = ui.row({ gap = 6, align = "center" }, active)
  else
    presentDetail = ui.label({ text = "Keeps the session awake, silences notifications, sends audio to the external display", fontSize = 12, color = "on_surface_variant", maxLines = 1, maxWidth = 440 })
  end
  table.insert(rows, ui.row({ gap = 10, align = "center", fill = "surface_variant/0.45", radius = 12, padding = 10 }, {
    ui.glyph({ name = "presentation", size = 20, color = presenting and "primary" or "on_surface_variant" }),
    ui.column({ gap = 2, flexGrow = 1 }, {
      ui.label({ text = presenting and "Presenting" or "Present", fontWeight = "bold" }),
      presentDetail,
    }),
    ui.toggle({ checked = presenting, enabled = not busy and (presenting or hasExternal), onChange = "onPresent" }),
  }))

  -- Remember: one switch per display setup, plus the Nix escape hatch.
  -- Checked only while the remembered layout is the live one; a kept manual layout offers to replace it.
  local remembered = snap.remembered == true and mode == "automatic"
  local rememberHint
  if remembered then rememberHint = "Reapplied automatically whenever these displays connect"
  elseif kept then rememberHint = "Reapply this layout automatically for these displays"
  elseif mode ~= "automatic" then rememberHint = "Keep the layout first"
  else rememberHint = "Choose and keep a layout first" end
  table.insert(rows, ui.row({ gap = 10, align = "center", fill = "surface_variant/0.45", radius = 12, padding = 10 }, {
    ui.glyph({ name = remembered and "bookmark-filled" or "bookmark", size = 20, color = remembered and "primary" or "on_surface_variant" }),
    ui.column({ gap = 2, flexGrow = 1 }, {
      ui.label({ text = "Remember for this setup", fontWeight = "bold" }),
      ui.label({ text = rememberHint, fontSize = 12, color = "on_surface_variant", maxLines = 1, maxWidth = 380 }),
    }),
    ui.button({ glyph = "clipboard-copy", glyphSize = 16, variant = "ghost", controlSize = "sm", width = 40, enabled = not busy, tooltip = "Copy a Nix profile snippet for monitors.nix", onClick = "onExport" }),
    ui.toggle({ checked = remembered, enabled = not busy and (remembered or kept), onChange = "onRemember" }),
  }))

  -- flexGrow: fill the host's fixed-height surface so the schematic card can grow.
  panel.render(ui.column({ gap = 8, flexGrow = 1 }, rows))
end

-- --- lifecycle + callbacks --------------------------------------------------

function onOpen() opened = true; hover = nil; panel.setWantsSecondTicks(true); render(); refresh() end
function onClose() opened = false; hover = nil; panel.setWantsSecondTicks(false) end
function update() if opened then refresh() end end

function onChipHover(state, key)
  if state == "true" then hover = key elseif hover == key then hover = nil end
  if opened then render() end
end

function onDrop(payload, value)
  -- payload: the dragged connector; value: "off" or "<left|right|above|below|mirror>:<anchor>"
  local relation, anchor = tostring(value):match("^(%a+):(.+)$")
  if value == "off" then
    run("hypr-display place " .. quote(payload) .. " off", "Could not turn the display off")
  elseif relation and anchor then
    run("hypr-display place " .. quote(payload) .. " " .. quote(relation) .. " " .. quote(anchor), "Could not move the display")
  end
end

function onTarget(index)
  local _, _, externals = outputs()
  local m = externals[tonumber(index) + 1]
  if m then targetName = m.name end
  render()
end

function onMode(index)
  local _, target = outputs()
  local m = target and target.availableModes and target.availableModes[tonumber(index) + 1]
  if m then tweak({ mode = (m:gsub("Hz$", "")) }) end
end
function onScale(index)
  local _, target = outputs()
  local scales = {}
  for _, s in ipairs(SCALES) do table.insert(scales, s) end
  local liveScale = target and target.scale or 1
  local known = false
  for _, s in ipairs(SCALES) do if math.abs(s - liveScale) < 0.01 then known = true end end
  if not known then table.insert(scales, 1, liveScale) end
  local s = scales[tonumber(index) + 1]
  if s then tweak({ scale = scaleLabel(s) }) end
end
function onConfirm() run("hypr-display confirm", "Could not keep the layout", function() noticeText = "Layout kept" end) end
function onRevert() run("hypr-display revert", "Could not undo the layout") end
function onPresent(value)
  if value == "true" then
    local _, target = outputs()
    run("hypr-display present --sink auto" .. (target and (" --target " .. quote(target.name)) or ""), "Presenting failed")
  else
    run("hypr-display done", "Could not stop presenting")
  end
end
function onRemember(value)
  if value == "true" then
    local internal, target, externals = outputs()
    local name = shapeLabel(liveShape(internal, target, externals))
    run("hypr-display remember --name " .. quote(name), "Could not remember the layout", function() noticeText = "Remembered for this display setup" end)
  else
    run("hypr-display forget", "Could not forget the layout", function() noticeText = "Forgotten · automatic profile applied" end)
  end
end
function onExport()
  run("hypr-display export", "Export failed", function(result)
    if noctalia.copyToClipboard(result.stdout, "text/plain") then
      noticeText = "Nix snippet copied · paste into hosts/<host>/monitors.nix"
    else
      errorText = "Could not copy to the clipboard"
    end
  end)
end
