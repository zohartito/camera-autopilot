"""Turn a plain-language intent into a safe exposure target and a visible mood card.

v0 is a tiny keyword map so the autopilot works with no LLM in the loop. Later, swap these for a
Claude call that interprets arbitrary creative intent ("make it feel like a faded film still").
"""

from __future__ import annotations

import re

from mood_preview import variant_recipe_from_values

# A natural midtone; the autopilot aims here unless the intent pushes brighter/darker.
NEUTRAL_TARGET = 112.0


# Base variant data. The ``recipe`` display string is NOT stored here: it is computed from the
# numeric fields below (see ``MOOD_VARIANTS``) so what a user reads and what capture applies share
# one source of truth and cannot drift.
_MOOD_VARIANTS_BASE = (
    {
        "id": "natural",
        "name": "Natural",
        "intent": "natural balanced photo",
        "target_brightness": NEUTRAL_TARGET,
        "white_balance": None,
        "css_filter": "none",
    },
    {
        "id": "dreamy_golden_hour",
        "name": "Dreamy Golden Hour",
        "intent": "dreamy golden-hour portrait",
        "target_brightness": 104.0,
        "white_balance": "warm",
        "css_filter": "brightness(1.08) contrast(.92) saturate(1.24) sepia(.18)",
    },
    {
        "id": "moody_noir",
        "name": "Moody Noir",
        "intent": "moody noir film still",
        "target_brightness": 82.0,
        "white_balance": None,
        "css_filter": "brightness(.68) contrast(1.28) saturate(.72)",
    },
    {
        "id": "cool_blue",
        "name": "Cool Blue",
        "intent": "cool blue winter morning",
        "target_brightness": 108.0,
        "white_balance": "cool",
        "css_filter": "brightness(.92) contrast(1.04) saturate(.96) hue-rotate(180deg)",
    },
)

# The recipe string is derived from each variant's own numeric fields, so display and application
# are the same numbers. Preserves the historic format ("brightness target N / <wb> WB").
MOOD_VARIANTS = tuple(
    {**variant, "recipe": variant_recipe_from_values(variant)}
    for variant in _MOOD_VARIANTS_BASE
)


def _has_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def white_balance_from_intent(intent: str | None) -> str | None:
    """Pick the safe global WB direction implied by the vibe, if there is one."""
    if not intent:
        return None
    text = intent.lower()
    if _has_any(text, ("golden", "warm", "sunset", "cozy", "candle", "candlelit", "nostalgic")):
        return "warm"
    if _has_any(text, ("cool", "cold", "blue", "winter", "icy")):
        return "cool"
    return None


def feel_from_intent(intent: str | None) -> str:
    """One short phrase describing what the autopilot did, in the user's own framing."""
    if not intent:
        return "nailed the exposure for you"
    text = _without_long_exposure_subjects(intent.lower())
    if _has_any(text, ("dreamy", "golden", "warm glow", "sunset")):
        return "kept it dreamy and warm"
    if _has_any(text, ("cold", "cool", "blue", "winter", "lonely", "quiet")):
        return "kept it cool and quiet"
    if any(word in text for word in ("moody", "dark", "dramatic", "noir")):
        return "kept it moody"
    if any(word in text for word in ("bright", "airy", "light", "clean")):
        return "kept it bright and clean"
    if any(word in text for word in ("sharp", "freeze", "action", "fast")):
        return "prioritized a faster exposure for motion"
    if any(word in text for word in ("blur", "bokeh", "background", "creamy")):
        return "requested subject separation"
    if long_exposure_subject_from_intent(intent):
        return "planned one long exposure for the movement"
    return "matched what you asked for"


