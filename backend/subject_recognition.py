"""Subject recognition — a zero-shot CLIP classifier that sets a shutter floor.

Arsenal parity item 2: "image recognition identifies subject-specific needs, e.g. a fast
shutter for birds". This module reads the meter frame with the *same* CLIP image encoder the
taste layer already ships (``embed_index``), scores it against a small fixed set of text
prompts, and — only when confident — imposes a subject-specific minimum shutter speed (a floor).

Determinism boundary (a hard project rule): the model only *classifies*. Everything that turns
a class into a camera constraint — the class list, the prompt set, the class -> shutter-floor
table, the focal-length rule and the confidence threshold — is exact code/data below, never
learned. The floor is then handed to ``physics.solve_exposure`` (via the brain's binding
shutter floor), which honours it as a hard constraint: if the scene is too dark to expose at
the floor it raises ISO first, and only breaks the floor once ISO is exhausted.

Graceful degradation: if CLIP is unavailable (e.g. the compact/offline profile, or a host with
no torch/open_clip), :func:`subject_evidence` records ``status="unavailable"`` and returns no
floor, so nothing about the exposure changes.

Tile pass: a person who fills only part of a wide frame loses the whole-frame vote to "static
object" (live A7 III, 24 mm, 2026-09-29: a friend walking across a room read static_object 0.51
and the shot was 1/25 s). So the same encoder also reads a fixed set of overlapping square
crops of the meter frame; a crop that is clearly a person (both person prompts together, against
every other prompt plus an "artwork" distractor for posters and paintings) sets the person floor.
The tile pass can only ever add the person floor; it never removes or loosens a floor.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


# --- the class list and its prompts, in ONE place -------------------------
# One representative "a photo of ..." prompt per class. Keep the ordering stable: the CLIP
# text features are cached in this order.
SUBJECT_PROMPTS: dict[str, str] = {
    "bird": "a photo of a bird",
    "person": "a photo of a person posing or standing still",
    "person_in_motion": "a photo of a person running, jumping or playing sport",
    "vehicle": "a photo of a moving car, motorcycle, bicycle or train",
    "pet_or_animal": "a photo of a pet, dog, cat or wild animal",
    "flowing_water": "a photo of a waterfall, river or flowing water",
    "sky_or_landscape": "a photo of a landscape, mountains, fields or sky",
    "static_object": "a photo of a still object or a static scene",
}

# --- class -> minimum shutter (a floor), with a one-line rationale each ----
# `floor_s` is the slowest acceptable shutter for that subject (seconds); ``None`` means the
# subject imposes no floor. These are exact photographic constants, not learned values.
SUBJECT_SHUTTER_FLOORS: dict[str, tuple[float | None, str]] = {
    "bird": (1 / 2000, "a bird in flight beats its wings fast; 1/2000s freezes them"),
    "person_in_motion": (1 / 1000, "running / sport action needs 1/1000s to freeze the body"),
    "vehicle": (1 / 1000, "a moving vehicle needs 1/1000s to render it sharp, not smeared"),
    "pet_or_animal": (1 / 500, "pets and animals move unpredictably; 1/500s catches them crisp"),
    "person": (1 / 125, "a posed person still sways and breathes; 1/125s keeps them sharp"),
    "flowing_water": (None, "flowing water is often shot slow on purpose (silky blur) — no floor"),
    "sky_or_landscape": (None, "a landscape or sky is static — no subject floor "
                               "(the camera-shake floor still applies)"),
    "static_object": (None, "a still object or scene has no subject motion — no floor"),
}

# Softmax probability the top class must clear before its floor is trusted. With 8 classes the
# uniform baseline is 0.125; 0.30 demands a clear winner before we constrain the camera.
CONFIDENCE_THRESHOLD = 0.30

# Every prompted class must have a floor-table entry (and vice versa).
assert set(SUBJECT_PROMPTS) == set(SUBJECT_SHUTTER_FLOORS), \
    "SUBJECT_PROMPTS and SUBJECT_SHUTTER_FLOORS must cover the same classes"

# --- the tile pass: fixed geometry and a conservative person rule ----------
# Square crops (no preprocess centre-crop loss) of side TILE_FRACTION x the short side: a 3 x 2
# grid spanning the frame (3 along the long side) plus one centred crop — 7 tiles, 7 CLIP encodes.
TILE_FRACTION = 0.6
TILE_GRID_LONG, TILE_GRID_SHORT = 3, 2
# Classes whose combined probability is the tile's "person" score (a walker splits between them).
TILE_PERSON_CLASSES = ("person", "person_in_motion")
# Prompts scored only in tiles. A poster, painting or framed photo of a face would otherwise read
# "a photo of a person"; this gives CLIP somewhere else to put it. No floor, never reported as a class.
TILE_DISTRACTOR_PROMPTS: dict[str, str] = {
    "artwork": "a painting, poster, photograph or artwork showing a face",
}
# A tile sets the person floor only when the person score clears TILE_PERSON_THRESHOLD and beats
# the best other prompt (distractor included) by TILE_PERSON_MARGIN. Measured 2026-09-29 on the live
# room (A7 III, 24 mm): tiles containing the walker scored 0.99 / 0.87 (margins +0.98 / +0.74); every
# tile of the same room without him, poster and paintings included, stayed at or below 0.017.
TILE_PERSON_THRESHOLD = 0.60
TILE_PERSON_MARGIN = 0.30
TILE_PERSON_CLASS = "person"  # the floor a tile may add: 1/125 s, tightened by focal as always

assert set(TILE_PERSON_CLASSES) <= set(SUBJECT_PROMPTS)
assert not set(TILE_DISTRACTOR_PROMPTS) & set(SUBJECT_PROMPTS)


@dataclass(frozen=True)
class SubjectResult:
    """What the classifier concluded about a meter frame and the floor it implies."""
    status: str            # "applied" | "no_floor" | "low_confidence" | "unavailable"
    subject_class: str | None
    confidence: float | None
    floor_s: float | None
    reason: str

    def as_evidence(self) -> dict:
        """The `evidence.subject` record stored on every shot."""
        return {"status": self.status, "class": self.subject_class,
                "confidence": (round(self.confidence, 3) if self.confidence is not None else None),
                "floor_s": self.floor_s, "reason": self.reason}


def tile_boxes(width: int, height: int) -> list[tuple[int, int, int, int]]:
    """The fixed tile crops ``(left, top, right, bottom)`` for a ``width`` x ``height`` frame:
    squares of side TILE_FRACTION x the short side on a TILE_GRID_LONG x TILE_GRID_SHORT grid
    whose outer tiles touch the frame edges, then one centred square. Pure and deterministic."""
    if width <= 0 or height <= 0:
        raise ValueError(f"frame size must be positive, got {width}x{height}")
    side = max(1, round(TILE_FRACTION * min(width, height)))
    columns, rows = ((TILE_GRID_LONG, TILE_GRID_SHORT) if width >= height
                     else (TILE_GRID_SHORT, TILE_GRID_LONG))

    def starts(extent: int, count: int) -> list[int]:
        return [round(index * (extent - side) / (count - 1)) for index in range(count)]

    boxes = [(left, top, left + side, top + side)
             for top in starts(height, rows) for left in starts(width, columns)]
    left, top = (width - side) // 2, (height - side) // 2
    boxes.append((left, top, left + side, top + side))
    return boxes


def person_tile(tiles: list[dict]) -> tuple[int, float, float] | None:
    """The first tile that is clearly a person: ``(index, person_score, margin)``, else None.

    Each tile is ``{"probabilities": {class: p}}`` over SUBJECT_PROMPTS plus the distractors. The
    person score sums TILE_PERSON_CLASSES; the margin is that score minus the best other prompt.
    Pure and deterministic — the seam the tests drive with stubbed scores."""
    for index, tile in enumerate(tiles):
        probabilities = tile["probabilities"]
        person = sum(probabilities.get(name, 0.0) for name in TILE_PERSON_CLASSES)
        other = max((p for name, p in probabilities.items() if name not in TILE_PERSON_CLASSES),
                    default=0.0)
        margin = person - other
        if person >= TILE_PERSON_THRESHOLD and margin >= TILE_PERSON_MARGIN:
            return index, person, margin
    return None


def _tile_scores(tiles: list[dict]) -> list[dict]:
    """The per-tile record kept in evidence: box, winning prompt, person score."""
    scores = []
    for index, tile in enumerate(tiles):
        probabilities = tile["probabilities"]
        top = max(probabilities, key=probabilities.get)
        scores.append({"tile": index, "box": [round(float(v), 3) for v in tile["box"]],
                       "top": top, "top_p": round(probabilities[top], 3),
                       "person": round(sum(probabilities.get(name, 0.0)
                                           for name in TILE_PERSON_CLASSES), 3)})
    return scores


def subject_floor(subject_class: str, focal_mm: float | None = None) -> tuple[float | None, str]:
    """Map a subject class (+ the focal length already known to the pipeline) to a shutter floor.

    Returns ``(floor_s or None, rationale)``. A longer lens magnifies subject motion, so for a
    class that already has a floor the reciprocal-focal rule (1/focal) tightens it when that is
    faster. Classes with no floor (water, landscape, static object) stay floor-free regardless of
    focal length — their only shutter concern is camera shake, handled elsewhere.
    """
    if subject_class not in SUBJECT_SHUTTER_FLOORS:
        raise KeyError(f"unknown subject class: {subject_class!r}")
    floor_s, rationale = SUBJECT_SHUTTER_FLOORS[subject_class]
    if floor_s is None:
        return None, rationale
    if focal_mm is not None and math.isfinite(focal_mm) and focal_mm > 0:
        focal_floor = 1.0 / focal_mm
        if focal_floor < floor_s:  # smaller seconds = faster = stricter
            return focal_floor, (f"{rationale}; tightened to 1/{focal_mm:.0f}s "
                                 f"for the {focal_mm:.0f}mm reach")
    return floor_s, rationale


def classify_from_probabilities(probabilities: dict[str, float], focal_mm: float | None = None
                                ) -> SubjectResult:
    """Turn a {class: probability} mapping into a SubjectResult. Pure and deterministic — this is
    the seam the tests drive with a stub, and the real CLIP path funnels through it too."""
    if not probabilities:
        return SubjectResult("unavailable", None, None, None,
                             "classifier returned no scores")
    subject_class, confidence = max(probabilities.items(), key=lambda kv: kv[1])
    if confidence < CONFIDENCE_THRESHOLD:
        return SubjectResult("low_confidence", subject_class, confidence, None,
                             f"top class '{subject_class}' at {confidence:.2f} is below the "
                             f"{CONFIDENCE_THRESHOLD:.2f} confidence threshold; no floor applied")
    floor_s, rationale = subject_floor(subject_class, focal_mm)
    status = "applied" if floor_s is not None else "no_floor"
    return SubjectResult(status, subject_class, confidence, floor_s, rationale)


def subject_evidence(image_path: str, focal_mm: float | None = None, *,
                     motion_intent: str | None = None, classifier=None, tile_classifier=None) -> dict:
    """Classify the meter frame and return the `evidence.subject` record.

    `classifier` is an injectable callable ``(image_path) -> {class: probability}`` — tests pass
    a stub; production uses the real CLIP zero-shot classifier. Any failure of the classifier
    (CLIP not installed, weights missing, unreadable frame) degrades to ``status="unavailable"``
    and no floor, so the shutter is never blocked on subject recognition.

    `tile_classifier` is an injectable callable ``(image_path) -> [{"box": [l, t, r, b] (frame
    fractions), "probabilities": {class: p}}, ...]`` for the tile pass (production: CLIP on
    :func:`tile_boxes`). It runs only when the whole frame set no floor at least as fast as the
    person floor, and only adds that floor (see :func:`person_tile`); its failure keeps the
    whole-frame result and records ``tiles.status="unavailable"``.

    `motion_intent` is the user's stated intent (from ``intent.motion_from_intent``). When it is
    ``"blur"`` (e.g. "light trails", "silky water", "long exposure") the user is *asking* for a
    slow shutter, so the subject is still classified and recorded but its floor is suppressed —
    the stated intent always outranks the classifier (and the tile pass is skipped).

    Besides the original keys (status, class, confidence, floor_s, reason) the record says which
    pass decided (``decided_by``: ``"whole_frame"`` or ``"tile_<index>"``), the whole-frame vote
    (``whole_frame``) and the tile pass (``tiles``: status, and per-tile scores when it ran).
    """
    classify = classifier if classifier is not None else _clip_classify
    try:
        probabilities = classify(image_path)
        result = classify_from_probabilities(probabilities, focal_mm)  # an unknown class is a failure too
    except Exception as exc:  # noqa: BLE001 — any classifier failure must not block the shutter
        result = SubjectResult("unavailable", None, None, None,
                               f"CLIP subject recognition unavailable ({type(exc).__name__}: {exc})")
    if result.status == "unavailable":
        evidence = result.as_evidence()
        evidence.update(decided_by=None, whole_frame=None,
                        tiles={"status": "skipped", "reason": "whole-frame classification unavailable"})
        return evidence
    whole_frame = {"class": result.subject_class,
                   "confidence": (round(result.confidence, 3) if result.confidence is not None else None),
                   "status": result.status}
    decided_by = "whole_frame"
    person_floor, _ = subject_floor(TILE_PERSON_CLASS, focal_mm)
    if motion_intent == "blur":
        tiles = {"status": "skipped", "reason": "the user asked for motion blur; no subject floor applies"}
    elif result.floor_s is not None and result.floor_s <= person_floor:
        tiles = {"status": "skipped",
                 "reason": "the whole frame already set a floor at least as fast as the person floor"}
    else:
        tile_classify = tile_classifier if tile_classifier is not None else _clip_classify_tiles
        try:
            tile_results = tile_classify(image_path)
            found = person_tile(tile_results)
            tiles = {"status": "ran", "count": len(tile_results), "scores": _tile_scores(tile_results)}
        except Exception as exc:  # noqa: BLE001 — the tile pass must never block the shutter either
            tiles = {"status": "unavailable",
                     "reason": f"tile pass unavailable ({type(exc).__name__}: {exc})"}
        else:
            if found is not None:
                index, person, margin = found
                floor_s, rationale = subject_floor(TILE_PERSON_CLASS, focal_mm)
                result = SubjectResult(
                    "applied", TILE_PERSON_CLASS, person, floor_s,
                    f"{rationale}; a person fills tile {index} (person {person:.2f}, margin "
                    f"{margin:+.2f}) though the whole frame read '{whole_frame['class']}'")
                decided_by = f"tile_{index}"
                tiles["person_tile"] = index
    if motion_intent == "blur":
        result = SubjectResult(
            "suppressed", result.subject_class, result.confidence, None,
            f"user_requested_motion_blur: '{result.subject_class}' floor suppressed because the "
            "user asked for motion blur")
    evidence = result.as_evidence()
    evidence.update(decided_by=decided_by, whole_frame=whole_frame, tiles=tiles)
    return evidence


# --- the real CLIP zero-shot classifier (reuses the shipped encoder) -------
_TEXT_FEATURES_CACHE: tuple | None = None  # (features (C,D) np.ndarray, classes list)


def _text_features(model, device):
    """Encode the fixed prompt set once with the shipped CLIP text encoder, then reuse."""
    global _TEXT_FEATURES_CACHE
    if _TEXT_FEATURES_CACHE is None:
        import open_clip
        import torch

        import embed_index
        classes = list(SUBJECT_PROMPTS)
        prompts = [SUBJECT_PROMPTS[name] for name in classes]
        tokenizer = open_clip.get_tokenizer(embed_index.MODEL_NAME)
        with torch.no_grad():
            feats = model.encode_text(tokenizer(prompts).to(device))
            feats = feats / feats.norm(dim=-1, keepdim=True)
        _TEXT_FEATURES_CACHE = (feats.cpu().numpy().astype("float32"), classes)
    return _TEXT_FEATURES_CACHE


def _clip_classify(image_path: str) -> dict[str, float]:
    """Zero-shot classify a frame against SUBJECT_PROMPTS using the same encoder embed_index uses.

    Raises if the ML environment is unavailable — the caller turns that into `unavailable`."""
    import perception_client
    if perception_client.remote():  # split runtime: CLIP runs in the perception role
        return perception_client.classify(image_path)
    import numpy as np

    import embed_index

    model, preprocess, device = embed_index._get_model()
    # Reuse the per-frame cached embedding — taste retrieval embeds the same meter frame in one
    # shot, so this is a cache hit rather than a second CLIP pass.
    image_vec = embed_index.embed_query(image_path, model, preprocess, device)  # (D,), unit
    text_feats, classes = _text_features(model, device)                              # (C,D), unit
    # CLIP's learned logit scale (~100) sharpens the cosine similarities before softmax.
    logits = 100.0 * (text_feats @ image_vec)
    exp = np.exp(logits - logits.max())
    probs = exp / exp.sum()
    return dict(zip(classes, probs.tolist()))


_TILE_TEXT_FEATURES_CACHE: tuple | None = None  # (features (C+K,D), classes + distractors)


def _tile_text_features(model, device):
    """The whole-frame prompt features plus the tile-only distractor prompts, encoded once."""
    global _TILE_TEXT_FEATURES_CACHE
    if _TILE_TEXT_FEATURES_CACHE is None:
        import numpy as np
        import open_clip
        import torch

        import embed_index
        base, classes = _text_features(model, device)
        names = list(TILE_DISTRACTOR_PROMPTS)
        tokenizer = open_clip.get_tokenizer(embed_index.MODEL_NAME)
        with torch.no_grad():
            feats = model.encode_text(tokenizer([TILE_DISTRACTOR_PROMPTS[n] for n in names]).to(device))
            feats = feats / feats.norm(dim=-1, keepdim=True)
        _TILE_TEXT_FEATURES_CACHE = (np.concatenate([base, feats.cpu().numpy().astype("float32")]),
                                     classes + names)
    return _TILE_TEXT_FEATURES_CACHE


# Tiles are downscaled (integer reduce) before cropping so each is at least this many pixels a
# side: CLIP's preprocess resizes to 224 anyway, and a 24 MP frame need not be cropped at full size.
_TILE_WORKING_SIDE = 448


def _clip_classify_tiles(image_path: str) -> list[dict]:
    """Zero-shot classify the :func:`tile_boxes` crops of a frame in one CLIP batch.

    Returns ``[{"box": [left, top, right, bottom] as frame fractions, "probabilities": {class: p}}]``
    over SUBJECT_PROMPTS plus TILE_DISTRACTOR_PROMPTS. Raises if the ML environment is unavailable."""
    import perception_client
    if perception_client.remote():  # split runtime: CLIP runs in the perception role
        return perception_client.classify_tiles(image_path)
    import numpy as np

    import embed_index
    from image_safety import open_rgb

    image = open_rgb(image_path)  # an unreadable frame fails before the encoder loads
    model, preprocess, device = embed_index._get_model()
    width, height = image.size
    side = TILE_FRACTION * min(width, height)
    factor = max(1, int(side // _TILE_WORKING_SIDE))
    if factor > 1:
        image = image.reduce(factor)
    boxes = tile_boxes(*image.size)
    vecs = embed_index._embed_batch(model, preprocess, device, [image.crop(box) for box in boxes])
    text_feats, classes = _tile_text_features(model, device)
    logits = 100.0 * (vecs @ text_feats.T)
    exp = np.exp(logits - logits.max(axis=1, keepdims=True))
    probs = exp / exp.sum(axis=1, keepdims=True)
    reduced_w, reduced_h = image.size
    return [{"box": [box[0] / reduced_w, box[1] / reduced_h, box[2] / reduced_w, box[3] / reduced_h],
             "probabilities": dict(zip(classes, row.tolist()))}
            for box, row in zip(boxes, probs)]
