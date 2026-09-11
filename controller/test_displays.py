import contextlib
import copy
import importlib.util
import io
import json
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('displays', Path(__file__).with_name('displays.py'))
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)

PANEL = dict(name='eDP-1', description='panel', width=2880, height=1800,
             refreshRate=120, x=0, y=0, scale=4/3, disabled=False, vrr=True)
HDMI = dict(name='HDMI-A-1', description='projector', width=1920, height=1080,
            refreshRate=60, x=2160, y=0, scale=1, disabled=False)
PROFILES = {
    'laptop': {'conditions': {'requiredMonitors': [{'description': 'panel'}]},
               'monitors': {'eDP-1': {'resolution': '2880x1800', 'scale': 4/3}}},
    'closed': {'conditions': {'requiredMonitors': [{'description': 'panel'}], 'lidState': 'closed'},
               'monitors': {'eDP-1': {'disabled': True}}},
    'dock': {'conditions': {'requiredMonitors': [{'description': 'panel'}, {'description': 'projector'}]},
             'monitors': {'desc:projector': {'scale': 1, 'refreshRate': 60, 'resolution': '1920x1080'}}},
}
SINKS = [
    {'id': 62, 'info': {'props': {'media.class': 'Audio/Sink', 'node.name': 'alsa_output.pci.analog-stereo',
                                  'node.description': 'Ryzen HD Audio Controller Analog Stereo'}}},
    {'id': 70, 'info': {'props': {'media.class': 'Audio/Source', 'node.name': 'mic', 'node.description': 'Mic'}}},
    {'id': 88, 'info': {'props': {'media.class': 'Audio/Sink', 'node.name': 'alsa_output.pci.hdmi-stereo',
                                  'node.description': 'GA104 High Definition Audio Controller Digital Stereo (HDMI)'}}},
]
INSPECT = 'id 62, type PipeWire:Interface:Node\n  * node.name = "alsa_output.pci.analog-stereo"\n'


def fake_run(replies=None, calls=None):
    """Return a `run` stand-in answering by command prefix and recording calls."""
    replies = replies or {}

    def run(*args):
        if calls is not None:
            calls.append(args)
        for prefix, reply in replies.items():
            if args[:len(prefix)] == prefix:
                if isinstance(reply, Exception):
                    raise reply
                return reply
        return 'ok'
    return run


class DisplaysTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / 'run').mkdir()
        for name, value in [('state_dir', root / 'run'), ('learned_path', root / 'state' / 'learned.json')]:
            p = patch.object(d, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    # --- existing behaviour -------------------------------------------------

    def test_snapshot_converts_mirror_id_to_connector(self):
        source = dict(PANEL, id=0)
        target = dict(HDMI, id=1, mirrorOf="0")
        self.assertEqual(d.snapshot([source, target])[1]['mirror'], 'eDP-1')

    def test_unknown_projector_has_readable_scale(self):
        name, specs = d.auto_plan([PANEL, HDMI], {'laptop': PROFILES['laptop']})
        self.assertEqual(name, 'laptop')
        self.assertEqual(specs[1]['scale'], 1)
        self.assertFalse(specs[1]['disabled'])

    def test_specific_dock_wins(self):
        name, specs = d.auto_plan([PANEL, HDMI], PROFILES)
        self.assertEqual(name, 'dock')
        self.assertEqual(specs[1]['mode'], '1920x1080@60')

    def test_closed_lid_never_disables_last_output(self):
        _, specs = d.auto_plan([PANEL], PROFILES, closed=True)
        self.assertFalse(specs[0]['disabled'])

    def test_mirror_uses_internal_source(self):
        with patch.object(d, 'lid_closed', return_value=False):
            specs = d.manual_plan([PANEL, HDMI], 'mirror', 'HDMI-A-1')
        self.assertEqual(specs[1]['mirror'], 'eDP-1')
        self.assertFalse(specs[0]['disabled'])
        self.assertFalse(specs[1]['vrr'])

    def test_external_only_enables_target_before_disabling_panel(self):
        specs = d.manual_plan([PANEL, HDMI], 'external', 'HDMI-A-1')
        with patch.object(d, 'monitors', return_value=[PANEL, HDMI]), patch.object(d, 'run', return_value='ok') as run:
            d.apply(specs)
        code = run.call_args.args[-1]
        self.assertLess(code.index('HDMI-A-1'), code.index('eDP-1'))

    def test_no_external_and_disconnected_target_are_rejected(self):
        with self.assertRaises(ValueError):
            d.manual_plan([PANEL], 'external')
        with self.assertRaises(ValueError):
            d.manual_plan([PANEL, HDMI], 'mirror', 'DP-404')

    def test_apply_rejects_empty_or_all_disabled(self):
        with patch.object(d, 'monitors', return_value=[PANEL]):
            for specs in [[], [{'output': 'eDP-1', 'disabled': True}]]:
                with self.assertRaises(ValueError): d.apply(specs)

    def test_revert_after_unplug_recovers_laptop(self):
        with patch.object(d, 'lid_closed', return_value=False), patch.object(d, 'apply') as apply:
            state = {'topology': [[['HDMI-A-1', 'projector'], ['eDP-1', 'panel']], False],
                     'before': d.snapshot([PANEL, HDMI]), 'deadline': 10}
            d.restore(state, [PANEL], PROFILES)
            specs = apply.call_args.args[0]
            self.assertEqual(len(specs), 1)
            self.assertFalse(specs[0]['disabled'])
            self.assertNotIn('deadline', d.read_state())

    def test_preview_expires_without_the_ui(self):
        with patch.object(d, 'lid_closed', return_value=False), patch.object(d, 'monitors', return_value=[PANEL, HDMI]), patch.object(d, 'apply') as apply:
            before = d.snapshot([PANEL, HDMI])
            d.save({'mode': 'external', 'previous_mode': 'automatic', 'before': before,
                    'deadline': 0, 'topology': d.topology([PANEL, HDMI])})
            d.tick(PROFILES)
            self.assertEqual(apply.call_args.args[0], before)
            self.assertNotIn('deadline', d.read_state())
            self.assertEqual(d.read_state()['mode'], 'automatic')

    # --- 1. two-phase apply -------------------------------------------------

    def test_mirror_of_disabled_source_uses_two_evals(self):
        panel_off = dict(PANEL, disabled=True)
        polls = [[panel_off, HDMI], [panel_off, HDMI], [PANEL, HDMI]]
        with patch.object(d, 'lid_closed', return_value=False):
            specs = d.manual_plan([panel_off, HDMI], 'mirror', 'HDMI-A-1')
        with patch.object(d, 'monitors', side_effect=polls), patch.object(d, 'run', return_value='ok') as run, patch.object(d.time, 'sleep') as sleep:
            d.apply(specs)
        evals = [c.args[-1] for c in run.call_args_list if c.args[:2] == ('hyprctl', 'eval')]
        self.assertEqual(len(evals), 2)
        self.assertNotIn('["mirror"]="eDP-1"', evals[0])  # phase one enables the source as a plain output
        self.assertIn('["mirror"]=""', evals[0])
        self.assertIn('["disabled"]=false', evals[0])
        self.assertIn('["mirror"]="eDP-1"', evals[1])
        self.assertTrue(sleep.called)

    def test_mirror_of_enabled_source_uses_one_eval(self):
        with patch.object(d, 'lid_closed', return_value=False):
            specs = d.manual_plan([PANEL, HDMI], 'mirror', 'HDMI-A-1')
        with patch.object(d, 'monitors', return_value=[PANEL, HDMI]), patch.object(d, 'run', return_value='ok') as run:
            d.apply(specs)
        self.assertEqual(run.call_count, 1)
        self.assertIn('["mirror"]="eDP-1"', run.call_args.args[-1])

    def test_mirror_source_that_never_enables_times_out(self):
        panel_off = dict(PANEL, disabled=True)
        with patch.object(d, 'lid_closed', return_value=False):
            specs = d.manual_plan([panel_off, HDMI], 'mirror', 'HDMI-A-1')
        with patch.object(d, 'monitors', return_value=[panel_off, HDMI]) as monitors, patch.object(d, 'run', return_value='ok') as run, patch.object(d.time, 'sleep'):
            with self.assertRaises(RuntimeError) as ctx:
                d.apply(specs)
        self.assertIn('eDP-1', str(ctx.exception))
        self.assertEqual(run.call_count, 1)  # the mirror eval never ran
        self.assertEqual(monitors.call_count, 1 + 50)

    # --- 2. manual_plan placement and mirror source -------------------------

    def test_extend_place_maps_to_auto_positions(self):
        for place, expected in [('right', 'auto-right'), ('left', 'auto-left'), ('above', 'auto-up'), ('below', 'auto-down')]:
            specs = d.manual_plan([PANEL, HDMI], 'extend', 'HDMI-A-1', place=place)
            self.assertEqual(specs[1]['position'], expected, place)
            self.assertEqual(specs[0]['position'], '0x0')
        self.assertEqual(d.manual_plan([PANEL, HDMI], 'extend', 'HDMI-A-1')[1]['position'], 'auto-right')
        with self.assertRaises(ValueError):
            d.manual_plan([PANEL, HDMI], 'extend', 'HDMI-A-1', place='diagonal')

    def test_mirror_with_external_source(self):
        with patch.object(d, 'lid_closed', return_value=False):
            specs = d.manual_plan([PANEL, HDMI], 'mirror', 'HDMI-A-1', '1920x1080@60.00', 1.25, source='external')
        internal, target = specs
        self.assertEqual(target['position'], '0x0')
        self.assertEqual(target['mode'], '1920x1080@60.00')
        self.assertEqual(target['scale'], 1.25)
        self.assertEqual(target['mirror'], '')
        self.assertEqual(internal['mirror'], 'HDMI-A-1')
        self.assertEqual(internal['mode'], 'preferred')
        self.assertFalse(internal['disabled'])
        with self.assertRaises(ValueError):
            d.manual_plan([PANEL, HDMI], 'mirror', 'HDMI-A-1', source='sideways')

    def test_mirror_rejected_with_lid_closed_for_both_sources(self):
        with patch.object(d, 'lid_closed', return_value=True):
            for source in ('internal', 'external'):
                with self.assertRaises(ValueError) as ctx:
                    d.manual_plan([PANEL, HDMI], 'mirror', 'HDMI-A-1', source=source)
                self.assertIn('External only', str(ctx.exception))

    # --- 3. learned layouts -------------------------------------------------

    def learned_entry(self):
        old = dict(HDMI, name='DP-9')
        with patch.object(d, 'lid_closed', return_value=False):
            specs = d.manual_plan([PANEL, old], 'mirror', 'DP-9', '1920x1080@60.00000')
        state = dict(mode='mirror', active=specs, place='right', source='internal')
        return d.learned_entry('room', state, [PANEL, old])

    def test_learned_matches_description_after_connector_rename(self):
        entry = self.learned_entry()
        new = dict(HDMI, name='DP-10')
        specs = d.resolve_learned(entry, [PANEL, new])
        self.assertEqual([s['output'] for s in specs], ['eDP-1', 'DP-10'])
        self.assertEqual(specs[1]['mirror'], 'eDP-1')
        self.assertTrue(all('description' not in s for s in specs))
        key = d.topology_key([PANEL, new], False)
        name, planned = d.auto_plan([PANEL, new], PROFILES, False, {key: entry})
        self.assertEqual(name, 'remembered:room')
        self.assertEqual(planned, specs)
        self.assertNotEqual(key, d.topology_key([PANEL, new], True))

    def test_learned_entry_without_description_is_ignored(self):
        entry = self.learned_entry()
        entry['specs'][1].pop('description')
        self.assertIsNone(d.resolve_learned(entry, [PANEL, HDMI]))
        nameless = dict(HDMI, description='')
        entry = self.learned_entry()
        entry['specs'][1]['description'] = ''
        self.assertIsNone(d.resolve_learned(entry, [PANEL, nameless]))
        key = d.topology_key([PANEL, nameless], False)
        name, _ = d.auto_plan([PANEL, nameless], PROFILES, False, {key: entry})
        self.assertEqual(name, 'laptop')

    def test_learned_file_roundtrip(self):
        self.assertEqual(d.load_learned(), {})
        d.save_learned({'k': {'name': 'x', 'specs': []}})
        self.assertEqual(d.load_learned()['k']['name'], 'x')
        self.assertTrue(d.learned_path().exists())

    def test_export_snippet_shape(self):
        entry = self.learned_entry()
        text = d.export_nix('Board room', entry['specs'], closed=False)
        self.assertTrue(text.startswith('{'))
        self.assertIn('board-room = {', text)
        self.assertIn('requiredMonitors = [', text)
        self.assertIn('{ description = "projector"; }', text)
        self.assertIn('"desc:projector" = {', text)
        self.assertIn('resolution = "1920x1080";', text)
        self.assertIn('refreshRate = 60;', text)
        self.assertIn('mirror = "desc:panel";', text)
        self.assertIn('scale = 1.3333;', text)
        self.assertNotIn('lidState', text)
        self.assertIn('lidState = "closed";', d.export_nix('x', entry['specs'], closed=True))

    def test_auto_plan_resolves_mirror_by_description(self):
        profiles = {'m': {'conditions': {'requiredMonitors': [{'description': 'projector'}]},
                          'monitors': {'desc:projector': {'mirror': 'desc:panel'}}}}
        _, specs = d.auto_plan([PANEL, HDMI], profiles)
        self.assertEqual(specs[1]['mirror'], 'eDP-1')

    # --- 4. present bundle --------------------------------------------------

    def test_bundle_start_records_previous_state(self):
        calls = []
        replies = {('noctalia', 'msg', 'notification-dnd-status'): 'off',
                   ('pw-dump',): json.dumps(SINKS), ('wpctl', 'inspect'): INSPECT}
        state = {}
        with patch.object(d, 'run', fake_run(replies, calls)):
            d.bundle_start(state, HDMI, 'auto')
        self.assertEqual(state['present'], {'dnd_before': 'off', 'sink_before': 'alsa_output.pci.analog-stereo',
                                            'sink': 'alsa_output.pci.hdmi-stereo',
                                            'sink_label': 'GA104 High Definition Audio Controller Digital Stereo (HDMI)'})
        self.assertIn(('noctalia', 'msg', 'caffeine-enable'), calls)
        self.assertIn(('noctalia', 'msg', 'notification-dnd-set', 'true'), calls)
        self.assertIn(('wpctl', 'set-default', '88'), calls)

    def test_bundle_sink_none_and_named(self):
        calls = []
        replies = {('pw-dump',): json.dumps(SINKS), ('wpctl', 'inspect'): INSPECT}
        state = {}
        with patch.object(d, 'run', fake_run(replies, calls)):
            d.bundle_start(state, HDMI, 'none')
        self.assertIsNone(state['present']['sink'])
        self.assertFalse(any(c[0] == 'wpctl' for c in calls))
        state = {}
        with patch.object(d, 'run', fake_run(replies, calls)):
            d.bundle_start(state, HDMI, 'Ryzen HD Audio Controller Analog Stereo')
        self.assertEqual(state['present']['sink'], 'alsa_output.pci.analog-stereo')
        self.assertIn(('wpctl', 'set-default', '62'), calls)

    def test_bundle_failures_are_reported_not_raised(self):
        state = {}
        err = io.StringIO()
        with patch.object(d, 'run', side_effect=RuntimeError('noctalia is down')), contextlib.redirect_stderr(err):
            d.bundle_start(state, HDMI, 'auto')
        self.assertEqual(state['present'], {'dnd_before': None, 'sink_before': None, 'sink': None, 'sink_label': None})
        self.assertIn('noctalia is down', err.getvalue())
        with patch.object(d, 'run', side_effect=RuntimeError('still down')), contextlib.redirect_stderr(err):
            d.bundle_stop(state)
        self.assertNotIn('present', state)

    def test_bundle_stop_restores_everything(self):
        calls = []
        state = {'present': {'dnd_before': 'off', 'sink_before': 'alsa_output.pci.analog-stereo', 'sink': 'alsa_output.pci.hdmi-stereo'}}
        with patch.object(d, 'run', fake_run({('pw-dump',): json.dumps(SINKS)}, calls)):
            d.bundle_stop(state)
        self.assertNotIn('present', state)
        self.assertIn(('noctalia', 'msg', 'caffeine-disable'), calls)
        self.assertIn(('noctalia', 'msg', 'notification-dnd-set', 'false'), calls)
        self.assertIn(('wpctl', 'set-default', '62'), calls)
        d.bundle_stop({})  # nothing to undo is fine

    def test_restore_undoes_bundle(self):
        calls = []
        before = d.snapshot([PANEL, HDMI])
        state = {'mode': 'extend', 'previous_mode': 'automatic', 'before': before, 'deadline': 0,
                 'topology': d.topology([PANEL, HDMI]), 'present': {'dnd_before': 'on', 'sink_before': None, 'sink': None}}
        with patch.object(d, 'lid_closed', return_value=False), patch.object(d, 'apply'), patch.object(d, 'run', fake_run({}, calls)):
            d.restore(state, [PANEL, HDMI], PROFILES)
        self.assertNotIn('present', d.read_state())
        self.assertIn(('noctalia', 'msg', 'caffeine-disable'), calls)
        self.assertIn(('noctalia', 'msg', 'notification-dnd-set', 'true'), calls)

    def test_topology_change_in_tick_undoes_bundle(self):
        calls = []
        state = {'mode': 'extend', 'active': [], 'topology': d.topology([PANEL, HDMI]),
                 'present': {'dnd_before': 'off', 'sink_before': None, 'sink': None}}
        with patch.object(d, 'lid_closed', return_value=False), patch.object(d, 'monitors', return_value=[PANEL]), patch.object(d, 'apply'), patch.object(d, 'run', fake_run({}, calls)):
            d.save(state)
            d.tick(PROFILES)
        self.assertNotIn('present', d.read_state())
        self.assertEqual(d.read_state()['mode'], 'automatic')
        self.assertIn(('noctalia', 'msg', 'caffeine-disable'), calls)

    # --- 6. watcher helpers -------------------------------------------------

    def test_parse_events(self):
        data = b'monitoradded>>DP-1\nmonitorremovedv2>>1,DP-1,Dell\nworkspace>>3\n'
        self.assertEqual(d.parse_events(data), ['monitoradded', 'monitorremovedv2', 'workspace'])
        self.assertEqual(d.parse_events(b''), [])

    def test_should_tick(self):
        self.assertTrue(d.should_tick(['monitoraddedv2'], False, False, None, 10))
        self.assertTrue(d.should_tick(['monitorremoved'], False, False, None, 10))
        self.assertTrue(d.should_tick([], False, True, None, 10))
        self.assertTrue(d.should_tick([], False, False, 9.5, 10))
        self.assertFalse(d.should_tick([], False, False, 11, 10))
        self.assertFalse(d.should_tick(['workspace', 'focusedmon'], True, True, None, 10))

    # --- CLI ----------------------------------------------------------------

    def cli(self, *argv, outputs=(PANEL, HDMI), run=None, closed=False):
        profiles = Path(self.tmp.name) / 'profiles.json'
        profiles.write_text(json.dumps(PROFILES))
        out = io.StringIO()
        with patch.object(sys, 'argv', ['displays.py', '--profiles', str(profiles), *argv]), \
                patch.object(d, 'monitors', return_value=list(outputs)), patch.object(d, 'lid_closed', return_value=closed), \
                patch.object(d, 'run', run or fake_run()), patch.object(d.time, 'sleep'), contextlib.redirect_stdout(out):
            d.main()
        return out.getvalue()

    def fresh_state(self, **extra):
        state = dict(mode='automatic', profile='dock', topology=d.topology([PANEL, HDMI]), heartbeat=d.time.monotonic())
        state.update(extra)
        with patch.object(d, 'lid_closed', return_value=False):
            state['topology'] = d.topology([PANEL, HDMI])
        d.save(state)
        return state

    def test_status_fields(self):
        self.fresh_state()
        status = json.loads(self.cli('status'))
        self.assertEqual(status['lid'], 'open')
        self.assertEqual(status['internal'], 'eDP-1')
        self.assertEqual(status['externals'], ['HDMI-A-1'])
        self.assertFalse(status['remembered'])
        self.assertEqual(status['learned'], [])
        self.assertIsNone(status['present'])
        self.assertIsNone(status['place'])
        self.assertIsNone(status['source'])
        self.assertTrue(status['watcher'])
        self.assertEqual(len(status['monitors']), 2)
        self.assertEqual(json.loads(self.cli('status', closed=True))['lid'], 'closed')

    def test_extend_records_place_and_source(self):
        self.fresh_state()
        self.cli('extend', '--target', 'HDMI-A-1', '--place', 'left', '--source', 'internal')
        state = d.read_state()
        self.assertEqual(state['mode'], 'extend')
        self.assertEqual(state['place'], 'left')
        self.assertEqual(state['active'][1]['position'], 'auto-left')
        self.assertIn('deadline', state)
        status = json.loads(self.cli('status'))
        self.assertEqual(status['place'], 'left')
        self.assertEqual(status['source'], 'internal')

    def test_remember_forget_and_export(self):
        self.fresh_state()
        self.cli('extend', '--target', 'HDMI-A-1')
        with self.assertRaises(ValueError):
            self.cli('remember')  # preview still pending
        self.cli('confirm')
        self.cli('remember', '--name', 'Board room')
        learned = d.load_learned()
        key = d.topology_key([PANEL, HDMI], False)
        self.assertEqual(learned[key]['name'], 'Board room')
        self.assertEqual(learned[key]['specs'][1]['description'], 'projector')
        state = d.read_state()
        self.assertEqual(state['mode'], 'automatic')
        self.assertEqual(state['profile'], 'remembered:Board room')
        status = json.loads(self.cli('status'))
        self.assertTrue(status['remembered'])
        self.assertEqual(status['learned'], [{'name': 'Board room', 'topology': key}])
        renamed = dict(HDMI, name='DP-10')
        self.assertEqual(json.loads(self.cli('status', outputs=(PANEL, renamed)))['remembered'], True)
        snippet = self.cli('export')
        self.assertIn('board-room = {', snippet)
        self.assertIn('position = "auto-right";', snippet)
        calls = []
        self.cli('forget', run=fake_run({}, calls))
        self.assertEqual(d.load_learned(), {})
        self.assertEqual(d.read_state()['profile'], 'dock')
        self.assertTrue(any(c[:2] == ('hyprctl', 'eval') for c in calls))
        with self.assertRaises(ValueError):
            self.cli('forget')
        self.assertIn('"desc:panel" = {', self.cli('export'))  # falls back to the live layout

    def test_present_and_done(self):
        self.fresh_state()
        calls = []
        replies = {('noctalia', 'msg', 'notification-dnd-status'): 'off',
                   ('pw-dump',): json.dumps(SINKS), ('wpctl', 'inspect'): INSPECT}
        self.cli('present', '--layout', 'extend', '--target', 'HDMI-A-1', '--sink', 'auto', run=fake_run(replies, calls))
        state = d.read_state()
        self.assertEqual(state['mode'], 'extend')
        self.assertEqual(state['present']['sink'], 'alsa_output.pci.hdmi-stereo')
        self.assertIn('deadline', state)
        self.assertEqual(json.loads(self.cli('status'))['present']['dnd_before'], 'off')
        self.cli('confirm')
        calls.clear()
        self.cli('done', run=fake_run(replies, calls))
        state = d.read_state()
        self.assertEqual(state['mode'], 'extend')  # done ends the bundle but keeps the layout
        self.assertNotIn('present', state)
        self.assertIn(('noctalia', 'msg', 'caffeine-disable'), calls)
        self.assertIn(('wpctl', 'set-default', '62'), calls)

    def test_present_without_layout_keeps_mode(self):
        self.fresh_state()
        calls = []
        replies = {('noctalia', 'msg', 'notification-dnd-status'): 'off',
                   ('pw-dump',): json.dumps(SINKS), ('wpctl', 'inspect'): INSPECT}
        self.cli('present', '--sink', 'auto', run=fake_run(replies, calls))
        state = d.read_state()
        self.assertEqual(state['mode'], 'automatic')
        self.assertNotIn('deadline', state)
        self.assertEqual(state['present']['sink'], 'alsa_output.pci.hdmi-stereo')
        self.assertEqual(state['present']['sink_label'], 'GA104 High Definition Audio Controller Digital Stereo (HDMI)')
        self.assertIn(('noctalia', 'msg', 'caffeine-enable'), calls)
        replies[('noctalia', 'msg', 'notification-dnd-status')] = 'on'
        self.cli('present', '--sink', 'auto', run=fake_run(replies, calls))
        self.assertEqual(d.read_state()['present']['dnd_before'], 'off')  # already presenting: nothing re-recorded

    def test_done_without_bundle_is_noop(self):
        self.fresh_state()
        calls = []
        self.cli('done', run=fake_run({}, calls))
        self.assertEqual(d.read_state()['mode'], 'automatic')
        self.assertFalse(any(c[:2] == ('hyprctl', 'eval') for c in calls))

    def test_failed_preview_restores_before_not_automatic(self):
        self.fresh_state()
        self.cli('extend', '--target', 'HDMI-A-1', '--place', 'left')
        self.cli('confirm')
        evals = []

        def flaky(*args):  # the new layout fails to apply; the rollback eval succeeds
            if args[:2] == ('hyprctl', 'eval'):
                evals.append(args)
                if len(evals) == 1:
                    raise RuntimeError('nope')
            return ''
        with self.assertRaises(RuntimeError):
            self.cli('mirror', '--target', 'HDMI-A-1', run=flaky)
        state = d.read_state()
        self.assertEqual(len(evals), 2)
        self.assertEqual(state['mode'], 'extend')
        self.assertNotIn('deadline', state)
        self.assertFalse(any(s.get('mirror') for s in state['active']))  # the kept layout, not the failed mirror

    def test_auto_previews_with_undo(self):
        self.fresh_state()
        self.cli('extend', '--target', 'HDMI-A-1', '--place', 'left')
        self.cli('confirm')
        self.assertEqual(json.loads(self.cli('status'))['target'], 'HDMI-A-1')
        self.cli('auto')
        state = d.read_state()
        self.assertEqual(state['mode'], 'automatic')
        self.assertEqual(state['profile'], 'dock')
        self.assertIn('deadline', state)
        self.cli('revert')
        state = d.read_state()
        self.assertEqual(state['mode'], 'extend')
        self.assertEqual(state['place'], 'left')
        self.assertNotIn('deadline', state)
        self.cli('auto'); self.cli('confirm')
        self.assertNotIn('active', d.read_state())
        calls = []
        self.cli('auto', run=fake_run({}, calls))  # already automatic: plain re-apply, no countdown
        self.assertNotIn('deadline', d.read_state())
        self.assertTrue(any(c[:2] == ('hyprctl', 'eval') for c in calls))

    def test_pick_sink_prefers_target_words(self):
        sinks = SINKS + [{'id': 90, 'info': {'props': {'media.class': 'Audio/Sink', 'node.name': 'alsa_output.usb.projector',
                                                         'node.description': 'Acme Projector Audio'}}}]
        with patch.object(d, 'run', fake_run({('pw-dump',): json.dumps(sinks)})):
            self.assertEqual(d.pick_sink('auto', 'Acme Projector')['id'], 90)
            self.assertEqual(d.pick_sink('auto', 'Dell U2720Q')['id'], 88)  # generic HDMI fallback

    def test_present_revert_undoes_bundle(self):
        self.fresh_state()
        calls = []
        self.cli('present', '--layout', 'external', '--target', 'HDMI-A-1', '--sink', 'none', run=fake_run({}, calls))
        self.assertIn(('noctalia', 'msg', 'caffeine-enable'), calls)
        calls.clear()
        self.cli('revert', run=fake_run({}, calls))
        self.assertNotIn('present', d.read_state())
        self.assertIn(('noctalia', 'msg', 'caffeine-disable'), calls)

    # --- place: drag-and-drop arrangement ----------------------------------

    def three(self):
        """Laptop (logical 2160x1350) at 0x0, projector at 2160x0, monitor at 4080x0."""
        dp = dict(name='DP-2', description='monitor', width=2560, height=1440, refreshRate=60, x=4080, y=0, scale=1, disabled=False)
        return [dict(PANEL, id=0), dict(HDMI, id=1), dict(dp, id=2)]

    def positions(self, specs):
        return {s['output']: s['position'] for s in specs if not s['disabled'] and not s['mirror']}

    def test_place_right_of_another_display_inserts_and_closes_the_gap(self):
        specs = d.place_plan(self.three(), 'HDMI-A-1', 'right', 'DP-2')
        self.assertEqual(self.positions(specs), {'eDP-1': '0x0', 'DP-2': '2160x0', 'HDMI-A-1': '4720x0'})

    def test_place_left_of_the_laptop_normalizes_to_origin(self):
        specs = d.place_plan(self.three(), 'HDMI-A-1', 'left', 'eDP-1')
        self.assertEqual(self.positions(specs), {'HDMI-A-1': '0x0', 'eDP-1': '1920x0', 'DP-2': '4080x0'})

    def test_place_below_keeps_the_row_above_intact(self):
        specs = d.place_plan(self.three(), 'HDMI-A-1', 'below', 'eDP-1')
        self.assertEqual(self.positions(specs), {'eDP-1': '0x0', 'DP-2': '2160x0', 'HDMI-A-1': '0x1350'})

    def test_place_above_shifts_everything_down(self):
        specs = d.place_plan(self.three(), 'DP-2', 'above', 'eDP-1')
        self.assertEqual(self.positions(specs), {'DP-2': '0x0', 'eDP-1': '0x1440', 'HDMI-A-1': '2160x1440'})

    def test_place_mirror_leaves_the_layout_to_the_others(self):
        with patch.object(d, 'lid_closed', return_value=False):
            specs = d.place_plan(self.three(), 'HDMI-A-1', 'mirror', 'eDP-1')
        hdmi = next(s for s in specs if s['output'] == 'HDMI-A-1')
        self.assertEqual((hdmi['mirror'], hdmi['position'], hdmi['disabled']), ('eDP-1', 'auto', False))
        self.assertEqual(self.positions(specs), {'eDP-1': '0x0', 'DP-2': '2160x0'})

    def test_place_off_disables_and_collapses(self):
        specs = d.place_plan(self.three(), 'HDMI-A-1', 'off')
        self.assertTrue(next(s for s in specs if s['output'] == 'HDMI-A-1')['disabled'])
        self.assertEqual(self.positions(specs), {'eDP-1': '0x0', 'DP-2': '2160x0'})
        with self.assertRaises(ValueError):
            d.place_plan([PANEL], 'eDP-1', 'off')

    def test_place_turns_a_disabled_display_on_using_its_preferred_mode(self):
        off = dict(HDMI, width=0, height=0, disabled=True, availableModes=['1920x1080@60.00Hz', '1280x720@60.00Hz'])
        specs = d.place_plan([PANEL, off], 'HDMI-A-1', 'right', 'eDP-1')
        hdmi = next(s for s in specs if s['output'] == 'HDMI-A-1')
        self.assertFalse(hdmi['disabled'])
        self.assertEqual(hdmi['mode'], 'preferred')
        self.assertEqual(self.positions(specs), {'eDP-1': '0x0', 'HDMI-A-1': '2160x0'})

    def test_place_copies_of_a_display_that_turns_off_are_turned_off(self):
        outputs = self.three()
        outputs[2]['mirrorOf'] = '1'  # DP-2 mirrors HDMI-A-1
        specs = d.place_plan(outputs, 'HDMI-A-1', 'off')
        self.assertTrue(all(s['disabled'] for s in specs if s['output'] in ('HDMI-A-1', 'DP-2')))

    def test_place_rejects_bad_operands(self):
        outputs = self.three()
        for args in [('DP-7', 'right', 'eDP-1'), ('HDMI-A-1', 'right', 'HDMI-A-1'), ('HDMI-A-1', 'diagonal', 'eDP-1'),
                     ('HDMI-A-1', 'right', None), ('HDMI-A-1', 'right', 'DP-7')]:
            with self.assertRaises(ValueError, msg=str(args)):
                d.place_plan(outputs, *args)
        outputs[2]['mirrorOf'] = '1'
        with self.assertRaises(ValueError):  # a copy has no place of its own
            d.place_plan(outputs, 'eDP-1', 'right', 'DP-2')
        with patch.object(d, 'lid_closed', return_value=True), self.assertRaises(ValueError):
            d.place_plan(outputs, 'HDMI-A-1', 'mirror', 'eDP-1')

    def test_set_scale_moves_the_neighbours_by_the_size_difference(self):
        specs = d.set_plan(self.three(), 'HDMI-A-1', None, 2.0)
        hdmi = next(s for s in specs if s['output'] == 'HDMI-A-1')
        self.assertEqual(hdmi['scale'], 2.0)
        self.assertEqual(self.positions(specs), {'eDP-1': '0x0', 'HDMI-A-1': '2160x0', 'DP-2': '3120x0'})

    def test_set_mode_changes_only_that_display(self):
        outputs = self.three()
        outputs[1]['availableModes'] = ['1920x1080@60.00Hz', '1280x720@60.00Hz']
        specs = d.set_plan(outputs, 'HDMI-A-1', '1280x720@60.00', None)
        hdmi = next(s for s in specs if s['output'] == 'HDMI-A-1')
        self.assertEqual(hdmi['mode'], '1280x720@60.00')
        self.assertEqual(self.positions(specs), {'eDP-1': '0x0', 'HDMI-A-1': '2160x0', 'DP-2': '3440x0'})
        with self.assertRaises(ValueError):
            d.set_plan(outputs, 'HDMI-A-1', '640x480@60.00', None)
        with self.assertRaises(ValueError):
            d.set_plan(outputs, 'DP-7', None, 1.5)
        outputs[1].update(disabled=True, width=0, height=0)
        with self.assertRaises(ValueError):
            d.set_plan(outputs, 'HDMI-A-1', None, 1.5)

    def test_set_cli_previews_as_custom(self):
        self.fresh_state()
        self.cli('set', 'HDMI-A-1', '--scale', '1.5')
        state = d.read_state()
        self.assertEqual(state['mode'], 'custom')
        self.assertEqual(next(s for s in state['active'] if s['output'] == 'HDMI-A-1')['scale'], 1.5)
        self.assertIn('deadline', state)

    def test_place_cli_previews_a_custom_layout_with_undo(self):
        self.fresh_state()
        self.cli('place', 'HDMI-A-1', 'left', 'eDP-1')
        state = d.read_state()
        self.assertEqual(state['mode'], 'custom')
        self.assertIn('deadline', state)
        self.assertEqual(self.positions(state['active']), {'HDMI-A-1': '0x0', 'eDP-1': '1920x0'})
        self.assertEqual(json.loads(self.cli('status'))['mode'], 'custom')
        self.cli('revert')
        self.assertEqual(d.read_state()['mode'], 'automatic')
        with self.assertRaises(ValueError):
            self.cli('place', 'HDMI-A-1')

    def test_present_layout_failure_leaves_no_bundle(self):
        self.fresh_state()
        calls = []
        with self.assertRaises(RuntimeError):
            self.cli('present', '--layout', 'extend', '--target', 'HDMI-A-1', run=fake_run({('hyprctl', 'eval'): 'error: nope'}, calls))
        self.assertFalse(any(c[:3] == ('noctalia', 'msg', 'caffeine-enable') for c in calls))
        self.assertNotIn('present', d.read_state())


if __name__ == '__main__': unittest.main()
