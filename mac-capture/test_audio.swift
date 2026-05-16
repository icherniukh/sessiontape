import Foundation
import ScreenCaptureKit

func test() async throws {
    let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
    guard let display = content.displays.first else { print("No display"); return }
    guard let app = content.applications.first(where: { $0.applicationName.lowercased() == "rekordbox" || $0.applicationName.lowercased() == "safari" }) else { print("No app found"); return }
    
    let filter = SCContentFilter(display: display, including: [app], exceptingWindows: [])
    let config = SCStreamConfiguration()
    config.capturesAudio = true
    config.excludesCurrentProcessAudio = false
    
    // Instead of using the video filter which might capture screen and no audio when there is no video activity,
    // let's create a stream using desktopIndependentApp
    print("Capturing \(app.applicationName)")
}
try await test()
