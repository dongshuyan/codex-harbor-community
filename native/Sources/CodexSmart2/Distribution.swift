import Foundation
import Darwin

struct Distribution: Equatable {
    let edition: String
    let appName: String
    let dataDirectory: String
    let version: String

    static let current = load(resourceURL: Bundle.main.resourceURL, bundleInfo: Bundle.main.infoDictionary ?? [:])

    var versionLabel: String { "\(edition == "community" ? "社区版" : "本机版") \(version)" }

    static func load(resourceURL: URL?, bundleInfo: [String: Any]) -> Distribution {
        let manifest = resourceURL?.appendingPathComponent("engine/edition.json")
        let object = manifest.flatMap { try? Data(contentsOf: $0) }
            .flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:]
        // A damaged community manifest must not turn a community bundle into
        // a reader or writer of the private edition's data.
        let community = object["edition"] as? String == "community" ||
            bundleInfo["CFBundleName"] as? String == "Codex Harbor Community" ||
            bundleInfo["CFBundleDisplayName"] as? String == "Codex Harbor Community" ||
            bundleInfo["CFBundleIdentifier"] as? String == "io.github.dongshuyan.codex-harbor-community"
        let fallbackName = community ? "Codex Harbor Community" : "Codex Harbor"
        let name = displayValue(object["app_name"], limit: 80) ?? fallbackName
        let configuredDirectory = object["data_directory"] as? String
        let directory = configuredDirectory.flatMap { value -> String? in
            guard safeComponent(value), !community || !["codex harbor", "codex smart-2"].contains(value.lowercased()) else { return nil }
            return value
        } ?? fallbackName
        let version = displayValue(object["version"], limit: 32) ??
            displayValue(bundleInfo["CFBundleShortVersionString"], limit: 32) ?? (community ? "0.9.0" : "0.8.0")
        return Distribution(edition: community ? "community" : "private", appName: name, dataDirectory: directory, version: version)
    }

    func dataRoot(home: URL) -> URL {
        home.appendingPathComponent("Library/Application Support", isDirectory: true)
            .appendingPathComponent(dataDirectory, isDirectory: true)
    }

    private static func displayValue(_ raw: Any?, limit: Int) -> String? {
        guard let value = raw as? String, !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              value.count <= limit, value.rangeOfCharacter(from: .controlCharacters) == nil else { return nil }
        return value
    }

    private static func safeComponent(_ value: String) -> Bool {
        displayValue(value, limit: 120) != nil && value != "." && value != ".." &&
            !value.contains("/") && !value.contains("\\") && value == value.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    static func fixtureRoot(distribution: Distribution = .current, environment: [String: String], bundleInfo: [String: Any]) -> URL? {
        guard distribution.edition != "community" || bundleInfo["HarborFixture"] as? Bool == true else { return nil }
        guard let raw = environment["HARBOR_TEST_ROOT"] ?? environment["SMART2_TEST_ROOT"] ??
                bundleInfo["HarborControlRoot"] as? String, raw.hasPrefix("/") else { return nil }
        let root = URL(fileURLWithPath: raw, isDirectory: true).resolvingSymlinksInPath()
        let temporaryRoots = [FileManager.default.temporaryDirectory, URL(fileURLWithPath: "/private/tmp", isDirectory: true)]
        guard temporaryRoots.contains(where: { root.path.hasPrefix($0.resolvingSymlinksInPath().path + "/") }) else { return nil }
        var directory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: root.path, isDirectory: &directory), directory.boolValue else { return nil }
        return root
    }

    static func fixtureExternal(root: URL, environment: [String: String], bundleInfo: [String: Any]) -> URL? {
        guard let raw = environment["HARBOR_TEST_EXTERNAL"] ?? bundleInfo["HarborExternalRoot"] as? String,
              raw.hasPrefix("/") else { return nil }
        let external = URL(fileURLWithPath: raw, isDirectory: true).resolvingSymlinksInPath()
        let fixture = root.deletingLastPathComponent().resolvingSymlinksInPath()
        guard external.path.hasPrefix(fixture.path + "/") else { return nil }
        return external
    }
}

enum PythonRuntime {
    static let candidatePaths = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"]

    static func resolve(candidates: [String] = candidatePaths,
                        isSupported: (String) -> Bool = { supportsRequiredVersion(at: $0) }) throws -> String {
        guard let executable = candidates.first(where: isSupported) else {
            throw EngineError(message: "未找到 Python 3.11 或更高版本。请从 python.org 安装 Python 3，或通过 Homebrew 安装（brew install python），然后重新打开应用。")
        }
        return executable
    }

    static func supportsRequiredVersion(at path: String, timeout: TimeInterval = 3) -> Bool {
        guard FileManager.default.isExecutableFile(atPath: path) else { return false }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: path)
        process.arguments = ["-I", "-B", "-S", "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"]
        process.environment = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8"]
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        let finished = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in finished.signal() }
        do { try process.run() } catch { return false }
        guard finished.wait(timeout: .now() + timeout) == .success else {
            if process.isRunning { process.terminate() }
            if finished.wait(timeout: .now() + 0.2) == .timedOut, process.isRunning {
                kill(process.processIdentifier, SIGKILL)
                _ = finished.wait(timeout: .now() + 0.2)
            }
            return false
        }
        return process.terminationReason == .exit && process.terminationStatus == 0
    }
}

enum CodexApplicationResources {
    static func candidates(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> [URL] {
        [URL(fileURLWithPath: "/Applications/Codex.app", isDirectory: true),
         URL(fileURLWithPath: "/Applications/ChatGPT.app", isDirectory: true),
         home.appendingPathComponent("Applications/Codex.app", isDirectory: true),
         home.appendingPathComponent("Applications/ChatGPT.app", isDirectory: true)]
    }

    static func iconURL(dark: Bool, applications: [URL] = candidates()) -> URL? {
        guard let app = applications.first(where: { application in
            guard let data = try? Data(contentsOf: application.appendingPathComponent("Contents/Info.plist")),
                  let info = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
                  let executable = info["CFBundleExecutable"] as? String,
                  !executable.isEmpty, !executable.contains("/"), executable != ".", executable != "..",
                  isFile(application.appendingPathComponent("Contents/MacOS/" + executable)) else { return false }
            return ["Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex", "Contents/Resources/codex"]
                .contains { isFile(application.appendingPathComponent($0)) }
        }) else { return nil }
        let icon = app.appendingPathComponent("Contents/Resources/" + (dark ? "icon-codex-dark-color.png" : "icon-codex-light.png"))
        return isFile(icon) ? icon : nil
    }

    private static func isFile(_ url: URL) -> Bool {
        var directory: ObjCBool = false
        return FileManager.default.fileExists(atPath: url.path, isDirectory: &directory) && !directory.boolValue
    }
}
