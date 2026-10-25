// Launch the signed desktop through macOS, as Codex Smart does. The helper
// retains Harbor's leases; neither it nor Python becomes Codex's responsible app.
import AppKit
import Darwin
import Foundation

private struct Request: Decodable {
    let app: String
    let environment: [String: String]
    let arguments: [String]
    let activates: Bool?
    let launchOnly: Bool?
}

private func processStamp(_ pid: pid_t) throws -> String? {
    let process = Process(), pipe = Pipe()
    process.executableURL = URL(fileURLWithPath: "/bin/ps")
    process.arguments = ["-p", String(pid), "-o", "lstart="]
    var environment = ProcessInfo.processInfo.environment
    environment["LANG"] = "C"; environment["LC_ALL"] = "C"
    process.environment = environment
    process.standardOutput = pipe
    process.standardError = FileHandle.nullDevice
    try process.run()
    let data = pipe.fileHandleForReading.readDataToEndOfFile()
    process.waitUntilExit()
    let stamp = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
    return process.terminationStatus == 0 && stamp?.isEmpty == false ? stamp : nil
}

private final class Launch {
    let job: URL
    var state: [String: Any]
    let worker: pid_t
    var application: NSRunningApplication?
    var cancelled = false
    var stoppingAt: Date?
    var failedToWrite = false
    var sources: [DispatchSourceSignal] = []

    init(_ path: String) throws {
        job = URL(fileURLWithPath: path)
        let attributes = try FileManager.default.attributesOfItem(atPath: path)
        guard path.hasPrefix("/"), attributes[.type] as? FileAttributeType == .typeRegular,
              (attributes[.ownerAccountID] as? NSNumber)?.uint32Value == getuid(),
              let value = try JSONSerialization.jsonObject(with: Data(contentsOf: job)) as? [String: Any],
              value["job"] as? String == job.deletingPathExtension().lastPathComponent,
              let parent = value["worker_pid"] as? Int, parent > 1, parent == Int(getppid()),
              let stamp = value["worker_pid_start"] as? String,
              try processStamp(pid_t(parent)) == stamp else {
            throw NSError(domain: "HarborLaunchInvalidOwner", code: 1)
        }
        state = value
        worker = pid_t(parent)
    }

    @discardableResult func save() -> Bool {
        do {
            try JSONSerialization.data(withJSONObject: state, options: [.sortedKeys])
                .write(to: job, options: .atomic)
            return true
        } catch {
            failedToWrite = true
            cancelled = true
            return false
        }
    }

    func fail(_ message: String) -> Never {
        state["desktop_launch_phase"] = "failed"
        state["status"] = "error"
        state["message"] = message
        save()
        exit(1)
    }

    func begin(_ request: Request) throws {
        state["launch_helper_pid"] = Int(getpid())
        state["launch_helper_pid_start"] = try processStamp(getpid())
        guard getppid() == worker else { fail("启动进程已退出，未请求 macOS 启动。") }
        state["desktop_launch_phase"] = "requested"
        guard save() else { exit(1) }
        for number in [SIGTERM, SIGINT] {
            signal(number, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
            source.setEventHandler { self.cancelled = true }
            source.resume()
            sources.append(source)
        }
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.activates = request.activates ?? true
        configuration.createsNewApplicationInstance = true
        configuration.promptsUserIfNeeded = false
        configuration.environment = request.environment
        configuration.arguments = request.arguments
        let appURL = URL(fileURLWithPath: request.app).standardizedFileURL
        NSWorkspace.shared.openApplication(at: appURL, configuration: configuration) { app, error in
            DispatchQueue.main.async {
                guard let app = app else {
                    self.fail("macOS 未能启动 Codex（错误码 \((error as NSError?)?.code ?? -1)）。")
                }
                // A process that exits before check-in can have no bundleURL.
                // The callback proves it has ended; do not strand its slot or
                // signal the already-dead (and possibly reusable) PID.
                if app.isTerminated {
                    self.state["desktop_launch_phase"] = "exited"
                    self.state["status"] = "error"
                    self.state["message"] = "Codex 在系统确认启动前已退出，未留下运行窗口。"
                    self.save()
                    exit(1)
                }
                // Never act on an unrelated app if LaunchServices returns an
                // unexpected result. Preserve an unresolved intent instead.
                guard app.bundleURL?.standardizedFileURL == appURL else {
                    self.state["status"] = "error"
                    self.state["message"] = "macOS 返回的应用身份不匹配，无法确认启动结果。"
                    self.save()
                    exit(1)
                }
                self.application = app
                self.state["pid"] = Int(app.processIdentifier)
                self.state["pid_start"] = try? processStamp(app.processIdentifier)
                if self.state["pid_start"] == nil && !app.isTerminated {
                    self.failedToWrite = true
                    self.cancelled = true
                } else {
                    self.state["desktop_launch_phase"] = "running"
                    self.state["status"] = "running"
                    self.save()
                }
                // A controlled original entry only needs the verified macOS
                // callback. Native Codex owns its account and process lifetime.
                if request.launchOnly == true && !self.failedToWrite { exit(0) }
                self.tick()
            }
        }
        // There is no cancellation handle for an NSWorkspace launch. Keep the
        // leases and receive late callbacks even if the worker exits or a UI
        // timeout expires. Do not automatically retry a pending OS request.
        _ = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { _ in self.tick() }
        RunLoop.main.run()
    }

    func tick() {
        if getppid() != worker { cancelled = true }
        guard let app = application else { return }
        if app.isTerminated {
            state["desktop_launch_phase"] = "exited"
            state["status"] = failedToWrite ? "error" : "exited"
            if failedToWrite { state["message"] = "无法完整记录启动状态，已退出本次窗口。" }
            // An NSWorkspace app is not our child: its exit code is unknown.
            state.removeValue(forKey: "exit_code")
            save()
            exit(failedToWrite ? 1 : 0)
        }
        if cancelled {
            if let start = stoppingAt {
                if Date().timeIntervalSince(start) > 8 { app.forceTerminate() }
            } else {
                stoppingAt = Date()
                app.terminate()
            }
        }
    }
}

umask(0o077)
do {
    guard CommandLine.arguments.count == 2 else { exit(1) }
    let launch = try Launch(CommandLine.arguments[1])
    let data = FileHandle.standardInput.readDataToEndOfFile()
    guard data.count <= 2 * 1024 * 1024, let request = try? JSONDecoder().decode(Request.self, from: data) else {
        launch.fail("启动请求不完整，未请求 macOS 启动。")
    }
    try launch.begin(request)
} catch {
    // Do not print paths, environment variables or private configuration.
    exit(1)
}
