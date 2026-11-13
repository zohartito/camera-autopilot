"""Durable controls applied only between accepted timelapse frames.

No camera operations here. Smartlapse shortens the pending suffix when measured
throughput cannot meet the requested duration; it never duplicates frames.
"""
from __future__ import annotations

import copy
import json
import math
import time
from stack_session import MAX_SEQUENCE_FRAMES, _atomic_json, regular_bytes, execution_budget_status
from stack_planner import MODE_DEFAULTS, TIMELAPSE_DEFAULTS, timelapse_config


def smartlapse_settings(value, exposure=None):
    """Validate a SmartLapse plan against the execution budget of `exposure`'s mode.

    The real duration may use the whole session budget that applies (Holy Grail 3 h,
    every other mode 1 h); the plan names that budget. Without an exposure, 1 h.
    """
    if not isinstance(value, dict) or set(value) - {'duration_s', 'output_s', 'fps'}:
        raise ValueError('Smartlapse requires duration_s, output_s and optional fps')
    values = {'fps': 24.0, **value}
    for key in ('duration_s', 'output_s', 'fps'):
        v = values.get(key)
        if type(v) not in (int, float) or not math.isfinite(v) or v <= 0:
            raise ValueError('Smartlapse durations and fps must be finite and positive')
    if not 1 <= values['fps'] <= 60:
        raise ValueError('Smartlapse fps must be 1..60')
    budget = execution_budget_status('timelapse', exposure, 0.0)
    if values['duration_s'] > budget['budget_s']:
        rule = 'Holy Grail' if budget['applies'] == 'holy_grail_timelapse' else 'session'
        raise ValueError(f"Smartlapse duration must fit the {budget['budget_s'] / 3600:g}-hour {rule} "
                         f"execution budget (<={budget['budget_s']:g} seconds)")
    frames = round(values['output_s'] * values['fps'])
    if not 2 <= frames <= MAX_SEQUENCE_FRAMES:
        raise ValueError(f'Smartlapse output_s * fps must yield 2..{MAX_SEQUENCE_FRAMES} real frames')
    if values['duration_s'] / (frames - 1) > 3600:
        # A session interval is at most one hour; a 3 h plan needs at least 4 frames.
        raise ValueError('Smartlapse interval must be <=3600 seconds; plan more movie frames')
    return {**values, 'requested_frames': frames, 'planned_output_s': frames / values['fps'],
            'interval_s': values['duration_s'] / (frames - 1),
            'execution_budget_s': budget['budget_s'], 'budget_applies': budget['applies']}


def read_controls(sessions, session_id):
    state = sessions.read(session_id)
    path = sessions._directory(session_id) / 'controls.json'
    if path.exists():
        value = json.loads(regular_bytes(path, 65536))
        if value.get('id') != session_id or type(value.get('revision')) is not int:
            raise ValueError('Invalid timelapse control identity')
        return value
    return {'id': session_id, 'revision': 0, 'smartlapse': None,
            'exposure': state.get('timelapse') or timelapse_config(), 'preview_every': 0,
            'deflicker': None}


# Owner decision 2026-09-27: deflicker is on by default for Holy Grail. These are the render
# path's defaults, the setting the Wave 0 simulation measured (~0.045 EV rendered steps versus
# ~0.25-0.33 EV raw third-stop steps; docs/WAVE0_TIMELAPSE_2026-09-22.md). Other policies stay off.
HOLY_GRAIL_DEFLICKER_DEFAULT = {'enabled': True, 'window': 7, 'max_gain_stops': 1.0}


def deflicker_settings(value):
    """Validate the optional Holy-Grail deflicker control. Off means the key is absent."""
    if not isinstance(value, dict) or set(value) - {'enabled', 'window', 'max_gain_stops'}:
        raise ValueError('deflicker accepts enabled, window and max_gain_stops only')
    if type(value.get('enabled')) is not bool:
        raise ValueError('deflicker.enabled must be a boolean')
    if not value['enabled']:
        return None
    window = value.get('window', 7)
    if type(window) is not int or window < 3 or window > 31 or window % 2 == 0:
        raise ValueError('deflicker.window must be an odd integer 3..31')
    max_gain_stops = value.get('max_gain_stops', 1.0)
    if type(max_gain_stops) not in (int, float) or not math.isfinite(max_gain_stops) \
            or not 0 < max_gain_stops <= 3:
        raise ValueError('deflicker.max_gain_stops must be a finite number in (0, 3]')
    return {'enabled': True, 'window': window, 'max_gain_stops': float(max_gain_stops)}


