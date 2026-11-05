// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "mac-capture",
    platforms: [
        .macOS(.v15)
    ],
    targets: [
        .executableTarget(
            name: "mac-capture",
            dependencies: [],
            linkerSettings: [
                .unsafeFlags([
                    "-Xlinker", "-sectcreate", "-Xlinker", "__TEXT", "-Xlinker", "__info_plist",
                    "-Xlinker", "Info.plist",
                ])
            ]
        ),
        .executableTarget(
            name: "mac-capture-benchmark",
            dependencies: []
        ),
    ]
)
