// Synthetic AppKit apps only. The private responsibility API is a read-only
// diagnostic, never a production dependency or a way to change attribution.
import AppKit
import Darwin
import Foundation
import Security

private func save(_ value: [String: Any], to path: String) throws {
    try JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys])
        .write(to: URL(fileURLWithPath: path), options: .atomic)
}

private func responsiblePID(_ pid: pid_t) throws -> pid_t {
    typealias Function = @convention(c) (pid_t) -> pid_t
    guard let handle = dlopen("/usr/lib/libSystem.B.dylib", RTLD_NOW) else {
        throw NSError(domain: "SyntheticResponsibilityMissing", code: 1)
    }
    defer { dlclose(handle) }
    guard let symbol = dlsym(handle, "responsibility_get_pid_responsible_for_pid") else {
        throw NSError(domain: "SyntheticResponsibilityMissing", code: 2)
    }
    let result = unsafeBitCast(symbol, to: Function.self)(pid)
    guard result > 0 else { throw NSError(domain: "SyntheticResponsibilityQuery", code: Int(result)) }
    return result
}

private func identity(_ pid: pid_t) throws -> [String: Any] {
    var code: SecCode?
    var status = SecCodeCopyGuestWithAttributes(nil, [kSecGuestAttributePid: pid] as CFDictionary, [], &code)
    guard status == errSecSuccess, let code = code else {
        throw NSError(domain: NSOSStatusErrorDomain, code: Int(status))
    }
    var staticCode: SecStaticCode?
    status = SecCodeCopyStaticCode(code, [], &staticCode)
    guard status == errSecSuccess, let staticCode = staticCode else {
        throw NSError(domain: NSOSStatusErrorDomain, code: Int(status))
    }
    var raw: CFDictionary?
    status = SecCodeCopySigningInformation(staticCode, SecCSFlags(rawValue: kSecCSSigningInformation), &raw)
    guard status == errSecSuccess, let info = raw as? [String: Any],
          let executable = info[kSecCodeInfoMainExecutable as String] as? URL,
          let identifier = info[kSecCodeInfoIdentifier as String] as? String,
          let hash = info[kSecCodeInfoUnique as String] as? Data else {
        throw NSError(domain: "SyntheticSigningInformation", code: Int(status))
    }
    return ["pid": pid, "executable": executable.path, "signing_id": identifier,
            "cdhash": hash.map { String(format: "%02x", $0) }.joined()]
}

private func report() throws -> [String: Any] {
    let pid = getpid(), responsible = try responsiblePID(pid)
    let environment = ProcessInfo.processInfo.environment
    let keys = ["SYNTHETIC_SLOT", "CODEX_HOME", "CODEX_SQLITE_HOME", "CODEX_CLI_PATH"]
    return ["pid": pid, "ppid": getppid(), "sid": getsid(0), "rpid": responsible,
            "self": try identity(pid), "responsible": try identity(responsible),
            "args": CommandLine.arguments,
            "env": Dictionary(uniqueKeysWithValues: keys.map { ($0, environment[$0] ?? "") })]
}

private func target(_ root: String) throws {
    let environment = ProcessInfo.processInfo.environment
    guard let slot = environment["SYNTHETIC_SLOT"], let python = environment["SYNTHETIC_PYTHON"] else {
        throw NSError(domain: "SyntheticEnvironment", code: 1)
    }
    var value = try report()
    let child = Process()
    child.executableURL = URL(fileURLWithPath: python)
    child.arguments = ["-I", "-B", root + "/chain.py", "backend", root, slot]
    child.environment = environment
    child.standardOutput = FileHandle.nullDevice
    child.standardError = FileHandle.nullDevice
    try child.run()
    child.waitUntilExit()
    value["backend_exit"] = child.terminationStatus
    try save(value, to: root + "/" + slot + ".json")
    let deadline = Date().addingTimeInterval(35)
    while !FileManager.default.fileExists(atPath: root + "/stop") && Date() < deadline {
        RunLoop.current.run(until: Date().addingTimeInterval(0.02))
    }
}

private final class LaunchReply: @unchecked Sendable {
    private let lock = NSLock()
    private var result: Result<NSRunningApplication, Error>?

    func complete(_ application: NSRunningApplication?, _ error: Error?) {
        lock.lock()
        defer { lock.unlock() }
        if let error = error { result = .failure(error) }
        else if let application = application { result = .success(application) }
        else { result = .failure(NSError(domain: "SyntheticEmptyLaunchReply", code: 1)) }
    }

    func read() -> Result<NSRunningApplication, Error>? {
        lock.lock()
        defer { lock.unlock() }
        return result
    }
}

