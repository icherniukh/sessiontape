import Foundation
import XCTest

@testable import mac_capture

final class DiagnosticLoggerTests: XCTestCase {
    func testAppendsToExistingLogInsteadOfTruncating() throws {
        let path = FileManager.default.temporaryDirectory
            .appendingPathComponent("mac-capture-\(UUID().uuidString).log").path
        defer { try? FileManager.default.removeItem(atPath: path) }
        try "INFO: previous run\n".write(toFile: path, atomically: true, encoding: .utf8)

        DiagnosticLogger(logPath: path).log(level: "INFO", message: "next run")

        XCTAssertEqual(
            try String(contentsOfFile: path, encoding: .utf8),
            "INFO: previous run\nINFO: next run\n")
    }

    func testCreatesMissingLog() throws {
        let path = FileManager.default.temporaryDirectory
            .appendingPathComponent("mac-capture-\(UUID().uuidString).log").path
        defer { try? FileManager.default.removeItem(atPath: path) }

        DiagnosticLogger(logPath: path).log(level: "ERROR", message: "first")

        XCTAssertEqual(try String(contentsOfFile: path, encoding: .utf8), "ERROR: first\n")
    }
}
