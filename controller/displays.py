"""Hyprland Lua display profiles, with serialized previews and timed rollback.

Only runtime state is writable. Profiles come from the Nix store; remembered
layouts live under $XDG_STATE_HOME. The watcher owns hotplug/lid recovery and
rollback, independently of Noctalia's lifetime.
"""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import select
import socket
import subprocess
import sys
import time

POSITIONS = {'right': 'auto-right', 'left': 'auto-left', 'above': 'auto-up', 'below': 'auto-down'}
SOURCES = ('internal', 'external')
LAYOUTS = ('extend', 'mirror', 'external')
RELATIONS = ('left', 'right', 'above', 'below', 'mirror', 'off')
MONITOR_EVENTS = ('monitoradded', 'monitoraddedv2', 'monitorremoved', 'monitorremovedv2')
PREVIEW_SECONDS = 20


def run(*args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=8)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or str(args))
    return result.stdout.strip()


def monitors():
    return json.loads(run('hyprctl', '-j', 'monitors', 'all'))


def lua(value):
    if isinstance(value, dict):
        return '{' + ','.join(f'[{lua(k)}]={lua(v)}' for k, v in value.items()) + '}'
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


def snapshot(outputs):
    names = {str(m.get("id")): m["name"] for m in outputs}
    return [dict(output=m['name'], disabled=m.get('disabled', False),
                 mode=f"{m['width']}x{m['height']}@{m['refreshRate']:.5f}",
                 position=f"{m['x']}x{m['y']}", scale=m['scale'],
                 transform=m.get('transform', 0), vrr=m.get('vrr', False),
                 mirror='' if m.get('mirrorOf', 'none') == 'none' else names.get(str(m['mirrorOf']), m['mirrorOf']))
            for m in outputs]


def panel(outputs):
    return next((m for m in outputs if m['name'].startswith(('eDP-', 'LVDS-', 'DSI-'))), None)


def lid_closed():
    return any('closed' in p.read_text() for p in Path('/proc/acpi/button/lid').glob('*/state'))


def topology(outputs):
    # JSON-shaped (lists, not tuples) so it compares equal to what read_state() returns.
    return [sorted([m['name'], m.get('description', '')] for m in outputs), lid_closed()]


def by_description(outputs, description):
    return next((m for m in outputs if description and m.get('description') == description), None)


def auto_plan(outputs, profiles, closed=False, learned=None):
    entry = (learned or {}).get(topology_key(outputs, closed))
    specs = resolve_learned(entry, outputs) if entry else None
    if specs:
        return 'remembered:' + entry.get('name', ''), specs
    matches = []
    for name, p in profiles.items():
        c = p.get('conditions', {})
        required = c.get('requiredMonitors', [])
        if not all(any(all(m.get(k) == v for k, v in r.items()) for m in outputs) for r in required):
            continue
        if c.get('lidState') and (c['lidState'] == 'closed') != closed:
            continue
        matches.append((len(required) * 2 + bool(c.get('lidState')), name, p))
    _, name, profile = max(matches, default=(0, 'fallback', {}), key=lambda x: (x[0], x[1]))
    result = []
    for m in outputs:
        settings = profile.get('monitors', {}).get(m['name'], {})
        settings = profile.get('monitors', {}).get('desc:' + m.get('description', ''), settings)
        spec = dict(output=m['name'], mode='preferred', position='auto', scale=1,
                    disabled=False, mirror='', transform=0, vrr=False)
        if settings.get('disabled'):
            spec['disabled'] = True
        else:
            spec.update({k: settings[k] for k in ['position', 'scale', 'transform', 'vrr'] if k in settings})
            if settings.get('resolution'):
                spec['mode'] = settings['resolution'] + (f"@{settings['refreshRate']}" if settings.get('refreshRate') else '')
            mirror = settings.get('mirror', '')
            if mirror.startswith('desc:'):
                source = by_description(outputs, mirror[5:])
                mirror = source['name'] if source else ''
            spec['mirror'] = mirror
        result.append(spec)
    # Closing the lid without an external display must not disable every output.
    if result and all(s['disabled'] for s in result):
        result[0].update(disabled=False, mode='preferred', position='0x0')
    return name, result


