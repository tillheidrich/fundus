// swift-tools-version: 6.0
import PackageDescription

// A Swift package rather than an .xcodeproj, deliberately: this builds with
// the Command Line Tools alone, and Xcode opens Package.swift directly as a
// project when you do install it. Nothing is lost either way.
let package = Package(
    name: "FundusApp",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "FundusApp",
            path: "Sources/FundusApp"
        )
    ]
)
