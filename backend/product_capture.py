"""The shared bounded product capture flow used by API clients."""

import copy
import inspect
import re
import time

import engine as engine_mod
import intent_brain


def _json_safe(value):
    """Coerce evidence into JSON-serializable primitives, dropping paths/bytes/other objects.

    Guarantees the critique trail carries no absolute filesystem paths, byte blobs, or opaque
    objects — only dict/list/str/int/float/bool/None survive.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()
                if isinstance(key, str)}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return None


def _observed_mean(shot):
    """Best-effort measured mean luma from a shot's evidence; None when the path records none.

    Honest by omission: the calibrated primary capture path doesn't expose a converged mean, so this
    is populated only where the engine actually records one (mock / JPEG-feedback iterations)."""
    evidence = getattr(shot, "evidence", None) or {}
    final_mean = evidence.get("final_mean")
    if isinstance(final_mean, (int, float)) and not isinstance(final_mean, bool):
        return round(float(final_mean), 2)
    iterations = evidence.get("iterations")
    if isinstance(iterations, list) and iterations and isinstance(iterations[-1], dict):
        for key in ("mean", "final_mean"):
            candidate = iterations[-1].get(key)
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                return round(float(candidate), 2)
    return None


def _structured_critique(critique, shot):
    """Normalize a raw vision critique into the trail's structured shape, or None."""
    if not critique:
        return None
    issues = critique.get("issues")
    return {
        "verdict": critique.get("exposure", "good"),
        "mean_brightness": _observed_mean(shot),
        "issues": [str(issue) for issue in issues] if isinstance(issues, list) else [],
        "brightness_delta": critique.get("brightness_delta"),
        # Which critic judged this frame: "vision" or "luma_fallback". Surfaced so the trail and Why
        # card never hide a deterministic fallback behind a vision-looking verdict.
        "source": critique.get("source"),
    }


def _shot_summary(shot):
    """Gallery-relative image name plus sanitized applied/readback settings — no private paths."""
    if shot is None:
        return None
    return {
        "image": getattr(shot, "image", "") or "",
        "settings": _json_safe(getattr(shot, "settings", None) or {}),
        "applied": _json_safe(getattr(shot, "applied", None) or {}),
    }


def _attach_refinement_trail(shot, base_target, result):
    """Attach a JSON-serializable critique trail + refinement summary for BOTH refine paths.

    ``result is None`` means refinement was disabled: a single-attempt trail, ``enabled`` False.
    Otherwise ``result`` is the ReflexionResult; each attempt becomes one trail entry, and only the
    delivered (last) attempt carries the loop's stop reason.
    """
    if result is None:
        trail = [{
            "attempt_index": 0,
            "target_brightness_requested": round(float(base_target), 2),
            "critique": None,
            "correction_applied": None,
            "shot": _shot_summary(shot),
            "stop_reason": "refinement_disabled",
        }]
        shot.evidence["critique_trail"] = trail
        # ``stopped_reason`` is the historic contract key (shoot.py / API consumers); ``stop_reason``
        # is the same value under the trail's naming. Both are kept so neither side breaks.
        shot.evidence["refinement"] = {"enabled": False, "attempts": 1,
                                       "stop_reason": "refinement_disabled",
                                       "stopped_reason": "refinement_disabled",
                                       "final_attempt_index": 0, "critique": None}
        return shot

    history = result.history
    last_index = len(history) - 1
    trail = []
    for position, attempt in enumerate(history):
        trail.append({
            "attempt_index": attempt.index,
            "target_brightness_requested": round(float(attempt.target), 2),
            "critique": _structured_critique(attempt.critique, attempt.report),
            "correction_applied": _json_safe(attempt.correction),
            "shot": _shot_summary(attempt.report),
            "stop_reason": result.stopped_reason if position == last_index else None,
        })
    shot.evidence["critique_trail"] = trail
    # ``stopped_reason`` is the historic contract key (shoot.py / API consumers); ``stop_reason`` is
    # the same value under the trail's naming. ``critique`` preserves the delivered frame's critique.
    shot.evidence["refinement"] = {"enabled": True, "attempts": result.attempts,
                                   "stop_reason": result.stopped_reason,
                                   "stopped_reason": result.stopped_reason,
                                   "final_attempt_index": last_index,
                                   "critique": _json_safe(result.final_critique)}
    return shot


