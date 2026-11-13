# Web and mobile verification — 2026-09-05

Scope: completed local implementation and explicit evidence; no physical-phone or beginner-study acceptance claim.

## Built

- Camera selector sends exact model and port; selection persists in the tab. Selected recipe sends the original intent and `variant_id`. Photo capture discloses the two-exposure ordinary / four-exposure refined upper bounds agreed with the engine owner.
- Result and review pages show structured decision cards and explicit pending status. Mood tiles are labeled previews, reuse one authenticated image blob, and do not claim a saved edit.
- Gallery lazily fetches bounded thumbnails, loads full images on request, previews exposure/crop on a thumbnail canvas, saves a full-size edited JPEG as a new image, downloads/shares saved bytes, and accepts thumbs plus shot-specific comments.
- Local delete-one/delete-all and retention controls. Delete-all was not exercised against the working gallery. Retention was not changed to auto-delete. Camera-card and exported copies remain outside local deletion scope.
- Real-engine computational-session controls for HDR, long-exposure blending, crowd removal, night stacking, and timelapse frames, with create/run/progress/cancel/resume/history. Panorama and focus bracketing remain explicitly unavailable in this path.
- Native Capacitor plugin uses Photos add-only permission and UIKit sharing, bounds JPEG input before decoding, cleans share temporary files, and reads native safe-area insets. No new third-party plugin dependency.
- SceneDelegate and scene manifest fix an actual iOS 27 SDK launch failure that an unsigned build alone did not reveal.

## Verified

- `pytest -q tests/test_photo_edits.py tests/test_web_voice_ui.py`: 11 passed. Editing checks source preservation, linear-light EV output, crop size, bad input/path/symlink/format rejection, displayed EXIF orientation, private EXIF removal, and private output permissions.
- All inline scripts and `client.js` pass Node syntax checks.
- Playwright CLI drove the actual local mock backend at a 430 × 932 viewport: capture, exact `cool_blue` reshoot while retaining original intent, three pending cards, review, +1 EV / 75% crop, comment, download, full-image viewing, and deletion of the exact generated edit.
- The edit initially disappeared because gallery enumeration accepted only `shot_*` sidecars. Parent repaired enumeration; the edited image then appeared in review and accepted comments/download.
- Original browser download matched saved bytes: SHA-256 `658d05fa578f99ef63b7eadf876a18dd119cd27b708b8bdb7905f62eeaa8a1ec`, 13,231 bytes.
- Generated edit was 540 × 480. Exact edit JPEG, sidecar and gallery card were deleted; its original source remained. Historical photos were not deleted.
- Computational-session UI was exercised with explicitly synthetic fetch fixtures: it sent exact camera/port, HDR/3 frames/2 EV/0 interval, then POST run, and displayed 3/3 saved. This establishes the UI contract only; real session proof belongs to the hardware verifier.
- Unsigned iOS simulator build, install, launch and visual inspection succeeded after the scene lifecycle fix. The native app loaded the camera page and authentication prompt on iOS 27.

## Evidence

Private, local artifacts:

- `output/playwright/original-review-card.png`
- `output/playwright/edited-review-card.png`
- `output/playwright/original-download.jpg`
- `output/playwright/edited-download.jpg`
- `output/playwright/session-controls-fixture.png` — synthetic UI fixture, not hardware proof
- `mobile/artifacts/ios-simulator/build-20260905.log`
- `mobile/artifacts/ios-simulator/launch-20260905.png`
- `/tmp/camera-autopilot-ios-derived/Build/Products/Debug-iphonesimulator/App.app`

No tokens are included in this report. Browser storage was authenticated from a private temporary token file.

## Not verified / external gates

- Native Save to Photos and native share sheet runtime acceptance have not passed. Compiled plugin code and native launch are separate from those operations.
- Current physical iPhone 15 Pro Max is paired and available over the local network, but Developer Mode is disabled; the Mac has zero valid code-signing identities. No device setting or signing state was changed.
- Physical camera capture is exclusively owned by the parent verifier. Mock-image browser checks are not real-camera proof.
- No real beginner study participant or result was generated.
- The CLI route-interception fixture caused two browser-session closures. A bounded in-page fetch fixture completed the session UI contract test; actual mock-backend photo/edit/download checks did not depend on that fixture.

## Feedback

Open **Review**, identify the exact photo, use thumbs or **Photo feedback comment → Save comment**. Success is shown only after the backend accepts the write. For native/device failures, give the device, operation, visible message and this report's date to the project task.

## Final integration checks

- Camera refresh now replaces stale ports and requires a new explicit selection when the saved model/port disappears; it does not switch silently to another body.
- Sessions show publication failures even when capture is completed. The browser fixture verified the retry calls only `/run` (not `/resume`) and displays the storage error and no-recapture wording. Evidence: `output/playwright/publication-retry-fixture.png`.
- Added bounded/streamed ZIP sequence downloads, strict local artifact path validation, session-result review links, and explicit session deletion with stronger confirmation for unresolved shutter outcomes.
- Movie UI is a receipt when no image is supplied. A browser fixture verified image/feedback are hidden, completion warnings remain visible, and structured 409 uncertain-stop errors say not to retry until the camera is checked. Evidence: `output/playwright/video-receipt-fixture.png`. This is a UI contract check, not native-camera movie proof.
- Physical movie receipt-persistence/readback warnings are displayed separately from the successful physical action. A7 III movie selection is disabled.