private func workspaceLaunch(_ app: String, environment: [String: String], arguments: [String]) throws -> [String: Any] {
    let configuration = NSWorkspace.OpenConfiguration()
    configuration.createsNewApplicationInstance = true
    configuration.allowsRunningApplicationSubstitution = false
    configuration.activates = false
    configuration.addsToRecentItems = false
    configuration.promptsUserIfNeeded = false
    configuration.environment = environment
    configuration.arguments = arguments
    let reply = LaunchReply()
    NSWorkspace.shared.openApplication(at: URL(fileURLWithPath: app), configuration: configuration) {
        reply.complete($0, $1)
    }
    let deadline = Date().addingTimeInterval(10)
    while reply.read() == nil && Date() < deadline {
        RunLoop.current.run(until: Date().addingTimeInterval(0.02))
    }
    guard let result = reply.read() else { throw NSError(domain: "SyntheticLaunchTimeout", code: 1) }
    let application = try result.get()
    return ["pid": application.processIdentifier, "bundle": application.bundleURL?.path ?? "",
            "executable": application.executableURL?.path ?? ""]
}

private func driver(_ root: String, python: String) throws {
    try save(report(), to: root + "/driver.json")
    let app = root + "/Target.app"
    var common = ["HOME": root + "/home", "CFFIXED_USER_HOME": root + "/home", "TMPDIR": root + "/tmp",
                  "PATH": "/usr/bin:/bin", "SYNTHETIC_PYTHON": python,
                  "CODEX_SQLITE_HOME": root + "/history"]
    var receipts: [String: Any] = [:]
    do {
        _ = try workspaceLaunch(root + "/Missing.app", environment: common, arguments: [])
        throw NSError(domain: "SyntheticUnexpectedLaunch", code: 1)
    } catch {
        let failure = error as NSError
        guard !failure.domain.hasPrefix("Synthetic") else { throw error }
        receipts["missing_app_callback_failed"] = true
        receipts["missing_app_error_domain"] = failure.domain
        receipts["missing_app_error_code"] = failure.code
    }
    for slot in ["ls-one", "ls-two", "open", "direct"] {
        common["SYNTHETIC_SLOT"] = slot
        common["CODEX_HOME"] = root + "/" + slot + "/codex"
        common["CODEX_CLI_PATH"] = root + "/" + slot + "/backend"
        let arguments = ["target", root, "--user-data-dir=" + root + "/" + slot + "/electron"]
        if slot.hasPrefix("ls-") {
            receipts[slot] = try workspaceLaunch(app, environment: common, arguments: arguments)
        } else {
            let request = Process()
            request.executableURL = URL(fileURLWithPath: slot == "open" ? "/usr/bin/open" : python)
            request.arguments = slot == "open"
                ? ["-n", "-g", "-a", app, "--args"] + arguments
                : ["-I", "-B", root + "/chain.py", "request", root, app + "/Contents/MacOS/Fixture"]
            request.environment = common
            request.standardOutput = FileHandle.nullDevice
            request.standardError = FileHandle.nullDevice
            try request.run()
            request.waitUntilExit()
            receipts[slot + "_request_exit"] = request.terminationStatus
            guard request.terminationStatus == 0 else { throw NSError(domain: "SyntheticSpawn", code: 1) }
        }
    }
    try save(receipts, to: root + "/launched.json")
    let names = ["ls-one", "ls-two", "open", "direct"]
    let deadline = Date().addingTimeInterval(15)
    while !names.allSatisfy({ FileManager.default.fileExists(atPath: root + "/" + $0 + ".json") }) && Date() < deadline {
        RunLoop.current.run(until: Date().addingTimeInterval(0.02))
    }
    guard names.allSatisfy({ FileManager.default.fileExists(atPath: root + "/" + $0 + ".json") }) else {
        throw NSError(domain: "SyntheticObservationTimeout", code: 1)
    }
    try save(["done": true], to: root + "/driver-done.json")
    // Keep the responsible driver alive until the runner has checked the data.
    let stopDeadline = Date().addingTimeInterval(10)
    while !FileManager.default.fileExists(atPath: root + "/stop") && Date() < stopDeadline {
        RunLoop.current.run(until: Date().addingTimeInterval(0.02))
    }
}

let arguments = CommandLine.arguments
guard arguments.count >= 3, arguments[2].hasPrefix("/private/tmp/harbor-launch-responsibility-") else { exit(2) }
let application = NSApplication.shared
application.setActivationPolicy(.accessory)
application.finishLaunching()
do {
    if arguments[1] == "driver", arguments.count == 4 { try driver(arguments[2], python: arguments[3]) }
    else if arguments[1] == "target" { try target(arguments[2]) }
    else { throw NSError(domain: "SyntheticArguments", code: 1) }
} catch {
    try? save(["error": String(describing: error)], to: arguments[2] + "/error-" + String(getpid()) + ".json")
    exit(1)
}