def manual_plan(outputs, mode, target=None, resolution='preferred', scale=1.0, place='right', source='internal'):
    if place not in POSITIONS:
        raise ValueError('Placement must be one of: ' + ', '.join(POSITIONS) + '.')
    if source not in SOURCES:
        raise ValueError('Mirror source must be internal or external.')
    internal = panel(outputs)
    externals = [m for m in outputs if m != internal]
    if mode in LAYOUTS:
        if not externals:
            raise ValueError('Connect an external display first.')
        target = next((m for m in externals if m['name'] == target), None) if target else externals[0]
        if not target:
            raise ValueError('The selected display is no longer connected.')
    else:
        target = None
    if mode in ('mirror', 'internal') and not internal:
        raise ValueError('No laptop panel was found.')
    if mode == 'mirror' and lid_closed():
        raise ValueError('Open the laptop lid before mirroring, or use External only.')
    result = snapshot(outputs)
    for s in result:
        s['mirror'] = ''
        is_internal = bool(internal) and s['output'] == internal['name']
        is_target = bool(target) and s['output'] == target['name']
        if mode == 'internal':
            s['disabled'] = not is_internal
        elif mode == 'external':
            s['disabled'] = not is_target
        else:
            s['disabled'] = False
            s['position'] = 'auto'
        if is_internal and mode != 'external':
            s.update(position='0x0', mode='preferred')
            if mode == 'mirror' and source == 'external':
                s.update(mirror=target['name'], position='auto')
        if is_target:
            s.update(mode=resolution, scale=scale, vrr=False, transform=0)
            if mode == 'extend':
                s['position'] = POSITIONS[place]
            elif mode == 'mirror' and source == 'internal':
                s.update(mirror=internal['name'], position='auto')
            else:  # external only, or the external is the mirror source
                s['position'] = '0x0'
    return result


# --- drag-and-drop arrangement ----------------------------------------------

def logical_rect(m):
    """Scaled rectangle of a monitor; a display that is off uses its preferred mode."""
    w, h = m.get('width') or 0, m.get('height') or 0
    if not w or not h:
        first = (m.get('availableModes') or ['1920x1080'])[0].split('@')[0]
        w, h = (int(v) for v in first.split('x'))
    scale = m.get('scale') or 1
    return dict(x=m.get('x', 0), y=m.get('y', 0), w=round(w / scale), h=round(h / scale))


def overlaps(a, b, axis):
    """Whether two rectangles share a band along the given axis ('x' or 'y')."""
    size = 'w' if axis == 'x' else 'h'
    return a[axis] < b[axis] + b[size] and b[axis] < a[axis] + a[size]


def collapse(rects, gone):
    """Close the hole a rectangle leaves behind: neighbours beyond it slide back."""
    for r in rects.values():
        if r['x'] >= gone['x'] + gone['w'] and overlaps(r, gone, 'y'):
            r['x'] -= gone['w']
        elif r['y'] >= gone['y'] + gone['h'] and overlaps(r, gone, 'x'):
            r['y'] -= gone['h']


def insert(rects, moved, side, a):
    """Put `moved` on one side of anchor `a`, pushing whatever was there further out."""
    if side == 'right':
        for r in rects.values():
            if r['x'] >= a['x'] + a['w'] and overlaps(r, a, 'y'):
                r['x'] += moved['w']
        moved.update(x=a['x'] + a['w'], y=a['y'])
    elif side == 'left':
        for r in rects.values():
            if r['x'] + r['w'] <= a['x'] and overlaps(r, a, 'y'):
                r['x'] -= moved['w']
        moved.update(x=a['x'] - moved['w'], y=a['y'])
    elif side == 'below':
        for r in rects.values():
            if r['y'] >= a['y'] + a['h'] and overlaps(r, a, 'x'):
                r['y'] += moved['h']
        moved.update(x=a['x'], y=a['y'] + a['h'])
    else:  # above
        for r in rects.values():
            if r['y'] + r['h'] <= a['y'] and overlaps(r, a, 'x'):
                r['y'] -= moved['h']
        moved.update(x=a['x'], y=a['y'] - moved['h'])


