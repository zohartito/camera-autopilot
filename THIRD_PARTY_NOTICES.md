# Third-Party Notices

Regenerated **2026-11-15** from the installed README environment
(`requirements-test.lock`, Python 3.14.10, macOS arm64) and `npm ci` in `mobile/`.
The tables report installed package metadata, including test and development
packages; pip and the separate audit-tool environment are excluded. Versions
are checked against both lockfiles by `scripts/third_party_inventory.py`.

Regenerate the tables in a clean checkout environment:

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements-test.lock
npm ci --prefix mobile
.venv/bin/python scripts/third_party_inventory.py
```

This inventory covers the source checkout's installed dependencies. It does
not inventory a compiled application, native/system libraries inside wheels,
model weights or datasets. Preserve each dependency's upstream license files
when distributing it. The project itself is **AGPL-3.0-or-later**; see
[`LICENSE`](LICENSE). Third-party components retain their own licenses.

## Installed Python packages (57)

| Name | Version | License metadata |
|---|---|---|
| annotated-doc | 0.0.4 | MIT |
| annotated-types | 0.7.0 | MIT License |
| anyio | 4.14.2 | MIT |
| astral | 3.2 | Apache Software License |
| attrs | 26.1.0 | MIT |
| certifi | 2026.10.18 | Mozilla Public License 2.0 (MPL 2.0) |
| cffi | 2.0.0 | MIT |
| click | 8.4.1 | BSD-3-Clause |
| cryptography | 50.0.0 | Apache-2.0 OR BSD-3-Clause |
| defusedxml | 0.7.1 | Python Software Foundation License |
| fastapi | 0.138.0 | MIT |
| gphoto2 | 2.6.4 | LGPL-3.0-or-later |
| h11 | 0.16.0 | MIT License |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpcore2 | 2.13.0 | BSD-3-Clause |
| httptools | 0.8.0 | MIT |
| httpx | 0.28.1 | BSD License |
| httpx-sse | 0.4.3 | MIT |
| httpx2 | 2.13.0 | BSD-3-Clause |
| idna | 3.18 | BSD-3-Clause |
| iniconfig | 2.3.0 | MIT |
| jsonschema | 4.26.0 | MIT |
| jsonschema-specifications | 2025.9.1 | MIT |
| markdown-it-py | 4.2.0 | MIT License |
| mcp | 2.3.0 | MIT License |
| mcp-types | 2.3.0 | MIT License |
| mdurl | 0.1.2 | MIT License |
| numpy | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| opencv-python | 5.0.0.93 | Apache Software License |
| opentelemetry-api | 1.45.2 | Apache-2.0 |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pillow | 13.0.0 | MIT-CMU |
| pluggy | 1.6.0 | MIT License |
| pycparser | 3.0 | BSD-3-Clause |
| pydantic | 2.13.4 | MIT |
| pydantic_core | 2.46.4 | MIT |
| pydantic-settings | 2.14.2 | MIT |
| Pygments | 2.20.0 | BSD-2-Clause |
| PyJWT | 2.15.0 | MIT |
| pytest | 9.2.0 | MIT |
| python-dotenv | 1.2.2 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| PyYAML | 6.0.3 | MIT License |
| referencing | 0.37.0 | MIT |
| rich | 15.0.0 | MIT License |
| rpds-py | 2026.5.1 | MIT |
| shellingham | 1.5.4 | ISC License (ISCL) |
| sse-starlette | 3.4.5 | BSD-3-Clause |
| starlette | 1.3.1 | BSD-3-Clause |
| truststore | 0.10.4 | MIT |
| typer | 0.26.7 | MIT |
| typing_extensions | 4.15.0 | PSF-2.0 |
| typing-inspection | 0.4.2 | MIT |
| uvicorn | 0.49.0 | BSD-3-Clause |
| uvloop | 0.22.1 | Apache Software License; MIT License |
| watchfiles | 1.2.0 | MIT License |
| websockets | 16.0 | BSD-3-Clause |

## Installed npm packages (99 name/version entries)

99 installed package locations, including CLI dependencies. Repeated copies of the same name/version are listed once.

| Name | Version | License metadata | Use |
|---|---|---|---|
| @capacitor/android | 8.6.2 | MIT | runtime |
| @capacitor/cli | 8.6.2 | MIT | development |
| @capacitor/core | 8.6.2 | MIT | runtime |
| @capacitor/ios | 8.6.2 | MIT | runtime |
| @ionic/cli-framework-output | 2.2.8 | MIT | development |
| @ionic/utils-array | 2.1.6 | MIT | development |
| @ionic/utils-fs | 3.1.7 | MIT | development |
| @ionic/utils-object | 2.1.6 | MIT | development |
| @ionic/utils-process | 2.1.12 | MIT | development |
| @ionic/utils-stream | 3.1.7 | MIT | development |
| @ionic/utils-subprocess | 3.0.1 | MIT | development |
| @ionic/utils-terminal | 2.3.5 | MIT | development |
| @isaacs/fs-minipass | 4.0.1 | ISC | development |
| @isaacs/nonrecursive-glob-match | 1.1.0 | BlueOak-1.0.0 | development |
| @types/fs-extra | 8.1.5 | MIT | development |
| @types/node | 25.9.3 | MIT | development |
| @types/slice-ansi | 4.0.0 | MIT | development |
| @xmldom/xmldom | 0.9.12 | MIT | development |
| ansi-regex | 5.0.1 | MIT | development |
| ansi-styles | 4.3.0 | MIT | development |
| astral-regex | 2.0.0 | MIT | development |
| at-least-node | 1.0.0 | ISC | development |
| balanced-match | 4.0.4 | MIT | development |
| base64-js | 1.5.1 | MIT | development |
| big-integer | 1.6.52 | Unlicense | development |
| bplist-creator | 0.1.0 | MIT | development |
| bplist-parser | 0.3.1 | MIT | development |
| bplist-parser | 0.3.2 | MIT | development |
| brace-expansion | 5.0.13 | MIT | development |
| buffer-crc32 | 0.2.13 | MIT | development |
| chownr | 3.0.0 | BlueOak-1.0.0 | development |
| color-convert | 2.0.1 | MIT | development |
| color-name | 1.1.4 | MIT | development |
| commander | 12.1.0 | MIT | development |
| cross-spawn | 7.0.6 | MIT | development |
| debug | 4.4.3 | MIT | development |
| define-lazy-prop | 2.0.0 | MIT | development |
| elementtree | 0.1.7 | Apache-2.0 | development |
| emoji-regex | 8.0.0 | MIT | development |
| env-paths | 2.2.1 | MIT | development |
| fd-slicer | 1.1.0 | MIT | development |
| fs-extra | 11.4.0 | MIT | development |
| fs-extra | 9.1.0 | MIT | development |
| glob | 14.0.6 | BlueOak-1.0.0 | development |
| graceful-fs | 4.2.11 | ISC | development |
| inherits | 2.0.4 | ISC | development |
| ini | 4.1.3 | ISC | development |
| is-docker | 2.2.1 | MIT | development |
| is-fullwidth-code-point | 3.0.0 | MIT | development |
| is-wsl | 2.2.0 | MIT | development |
| isexe | 2.0.0 | ISC | development |
| jsonfile | 6.2.1 | MIT | development |
| kleur | 3.0.3 | MIT | development |
| kleur | 4.1.5 | MIT | development |
| lru-cache | 11.5.3 | BlueOak-1.0.0 | development |
| minimatch | 11.0.8 | BlueOak-1.0.0 | development |
| minipass | 7.1.3 | BlueOak-1.0.0 | development |
| minizlib | 3.1.0 | MIT | development |
| ms | 2.1.3 | MIT | development |
| native-run | 2.0.3 | MIT | development |
| open | 8.4.2 | MIT | development |
| package-json-from-dist | 1.0.1 | BlueOak-1.0.0 | development |
| path-key | 3.1.1 | MIT | development |
| path-scurry | 3.0.0 | BlueOak-1.0.0 | development |
| pend | 1.2.0 | MIT | development |
| plist | 3.1.1 | MIT | development |
| prompts | 2.4.2 | MIT | development |
| readable-stream | 3.6.2 | MIT | development |
| rimraf | 6.1.4 | BlueOak-1.0.0 | development |
| safe-buffer | 5.2.1 | MIT | development |
| sax | 1.1.4 | ISC | development |
| semver | 7.8.4 | ISC | development |
| shebang-command | 2.0.0 | MIT | development |
| shebang-regex | 3.0.0 | MIT | development |
| signal-exit | 3.0.7 | ISC | development |
| simple-plist | 1.3.1 | MIT | development |
| sisteransi | 1.0.5 | MIT | development |
| slice-ansi | 4.0.0 | MIT | development |
| split2 | 4.2.0 | ISC | development |
| stream-buffers | 2.2.0 | Unlicense | development |
| string-width | 4.2.3 | MIT | development |
| string_decoder | 1.3.0 | MIT | development |
| strip-ansi | 6.0.1 | MIT | development |
| tar | 7.5.22 | BlueOak-1.0.0 | development |
| through2 | 4.0.2 | MIT | development |
| tree-kill | 1.2.2 | MIT | development |
| tslib | 2.8.1 | 0BSD | runtime |
| undici-types | 7.24.6 | MIT | development |
| universalify | 2.0.1 | MIT | development |
| untildify | 4.0.0 | MIT | development |
| util-deprecate | 1.0.2 | MIT | development |
| which | 2.0.2 | ISC | development |
| wrap-ansi | 7.0.0 | MIT | development |
| xcode | 4.0.0 | Apache-2.0 | development |
| xml2js | 0.6.2 | MIT | development |
| xmlbuilder | 11.0.1 | MIT | development |
| xmlbuilder | 15.1.1 | MIT | development |
| yallist | 5.0.0 | BlueOak-1.0.0 | development |
| yauzl | 2.10.0 | MIT | development |

## Optional ML environment (not installed for this snapshot)

`backend/requirements-ml.txt` declares a separate environment. The following
licenses are upstream declarations retained from the previous inventory, not
observations from an installed ML build. Re-inventory any actual ML distribution.

| Component | Declared requirement | Declared license |
|---|---|---|
| opencv-contrib-python | ==5.0.0.93 | Apache-2.0 |
| torch | >=2.15.0 | BSD-3-Clause |
| torchvision | >=0.29.2 | BSD-3-Clause |
| open_clip_torch | unpinned | MIT |

CUDA builds can include additional NVIDIA components; these are outside this
CPU test-environment snapshot. The older `third_party/python-macos-arm64/`
files describe a separate historical runtime, not the current README install.

## Bundled native templates and wrapper

- **Capacitor native projects and template assets:** the iOS project and its
  icons/splashes in `mobile/ios/`, and the Android project and resources in
  `mobile/android/`, derive from the [iOS](https://github.com/ionic-team/capacitor/tree/8.4.2/ios-pods-template)
  and [Android](https://github.com/ionic-team/capacitor/tree/8.4.2/android-template) templates.
  Copyright (c) 2017-present Drifty Co., **MIT**. The original projects used
  Capacitor 8.4.2; their dependencies are now 8.6.2. The complete upstream
  copyright and permission notice is included in
  [`third_party/capacitor/LICENSE`](third_party/capacitor/LICENSE), copied from
  the installed `@capacitor/cli` package. The four installed Capacitor packages
  carry the same MIT notice.
- **iOS CocoaPods:** `Capacitor` and `CapacitorCordova` **8.6.2**, MIT, resolve
  from the installed `@capacitor/ios` package; see
  [`mobile/ios/App/Podfile.lock`](mobile/ios/App/Podfile.lock). No compiled iOS
  framework inventory is claimed here.
- **Gradle wrapper 8.14.3:** `mobile/android/gradlew`, `gradlew.bat` and
  `gradle/wrapper/gradle-wrapper.jar` originate from Gradle, **Apache-2.0**.
  The scripts retain their notices and the JAR includes `META-INF/LICENSE`.
  [`tests/test_gradle_wrapper_provenance.py`](tests/test_gradle_wrapper_provenance.py)
  pins the wrapper and distribution to Gradle's published checksums.

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

## Remaining distribution inventory gaps

- Native `libgphoto2`, system libraries and libraries bundled inside Python
  wheels need artifact-specific versions and license texts for a shipped build.
  The installed `gphoto2` binding reports LGPL-3.0-or-later; `certifi` reports
  MPL-2.0. These licenses and their notices must be retained with those packages.
- Android Gradle/Maven dependencies and Xcode-produced frameworks need an
  inventory from the native build; the npm and CocoaPods metadata above do not
  establish that inventory.
- Model weights, image corpora, photos and proprietary Sony materials are not
  distributed in this source checkout. Any future bundle needs its own source
  and license records.
- Wheel hashes and native-binary provenance are outside the version lock.
  Product-distribution checks remain in
  [`docs/SECURITY_PRIVACY_RELEASE.md`](docs/SECURITY_PRIVACY_RELEASE.md).