def _record_prior_exposures(sessions, state, meter) -> int:
    """Count this request's exposures before an identity refusal (meter frames plus frames the
    session accepted) into the session's saved identity_refusal; returns the count."""
    prior = ((meter["physical_exposure_count"] if meter is not None else 0)
             + sum(frame.get("status") == "accepted" for frame in state.get("frames") or []))
    refusal = state.get("identity_refusal")
    if prior and isinstance(refusal, dict) and refusal.get("prior_exposures") != prior \
            and not state.get("readback_unavailable"):
        refusal["prior_exposures"] = prior
        try:
            sessions.save(state)
        except (OSError, ValueError):
            state["receipt_save_failed"] = True
    return prior


def _refused_before_trigger(exc) -> bool:
    try:
        from sonyremote import CapturePreconditionError
    except ImportError:  # a mock server without the USB package cannot refuse a USB trigger
        return False
    return isinstance(exc, CapturePreconditionError)


def _save_policy_result(shot, intent):
    """Metadata recovery must never turn delivered pixels into another shutter."""
    if not shot.image:
        return shot
    if shot.evidence.get("persistence", {}).get("status") == "failed":
        shot.evidence["persistence"] = {"status": "recovered", "retry_capture": False}
    try:
        engine_mod.save_shot_record(shot, intent)
    except (OSError, ValueError, TypeError) as exc:
        if not shot.image:
            return shot
        path = engine_mod.SHOTS_DIR / shot.image
        if not path.is_file() or path.is_symlink():
            raise
        shot.evidence["persistence"] = {"status": "failed", "retry_capture": False,
                                        "reason": type(exc).__name__}
    return shot


_EXPOSURE_KEYS = ("iso", "aperture", "shutter")