- A confirmed photo is retained before loading its preview. A synthetic successful `/shoot` plus failed JPEG GET preserved the summary, Review link and explicit “Photo saved; preview unavailable” / no-recapture message. Only one shoot request occurred. Preview fetching is bounded to 15 seconds. Evidence: `output/playwright/saved-preview-failure-fixture.png`.
- Session deletion recognizes pending/failed restoration as unresolved evidence, warns before discarding it, and sends the matching explicit backend flag.
- Added an XCTest UI target and shared scheme for actual native save/share acceptance. `build-for-testing` passed. The first runtime connected to the simulator but stalled before token-entry interaction for about six minutes; it was interrupted (exit 75). Logs report a missing debugger URL and a target-runner operation awaiting workers. This is not a Photos or sharing pass. Evidence: `mobile/artifacts/ios-simulator/uitest-runtime-20260905.log`, `uitest-runner-log.log`, and private `/tmp/camera-autopilot-native-media-20260905.xcresult`.

- An additional synthetic browser check exercised deletion of a session whose frames were accepted but settings restoration failed. It sent `DELETE /sessions/<id>?discard_unresolved=true` only after the explicit restoration-evidence warning. Evidence: `output/playwright/session-restore-delete-fixture.png`.

- Final native retry used a no-debugger PosixSpawn XCTest scheme. Its unsigned `build-for-testing` passed again; `test-without-building` still stalled and was interrupted at the enforced 120-second deadline. The interrupted log also reports `xcrun: error: unable to find utility "simctl", not a developer tool or in PATH`. No Save to Photos or share-sheet assertion ran successfully. Evidence: `mobile/artifacts/ios-simulator/uitest-build-final.log`, `uitest-posix-runtime.log`, `uitest-posix-outcome.log`, and private `/tmp/camera-autopilot-native-media-posix-20260905.xcresult`. Private test credentials were removed from the generated xctestrun file after each attempt, and both result bundles are private.
- Final verification: all browser scripts parse, 11 focused edit/UI tests pass, and `npm --prefix mobile test` passes. No native test runner remains running.

## Native environment follow-up

The default `xcrun --find simctl` exits 72; `DEVELOPER_DIR=$HOME/Downloads/Xcode-beta.app/Contents/Developer xcrun --find simctl` succeeds and points to the installed Xcode tool. The earlier `simctl` error occurred while collecting diagnostics after interruption, so it does not establish the original stall's cause. A final distinct attempt passes `DEVELOPER_DIR` and the Xcode tools `PATH` explicitly to both xctestrun environment dictionaries and the host command, with parallel testing disabled and a 120-second limit. No system Xcode selection, device setting, or signing state is changed.

Outcome: the explicit-environment attempt also stalled and was interrupted after 120 seconds. It logged the same unresolved debugger URL and the same post-interruption diagnostic `simctl` lookup failure despite explicit host, runner and test environments. No test-case assertions passed. `simctl` and LLDB binaries exist in the chosen Xcode installation, but `Contents/Developer/Applications/Simulator.app` does not. A complete, functioning Xcode UI-test environment is required for the remaining native acceptance check; these logs do not identify a proven single root cause. No signing or simulator-control package was installed. Evidence: `mobile/artifacts/ios-simulator/uitest-explicit-outcome.json`, `uitest-explicit-runtime.log`, `uitest-explicit-runner.log`, `uitest-explicit-progress.png`, private `/tmp/camera-autopilot-native-media-explicit-20260905.xcresult`. Test credentials were removed from the generated per-attempt xctestrun file.

## Final camera choice and recipe truthfulness

- A fresh tab with two cameras keeps selection empty and disables photo/session capture until a camera is chosen. A saved selection is matched by exact model/port; only a sole camera can be selected automatically when there is no stale saved identity. Capture also waits for initial status resolution.
- The two-camera browser fixture proved that direct photo, movie and session-start attempts sent zero actuation requests before selection; after selecting the second camera, exactly one synthetic shoot request contained that camera's model and port.
- Result and review copy labels the mood a requested recipe. It separately displays actual white balance from saved shot settings and verified/unverified control status from `shot.applied.white_balance`; it states that the recipe outcome has not been verified from the photo. The browser fixture used requested warm / actual Auto / unverified control and displayed all three truthfully. Evidence: `output/playwright/two-camera-recipe-fixture.png`.
- The first fixture assertion expected mixed case from rendered `innerText`, which CSS displays uppercase; the corrected assertion checks semantic `textContent` and passed. JavaScript parsing and all 11 focused tests passed after the changes.

## Lost capture responses

Photo/movie POST network rejection and unreadable success responses now retain their operation and mark the outcome uncertain. The visible result persists instructions to inspect Review/camera card for a photo, or the actual camera recording state/card for a movie, before another action; neither recommends repeating an uncertain capture. A pre-action 401 can still obtain a token and retry.

Five executable JavaScript-helper tests cover photo/movie network loss, photo/movie unreadable success JSON, exactly one request for each ambiguous outcome, and the authorized 401 retry. Together with existing focused checks: **16 passed**. A Playwright browser fixture also verified both persistent result messages with exactly one synthetic request per operation. Evidence: `output/playwright/lost-capture-response-fixture.png`; test source: `tests/test_web_network_outcomes.py`. No hardware request was made.
