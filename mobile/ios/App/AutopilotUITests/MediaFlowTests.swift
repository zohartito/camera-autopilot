import XCTest

final class MediaFlowTests: XCTestCase {
    // NOTE ON TIMEOUTS: this app is a Capacitor WKWebView. XCUITest accessibility
    // snapshots of that web content are very slow on the simulator (observed
    // 30-66s per snapshot), so every `waitForExistence` timeout below is sized to
    // span several snapshot cycles. Short timeouts (15-30s) expire before a single
    // snapshot can even observe an on-screen element and produce false negatives.
    private let snapshotWait: TimeInterval = 240

    func testNativePhotoSaveAndShare() throws {
        continueAfterFailure = false
        guard let token = ProcessInfo.processInfo.environment["AUTOPILOT_TEST_TOKEN"], !token.isEmpty else {
            throw XCTSkip("A private mock-backend test token is required.")
        }
        let app = XCUIApplication(bundleIdentifier: "com.zohar.autopilot")
        app.launch()

        // The web client requests the API token through a native window.prompt()
        // dialog titled "Camera token". It only appears after the page's JS runs,
        // so wait well past page-load plus one slow snapshot cycle.
        let prompt = app.alerts.firstMatch
        XCTAssertTrue(prompt.waitForExistence(timeout: snapshotWait), "Token prompt never appeared. " + app.debugDescription)
        let field = prompt.textFields.firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: snapshotWait), "Token prompt had no text field.")
        field.tap()
        field.typeText(token)
        // "Ok" is the confirm button rendered by WKWebView's prompt().
        let ok = prompt.buttons["Ok"].firstMatch
        if ok.exists { ok.tap() } else { prompt.buttons.element(boundBy: prompt.buttons.count - 1).tap() }

        let shutter = app.buttons["Get the shot"].firstMatch
        XCTAssertTrue(shutter.waitForExistence(timeout: snapshotWait), app.debugDescription)
        shutter.tap()

        let review = app.links["Edit / share / delete"].firstMatch
        XCTAssertTrue(review.waitForExistence(timeout: snapshotWait), app.debugDescription)
        review.tap()

        let save = app.buttons["Save to Photos"].firstMatch
        XCTAssertTrue(save.waitForExistence(timeout: snapshotWait), "Native bridge must expose Save to Photos; a Download fallback does not pass.")

        // Photos add-only permission is pre-granted via `simctl privacy grant
        // photos-add` for a deterministic run, but keep the interruption handlers
        // in case the harness presents the add-only prompt anyway.
        addUIInterruptionMonitor(withDescription: "Photos add permission") { alert in
            let allow = alert.buttons.matching(NSPredicate(format: "label CONTAINS[c] 'Allow' OR label CONTAINS[c] 'Add Photos' OR label ==[c] 'OK'")).firstMatch
            if allow.exists { allow.tap(); return true }
            return false
        }
        save.tap()
        if app.alerts.firstMatch.waitForExistence(timeout: 5) {
            let allow = app.alerts.firstMatch.buttons.matching(NSPredicate(format: "label CONTAINS[c] 'Allow' OR label CONTAINS[c] 'Add Photos' OR label ==[c] 'OK'")).firstMatch
            if allow.exists { allow.tap() }
        }

        XCTAssertTrue(app.staticTexts["Saved to Photos"].firstMatch.waitForExistence(timeout: snapshotWait), app.debugDescription)
        let saved = XCTAttachment(screenshot: app.screenshot()); saved.name = "Native Photos save success"; saved.lifetime = .keepAlways; add(saved)

        app.buttons["Share"].firstMatch.tap()
        // iOS 27's UIActivityViewController does not expose a button labelled
        // "Close"; detect the native share sheet by its container or a standard
        // system activity (Copy / Save Image), then dismiss it robustly.
        let shareSheet = app.otherElements["ActivityListView"].firstMatch
        let copyActivity = app.buttons["Copy"].firstMatch
        let saveImageActivity = app.buttons["Save Image"].firstMatch
        let sheetShown = shareSheet.waitForExistence(timeout: snapshotWait)
            || copyActivity.waitForExistence(timeout: 15)
            || saveImageActivity.waitForExistence(timeout: 15)
        XCTAssertTrue(sheetShown, "Expected the native share sheet. " + app.debugDescription)
        let shared = XCTAttachment(screenshot: app.screenshot()); shared.name = "Native share sheet"; shared.lifetime = .keepAlways; add(shared)
        let close = app.buttons["Close"].firstMatch
        if close.exists { close.tap() } else { app.swipeDown() }
    }
}