def _session_capture(engine, decision, policy, intent, *, meter_plan=None):
    """Caller owns the camera exclusion; product_sessions owns durable recovery.

    ``meter_plan`` (a Fixed stack whose baseline is ``metered``) takes one meter frame through the
    /shoot metering path before the session exists, and the frames bracket around that exposure.
    Without it the session starts from whatever exposure is on the body (Auto, which follows its
    own ordinary photograph, and an explicit ``baseline: "current"``)."""
    import product_sessions
    model, port = getattr(engine, "camera_model", None), getattr(engine, "camera_port", None)
    if (engine.name != "real" or model not in engine_mod.USB_CONTROL_MODELS
            or not isinstance(port, str) or not re.fullmatch(r"usb:[0-9]+,[0-9]+", port)):
        raise NotImplementedError("Stack capture requires a selected exact Sony USB model and port")
    serial = getattr(engine, "camera_serial", None)
    if not serial:
        from sonyremote import CameraIdentityError
        raise CameraIdentityError("Select the camera again: its USB serial is required before capture.",
                                  reason="serial_required", model=model, port=port)
    sessions = product_sessions.runner()
    if len(sessions.list()) >= 100:
        raise ValueError("Delete an existing session before creating more (limit 100)")
    meter = None
    if meter_plan is not None:
        # Every failure here is raised before the session exists: no stack frame can follow it.
        # HDR frames sit at the offsets SessionRunner.create assigns; positive ones need headroom
        # under the USB shutter cap, which the meter reserves by moving stops to ISO.
        ev_step = decision.get("ev_step", 1.0)
        offsets = ([(index - (decision["frames"] - 1) / 2) * ev_step for index in range(decision["frames"])]
                   if decision["mode"] == "hdr" else [])
        meter = engine.meter_baseline(intent, **meter_plan, bracket_ev_offsets=offsets)
    metadata = {"camera_model": model, "camera_port": port, "camera_serial": serial,
                "capture_policy": copy.deepcopy(policy), "intent": intent}
    if meter is not None:
        # Persisted with the session so a resumed or recovered stack still reports its meter.
        metadata["stack_baseline"] = _json_safe({
            "source": "metered", "inherited": meter["inherited"], "metered": meter["metered"],
            "recommendation_source": meter.get("recommendation_source"),
            "meter_passes": meter.get("meter_passes"), "meter_reliability": meter.get("meter_reliability"),
            "meter_physical_exposures": meter["physical_exposure_count"],
            "meter_restore": meter.get("meter_restore"),
            "bracket_headroom": meter.get("bracket_headroom")})
    try:
        # One manifest write: the metered settings, and the body's pre-meter exposure as the
        # restoration baseline (never the meter frame's), exist together or not at all.
        state = sessions.create(decision["mode"], decision["frames"], ev_step=decision.get("ev_step", 1.0),
                                settings=dict(meter["metered"]) if meter is not None else None,
                                metadata=metadata,
                                restore_baseline=meter["restore_baseline"] if meter is not None else None)
    except Exception as exc:
        if meter is None:
            raise
        from sonyremote import CaptureOutcomeUncertain
        restore = meter.get("meter_restore") or {"status": "unknown"}
        restored = ("the pre-meter exposure was restored" if restore.get("status") in ("completed", "unchanged")
                    else f"the pre-meter exposure could not be restored ({restore.get('reason', 'no receipt')}); "
                         "the body may still be at the metering exposure")
        failure = CaptureOutcomeUncertain(
            f"The meter photograph was taken but the stack session could not be saved; no stack "
            f"frame was issued and {restored}: {type(exc).__name__}: {exc}")
        failure.meter_restore = copy.deepcopy(restore)
        raise failure from exc
    session_id = state["id"]
    started = time.monotonic()
    try:
        state = product_sessions.run(session_id)
    except Exception as exc:
        # A session exists before the first shutter. Preserve it even when setup,
        # capture, restoration or publication fails outside the runner's handler.
        try:
            state = sessions.read(session_id)
        except Exception as recovery_exc:
            state["readback_unavailable"] = True
            state["product_error"] = f"{type(exc).__name__}: {exc}; session readback unavailable: {type(recovery_exc).__name__}"
        else:
            state["product_error"] = f"{type(exc).__name__}: {exc}"
            try:
                sessions.save(state)
            except (OSError, ValueError):
                state["receipt_save_failed"] = True
        if getattr(exc, "identity_refused", False) is True:
            if meter is not None and (meter.get("meter_restore") or {}).get("status") == "failed":
                exc.meter_restore = copy.deepcopy(meter["meter_restore"])
            # The refused operation fired nothing, but this request's meter frame (and any frame
            # the session accepted before the refusal) did: count them separately.
            prior = _record_prior_exposures(sessions, state, meter)
            if prior:
                exc.prior_exposures = prior
            raise  # a clean identity refusal keeps its type; the session stays resumable
    # SessionRunner records a mid-session identity refusal in the state instead of raising.
    _record_prior_exposures(sessions, state, meter)
    elapsed = time.monotonic() - started
    frames = state.get("frames", [])
    accepted = sum(frame.get("status") == "accepted" for frame in frames)
    uncertain = bool(state.get("readback_unavailable")) or any(frame.get("status") in ("capturing", "ambiguous") for frame in frames)
    publication = state.get("publication") or {}
    image = state.get("image", "") if publication.get("status") == "published" else ""
    ok = bool(image and state.get("status") == "completed" and state.get("restore") == "completed")
    ladder_refusal = state.get("ladder_refusal")
    import product_sessions
    heavy = state["heavy_job"].get("status") if isinstance(state.get("heavy_job"), dict) else None
    processing = (not ok and heavy in ("queued", "running") and state.get("status") == "completed"
                  and state.get("restore") == "completed")
    summary = (f"{decision['mode']} completed from {accepted} frames." if ok
               else f"{decision['mode']} captured {accepted} frames; the merge is {heavy} and will appear in the gallery."
               if processing else "The fixed bracket cannot be captured from this baseline. "
               + product_sessions.ladder_remedy(ladder_refusal)
               if ladder_refusal else "Stack session needs review; continue from its saved session.")
    # This shot record replaces the session's published sidecar, so the HDR verdict (recovered or
    # nothing to recover) and its motion evidence must travel here as they do in publication.
    details = (state.get("output") or {}).get("details") or {}
    hdr_report = details.get("hdr") if ok and state.get("mode") == "hdr" else None
    if isinstance(hdr_report, dict) and hdr_report.get("summary"):
        summary = f"{summary} {hdr_report['summary']}"
    restore_baseline = state.get("restore_baseline") or {}
    if meter is not None:
        metered = meter["metered"]
        baseline = copy.deepcopy(metadata["stack_baseline"])
        why = (f"One meter photograph set the baseline ISO {metered['iso']} · {metered['aperture']} · "
               f"{metered['shutter']}; the frames bracket around it, not around the exposure the body "
               "held before. Settings restoration and per-frame receipts are recorded.")
        meter_count = meter["physical_exposure_count"]
    else:
        # Auto follows its own ordinary photograph, which left its metered exposure on the body.
        baseline = {"source": "ordinary_capture" if policy.get("mode") == "auto" else "inherited", "metered": None,
                    "inherited": {key: restore_baseline.get(key) for key in _EXPOSURE_KEYS}
                    if restore_baseline else None}
        why = "Explicit session from current confirmed camera settings. No preliminary metering photograph. Settings restoration and per-frame receipts are recorded."
        meter_count = 0
    durability = state.get("delivery_durability")
    if durability:
        summary += " " + durability["warning"]
    meter_restore = (meter or {}).get("meter_restore") or {}
    restore_failed = meter is not None and meter_restore.get("status") == "failed"
    if restore_failed:
        # No camera write here: say what is known about the body's exposure.
        reason = meter_restore.get("reason") or "no reason recorded"
        summary += (f" Note: restoring the pre-meter exposure right after metering failed ({reason}); "
                    "the stack session restored it afterwards." if state.get("restore") == "completed" else
                    f" Warning: the pre-meter exposure could not be restored after metering ({reason}) and "
                    "the stack session has not restored it; the body may still be at the metering exposure.")
    return engine_mod.Shot(ok=ok, image=image, summary=summary,
        why=ladder_refusal["detail"] if ladder_refusal else why,
        mood={}, settings=dict(state.get("settings") or {}), cards=[],
        applied={"session_frames": [frame.get("applied", {}) for frame in frames]},
        evidence={"source": "usb_session", "session_id": session_id,
                  "physical_exposure_count": None if uncertain else meter_count + accepted,
                  "capture_timing": {"stack_session_s": elapsed,
                                     "scope": "capture, transfer, merge and publication combined"},
                  "stack_policy": {"requested": policy, "decision": decision,
                      "budget": __import__("capture_policy").exposure_budget(policy),
                      "session_id": session_id, "session_url": f"/sessions/{session_id}",
                      "status": state.get("status"), "restore": state.get("restore"),
                      "publication": publication.get("status"), "accepted_frames": accepted,
                      "heavy_job": heavy,
                      "may_have_fired": uncertain, "retry_capture": False,
                      "baseline": baseline,
                      "identity_refusal": state.get("identity_refusal"),
                      **({"ladder_refusal": state["ladder_refusal"]} if state.get("ladder_refusal") else {}),
                      "error": state.get("product_error") or state.get("error"),
                      # Travels with stack_policy onto an Auto fallback's ordinary photo record.
                      "delivery_durability_checked": True},
                  "delivery_durability_checked": True,  # decided from the manifest just read
                  # Same record publication writes into the session sidecar; the shot record that
                  # later replaces that sidecar must keep it.
                  **({"stack_baseline": copy.deepcopy(metadata["stack_baseline"])} if meter is not None else {}),
                  **({"meter_restore": copy.deepcopy(meter_restore)} if restore_failed else {}),
                  **({"hdr": copy.deepcopy(hdr_report)} if isinstance(hdr_report, dict) else {}),
                  **({"hdr_motion": copy.deepcopy(details["hdr_motion"])}
                     if isinstance(hdr_report, dict) and details.get("hdr_motion") else {}),
                  **({"delivery_durability": durability} if durability else {})})