def update_controls(sessions, session_id, patch):
    if not isinstance(patch, dict) or not patch or set(patch) - {'smartlapse', 'exposure', 'preview_every', 'deflicker'}:
        raise ValueError('Unknown or empty timelapse control update')
    # Short control lock is shared with cancel/delete. It never waits on the long
    # lifecycle lock or running camera operation for ordinary exposure updates.
    with sessions._control(session_id):
        state = sessions.read(session_id)
        if state['mode'] != 'timelapse':
            raise ValueError('Controls require a timelapse session')
        if state['status'] in ('completed', 'needs_reconciliation'):
            raise ValueError('Completed or ambiguous sessions cannot change controls')
        current = read_controls(sessions, session_id)
        result = copy.deepcopy(current)
        if 'exposure' in patch:
            exposure = patch['exposure']
            if not isinstance(exposure, dict):
                raise ValueError('Exposure controls must be an object')
            if 'fps' in exposure and exposure['fps'] != result['exposure']['fps']:
                raise ValueError('Set output fps with Smartlapse before capture; midrun fps changes are unsupported')
            merged = {**result['exposure'], **exposure}
            mode = merged.pop('mode', 'fixed')
            previous_mode = result['exposure'].get('mode', 'fixed')
            if mode != previous_mode:
                # Mode-dependent defaults (Holy Grail's smoothing and 20 EV range, Auto's
                # 1 EV step) follow the destination mode unless this patch sets them; a
                # value still equal to the previous mode's default is not a user choice.
                # Equality cannot tell an inherited default from a deliberate equal value;
                # a caller that means to keep one repeats it in the mode-change patch.
                previous_defaults = {**TIMELAPSE_DEFAULTS, **MODE_DEFAULTS.get(previous_mode, {})}
                for key in {key for defaults in MODE_DEFAULTS.values() for key in defaults}:
                    if key not in exposure and merged.get(key, previous_defaults[key]) == previous_defaults[key]:
                        merged.pop(key, None)
                # A carried value the destination mode cannot hold (a 10 EV Holy Grail range
                # into Fixed, Auto's 1 EV step into Holy Grail) falls back to its default.
                ceilings = {'max_total_ev': 20 if mode == 'holy_grail' else 6,
                            'max_step_ev': 1.0 if mode == 'auto_custom' else 0.3}
                for key, ceiling in ceilings.items():
                    if key not in exposure and merged.get(key, 0) > ceiling:
                        merged.pop(key, None)
            elif 'smoothing_frames' not in result['exposure'] and 'smoothing_frames' not in exposure:
                # A session stored before smoothing existed keeps its original frame-by-frame law.
                merged['smoothing_frames'] = 1
            result['exposure'] = timelapse_config(mode, merged)
        if 'preview_every' in patch:
            every = patch['preview_every']
            if type(every) is not int or every != 0 and not 2 <= every <= 20:
                raise ValueError('preview_every must be 0 or an integer 2..20')
            result['preview_every'] = every
        if 'deflicker' in patch:
            result['deflicker'] = deflicker_settings(patch['deflicker'])
        if 'smartlapse' in patch:
            if any(f['status'] != 'pending' for f in state['frames']) or state['status'] != 'ready':
                raise ValueError('Smartlapse duration must be configured before any capture')
            job = sessions._directory(session_id) / 'job.json'
            if job.exists() and json.loads(regular_bytes(job, 65536)).get('status') in ('queued', 'running'):
                raise ValueError('Configure Smartlapse before submitting a background job')
            result['smartlapse'] = smartlapse_settings(patch['smartlapse'], result['exposure'])
            with sessions.operation(session_id):
                # Recheck after acquiring operation ownership; never overwrite a run.
                state = sessions.read(session_id)
                if state['status'] != 'ready' or any(f['status'] != 'pending' for f in state['frames']):
                    raise ValueError('Session started while configuring Smartlapse')
                plan = result['smartlapse']
                count = plan['requested_frames']
                state['frames'] = [{'index': i, 'status': 'pending', 'ev_offset': 0.0,
                                    'destination': str(sessions._directory(session_id) / f'frame_{i:04d}.jpg')}
                                   for i in range(count)]
                state.update(frame_count=count, interval_s=plan['interval_s'], requested_interval_s=plan['interval_s'])
                state['timelapse']['fps'] = plan['fps']
                result['exposure']['fps'] = plan['fps']
                # Intent is written before the manifest so a crash cannot silently
                # run a changed count without its target-duration contract.
                result.update(revision=current['revision'] + 1, updated_at=time.time())
                _atomic_json(sessions._directory(session_id) / 'controls.json', result)
                sessions.save(state)
                return result
        result.update(revision=current['revision'] + 1, updated_at=time.time())
        _atomic_json(sessions._directory(session_id) / 'controls.json', result)
        return result


