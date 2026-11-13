import UIKit
import Capacitor

@UIApplicationMain
class AppDelegate: UIResponder, UIApplicationDelegate {

    var window: UIWindow?

    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        // Override point for customization after application launch.
        return true
    }

    func applicationWillResignActive(_ application: UIApplication) {
        // Sent when the application is about to move from active to inactive state. This can occur for certain types of temporary interruptions (such as an incoming phone call or SMS message) or when the user quits the application and it begins the transition to the background state.
        // Use this method to pause ongoing tasks, disable timers, and invalidate graphics rendering callbacks. Games should use this method to pause the game.
    }

    func applicationDidEnterBackground(_ application: UIApplication) {
        // Use this method to release shared resources, save user data, invalidate timers, and store enough application state information to restore your application to its current state in case it is terminated later.
        // If your application supports background execution, this method is called instead of applicationWillTerminate: when the user quits.
    }

    func applicationWillEnterForeground(_ application: UIApplication) {
        // Called as part of the transition from the background to the active state; here you can undo many of the changes made on entering the background.
    }

    func applicationDidBecomeActive(_ application: UIApplication) {
        // Restart any tasks that were paused (or not yet started) while the application was inactive. If the application was previously in the background, optionally refresh the user interface.
    }

    func applicationWillTerminate(_ application: UIApplication) {
        // Called when the application is about to terminate. Save data if appropriate. See also applicationDidEnterBackground:.
    }

    func application(_ app: UIApplication, open url: URL, options: [UIApplication.OpenURLOptionsKey: Any] = [:]) -> Bool {
        // Called when the app was launched with a url. Feel free to add additional processing here,
        // but if you want the App API to support tracking app url opens, make sure to keep this call
        return ApplicationDelegateProxy.shared.application(app, open: url, options: options)
    }

    func application(_ application: UIApplication, continue userActivity: NSUserActivity, restorationHandler: @escaping ([UIUserActivityRestoring]?) -> Void) -> Bool {
        // Called when the app was launched with an activity, including Universal Links.
        // Feel free to add additional processing here, but if you want the App API to support
        // tracking app url opens, make sure to keep this call
        return ApplicationDelegateProxy.shared.application(application, continue: userActivity, restorationHandler: restorationHandler)
    }

}

// Built-in Photos/UIKit bridge: no public URLs or third-party media service required.
import Photos
import ImageIO

class AutopilotViewController: CAPBridgeViewController {
    override func capacitorDidLoad() {
        bridge?.registerPluginInstance(AutopilotMediaPlugin())
    }
}

@objc(AutopilotMediaPlugin)
public class AutopilotMediaPlugin: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "AutopilotMediaPlugin"
    public let jsName = "AutopilotMedia"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "sharePhoto", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "savePhoto", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "safeArea", returnType: CAPPluginReturnPromise)
    ]

    @objc public func safeArea(_ call: CAPPluginCall) {
        DispatchQueue.main.async {
            let insets = self.bridge?.viewController?.view.safeAreaInsets ?? .zero
            let scene = self.bridge?.viewController?.view.window?.windowScene
                ?? UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first
            let statusHeight = scene?.statusBarManager?.statusBarFrame.height ?? 0
            call.resolve(["top": max(insets.top, statusHeight), "bottom": insets.bottom])
        }
    }

    private func jpegData(_ call: CAPPluginCall) -> Data? {
        guard let encoded = call.getString("data"), encoded.count <= 45_000_000,
              let data = Data(base64Encoded: encoded), data.count <= 32 * 1024 * 1024,
              data.starts(with: [0xff, 0xd8, 0xff]),
              let source = CGImageSourceCreateWithData(data as CFData, nil),
              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              let width = properties[kCGImagePropertyPixelWidth] as? Int,
              let height = properties[kCGImagePropertyPixelHeight] as? Int,
              width > 0, height > 0, width <= 40000000 / height else {
            call.reject("Expected a JPEG up to 32 MiB and 40 megapixels.")
            return nil
        }
        return data
    }

    @objc public func savePhoto(_ call: CAPPluginCall) {
        guard let data = jpegData(call) else { return }
        PHPhotoLibrary.requestAuthorization(for: .addOnly) { status in
            guard status == .authorized || status == .limited else {
                call.reject("Photos permission was not granted. Enable Add Photos access in Settings.")
                return
            }
            PHPhotoLibrary.shared().performChanges({
                let request = PHAssetCreationRequest.forAsset()
                request.addResource(with: .photo, data: data, options: nil)
            }) { success, error in
                if success { call.resolve(["saved": true]) }
                else { call.reject(error?.localizedDescription ?? "Could not save photo.") }
            }
        }
    }

    @objc public func sharePhoto(_ call: CAPPluginCall) {
        guard let data = jpegData(call) else { return }
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("AutopilotShare", isDirectory: true)
        // Retire abandoned share files from a previous terminated app session.
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        if let files = try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: [.creationDateKey]) {
            for file in files {
                let created = try? file.resourceValues(forKeys: [.creationDateKey]).creationDate
                if let created = created, Date().timeIntervalSince(created) > 3600 { try? FileManager.default.removeItem(at: file) }
            }
        }
        let url = directory.appendingPathComponent(UUID().uuidString + ".jpg")
        do { try data.write(to: url, options: [.atomic, .completeFileProtection]) }
        catch { call.reject("Could not prepare photo for sharing."); return }
        DispatchQueue.main.async {
            guard let controller = self.bridge?.viewController else {
                try? FileManager.default.removeItem(at: url)
                call.reject("Share controller unavailable.")
                return
            }
            let sheet = UIActivityViewController(activityItems: [url], applicationActivities: nil)
            sheet.popoverPresentationController?.sourceView = controller.view
            sheet.popoverPresentationController?.sourceRect = CGRect(x: controller.view.bounds.midX, y: controller.view.bounds.midY, width: 1, height: 1)
            sheet.completionWithItemsHandler = { _, completed, _, error in
                try? FileManager.default.removeItem(at: url)
                if let error = error { call.reject(error.localizedDescription) }
                else { call.resolve(["shared": completed, "cancelled": !completed]) }
            }
            controller.present(sheet, animated: true)
        }
    }
}

// iOS 27 SDK requires scene lifecycle adoption, including for a single window.
class SceneDelegate: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?

    func scene(_ scene: UIScene, openURLContexts URLContexts: Set<UIOpenURLContext>) {
        for context in URLContexts {
            _ = ApplicationDelegateProxy.shared.application(UIApplication.shared, open: context.url,
                options: [.sourceApplication: context.options.sourceApplication as Any,
                          .annotation: context.options.annotation as Any])
        }
    }

    func scene(_ scene: UIScene, continue userActivity: NSUserActivity) {
        _ = ApplicationDelegateProxy.shared.application(UIApplication.shared, continue: userActivity,
                                                        restorationHandler: { _ in })
    }
}