def shoot(engine, intent=None, *, variant_id=None, refine=False, applied_plan=None, stack_policy=None):
    from photo_lifecycle import _LOCK
    from capture_policy import validate_stack_policy, decide_stack_policy, exposure_budget
    policy = validate_stack_policy(stack_policy, refine=refine)
    # A delete/retention sweep cannot interleave copy, sidecar and enrollment.
    # The API owns camera exclusion; do not acquire a nested camera lock here.
    with _LOCK:
        if policy["mode"] == "fixed":
            decision = {"mode": policy["stack_mode"], "frames": policy["frames"],
                        "reason": "Explicit fixed stack selection."}
            meter_plan = (None if policy["baseline"] == "current"
                          else {"variant_id": variant_id, "applied_plan": applied_plan})
            return _save_policy_result(_session_capture(engine, decision, policy, intent,
                                                        meter_plan=meter_plan), intent)
        if policy["mode"] == "off":
            receipt = {"requested": policy, "decision": {"mode": "single", "frames": 1, "reason": "Stacking is off."},
                       "budget": exposure_budget(policy, refine=refine), "retry_capture": False}
            return _shoot(engine, intent, variant_id=variant_id, refine=refine,
                          applied_plan=applied_plan, policy_receipt=receipt)
        started = time.monotonic()
        shot = _shoot(engine, intent, variant_id=variant_id, refine=refine, applied_plan=applied_plan)
        elapsed = time.monotonic() - started
        decision = decide_stack_policy(policy, shot, engine_mod.SHOTS_DIR / shot.image, intent=intent)
        shot.evidence["stack_policy"] = {"requested": policy, "decision": decision,
                                        "budget": exposure_budget(policy, refine=refine),
                                        "retry_capture": False}
        shot.evidence["capture_timing"] = {"ordinary_pipeline_s": elapsed,
            "scope": "ordinary capture, metering, transfer, processing and optional refinement combined"}
        if policy["mode"] == "auto" and decision["mode"] != "single":
            try:
                stacked = _session_capture(engine, decision, policy, intent)
            except (NotImplementedError, OSError, ValueError) as exc:
                shot.evidence["stack_policy"]["decision"] = {"mode": "single", "frames": 1, "reason": str(exc)}
            except RuntimeError as exc:
                if getattr(exc, "identity_refused", False) is not True:
                    raise
                # The ordinary photo stands; the additional stack was refused before any shutter.
                shot.evidence["stack_policy"]["decision"] = {"mode": "single", "frames": 1, "reason": str(exc)}
                shot.evidence["stack_policy"]["identity_refusal"] = exc.as_dict()
            else:
                prior_count = shot.evidence.get("physical_exposure_count")
                stack_count = stacked.evidence.get("physical_exposure_count")
                stacked.evidence["physical_exposure_count"] = (prior_count + stack_count
                    if type(prior_count) is int and type(stack_count) is int else None)
                stacked.evidence["capture_timing"]["ordinary_pipeline_s"] = elapsed
                stacked.evidence["stack_policy"]["ordinary_image"] = shot.image
                ordinary_durability = shot.evidence.get("delivery_durability")
                if stacked.ok and ordinary_durability:
                    # One of the counted exposures (the ordinary photo, #61) had an unconfirmed save.
                    # Its own key, so the session's rollup keeps evidence.delivery_durability.
                    stacked.evidence["stack_policy"]["ordinary_delivery_durability"] = ordinary_durability
                    stacked.summary += (f" The ordinary photo ({shot.image}): saving was not confirmed durable"
                                        f" — {ordinary_durability.get('reason') or 'no reason was recorded'}.")
                if stacked.ok:
                    shot = stacked
                else:
                    # Preserve the accepted ordinary photo and link the failed stack;
                    # the request must never recapture it or silently start another job.
                    shot.evidence["stack_policy"] = stacked.evidence["stack_policy"]
                    shot.evidence["physical_exposure_count"] = stacked.evidence["physical_exposure_count"]
                    shot.evidence["capture_timing"].update(stacked.evidence["capture_timing"])
                    shot.why += " Stack session needs review; the original photograph is preserved."
                    durability = stacked.evidence.get("delivery_durability")
                    if durability:
                        # The stack's unconfirmed frame saves stay visible on the photo that stands.
                        # The ordinary photo's own #61 evidence, if any, keeps the top-level key.
                        shot.evidence["stack_policy"]["delivery_durability"] = durability
                        shot.evidence.setdefault("delivery_durability", durability)
                        if durability["warning"] not in shot.summary:
                            shot.summary += " " + durability["warning"]
        if policy["mode"] == "auto" and decision["mode"] == "single":
            shot.why += " " + decision["reason"]
        return _save_policy_result(shot, intent)


