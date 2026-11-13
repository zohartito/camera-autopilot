# Third-Party Notices

Generated for release candidate **2026-09-07** (commit `0f0c832d`) by running
`pip-licenses` over the exact packages installed from
[`requirements-test.lock`](requirements-test.lock) into a clean
`python3.14 -m venv` (**Python 3.14.7, macOS arm64**). Versions and licenses
below are the *observed installed* metadata of that verified test lane, not a
hand-maintained list.

Regenerate:

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements-test.lock pip-licenses
.venv/bin/pip-licenses --format=markdown --order=license
```

These are installed-runtime records, **not an approval to distribute**. Wheel
hashes, native/system libraries, model weights, datasets, and iOS/npm artifacts
are outside this snapshot and are tracked in the gaps section below. A prior
notice inventoried a Python 3.13 runtime (68 packages); this file supersedes it
for the RC test lane and is grounded in the RC's own lockfile.

## License family summary (RC test lane, 52 packages)

| License family | Count | Copyleft? |
| --- | --- | --- |
| MIT / MIT License / MIT-CMU | 29 | No (permissive) |
| BSD (BSD-2/BSD-3 and BSD-containing) | 12 | No (permissive) |
| Apache-2.0 (incl. dual Apache/BSD, Apache/MIT) | 6 | No (permissive) |
| Python Software Foundation / PSF-2.0 | 2 | No (permissive) |
| ISC | 1 | No (permissive) |
| MPL-2.0 (certifi) | 1 | **Weak, file-level** |
| LGPL-3.0-or-later (gphoto2) | 1 | **Weak (library)** |

No **strong copyleft** (GPL/AGPL) and no **Unknown/unlicensed** package appears
in the observed set. Every one of the 52 packages reported license metadata.

### Copyleft flags (review before distribution)

- **gphoto2 2.6.4 — LGPL-3.0-or-later.** This is the `python-gphoto2` binding to
  the native `libgphoto2` library, used only in real-camera mode
  (`CAMERA_ENGINE=real`). LGPL permits use from a permissively-licensed
  application, but redistribution must preserve the LGPL notice and offer the
  library's corresponding source / a means to relink. It is absent from the
  camera-free `mock` test lane at runtime even though it installs into the lock.
- **certifi 2026.6.17 — MPL-2.0.** Weak, file-level copyleft; redistributing the
  unmodified CA bundle is compliant by shipping its license text. Only modified
  MPL files would carry a source-disclosure obligation.

## Full observed inventory (RC test lane)

| Name | Version | License |
|---|---|---|
| astral | 3.2 | Apache Software License |
| opencv-python | 4.13.0.92 | Apache Software License |
| uvloop | 0.22.1 | Apache Software License; MIT License |
| python-multipart | 0.0.32 | Apache-2.0 |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| cryptography | 49.0.0 | Apache-2.0 OR BSD-3-Clause |
| httpx | 0.28.1 | BSD License |
| Pygments | 2.20.0 | BSD-2-Clause |
| click | 8.4.1 | BSD-3-Clause |
| httpcore | 1.0.9 | BSD-3-Clause |
| idna | 3.18 | BSD-3-Clause |
| pycparser | 3.0 | BSD-3-Clause |
| python-dotenv | 1.2.2 | BSD-3-Clause |
| sse-starlette | 3.4.5 | BSD-3-Clause |
| starlette | 1.3.1 | BSD-3-Clause |
| uvicorn | 0.49.0 | BSD-3-Clause |
| websockets | 16.0 | BSD-3-Clause |
| numpy | 2.5.0 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| shellingham | 1.5.4 | ISC License (ISCL) |
| gphoto2 | 2.6.4 | LGPL-3.0-or-later |
| PyJWT | 2.13.0 | MIT |
| annotated-doc | 0.0.4 | MIT |
| anyio | 4.14.0 | MIT |
| attrs | 26.1.0 | MIT |
| cffi | 2.0.0 | MIT |
| fastapi | 0.138.0 | MIT |
| httptools | 0.8.0 | MIT |
| httpx-sse | 0.4.3 | MIT |
| iniconfig | 2.3.0 | MIT |
| jsonschema | 4.26.0 | MIT |
| jsonschema-specifications | 2025.9.1 | MIT |
| pydantic | 2.13.4 | MIT |
| pydantic-settings | 2.14.2 | MIT |
| pydantic_core | 2.46.4 | MIT |
| pytest | 9.1.1 | MIT |
| referencing | 0.37.0 | MIT |
| rpds-py | 2026.5.1 | MIT |
| typer | 0.26.7 | MIT |
| typing-inspection | 0.4.2 | MIT |
| PyYAML | 6.0.3 | MIT License |
| annotated-types | 0.7.0 | MIT License |
| h11 | 0.16.0 | MIT License |
| markdown-it-py | 4.2.0 | MIT License |
| mcp | 1.28.0 | MIT License |
| mdurl | 0.1.2 | MIT License |
| pluggy | 1.6.0 | MIT License |
| rich | 15.0.0 | MIT License |
| watchfiles | 1.2.0 | MIT License |
| pillow | 12.2.0 | MIT-CMU |
| certifi | 2026.6.17 | Mozilla Public License 2.0 (MPL 2.0) |
| typing_extensions | 4.15.0 | PSF-2.0 |
| defusedxml | 0.7.1 | Python Software Foundation License |

## Runtime dependencies declared but NOT in the RC test lane

`backend/requirements.txt` and `backend/requirements-ml.txt` define runtime and
heavy-CV/retrieval dependencies that are installed into separate venvs
(`backend/.venv`, `backend/.venv-ml`) and are **not** part of the observed lock
above. Their licenses below are **upstream-declared, not observed** in this
snapshot and must be re-inventoried from the actual shipped build:

| Component | Declared source | Upstream-declared license | Note |
|---|---|---|---|
| opencv-contrib-python | `backend/requirements-ml.txt` (==5.0.0.93) | Apache-2.0 | CSRT/KCF trackers; supersedes opencv-python in the ML venv |
| torch | `backend/requirements-ml.txt` (>=2.13.0) | BSD-3-Clause | CPU or CUDA build; CUDA wheel adds NVIDIA-licensed components |
| torchvision | `backend/requirements-ml.txt` (>=0.28.0) | BSD-3-Clause | — |
| open_clip_torch | `backend/requirements-ml.txt` | MIT | CLIP taste-retrieval layer |

CUDA builds of torch pull NVIDIA runtime libraries under the NVIDIA CUDA EULA;
inventory those separately for any GPU distribution.

## Model weights

| Weights | Loaded by | Licence (as declared by the publisher) | Source, checked 2026-09-27 |
|---|---|---|---|
| CLIP ViT-B-32, pretrained `laion2b_s34b_b79k` (`open_clip_pytorch_model.bin`, SHA-256 `1bd3c7172de5b207ceac554f5ab5266166f3b9baccc9af5989bc801016d080ad`) from `laion/CLIP-ViT-B-32-laion2B-s34B-b79K` at revision `1a25a446712ba5ee05982a381eed697ef9b435cf` | `backend/embed_index.py` (`open_clip.create_model_and_transforms("ViT-B-32", pretrained=<verified local file>)`), provisioned by `tools/prepare_taste_runtime.py`, pinned in `backend/runtime_resources.py` and `data/clip_encoder.MANIFEST.json` | **MIT** (model card metadata `license: mit`, same at the pinned revision and at `main`) | [model card](https://huggingface.co/laion/CLIP-ViT-B-32-laion2B-s34B-b79K), [card at the pinned revision](https://huggingface.co/laion/CLIP-ViT-B-32-laion2B-s34B-b79K/blob/1a25a446712ba5ee05982a381eed697ef9b435cf/README.md) |

The MIT tag is the licence on the weights file. It does not settle every question
for a proprietary product:

- The same model card's "Out-of-Scope Use" section states that **any deployed use
  case of the model, commercial or not, is currently out of scope**, and that
  surveillance and facial-recognition uses are always out of scope. That is the
  publisher's use guidance rather than a licence term, but a shipped product is a
  deployed use, so this needs an owner/legal decision before distribution.
- The weights were trained on LAION-2B, a web-scraped image-text set whose images
  remain under their owners' copyrights. Whether that affects use of the trained
  weights is a legal question this file does not answer.
- The weights are not redistributed by this project today
  (`data/clip_encoder.MANIFEST.json`: "Local development artifact only"). Bundling
  them in a build would need the MIT notice shipped with them.

## Native, camera, and proprietary components

The native `libgphoto2` library (LGPL) behind the `gphoto2` binding, the Sony
Camera Remote Command material, PMCA/OpenMemories tooling, and Sony firmware /
proprietary camera APIs carry redistribution and legal-review obligations that
are **not resolved by this file**. See the checklist under **"Distribution,
intellectual property, and operations"** in
[`docs/SECURITY_PRIVACY_RELEASE.md`](docs/SECURITY_PRIVACY_RELEASE.md) — every box
there must be satisfied before any public-release claim.

## Known inventory gaps

- The project itself is licensed under the GNU Affero General Public License,
  version 3 or later (AGPL-3.0-or-later): see [`LICENSE`](LICENSE). That licence
  covers this repository's own work only; third-party components keep the
  licences recorded here.
- npm/Capacitor transitive dependencies and Xcode-produced iOS frameworks are
  not flattened here; they need a build-specific inventory.
- Reproducible wheel hashes and native-binary provenance remain unverified; the
  lock excludes wheel hashes by design.
- The native `libgphoto2` build's exact version and license text must be
  captured from the shipped binary before distribution.
- The CLIP weights licence is recorded under **Model weights** above, with its
  deployed-use caveat still open. Datasets, image corpora, and EXIF/test media
  need separate source and redistribution-rights records.
- Sony command-package rights and patent/freedom-to-operate questions require
  qualified legal review.

Before release, replace every declared-not-observed and native/proprietary row
with an artifact-backed determination, bundle the required notices and license
texts, and link the resulting inventory from the product's legal/privacy screen.