def adapt_pending(state, plan, *, elapsed_s, observed_frame_s):
    """Reduce pending suffix, preserving every accepted receipt and original demand."""
    if not all(math.isfinite(v) and v >= 0 for v in (elapsed_s, observed_frame_s)):
        raise ValueError('Invalid Smartlapse timing')
    accepted = sum(frame['status'] == 'accepted' for frame in state['frames'])
    if any(frame['status'] != 'pending' for frame in state['frames'][accepted:]):
        raise ValueError('Smartlapse cannot rewrite an ambiguous or non-prefix frame')
    remaining_s = max(0.0, plan['duration_s'] - elapsed_s)
    cycle_s = max(0.001, observed_frame_s)
    feasible_pending = math.floor(remaining_s / cycle_s)
    effective_count = max(2, accepted, min(state['frame_count'], accepted + feasible_pending))
    del state['frames'][effective_count:]
    state['frame_count'] = effective_count
    pending = effective_count - accepted
    state['interval_s'] = min(3600.0, remaining_s / pending) if pending else 0.0
    state['smartlapse_status'] = {'requested_count': plan['requested_frames'],
        'effective_count': effective_count, 'accepted_count': accepted,
        'actual_output_s': accepted / plan['fps'], 'planned_output_s': effective_count / plan['fps'],
        'elapsed_s': elapsed_s, 'target_duration_s': plan['duration_s'],
        'observed_frame_s': observed_frame_s,
        'reduced_for_throughput': effective_count < plan['requested_frames'],
        'duration_overrun': elapsed_s > plan['duration_s'],
        'note': 'Only real accepted frames are included; a shorter output is reported when capture throughput is insufficient.'}
    return state['smartlapse_status']


def mounted_lens_for_frame(caps, current_aperture, accepted_path=None):
    """Derive conservative lens limits from the preceding accepted JPEG only.

    Without a lens identity/profile, stopping down beyond the current readback
    is withheld. No new metering exposure is created to obtain missing metadata.
    """
    import camera_caps
    from image_safety import open_image
    specification = maximum = None
    lens_model = ''
    if accepted_path is not None:
        with open_image(accepted_path) as image:
            exif = image.getexif()
            detail = exif.get_ifd(34665) if 34665 in exif else exif
            specification = detail.get(42034, exif.get(42034))
            maximum = detail.get(37381, exif.get(37381))
            lens_model = str(detail.get(42036, exif.get(42036, ''))).strip()
    import rig_profile
    # The profiled Tamron under the same rule as /shoot: any accepted spelling, and the body's
    # generic "E 28-75mm F2.8-2.8" only when this JPEG's LensSpecification is 28-75 f/2.8-2.8.
    named_tamron = rig_profile.lens_is_profiled_tamron(lens_model, specification)
    verified_profile = (2.8, 22.0) if named_tamron else None
    bounded, evidence = camera_caps.constrain_mounted_lens(caps, current_aperture,
        lens_specification=specification, max_aperture_value=maximum,
        verified_profile=verified_profile)
    evidence.update(lens_model=lens_model or None, source='previous_accepted_jpeg' if accepted_path else 'current_readback_only')
    return bounded, evidence