def target_from_intent(intent: str | None) -> float:
    """Map the intent to a mean-luminance target for the auto-exposure loop."""
    if not intent:
        return NEUTRAL_TARGET
    text = _without_long_exposure_subjects(intent.lower())
    if _has_any(text, ("low-key", "shadowy", "thriller", "tense")):
        return 82.0
    if any(word in text for word in ("moody", "dark", "dramatic", "noir")):
        return 90.0
    if _has_any(text, ("golden", "dreamy", "cozy", "candle", "candlelit", "nostalgic")):
        return 104.0
    if _has_any(text, ("cold", "cool", "blue", "winter", "lonely", "quiet")):
        return 108.0
    if any(word in text for word in ("bright", "airy", "light", "clean")):
        return 140.0
    if any(word in text for word in ("sharp", "freeze", "action", "fast")):
        return 124.0
    return NEUTRAL_TARGET


def _mood_name(text: str | None, white_balance: str | None) -> str:
    if not text:
        return "Natural"
    if _has_any(text, ("natural", "neutral", "balanced")):
        return "Natural"
    if _has_any(text, ("dreamy", "golden", "warm glow", "sunset")):
        return "Dreamy Golden Hour"
    if _has_any(text, ("cool", "cold", "blue", "winter", "icy")):
        return "Cool Blue"
    if _has_any(text, ("noir", "low-key", "shadowy", "thriller", "tense")):
        return "Moody Noir"
    if _has_any(text, ("moody", "dark", "dramatic")):
        return "Dark Dramatic"
    if _has_any(text, ("bright", "airy", "high-key", "clean")):
        return "Bright Airy"
    if _has_any(text, ("freeze", "action", "sharp", "fast")):
        return "Crisp Action"
    if _has_any(text, ("blur", "bokeh", "creamy", "background")):
        return "Soft Portrait"
    if white_balance == "warm":
        return "Warm"
    if white_balance == "cool":
        return "Cool"
    return "Custom Vibe"


def _lighting_for_mood(name: str) -> str:
    if name == "Dreamy Golden Hour":
        return "soft, warm light with gentle highlight protection"
    if name == "Cool Blue":
        return "cooler ambient light without forcing the scene dark"
    if name in {"Moody Noir", "Dark Dramatic"}:
        return "low-key light that protects shadows and avoids a flat bright read"
    if name == "Bright Airy":
        return "high-key light that keeps the frame open and clean"
    if name == "Crisp Action":
        return "brighter exposure support for a faster, sharper capture"
    if name == "Soft Portrait":
        return "balanced light with subject separation in mind"
    return "natural light balanced for the scene"


def _color_for_mood(name: str, white_balance: str | None) -> str:
    if white_balance == "warm":
        return "warm amber highlights and skin-friendly warmth"
    if white_balance == "cool":
        return "cool blue shadows and a calmer color temperature"
    if name in {"Moody Noir", "Dark Dramatic"}:
        return "controlled contrast with restrained color"
    if name == "Bright Airy":
        return "clean whites and soft neutral color"
    return "natural color without a strong temperature push"


def _camera_for_mood(name: str) -> str:
    if name == "Crisp Action":
        return "favor a steadier, faster handheld-safe exposure"
    if name == "Soft Portrait":
        return "favor subject separation while leaving native autofocus in charge"
    if name == "Cool Blue":
        return "keep exposure natural while nudging color cooler"
    if name in {"Moody Noir", "Dark Dramatic"}:
        return "hold the exposure darker while leaving focus to native autofocus"
    return "request exposure and white-balance choices while leaving native autofocus in charge"


def _exposure_bias(target_brightness: float) -> str:
    if target_brightness < 100:
        return "under"
    if target_brightness > 128:
        return "over"
    return "neutral"


