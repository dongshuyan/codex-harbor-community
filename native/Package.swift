// swift-tools-version: 6.0
import PackageDescription
let package = Package(name: "CodexSmart2", platforms: [.macOS(.v14)], products: [
    .executable(name: "CodexSmart2", targets: ["CodexSmart2"]),
    .executable(name: "HarborKeychain", targets: ["HarborKeychain"])
], targets: [.executableTarget(name: "CodexSmart2", swiftSettings: [.swiftLanguageMode(.v5)]),
             .executableTarget(name:"HarborKeychain",swiftSettings:[.swiftLanguageMode(.v5)])])
