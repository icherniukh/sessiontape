import ScreenCaptureKit

func test() async throws {
    let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
    for app in content.applications {
        print("\(app.applicationName): \(app.processID)")
    }
}
try await test()