def mood_card_from_intent(
    intent: str | None,
    target_brightness: float,
    white_balance: str | None,
) -> dict:
    """Build the visible card the app can show after a spoken vibe is heard."""
    heard = intent.strip() if isinstance(intent, str) and intent.strip() else None
    text = heard.lower() if heard else None
    name = _mood_name(text, white_balance)
    return {
        "heard": heard,
        "name": name,
        "lighting": _lighting_for_mood(name),
        "color": _color_for_mood(name, white_balance),
        "camera": _camera_for_mood(name),
        "settings": {
            "brightness_target": int(round(target_brightness)),
            "white_balance": white_balance or "auto",
            "exposure_bias": _exposure_bias(target_brightness),
            "focus": "native autofocus",
        },
    }


def mood_variants_from_intent(intent: str | None) -> dict:
    """Return the stable same-scene mood variants for a spoken vibe."""
    heard = intent.strip() if isinstance(intent, str) and intent.strip() else None
    requested_mood = mood_card_from_intent(
        heard,
        target_from_intent(heard),
        white_balance_from_intent(heard),
    )["name"]
    selected = "natural"
    variants = []

    for variant in MOOD_VARIANTS:
        is_requested = variant["name"] == requested_mood
        if is_requested:
            selected = variant["id"]

        variant_intent = heard if is_requested and heard else variant["intent"]
        mood_intent = None if variant["id"] == "natural" else variant_intent
        mood = mood_card_from_intent(
            mood_intent,
            float(variant["target_brightness"]),
            variant["white_balance"],
        )
        variants.append(
            {
                "id": variant["id"],
                "name": variant["name"],
                "intent": variant_intent,
                "mood": mood,
                "settings": mood["settings"],
                "preview": {
                    "css_filter": variant["css_filter"],
                    "recipe": variant["recipe"],
                    "approximate": True,
                    "baked_into_capture": False,
                    "note": "Illustrative preview; capture applies target brightness and white balance only.",
                },
            }
        )

    return {"selected": selected, "variants": variants}


# Requests that need ONE long exposure, in priority order (the first subject that matches wins).
_LONG_EXPOSURE_SUBJECTS = (
    ("light_trails", ("light trail", "trails of light", "light streak", "car trails", "traffic trails")),
    ("silky_water", ("silky water", "smooth water", "milky water")),  # never bare "silky" (skin, fabric)
    ("motion_blur", ("motion blur", "blurred lights", "blur the lights", "blur of lights")),
    ("long_exposure", ("long exposure", "long-exposure", "slow shutter")),
)


def _phrase_pattern(phrase: str) -> re.Pattern:
    """Whole words only ("flight trails" and "highlight streaks" are not light trails), a space or
    hyphen between words ("light-trails"), and a plural ending ("long exposures")."""
    body = r"[\s-]+".join(re.escape(word) for word in phrase.split())
    return re.compile(rf"(?<![a-z']){body}(?:s|es)?(?![a-z'])")


_SUBJECT_PATTERNS = tuple((subject, tuple(_phrase_pattern(phrase) for phrase in words))
                          for subject, words in _LONG_EXPOSURE_SUBJECTS)


def _without_long_exposure_subjects(text: str) -> str:
    """"light trails" / "blur the lights" name a long-exposure subject, not a bright look."""
    for _subject, patterns in _SUBJECT_PATTERNS:
        for pattern in patterns:
            text = pattern.sub(" ", text)
    return text


# "silky / smooth / milky" asks for a long exposure only within a few words of water; alone it
# means skin, hair or fabric ("silky hair"), which must stay an ordinary photo.
_SMOOTHING_WORDS = ("silky", "smooth", "milky")
_WATER_NOUNS = ("water", "waters", "river", "rivers", "stream", "streams", "creek", "creeks",
                "waterfall", "waterfalls", "falls", "cascade", "cascades", "sea", "seas", "ocean",
                "oceans", "waves", "lake", "lakes", "tide", "tides", "surf", "rapids", "ripples",
                "brook", "brooks", "pond", "bay", "fountain", "current", "currents")
