import Foundation

@main struct NativeDistributionAcceptance {
    static func main() throws {
        var checks = 0
        var failed = 0
        func check(_ value: Bool, _ name: String) {
            checks += 1
            if !value { failed += 1; print("FAIL: " + name) }
        }
        let manager = FileManager.default
        let temporary = manager.temporaryDirectory.appendingPathComponent("harbor-distribution-" + UUID().uuidString)
        try manager.createDirectory(at: temporary, withIntermediateDirectories: true)
        defer { try? manager.removeItem(at: temporary) }
        let resources = temporary.appendingPathComponent("Resources")
        let engine = resources.appendingPathComponent("engine")
        try manager.createDirectory(at: engine, withIntermediateDirectories: true)
        let manifest = engine.appendingPathComponent("edition.json")
        let home = temporary.appendingPathComponent("synthetic-home")

        let legacy = Distribution.load(resourceURL: resources, bundleInfo: [:])
        check(legacy.appName == "Codex Harbor" && legacy.dataDirectory == "Codex Harbor",
              "An existing private bundle without an edition manifest keeps its original identity")

        try Data(#"{"edition":"community","app_name":"Codex Harbor Community","data_directory":"Codex Harbor Community","version":"0.9.0"}"#.utf8).write(to: manifest)
        let community = Distribution.load(resourceURL: resources, bundleInfo: [:])
        check(community.edition == "community" && community.appName == "Codex Harbor Community" && community.version == "0.9.0",
              "The native app reads the bundled community edition manifest")
        let expected = home.appendingPathComponent("Library/Application Support/Codex Harbor Community", isDirectory: true)
        check(Engine.root(distribution: community, environment: [:], bundleInfo: [:], home: home) == expected,
              "Community uses its own application support directory")
        check(community.versionLabel == "社区版 0.9.0", "The header displays the edition and manifest version")
        check(!manager.fileExists(atPath: expected.path), "Resolving the data root does not create or import any data")

        for value in ["../Codex Harbor", "/tmp/private", "Codex Harbor", "codex harbor", "CODEX HARBOR", "Codex Smart-2", "", ".", "..", "unsafe\nname"] {
            let object = ["edition": "community", "app_name": "Codex Harbor Community", "data_directory": value, "version": "0.9.0"]
            try JSONSerialization.data(withJSONObject: object).write(to: manifest)
            let isolated = Distribution.load(resourceURL: resources, bundleInfo: [:])
            check(isolated.dataDirectory == "Codex Harbor Community", "Unsafe community data directory is rejected: \(value.debugDescription)")
        }
        try Data("{broken".utf8).write(to: manifest)
        let damaged = Distribution.load(resourceURL: resources, bundleInfo: ["CFBundleName": "Codex Harbor Community", "CFBundleShortVersionString": "0.9.0"])
        check(damaged.dataDirectory == "Codex Harbor Community" && damaged.edition == "community",
              "A damaged community manifest cannot select the private data directory")
        let renamed = Distribution.load(resourceURL: resources, bundleInfo: ["CFBundleIdentifier": "io.github.dongshuyan.codex-harbor-community"])
        check(renamed.dataDirectory == "Codex Harbor Community", "The community bundle identifier preserves isolation without display metadata")

        let fixture = temporary.appendingPathComponent("control")
        try manager.createDirectory(at: fixture, withIntermediateDirectories: true)
        let fixtureInfo: [String: Any] = ["HarborFixture": true]
        let fixtureRoot = Engine.root(distribution: community, environment: ["HARBOR_TEST_ROOT": fixture.path], bundleInfo: fixtureInfo, home: home)
        check(fixtureRoot.resolvingSymlinksInPath() == fixture.resolvingSymlinksInPath(), "Existing temporary GUI fixtures retain their explicit root")
        let oldFixtureRoot = Engine.root(distribution: community, environment: ["SMART2_TEST_ROOT": fixture.path], bundleInfo: fixtureInfo, home: home)
        check(oldFixtureRoot.resolvingSymlinksInPath() == fixture.resolvingSymlinksInPath(), "The legacy temporary fixture override remains supported")
        check(Engine.root(distribution: community, environment: ["HARBOR_TEST_ROOT": fixture.path], bundleInfo: [:], home: home) == expected,
              "A release community app ignores inherited test-root variables without an explicit fixture marker")
        check(Engine.root(distribution: legacy, environment: ["HARBOR_TEST_ROOT": fixture.path], bundleInfo: [:], home: home).path == fixtureRoot.path,
              "Existing private GUI fixtures remain compatible without a new marker")
        let absent = temporary.appendingPathComponent("absent/control")
        check(Engine.root(distribution: community, environment: ["HARBOR_TEST_ROOT": absent.path], bundleInfo: [:], home: home) == expected,
              "A stale fixture override cannot create a new control directory")
        check(Engine.root(distribution: community, environment: ["HARBOR_TEST_ROOT": "/usr"], bundleInfo: [:], home: home) == expected,
              "An environment override cannot redirect a release to a non-temporary data directory")
        check(Engine.root(distribution: community, environment: [:], bundleInfo: ["HarborControlRoot": "/usr"], home: home) == expected,
              "A legacy bundle root cannot redirect community to a non-temporary directory")

        var probed: [String] = []
        let selected = try PythonRuntime.resolve(candidates: ["old-python", "compatible-python", "later-python"]) { path in
            probed.append(path)
            return path != "old-python"
        }
        check(selected == "compatible-python" && probed == ["old-python", "compatible-python"],
              "Python resolution selects the first verified compatible interpreter")
        check(PythonRuntime.candidatePaths == ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"],
              "Python discovery supports both Homebrew prefixes and the system interpreter")
        do {
            _ = try PythonRuntime.resolve(candidates: ["SYNTHETIC_SECRET_PATH"]) { _ in false }
            check(false, "Missing compatible Python must report an error")
        } catch {
            let message = error.localizedDescription
            check(message.contains("Python 3.11") && message.contains("安装") && !message.contains("SYNTHETIC_SECRET_PATH"),
                  "The prerequisite error is localized, actionable, and omits candidate diagnostics")
        }

        func executable(_ name: String, _ body: String) throws -> String {
            let file = temporary.appendingPathComponent(name)
            try Data(("#!/bin/sh\n" + body).utf8).write(to: file)
            try manager.setAttributes([.posixPermissions: 0o700], ofItemAtPath: file.path)
            return file.path
        }
        let compatible = try executable("synthetic-python", "test \"$1\" = '-I' && test \"$2\" = '-B' && test \"$3\" = '-S' && test \"$4\" = '-c' && test \"$5\" = 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'\n")
        check(PythonRuntime.supportsRequiredVersion(at: compatible), "The executable version probe runs isolated, without site initialization or bytecode writes")
        let incompatible = try executable("synthetic-old-python", "printf 'SYNTHETIC_SECRET_DIAGNOSTIC' >&2\nexit 1\n")
        check(!PythonRuntime.supportsRequiredVersion(at: incompatible), "An incompatible executable is rejected without surfacing stderr")
        let hanging = try executable("synthetic-hanging-python", "exec /bin/sleep 2\n")
        let started = Date()
        check(!PythonRuntime.supportsRequiredVersion(at: hanging, timeout: 0.05) && Date().timeIntervalSince(started) < 1.5,
              "A nonresponsive candidate cannot hang interpreter discovery")

        let applications = [temporary.appendingPathComponent("Codex.app"), temporary.appendingPathComponent("ChatGPT.app")]
        let selectedResources = applications[1].appendingPathComponent("Contents/Resources")
        try manager.createDirectory(at: selectedResources, withIntermediateDirectories: true)
        let selectedMacOS = applications[1].appendingPathComponent("Contents/MacOS")
        try manager.createDirectory(at: selectedMacOS, withIntermediateDirectories: true)
        try Data().write(to: selectedMacOS.appendingPathComponent("ChatGPT"))
        let info = try PropertyListSerialization.data(fromPropertyList: ["CFBundleExecutable": "ChatGPT"], format: .xml, options: 0)
        try info.write(to: applications[1].appendingPathComponent("Contents/Info.plist"))
        let cli = selectedResources.appendingPathComponent("codex-cli/CodexCLI.app/Contents/MacOS/codex")
        try manager.createDirectory(at: cli.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data().write(to: cli)
        let lightIcon = selectedResources.appendingPathComponent("icon-codex-light.png")
        try Data().write(to: lightIcon)
        check(CodexApplicationResources.iconURL(dark: false, applications: applications) == lightIcon,
              "Official account artwork follows the installed Codex application instead of a fixed app path")
        check(CodexApplicationResources.iconURL(dark: true, applications: applications) == nil,
              "Missing vendor artwork leaves the generic terminal fallback available")
        let emptyResources = applications[0].appendingPathComponent("Contents/Resources")
        try manager.createDirectory(at: emptyResources, withIntermediateDirectories: true)
        try Data().write(to: emptyResources.appendingPathComponent("codex"))
        try Data().write(to: emptyResources.appendingPathComponent("icon-codex-light.png"))
        check(CodexApplicationResources.iconURL(dark: false, applications: applications) == lightIcon,
              "An incomplete app without its declared desktop executable cannot supply another app's icon")

        print("Native distribution checks: \(checks - failed)/\(checks) passed")
        if failed > 0 { exit(1) }
    }
}