def place_plan(outputs, name, relation, anchor=None):
    """One drag-and-drop step: move `name` beside `anchor`, onto it (mirror) or off, keeping everything else."""
    if relation not in RELATIONS:
        raise ValueError('Relation must be one of: ' + ', '.join(RELATIONS) + '.')
    specs = {s['output']: s for s in snapshot(outputs)}
    by_name = {m['name']: m for m in outputs}
    if name not in specs:
        raise ValueError(f'{name} is not connected.')
    moved = specs[name]
    internal = panel(outputs)
    if relation != 'off':
        if not anchor or anchor not in specs:
            raise ValueError('Choose a connected display to place it next to.')
        if anchor == name:
            raise ValueError('A display cannot be placed next to itself.')
        if specs[anchor]['disabled'] or specs[anchor]['mirror']:
            raise ValueError(f'{anchor} is off or mirroring another display; drop onto a display that shows its own desktop.')
    closed = lid_closed()
    if relation == 'mirror' and closed and internal and internal['name'] in (name, anchor):
        raise ValueError('Open the laptop lid before mirroring, or use External only.')
    if moved['disabled'] and relation != 'off' and closed and internal and internal['name'] == name:
        raise ValueError('Open the laptop lid to turn its screen on.')
    # Copies of a display that stops showing its own desktop would go blank: turn them off instead.
    if relation in ('off', 'mirror'):
        for s in specs.values():
            if s['mirror'] == name:
                s.update(disabled=True, mirror='')
    rects = {n: logical_rect(by_name[n]) for n, s in specs.items() if not s['disabled'] and not s['mirror'] and n != name}
    mine = logical_rect(by_name[name])
    if not moved['disabled'] and not moved['mirror']:
        collapse(rects, mine)
    if relation == 'off':
        if not any(not s['disabled'] for n, s in specs.items() if n != name):
            raise ValueError('Turn on another display first.')
        moved['disabled'] = True
    elif relation == 'mirror':
        moved.update(disabled=False, mirror=anchor, position='auto')
    else:
        insert(rects, mine, relation, rects[anchor])
        rects[name] = mine
        if moved['disabled']:
            moved['mode'] = 'preferred'
        moved.update(disabled=False, mirror='')
    dx = min((r['x'] for r in rects.values()), default=0)
    dy = min((r['y'] for r in rects.values()), default=0)
    for n, r in rects.items():
        specs[n]['position'] = f"{r['x'] - dx}x{r['y'] - dy}"
    return list(specs.values())


def set_plan(outputs, name, mode=None, scale=None):
    """Change one display's mode and/or scale in place; neighbours move by the size difference."""
    specs = {s['output']: s for s in snapshot(outputs)}
    by_name = {m['name']: m for m in outputs}
    if name not in specs:
        raise ValueError(f'{name} is not connected.')
    target = specs[name]
    if target['disabled'] or target['mirror']:
        raise ValueError(f'{name} is off or mirroring another display; turn it on first.')
    if mode and mode != 'preferred' and mode not in [v.removesuffix('Hz') for v in by_name[name].get('availableModes', [])]:
        raise ValueError('Select a mode advertised by this display.')
    if scale is not None and not 0.5 <= scale <= 3:
        raise ValueError('Scale must be between 0.5 and 3.')
    old = logical_rect(by_name[name])
    new_scale = scale if scale is not None else (by_name[name].get('scale') or 1)
    if mode and mode != 'preferred':
        w, h = (int(v) for v in mode.split('@')[0].split('x'))
    else:
        w, h = by_name[name]['width'], by_name[name]['height']
    new = dict(x=old['x'], y=old['y'], w=round(w / new_scale), h=round(h / new_scale))
    rects = {n: logical_rect(by_name[n]) for n, s in specs.items() if not s['disabled'] and not s['mirror'] and n != name}
    for r in rects.values():
        if r['x'] >= old['x'] + old['w'] and overlaps(r, old, 'y'):
            r['x'] += new['w'] - old['w']
        elif r['y'] >= old['y'] + old['h'] and overlaps(r, old, 'x'):
            r['y'] += new['h'] - old['h']
    rects[name] = new
    if mode:
        target['mode'] = mode
    if scale is not None:
        target['scale'] = scale
    dx = min(r['x'] for r in rects.values())
    dy = min(r['y'] for r in rects.values())
    for n, r in rects.items():
        specs[n]['position'] = f"{r['x'] - dx}x{r['y'] - dy}"
    return list(specs.values())