_WATER_WINDOW_WORDS = 3
# A smoothing word attached to one of these describes the thing, not the water ("silky skin by water").
_NOT_WATER_NOUNS = ("skin", "hair", "fabric", "dress", "silk", "cloth", "shirt", "sheets", "gown",
                    "skirt", "scarf", "fur", "coat", "texture", "face", "complexion", "legs", "voice",
                    "jazz", "finish", "material", "feel", "touch")
_ATTACHED_WORDS = 2
_WATER = "|".join(_WATER_NOUNS)
# Bounded constructions whose water word sits further than the window: "make the water look soft
# and silky", "smooth out the choppy sea".
_WATER_SMOOTHING_PATTERNS = (
    re.compile(rf"\b(?:make|turn|render)\b(?:\W+[a-z']+){{0,2}}?\W+(?:{_WATER})\b(?:\W+[a-z']+){{0,4}}?"
               r"\W+(?:silky|smooth|milky)\b"),
    re.compile(rf"\bsmooth\s+out\b(?:\W+[a-z']+){{0,3}}?\W+(?:{_WATER})\b"),
)


# A negation earlier in the same clause cancels a long-exposure phrase ("freeze it, no motion
# blur", "avoid a long exposure"). A clause ends at , ; . ! ? or "but", so "not too bright, light
# trails" still asks for trails. When in doubt this falls back to the ordinary (capped) route.
_NEGATIONS = ("no", "not", "without", "avoid", "avoiding", "don't", "dont", "never", "nor",
              "stop", "prevent", "isn't", "shouldn't", "won't", "can't", "cant", "cannot",
              # Asking to remove or reduce blur is the inverse of asking for it ("remove motion blur",
              # "get rid of the light trails"; "no more" is covered by "no").
              "remove", "removing", "eliminate", "eliminating", "reduce", "reducing",
              "minimize", "minimise", "minimizing", "minimising", "rid")


_CLAUSE_BREAK = re.compile(r"[,;.!?]|\bbut\b")


def _clauses(text: str) -> list[str]:
    return _CLAUSE_BREAK.split(text)


def _negated_before(clause: str, position: int) -> bool:
    return any(word in _NEGATIONS for word in re.findall(r"[a-z']+", clause[:position]))


# A verdict after the phrase declines it too ("light trails are not desired", "motion blur? not
# wanted"). Only a negated want/need verdict counts, so "light trails, not a sharp car" still asks.
_TRAILING_VERDICT = re.compile(
    r"(?<![a-z'])(?:not|never|no longer|isn't|aren't|wasn't|weren't|don't|doesn't|won't)\s+"
    r"(?:[a-z']+\s+)?(?:desired|wanted|needed|required|necessary|welcome|want|need)(?![a-z'])")


def _verdict_follows(text: str, position: int) -> bool:
    """A negated want/need verdict later in the same clause ("light trails are not desired"), or
    opening the very next clause ("light trails? not wanted"). A later clause about something else
    ("light trails, portraits are not wanted") does not count."""
    breaks = list(_CLAUSE_BREAK.finditer(text, position))
    end = breaks[0].start() if breaks else len(text)
    if _TRAILING_VERDICT.search(text, position, end):
        return True
    if breaks:
        following = breaks[1].start() if len(breaks) > 1 else len(text)
        return _TRAILING_VERDICT.match(text[breaks[0].end():following].lstrip()) is not None
    return False


def _affirmed_phrase(text: str, patterns: tuple[re.Pattern, ...]) -> bool:
    for pattern in patterns:
        for match in pattern.finditer(text):
            boundaries = [end.end() for end in _CLAUSE_BREAK.finditer(text, 0, match.start())]
            clause_start = boundaries[-1] if boundaries else 0
            if _negated_before(text[clause_start:], match.start() - clause_start):
                continue
            if _verdict_follows(text, match.end()):
                continue
            return True
    return False