def _shoot(engine, intent=None, *, variant_id=None, refine=False, applied_plan=None, policy_receipt=None):
    plan = (copy.deepcopy(applied_plan) if applied_plan is not None else
            intent_brain.interpret(intent, variant_id=variant_id) if variant_id
            else intent_brain.interpret(intent))
    if (refine and plan.get("exposure_route") == "long_single_exposure"
            and getattr(engine, "name", None) == "real" and engine_mod.long_exposure_enabled()):
        # A critique-driven reshoot would fire a second exposure of up to 30 s; refuse up front.
        raise ValueError("Refine is not available for a long exposure; ask again without Refine.")

    def persist(shot):
        prior_failure = shot.evidence.get("persistence", {}).get("status") == "failed"
        if prior_failure:
            shot.evidence["persistence"] = {"status": "recovered", "retry_capture": False}
        try:
            engine_mod.save_shot_record(shot, intent)
        except (OSError, ValueError, TypeError) as exc:
            # The shutter is already spent and the photo is delivered. Metadata failure
            # must never invite another exposure or a corrective retry.
            path = engine_mod.SHOTS_DIR / shot.image
            if not shot.image or not path.is_file() or path.is_symlink():
                raise
            shot.evidence["persistence"] = {"status": "failed", "retry_capture": False,
                                           "reason": type(exc).__name__}
        return shot

    capture_attempts = []
    delivered = []   # images earlier attempts of this request already took and published

    def capture(target=None):
        kwargs = {}
        if "applied_plan" in inspect.signature(engine.shoot).parameters:
            kwargs["applied_plan"] = copy.deepcopy(plan)
        if variant_id:
            kwargs["variant_id"] = variant_id
        if target is not None:
            kwargs["target_override"] = target
        started = time.monotonic()
        try:
            shot = engine.shoot(intent, **kwargs)
        except Exception as exc:
            if capture_attempts and (getattr(exc, "identity_refused", False) is True
                                     or getattr(exc, "reason", None) == "placeholder_settings"):
                # Stays a clean identity refusal (this operation fired nothing), but the request's
                # earlier attempts did fire and published a photo: report them separately.
                if getattr(exc, "capture_path", None) is None and delivered:
                    exc.capture_path = engine_mod.SHOTS_DIR / delivered[-1]
                # Earlier attempts add to what this attempt already fired (its meter frames).
                counts = [attempt["physical_exposure_count"] for attempt in capture_attempts]
                exc.prior_exposures = (sum(counts) + (getattr(exc, "prior_exposures", None) or 0)
                                       if all(type(count) is int for count in counts) else None)
                raise
            # "Refused before any trigger" only describes this attempt. Once an earlier attempt of
            # the same request fired, the request as a whole is uncertain and is never retried.
            if not capture_attempts or not _refused_before_trigger(exc):
                raise
            from sonyremote import CaptureOutcomeUncertain
            failure = CaptureOutcomeUncertain(
                f"A later capture of this request was refused before its trigger after "
                f"{len(capture_attempts)} earlier capture attempt(s) had fired; the earlier photo is "
                f"kept and nothing is retried: {exc}",
                capture_path=engine_mod.SHOTS_DIR / delivered[-1] if delivered else None)
            if getattr(exc, "meter_restore", None):
                failure.meter_restore = exc.meter_restore
            # Earlier attempts add to what this attempt already fired (its meter frames).
            counts = [attempt["physical_exposure_count"] for attempt in capture_attempts]
            if all(type(count) is int for count in counts):
                failure.prior_exposures = sum(counts) + (getattr(exc, "prior_exposures", None) or 0)
            raise failure from exc
        if shot.image:
            delivered.append(shot.image)
        capture_attempts.append({"pipeline_s": time.monotonic() - started,
                                 "physical_exposure_count": shot.evidence.get("physical_exposure_count")})
        counts = [attempt["physical_exposure_count"] for attempt in capture_attempts]
        if all(type(count) is int for count in counts):
            shot.evidence["physical_exposure_count"] = sum(counts)
        shot.evidence["capture_timing"] = {"attempts": copy.deepcopy(capture_attempts),
            "scope": "each engine capture includes metering, shutter, transfer and processing; no isolated shutter latency claim"}
        if policy_receipt is not None:
            shot.evidence["stack_policy"] = copy.deepcopy(policy_receipt)
        shot.mood = copy.deepcopy(shot.mood or {})
        wb_evidence = shot.applied.get("white_balance") or {}
        if not isinstance(wb_evidence, dict):
            wb_evidence = {"verified": False}
        requested_wb = wb_evidence.get("requested", plan.get("white_balance"))
        shot.mood.update(status="requested_recipe", outcome_verified=False,
                         observed_settings=dict(shot.settings or {}),
                         white_balance_verification=wb_evidence)
        shot.mood.setdefault("settings", {})["white_balance"] = requested_wb or "unchanged"
        if (requested_wb and engine.name != "mock"
                and wb_evidence.get("verified") is False):
            shot.why += " White-balance request was not verified."
        return persist(shot)

    if not refine:
        shot = capture()
        _attach_refinement_trail(shot, plan["target_brightness"], None)
        return persist(shot)
    from reflexion import reflexion_shoot

    def critique(shot):
        if not shot.ok or engine.name == "mock" or shot.evidence.get("persistence", {}).get("status") == "failed":
            return None
        # Vision first; when its host is unreachable, fall back to a deterministic luma critique of
        # the JPEG itself so Refine still corrects exposure offline. The critique carries its
        # ``source`` so the trail records which critic judged — never a silent fallback.
        from luma_critique import critique_with_fallback
        return critique_with_fallback(engine_mod.SHOTS_DIR / shot.image, intent, plan["target_brightness"])

    result = reflexion_shoot(capture, critique, plan["target_brightness"], max_attempts=2)
    shot = result.final_report
    _attach_refinement_trail(shot, plan["target_brightness"], result)
    return persist(shot)