def evaluate(specs):
    code = ';'.join('hl.monitor(' + lua(s) + ')' for s in specs)
    reply = run('hyprctl', 'eval', code)
    if reply not in ('', 'ok'):
        raise RuntimeError(reply)


def wait_enabled(names, attempts=50):
    names = set(names)
    for _ in range(attempts):
        if names <= {m['name'] for m in monitors() if not m.get('disabled')}:
            return
        time.sleep(0.1)
    raise RuntimeError('Timed out waiting for ' + ', '.join(sorted(names)) + ' to turn on before mirroring.')


def apply(specs):
    current = monitors()
    connected = {m['name'] for m in current}
    off = {m['name'] for m in current if m.get('disabled')}
    specs = [s for s in specs if s['output'] in connected]
    if not specs or all(s.get('disabled') for s in specs):
        raise ValueError('Refusing to disable every connected display.')
    # Readability only: Hyprland applies one eval as a single pass in monitor-id
    # order, so the order of rules here does not control application order.
    specs.sort(key=lambda s: (bool(s.get('disabled')), bool(s.get('mirror'))))
    # A mirror whose source is disabled at apply time silently becomes "no
    # mirror", so enable sources first and send the mirror rules afterwards.
    pending = {s['mirror'] for s in specs if s.get('mirror') in off}
    if pending:
        evaluate([dict(s, mirror='') for s in specs])
        wait_enabled(pending)
    evaluate(specs)


def state_dir():
    signature = os.environ.get('HYPRLAND_INSTANCE_SIGNATURE')
    if not signature:
        raise RuntimeError('Run this inside a Hyprland session.')
    path = Path(os.environ['XDG_RUNTIME_DIR']) / ('display-profiles-' + signature)
    path.mkdir(mode=0o700, exist_ok=True)
    return path


@contextlib.contextmanager
def locked():
    with (state_dir() / 'lock').open('w') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def read_state():
    try:
        return json.loads((state_dir() / 'state.json').read_text())
    except FileNotFoundError:
        return {}


def save(state):
    path = state_dir() / 'state.json'
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(state))
    tmp.replace(path)


# --- remembered layouts -----------------------------------------------------

def learned_path():
    base = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local' / 'state')
    return base / 'display-profiles' / 'learned.json'


def load_learned():
    try:
        return json.loads(learned_path().read_text())
    except FileNotFoundError:
        return {}


def save_learned(learned):
    path = learned_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(learned, indent=2))
    tmp.replace(path)


def topology_key(outputs, closed):
    descriptions = sorted(m.get('description', '') for m in outputs)
    return '; '.join(descriptions) + (' [lid closed]' if closed else ' [lid open]')


def learned_entry(name, state, outputs):
    descriptions = {m['name']: m.get('description', '') for m in outputs}
    specs = [dict(s, description=descriptions.get(s['output'], '')) for s in state['active']]
    return dict(name=name, mode=state.get('mode'), place=state.get('place'), source=state.get('source'), specs=specs)


def resolve_learned(entry, outputs):
    """Map a learned entry onto current connector names, or None when any description is missing."""
    free = list(outputs)
    rename = {}
    for s in entry.get('specs', []):
        candidates = [m for m in free if s.get('description') and m.get('description') == s['description']]
        if not candidates:
            return None
        match = next((m for m in candidates if m['name'] == s['output']), candidates[0])
        free.remove(match)
        rename[s['output']] = match['name']
    return [dict({k: v for k, v in s.items() if k != 'description'}, output=rename[s['output']],
                 mirror=rename.get(s.get('mirror', ''), s.get('mirror', '')))
            for s in entry.get('specs', [])]


def automatic(outputs, profiles):
    return auto_plan(outputs, profiles, lid_closed(), load_learned())