def _declines_the_technique(text: str) -> bool:
    """"smooth water without a long exposure": an explicitly declined long exposure, slow shutter
    or motion blur anywhere in the request declines the long route as a whole."""
    return any(not _affirmed_phrase(text, patterns) and any(pattern.search(text) for pattern in patterns)
               for subject, patterns in _SUBJECT_PATTERNS if subject in ("long_exposure", "motion_blur"))


def _smoothed_water(text: str) -> bool:
    clause_starts = [0] + [match.end() for match in _CLAUSE_BREAK.finditer(text)]
    for clause_start, clause in zip(clause_starts, _clauses(text)):
        for pattern in _WATER_SMOOTHING_PATTERNS:
            match = pattern.search(clause)
            following = re.findall(r"[a-z']+", clause[match.end():])[:_ATTACHED_WORDS] if match else []
            if (match and not _negated_before(clause, match.start())
                    and not any(noun in _NOT_WATER_NOUNS for noun in following)
                    and not _verdict_follows(text, clause_start + match.end())):
                return True
        tokens = list(re.finditer(r"[a-z']+", clause))
        words = [token.group() for token in tokens]
        for index, word in enumerate(words):
            if word not in _SMOOTHING_WORDS or any(earlier in _NEGATIONS for earlier in words[:index]):
                continue
            attached = words[max(0, index - _ATTACHED_WORDS):index + _ATTACHED_WORDS + 1]
            if any(noun in _NOT_WATER_NOUNS for noun in attached):
                continue  # "silky skin", "hair so silky": the word describes skin, hair or fabric
            if not any(near in _WATER_NOUNS for near in
                       words[max(0, index - _WATER_WINDOW_WORDS):index + _WATER_WINDOW_WORDS + 1]):
                continue
            if _verdict_follows(text, clause_start + tokens[index].start()):
                continue  # "smooth water is not wanted", "silky river? not wanted"
            return True
    return False


def long_exposure_subject_from_intent(intent: str | None) -> str | None:
    """Which long single exposure the words ask for (light_trails, silky_water, motion_blur,
    long_exposure), or None. A request, never proof that the photograph shows it; a negated
    phrase ("no motion blur") asks for nothing."""
    text = (intent or "").lower().replace("\u2019", "'")  # a curly apostrophe ("can’t") is one word
    if _declines_the_technique(text):
        return None
    for subject, patterns in _SUBJECT_PATTERNS:
        if _affirmed_phrase(text, patterns) or (subject == "silky_water" and _smoothed_water(text)):
            return subject
    return None


# A person moving at walking pace ("my friend walking across the room", "kids playing"). Live on
# 2026-09-29 that request shot 1/25 s on the A7 III and blurred the walker: nothing in the words
# asked for a faster shutter. Whole words only, and never a hyphenated compound ("walk-in closet",
# "sidewalk", "walkway" do not match).
_WALKING_WORDS = (r"walk(?:s|ing|ed)?", r"stroll(?:s|ing|ed)?", r"danc(?:e|es|ing|ed)")
_WALKING = re.compile(rf"(?<![a-z'-])(?:{'|'.join(_WALKING_WORDS)})(?![a-z'-])")
_PLAYING = re.compile(r"(?<![a-z'-])(?:kids?|children|child|toddlers?|people|boys?|girls?)\s+"
                      r"(?:(?:are|were|is|was)\s+)?play(?:s|ing)?(?![a-z'-])")
# The walking word names a thing, not a movement ("walking distance", "dance floor"). Only nouns a
# person does not walk or dance: "walking trails" / "walking paths" may be people walking them, and a
# missed walker (a blurred photo) costs more than an unneeded 1/250 s floor (a higher ISO).
_WALKING_COMPOUND_NOUNS = ("distance", "stick", "sticks", "cane", "canes", "shoe", "shoes", "boot",
                           "boots", "closet", "closets", "floor", "floors", "hall", "halls", "studio",
                           "studios", "frame", "frames")
