-- Bar capsule: a glyph that follows the display state, plus the rollback
-- countdown while a layout preview is pending. Click opens the panel.
local snap = {}
local polling = false
noctalia.setUpdateInterval(5000)

local function render()
  local remaining = snap.remaining or 0
  local glyph, color = "device-desktop", nil
  if snap.mode == "internal" then glyph = "device-laptop" end
  if snap.mode == "mirror" then glyph = "copy" end
  if type(snap.present) == "table" then glyph, color = "presentation", "primary" end
  if snap.watcher == false then glyph, color = "device-desktop-off", "error" end
  -- Poll faster only while a countdown is visible.
  noctalia.setUpdateInterval(remaining > 0 and 1000 or 5000)
  local kids = { ui.glyph({ name = glyph, color = color }) }
  if remaining > 0 then
    table.insert(kids, ui.label({ text = remaining .. "s", fontWeight = "bold", color = "primary" }))
  end
  local container = barWidget.isVertical() and ui.column or ui.row
  barWidget.render(container({ gap = 4, align = "center" }, kids))
end

function update()
  if polling then return end
  polling = true
  local accepted = noctalia.runAsync("hypr-display status", function(result)
    polling = false
    if result.exitCode == 0 and not result.timedOut then
      local ok, value = pcall(noctalia.json.decode, result.stdout)
      if ok and type(value) == "table" then snap = value end
    else
      snap = { watcher = false }
    end
    render()
  end, 4000)
  if not accepted then polling = false end
end

function onClick()
  noctalia.togglePanel("rdobre/displays:panel")
end