def number(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if float(value).is_integer():
        return str(int(value))
    return f'{value:.4f}'.rstrip('0').rstrip('.')


def nix(value, indent=0):
    pad = '  ' * indent
    if isinstance(value, (bool, int, float)):
        return number(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False).replace('${', '\\${')
    if isinstance(value, list):
        return '[\n' + ''.join(f'{pad}  {nix(v, indent + 1)}\n' for v in value) + pad + ']'
    items = [f'{k if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", k) else nix(k)} = {nix(v, indent + 1)};' for k, v in value.items()]
    if all('\n' not in item for item in items) and len(', '.join(items)) < 70:
        return '{ ' + ' '.join(items) + ' }'
    return '{\n' + ''.join(f'{pad}  {item}\n' for item in items) + pad + '}'


def export_nix(name, specs, closed):
    slug = re.sub(r'[^A-Za-z0-9_-]+', '-', name).strip('-').lower() or 'remembered'
    descriptions = {s['output']: s.get('description', '') for s in specs}
    conditions = {'requiredMonitors': [{'description': s.get('description', '')} for s in specs]}
    if closed:
        conditions['lidState'] = 'closed'
    monitors = {}
    for s in specs:
        settings = {}
        if s.get('disabled'):
            settings['disabled'] = True
        else:
            resolution, _, rate = s.get('mode', 'preferred').partition('@')
            if resolution != 'preferred':
                settings['resolution'] = resolution
                if rate:
                    settings['refreshRate'] = float(rate)
            settings.update(position=s.get('position', 'auto'), scale=s.get('scale', 1), vrr=bool(s.get('vrr')))
            if s.get('transform'):
                settings['transform'] = s['transform']
            if s.get('mirror'):
                settings['mirror'] = 'desc:' + descriptions.get(s['mirror'], s['mirror'])
        monitors['desc:' + s.get('description', '')] = settings
    return nix({slug: {'conditions': conditions, 'monitors': monitors}})


# --- present bundle ---------------------------------------------------------

def attempt(label, func):
    """Bundle helpers must never abort a layout change."""
    try:
        return func()
    except Exception as exc:
        print(f'{label}: {exc}', file=sys.stderr, flush=True)
        return None


def noctalia(*args):
    return run('noctalia', 'msg', *args)


def sinks():
    result = []
    for node in json.loads(run('pw-dump')):
        props = node.get('info', {}).get('props', {})
        if props.get('media.class') == 'Audio/Sink':
            result.append(dict(id=node['id'], name=props.get('node.name', ''), description=props.get('node.description', '')))
    return result


def default_sink():
    for line in run('wpctl', 'inspect', '@DEFAULT_AUDIO_SINK@').splitlines():
        if 'node.name' in line:
            return line.split('=', 1)[1].strip().strip('"')
    return None


def pick_sink(choice, target_description=''):
    available = sinks()
    if choice != 'auto':
        return next((s for s in available if choice in (s['name'], s['description'])), None)
    words = {w.strip('.,()').lower() for w in target_description.split() if len(w.strip('.,()')) > 3}
    def text(s):
        return (s['name'] + ' ' + s['description']).lower()
    # A sink that names the target display wins over any generic HDMI/DisplayPort sink.
    return (next((s for s in available if any(w in text(s) for w in words)), None)
            or next((s for s in available if 'hdmi' in text(s) or 'displayport' in text(s)), None))


def set_sink(name_or_choice, target_description=''):
    chosen = pick_sink(name_or_choice, target_description)
    if not chosen:
        raise RuntimeError('No matching audio output was found.')
    run('wpctl', 'set-default', str(chosen['id']))
    return chosen


def bundle_start(state, target=None, sink='auto'):
    present = dict(dnd_before=None, sink_before=None, sink=None, sink_label=None)
    attempt('keep awake', lambda: noctalia('caffeine-enable'))

    def dnd():
        present['dnd_before'] = noctalia('notification-dnd-status')
        noctalia('notification-dnd-set', 'true')

    def audio():
        present['sink_before'] = default_sink()
        chosen = set_sink(sink, (target or {}).get('description', ''))
        present.update(sink=chosen['name'], sink_label=chosen['description'])
    attempt('do not disturb', dnd)
    if sink != 'none':
        attempt('audio', audio)
    state['present'] = present


def bundle_stop(state):
    present = state.pop('present', None)
    if not present:
        return
    attempt('keep awake', lambda: noctalia('caffeine-disable'))
    if present.get('dnd_before') is not None:
        wanted = 'true' if present['dnd_before'].strip().lower() in ('true', 'on', '1') else 'false'
        attempt('do not disturb', lambda: noctalia('notification-dnd-set', wanted))
    if present.get('sink_before'):
        attempt('audio', lambda: set_sink(present['sink_before']))


# --- state transitions ------------------------------------------------------

def restore(state, outputs, profiles):
    # Leaving a layout ends any presentation bundle that came with it.
    bundle_stop(state)
    # A physical hotplug makes the previous snapshot unsafe: restore the current
    # automatic profile instead. Normalize JSON tuples via a serialization roundtrip.
    current = json.loads(json.dumps(topology(outputs)))
    if state.get('topology') == current and state.get('before'):
        apply(state['before'])
        state['active'] = state['before']
        state['mode'] = state.pop('previous_mode', 'automatic')
    else:
        name, specs = automatic(outputs, profiles)
        apply(specs)
        for key in ['active', 'previous_mode', 'place', 'source', 'target']:
            state.pop(key, None)
        state.update(mode='automatic', profile=name, topology=current)
    state.pop('deadline', None)
    state.pop('before', None)
    save(state)


def reset(state, outputs, profiles):
    """Apply the automatic profile and keep only what survives a layout reset."""
    name, specs = automatic(outputs, profiles)
    apply(specs)
    fresh = dict(mode='automatic', profile=name, topology=topology(outputs), heartbeat=state.get('heartbeat', 0))
    if state.get('present'):
        fresh['present'] = state['present']
    save(fresh)


def tick(profiles, initial=False):
    with locked():
        state = read_state()
        outputs = monitors()
        current = json.loads(json.dumps(topology(outputs)))
        if not outputs:
            return
        if state.get('deadline', float('inf')) <= time.monotonic():
            restore(state, outputs, profiles)
            state = read_state()
        if initial or state.get('topology') != current:
            bundle_stop(state)
            name, specs = automatic(outputs, profiles)
            apply(specs)
            state = dict(mode='automatic', profile=name, topology=current)
        state['heartbeat'] = time.monotonic()
        save(state)


def heartbeat():
    with locked():
        state = read_state()
        state['heartbeat'] = time.monotonic()
        save(state)


# --- watcher ----------------------------------------------------------------

def parse_events(data):
    return [line.split('>>', 1)[0] for line in data.decode(errors='replace').split('\n') if line]


def should_tick(events, lid_before, lid_now, deadline, now):
    return (any(e in MONITOR_EVENTS for e in events) or lid_before != lid_now
            or (deadline is not None and now >= deadline))


def event_socket():
    path = Path(os.environ['XDG_RUNTIME_DIR']) / 'hypr' / os.environ['HYPRLAND_INSTANCE_SIGNATURE'] / '.socket2.sock'
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(path))
    return sock


