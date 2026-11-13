# Taste-corpus sources — where to get excellent photos + their settings (2026-06-22)

Decision record for the **taste index**: pairs of (excellent photo → the camera settings the
photographer used). We read only the **numeric EXIF settings** (ISO / f-number / shutter / focal —
uncopyrightable facts) into a CSV; **we never store or redistribute photos.** Goal: push the corpus
past the **~234 k** Wikimedia Quality-Images ceiling toward **~450 k**.

> Verified by a 10-agent research workflow (survey → adversarial verify), live-tested against
> current 2026 docs/APIs — not memory. The landscape shifted under us, so memory was untrustworthy.

## Current status (verified 2026-07-27)

The flat Wikimedia Quality Images lane is complete: **233,788 harvested rows** produced
**233,785 validated embeddings**. The 697,615,942-byte v1 artifact matches its manifest at
SHA-256 `949d4bf79d42345df73a5c4eee886a4b4a6383a06e26b9abd20bb0d7a9531128`.

The current corpus is a shippable baseline. Expansion toward 450 k is now optional and should be
triggered by a measured retrieval-quality gap, not by the old row-count target alone. Validation,
performance, transfer, and unattended-runner commands live in
[`TASTE_INDEX_OPERATIONS.md`](TASTE_INDEX_OPERATIONS.md).

## What changed: Flickr paywalled its free API keys
As of 2026, requesting a **new** Flickr API key requires a paid **Flickr PRO** subscription (~$72/yr).
`flickr.photos.getExif` still returns full EXIF, but the free on-ramp is gone. **`harvest_flickr.py`
is preserved** (the `_b`-size fix landed) but **parked** — do not architect new free harvesting on it.
A pre-paywall grandfathered key would still work, but we don't rely on that.

## ✅ Decision: Wikimedia Commons *beyond* Quality Images
The best second source is the one we already use — just more of it. **Verdict: STRONG, live-tested.**

| Why it wins | Detail |
|---|---|
| **Free** | No key, no paywall — sidesteps the whole Flickr/Unsplash account mess. |
| **Commercial-safe** ⭐ | EXIF = uncopyrightable facts; Commons file metadata is CC0/PD. **Decisive, because camera-autopilot is meant to be sold.** No license clause restricts a model trained on it. |
| **Already built** | Identical pipeline to the QI harvester (`action=query&prop=imageinfo`). Reuse code; just swap the seed category. |
| **Big enough** | 143 M files; **~90 % all-four hit rate** on curated photo categories; **800 k–2 M** harvestable beyond the 234 k QI set. Clears 450 k from Commons alone. |
| **Curated tiers** | Featured (~15 k usable, pristine) ▸ Valued (~30–40 k) ▸ broad `Photographs_by_camera` (the volume). Lets us quality-weight. |

Live test (2026-06-22): a real Commons file returned `ISOSpeedRatings`, `FNumber`, `ExposureTime`,
`FocalLength` all present as rationals. Coverage measured ~92 % all-four on Featured/Valued samples.

### Implementation plan — `harvest_wikimedia_broad.py`
Seed from Featured + Valued + the broad `Photographs_by_camera` tree instead of only Quality Images.
Gotchas the verify pass surfaced:
- **Recursive category traversal** — Featured/Valued/`Photographs_by_camera` are *container* categories
  (mostly sub-cats); flat enumeration undershoots badly. Use PetScan/Quarry seeds or recurse.
- **`iiprop=metadata` only** — NOT `extmetadata` (docs flag it as expensive); raw `metadata` carries EXIF and is cheap.
- **`generator=categorymembers` piped into `imageinfo`** — candidate-listing + EXIF-fetch in one paginated stream (≤500/page).
- **Etiquette** — meaningful User-Agent (clientname/version + contact), `maxlag` param, serial requests, exponential backoff on `ratelimited`.
- **Filter to all-four-present** — drop the ~5–10 % with no EXIF (scans, screenshots, historical restorations).
- **Dedupe against the existing 234 k** by file title / SHA1 before counting toward 450 k (tiers overlap).

## Runners-up
- **YFCC100M EXIF pack** (Flickr-CC *without* the paid API) — 🥈 **backup**. ~10–30 M complete quadruples,
  one free bulk `.bz2` (AWS `multimedia-commons` mirror; Webscope is down). But quality is *mixed* (not
  curated) and per-photo licenses include CC-NC. Hold in reserve; Wikimedia-broad makes it unnecessary.

## ⚠️ Trap: Unsplash
- **Full dataset** — 7.4 M photos, **84.6 % EXIF-complete** (measured), fast bulk download… but
  **NON-COMMERCIAL-only**, and its terms **bar commercial use of any model trained on it.** That would
  poison a sellable product. **Rejected.**
- **API / Lite** — commercial-OK but rate-limited (5000/hr → weeks for 200 k) and <50 % EXIF yield. Weak.

## Rejected (verified, so we don't re-investigate)
- **No camera EXIF at all:** Pexels, Pixabay, Openverse, Smithsonian, Europeana, all museum/GLAM APIs,
  Google Open Images, the Internet Archive "Flickr metadata 2016" dump.
- **Dead / no API:** 500px (API killed 2018), EyeEm (bankrupt, ~Jan 2026), Behance (adobe.io limbo),
  Reddit (strips EXIF + paywalled), GuruShots / ViewBug (no API).
- **Paywalled:** SmugMug (subscriber-only keys), Flickr API (now PRO-only).
- **Too small / NC:** RAISE (~8 k, NC), MIT-Adobe FiveK (~5 k, 50 GB), DPReview (scrape + proprietary).
- **Unverified yield:** re-LAION-2B EXIF column (could not confirm the four-field hit rate).

## Bottom line
233,785 validated QI embeddings already beat Arsenal's ~200 k reference corpus. The free,
commercial-safe path to 450 k is **more Wikimedia**, via the broad harvester we already trust.
That expansion is optional, not a blocker for the current Mac-hosted product.
