import ScreenCaptureKit

func test() async throws {
    let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
    if let display = content.displays.first, let app = content.applications.first {
        _ = SCContentFilter(display: display, includingApplications: [app], exceptingWindows: [])
        print("OK")
    }
}