def safe_tick(profiles, initial=False):
    try:
        tick(profiles, initial)
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr, flush=True)
        time.sleep(1)


def listen(sock, profiles):
    """Block on compositor events; returns only by raising OSError on socket failure."""
    buffer = b''
    lid = lid_closed()
    beat = 0
    while True:
        deadline = read_state().get('deadline')
        timeout = 2.0 if deadline is None else max(0.0, min(2.0, deadline - time.monotonic()))
        ready, _, _ = select.select([sock], [], [], timeout)
        events = []
        if ready:
            chunk = sock.recv(65536)
            if not chunk:
                raise OSError('event socket closed')
            buffer += chunk
            complete, _, buffer = buffer.rpartition(b'\n')
            events = parse_events(complete)
        closed = lid_closed()
        if should_tick(events, lid, closed, deadline, time.monotonic()):
            safe_tick(profiles)
            beat = time.monotonic()
        elif time.monotonic() - beat >= 1:  # unrelated events are frequent; one heartbeat write per second is plenty
            heartbeat()
            beat = time.monotonic()
        lid = closed


def poll(profiles):
    initial = True
    while True:
        safe_tick(profiles, initial)
        initial = False
        time.sleep(1)


def watch(profiles):
    try:
        sock = event_socket()
    except (OSError, KeyError) as exc:
        print(f'Hyprland event socket unavailable ({exc}); polling every second.', file=sys.stderr, flush=True)
        return poll(profiles)
    safe_tick(profiles, initial=True)
    backoff = 1
    while True:
        try:
            listen(sock, profiles)
        except OSError as exc:
            print(f'Event socket error ({exc}); reconnecting in {backoff}s.', file=sys.stderr, flush=True)
        sock.close()
        while True:
            safe_tick(profiles)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            try:
                sock = event_socket()
                backoff = 1
                break
            except OSError:
                continue


# --- CLI actions ------------------------------------------------------------

