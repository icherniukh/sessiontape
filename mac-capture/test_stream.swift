import ScreenCaptureKit

func test() async throws {
    let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
    guard let display = content.displays.first else { print("No display"); return }
    guard let app = content.applications.first(where: { $0.applicationName == "Raycast" }) else { print("No Raycast"); return }
    
    let filter = SCContentFilter(display: display, including: [app], exceptingWindows: [])
    let config = SCStreamConfiguration()
    config.capturesAudio = true
    config.excludesCurrentProcessAudio = false
    config.showsCursor = false
    
    // Test stream creation
    let stream = SCStream(filter: filter, configuration: config, delegate: nil)
    try await stream.startCapture()
    print("Started successfully")
    try await stream.stopCapture()
}
try await test()