# Negation is scoped to the walking itself. A subject that is nobody ("nobody walking", "none of the
# people are walking", "no one in the background is walking") cancels it anywhere earlier in the
# clause; any other negation must sit right before the walking word ("aren't walking", "don't walk",
# "no kids playing"). So "avoid motion blur of my friend walking" and "no flash while my friend is
# walking" still ask for a sharp walker.
_NOBODY_SUBJECTS = frozenset({"nobody", "none", "noone"})
_WALKING_NEGATIONS = frozenset(_NEGATIONS) | _NOBODY_SUBJECTS | {
    "arent", "wasnt", "werent", "isnt", "doesnt", "didnt", "havent", "hasnt", "hadnt", "wouldnt",
    "couldnt"}  # plus every "…n't" contraction ("didn't walk", "haven't walked")
_NEGATION_REACH_WORDS = 2
# Forms that double as a noun in a compound ("walking stick", "dance floor"); "walks" and "walked"
# are always the verb.
_NOUN_FORMS = frozenset({"walk", "walking", "dance", "dancing", "stroll"})
# Faster than walking: the existing freeze class (1/500 s).
_FREEZE_WORDS = ("freeze", "action", "running", "moving", "sports", "fast", "jogging", "jogger")


def _asks_for_walking_motion(text: str) -> bool:
    """A person (or people) moving at walking pace, affirmed in its clause ("nobody walking" is not)."""
    for pattern in (_WALKING, _PLAYING):
        for match in pattern.finditer(text):
            following = re.match(r"\s+([a-z']+)", text[match.end():])  # same sentence, next word
            if (pattern is _WALKING and match.group() in _NOUN_FORMS and following
                    and following.group(1) in _WALKING_COMPOUND_NOUNS):
                continue
            boundaries = [end.end() for end in _CLAUSE_BREAK.finditer(text, 0, match.start())]
            clause_start = boundaries[-1] if boundaries else 0
            before = re.findall(r"[a-z']+", text[clause_start:match.start()])
            bare = [word.removesuffix("'s") for word in before]  # "nobody's walking", "no one's"
            nobody = any(word in _NOBODY_SUBJECTS or (word == "no" and after == "one")
                         for word, after in zip(bare, bare[1:] + [""]))
            if nobody or any(word in _WALKING_NEGATIONS or word.endswith("n't")
                             for word in before[-_NEGATION_REACH_WORDS:]):
                continue
            return True
    return False


def motion_from_intent(intent: str | None) -> str:
    """Requested treatment, never proof that a captured subject is sharp or blurred.

    ``"blur"`` (a long exposure was asked for), ``"freeze"`` (fast action), ``"walk"`` (a person
    moving at walking pace) or ``"unspecified"``."""
    text = (intent or "").lower().replace("\u2019", "'")
    # An explicit blur request wins over the subject's words ("motion blur of moving lights").
    if long_exposure_subject_from_intent(text):
        return "blur"
    if _has_any(text, _FREEZE_WORDS):
        return "freeze"
    if _asks_for_walking_motion(text):
        return "walk"
    return "unspecified"


# The shutter floor (slowest acceptable shutter, seconds) each stated motion asks for. Exact
# photographic constants: 1/500 s freezes running or sport; 1/250 s keeps a walking person sharp
# at ordinary focal lengths. "blur" and "unspecified" ask for none.
MOTION_INTENT_FLOORS_S: dict[str, float] = {"freeze": 1 / 500, "walk": 1 / 250}


def motion_intent_floor_s(motion_intent: str | None) -> float | None:
    """The shutter floor a stated motion intent sets, or None."""
    return MOTION_INTENT_FLOORS_S.get(motion_intent or "")


def variant_recipe(variant_id: str) -> dict:
    """Copy a known recipe; unknown IDs must not silently capture a different look."""
    for recipe in MOOD_VARIANTS:
        if recipe["id"] == variant_id:
            return dict(recipe)
    raise ValueError(f"Unknown mood variant: {variant_id}")