def status(state, outputs):
    closed = lid_closed()
    internal = panel(outputs)
    learned = load_learned()
    state.update(monitors=outputs,
                 remaining=max(0, round(state.get('deadline', 0) - time.monotonic())),
                 watcher=time.monotonic() - state.get('heartbeat', 0) < 5,
                 lid='closed' if closed else 'open',
                 internal=internal['name'] if internal else None,
                 externals=[m['name'] for m in outputs if m is not internal],
                 remembered=topology_key(outputs, closed) in learned,
                 learned=[dict(name=e.get('name', ''), topology=k) for k, e in learned.items()],
                 present=state.get('present'), place=state.get('place'), source=state.get('source'), target=state.get('target'))
    return state


def preview(state, outputs, profiles, layout, args):
    if time.monotonic() - state.get('heartbeat', 0) > 5:
        raise ValueError('Display rollback service is not running. Start display-profiles.service first.')
    if not 0.5 <= args.scale <= 3:
        raise ValueError('Scale must be between 0.5 and 3.')
    target = next((m for m in outputs if m['name'] == args.target), None)
    if args.mode != 'preferred' and (not target or args.mode not in [v.removesuffix('Hz') for v in target.get('availableModes', [])]):
        raise ValueError('Select a mode advertised by this display.')
    specs = manual_plan(outputs, layout, args.target, args.mode, args.scale, args.place, args.source)
    arm_and_apply(state, outputs, profiles, layout, specs, place=args.place, source=args.source, target=target['name'] if target else None)


def arm_and_apply(state, outputs, profiles, mode, specs, **fields):
    """Record the layout and arm the rollback before touching any display; a failed apply restores the screen."""
    if not state.get('deadline'):
        state.update(before=snapshot(outputs), previous_mode=state.get('mode', 'automatic'))
    state.update(mode=mode, active=specs, deadline=time.monotonic() + PREVIEW_SECONDS, topology=topology(outputs), **fields)
    save(state)
    try:
        apply(specs)
    except Exception:
        restore(state, monitors(), profiles)
        raise


def set_display(state, outputs, profiles, operands, args):
    """`set <display> [--mode …] [--scale …]`: resolution or scale of one display inside any arrangement."""
    if len(operands) != 1:
        raise ValueError('Usage: set <display> [--mode WxH@rate] [--scale N]')
    if time.monotonic() - state.get('heartbeat', 0) > 5:
        raise ValueError('Display rollback service is not running. Start display-profiles.service first.')
    mode = args.mode if args.mode != 'preferred' or '--mode' in sys.argv else None
    scale = args.scale if '--scale' in sys.argv else None
    specs = set_plan(outputs, operands[0], mode, scale)
    arm_and_apply(state, outputs, profiles, 'custom', specs, place=None, source=None, target=operands[0])


def place(state, outputs, profiles, operands):
    """`place <display> <left|right|above|below|mirror|off> [anchor]`: one drag-and-drop step, previewed like any layout."""
    if len(operands) < 2:
        raise ValueError('Usage: place <display> <left|right|above|below|mirror|off> [anchor]')
    if time.monotonic() - state.get('heartbeat', 0) > 5:
        raise ValueError('Display rollback service is not running. Start display-profiles.service first.')
    name, relation = operands[0], operands[1]
    specs = place_plan(outputs, name, relation, operands[2] if len(operands) > 2 else None)
    arm_and_apply(state, outputs, profiles, 'custom', specs, place=None, source=None, target=name)


def preview_auto(state, outputs, profiles):
    """Apply the automatic profile with the same countdown and Undo as any other layout."""
    if state.get('mode', 'automatic') == 'automatic' and not state.get('deadline'):
        reset(state, outputs, profiles)
        return
    if time.monotonic() - state.get('heartbeat', 0) > 5:
        raise ValueError('Display rollback service is not running. Start display-profiles.service first.')
    name, specs = automatic(outputs, profiles)
    if not state.get('deadline'):
        state.update(before=snapshot(outputs), previous_mode=state.get('mode', 'automatic'))
    state.pop('active', None)
    state.update(mode='automatic', profile=name, deadline=time.monotonic() + PREVIEW_SECONDS, topology=topology(outputs))
    save(state)
    try:
        apply(specs)
    except Exception:
        restore(state, monitors(), profiles)
        raise


