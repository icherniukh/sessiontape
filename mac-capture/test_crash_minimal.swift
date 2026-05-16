import Foundation
import ScreenCaptureKit
import AVFoundation

func test() async throws {
    let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
    print("Content count: \(content.applications.count)")
}

Task {
    try? await test()
}

dispatchMain()