def present(state, outputs, profiles, args):
    if args.layout:
        preview(state, outputs, profiles, args.layout, args)
    if state.get('present'):
        return  # already presenting: keep the recorded pre-presentation state
    internal = panel(outputs)
    target = next((m for m in outputs if m['name'] == args.target), None) or next((m for m in outputs if m is not internal), None)
    bundle_start(state, target, args.sink)
    save(state)


def remember(state, outputs, name):
    if state.get('mode', 'automatic') == 'automatic' or not state.get('active'):
        raise ValueError('Apply a layout first, then remember it.')
    if state.get('deadline'):
        raise ValueError('Keep the preview before remembering it.')
    name = name or state['mode']
    learned = load_learned()
    learned[topology_key(outputs, lid_closed())] = learned_entry(name, state, outputs)
    save_learned(learned)
    for key in ['active', 'place', 'source']:
        state.pop(key, None)
    state.update(mode='automatic', profile='remembered:' + name, topology=topology(outputs))
    save(state)


def forget(outputs):
    learned = load_learned()
    key = topology_key(outputs, lid_closed())
    if key not in learned:
        raise ValueError('Nothing is remembered for this display setup.')
    del learned[key]
    save_learned(learned)


def export(state, outputs):
    closed = lid_closed()
    entry = load_learned().get(topology_key(outputs, closed))
    if entry:
        return export_nix(entry.get('name', ''), entry['specs'], closed)
    mode = state.get('mode', 'automatic')
    live = dict(active=state.get('active') or snapshot(outputs), mode=mode)
    name = state.get('profile', 'current') if mode == 'automatic' else mode
    return export_nix(name, learned_entry('', live, outputs)['specs'], closed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profiles', type=Path, required=True)
    parser.add_argument('action', choices=['status', 'auto', 'extend', 'mirror', 'external', 'internal', 'confirm', 'revert',
                                           'reload', 'watch', 'present', 'done', 'remember', 'forget', 'export', 'place', 'set'])
    parser.add_argument('operands', nargs='*', help='place: <display> <left|right|above|below|mirror|off> [anchor]; set: <display>')
    parser.add_argument('--target')
    parser.add_argument('--mode', default='preferred', help='Advertised resolution@refresh, or preferred')
    parser.add_argument('--scale', type=float, default=1.0)
    parser.add_argument('--place', choices=list(POSITIONS), default='right', help='Where extend puts the external display')
    parser.add_argument('--source', choices=list(SOURCES), default='internal', help='Which display a mirror copies')
    parser.add_argument('--layout', choices=list(LAYOUTS), help='Layout applied by present; omit to keep the current layout')
    parser.add_argument('--sink', default='auto', help='present audio output: auto, none, or a PipeWire sink name/description')
    parser.add_argument('--name', help='Label for remember')
    args = parser.parse_args()
    profiles = json.loads(args.profiles.read_text())
    if args.action == 'watch':
        return watch(profiles)
    with locked():
        state = read_state()
        outputs = monitors()
        if args.action == 'status':
            print(json.dumps(status(state, outputs)))
        elif args.action == 'confirm':
            if state.get('deadline', 0) <= time.monotonic():
                raise ValueError('The preview has expired.')
            for key in ['deadline', 'before', 'previous_mode']:
                state.pop(key, None)
            save(state)
        elif args.action == 'revert':
            restore(state, outputs, profiles)
        elif args.action == 'reload' and state.get('mode') != 'automatic' and state.get('active') and state.get('topology') == json.loads(json.dumps(topology(outputs))):
            apply(state['active'])
        elif args.action == 'auto':
            preview_auto(state, outputs, profiles)
        elif args.action == 'done':
            bundle_stop(state)
            save(state)
        elif args.action == 'reload':
            reset(state, outputs, profiles)
        elif args.action == 'remember':
            remember(state, outputs, args.name)
        elif args.action == 'forget':
            forget(outputs)
            reset(state, outputs, profiles)
        elif args.action == 'export':
            print(export(state, outputs))
        elif args.action == 'present':
            present(state, outputs, profiles, args)
        elif args.action == 'place':
            place(state, outputs, profiles, args.operands)
        elif args.action == 'set':
            set_display(state, outputs, profiles, args.operands, args)
        else:
            preview(state, outputs, profiles, args.action, args)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
