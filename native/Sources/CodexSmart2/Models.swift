import Foundation
import SwiftUI

struct ModelOption: Codable, Identifiable, Equatable {
    var id: String
    var name: String
    var efforts: [String]
    var context: Int
}

struct ModelMapping: Codable, Identifiable, Equatable {
    var id = UUID().uuidString
    var alias = "gpt-6-luna"
    var upstream = ""
    var effort = "low"
    var context = 128000
    var reasoning = "off"
    var levels: [String] = []
    var effortField = "reasoning.effort"
    var effortValues: [String:String] = [:]
    var parameters = ParameterState()
    enum CodingKeys: String, CodingKey {
        case alias, upstream, effort, context, reasoning, levels, parameters
        case effortField = "effort_field", effortValues = "effort_values"
    }
}

extension ModelMapping {
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        alias = try values.decode(String.self, forKey: .alias)
        // Server aliases are unique per profile. A stable decoded identity keeps
        // polling from replacing every row; draft edits keep this stored ID.
        id = alias
        upstream = try values.decode(String.self, forKey: .upstream)
        effort = try values.decode(String.self, forKey: .effort)
        context = try values.decode(Int.self, forKey: .context)
        reasoning = try values.decodeIfPresent(String.self, forKey: .reasoning) ?? "off"
        levels = try values.decodeIfPresent([String].self, forKey: .levels) ?? []
        effortField = try values.decodeIfPresent(String.self, forKey: .effortField) ?? "reasoning.effort"
        effortValues = try values.decodeIfPresent([String:String].self, forKey: .effortValues) ?? [:]
        parameters = try values.decodeIfPresent(ParameterState.self, forKey: .parameters) ?? .legacy
    }
}

struct Profile: Codable, Identifiable, Equatable {
    var id = UUID().uuidString.replacingOccurrences(of:"-",with:"").lowercased()
    var name = ""
    var kind = "api"
    var url: String? = ""
    var wireProtocol: String? = "responses"
    var models: [ModelMapping] = []
    var hasKey: Bool? = false
    var revision: Int?
    enum CodingKeys: String, CodingKey {
        case id, name, kind, url, models, revision
        case wireProtocol = "protocol", hasKey = "has_key"
    }
}

struct Job: Codable, Identifiable, Equatable {
    var id: String { job }
    var job: String
    var profileID: String
    var storage: String
    var status: String
    var pid: Int?
    var message: String?
    var exitCode: Int?
    enum CodingKeys: String, CodingKey {
        case job, storage, status, pid, message
        case profileID = "id"
        case exitCode = "exit_code"
    }
}

struct StorageLocation: Codable, Identifiable, Equatable {
    var id = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
    var name = ""
    var path = ""
    var enabled = true
    var protected = false
    var available = false
    var exists = false
    var reason = "尚未保存"
    var preserveMissing: Bool? = nil
    enum CodingKeys: String, CodingKey {
        case id, name, path, enabled, protected, available, exists, reason
        case preserveMissing = "preserve_missing"
    }
}

struct LoginState: Codable, Equatable {
    var state: String
    var credentialPresent: Bool
    var message: String?
    var maskedAccount: String?
    var accountEmail: String?
    var credentialRevision: String? = nil
    enum CodingKeys: String, CodingKey {
        case state, message
        case credentialPresent = "credential_present"
        case maskedAccount = "masked_account"
        case accountEmail = "account_email"
        case credentialRevision = "credential_revision"
    }
    var displayTitle: String {
        if state == "waiting" { return "等待浏览器授权" }
        if state == "starting" { return "正在开始授权" }
        if credentialPresent { return state == "success" ? "已登录（授权完成）" : "已保存登录" }
        return "尚未登录"
    }
    var displayDetail: String {
        if state == "success" && credentialPresent {
            return "已完成 OpenAI 网页授权。模型服务是否可用，以实际发送消息的结果为准。"
        }
        if state == "success" { return "之前完成过授权，但当前登录文件已不存在。请重新登录。" }
        if credentialPresent && ["error", "timeout", "cancelled"].contains(state) {
            return (message ?? "本次授权未完成。") + " 当前仍有登录文件，请核对上方账号。"
        }
        return message ?? (credentialPresent ? "已找到此账号的登录信息，可直接启动 Codex。" : "点击“在浏览器登录”完成官方授权。")
    }
}

struct Snapshot: Codable, Equatable {
    var profiles: [Profile]
    var removedProfiles: [Profile]
    var models: [ModelOption]
    var jobs: [Job]
    var storages: [StorageLocation]
    var removedStorages: [StorageLocation]
    var storageRevision: Int
    var busyProfiles: [String]
    var accounts: [String:LoginState]
    var profileOrderRevision: String? = nil
    enum CodingKeys: String, CodingKey {
        case profiles, models, jobs, storages, accounts
        case profileOrderRevision = "profile_order_revision"
        case storageRevision = "storage_revision", busyProfiles = "busy_profiles"
        case removedProfiles = "removed_profiles", removedStorages = "removed_storages"
    }
}

struct StorageSaveResult: Codable {
    var revision: Int
    var storages: [StorageLocation]
    var removedStorages: [StorageLocation]
    enum CodingKeys: String, CodingKey { case revision, storages; case removedStorages = "removed_storages" }
}

struct ProbeAttempt: Codable {
    var maxOutputTokens: Int
    var outcome: String
    var elapsedMs: Int
    var httpStatus: Int?
    var inputTokens: Int?
    var outputTokens: Int?
    enum CodingKeys: String, CodingKey {
        case outcome
        case maxOutputTokens = "max_output_tokens", elapsedMs = "elapsed_ms", httpStatus = "http_status"
        case inputTokens = "input_tokens", outputTokens = "output_tokens"
    }
}

struct ProbeResult: Codable {
    var outcome: String
    var message: String
    var model: String
    var elapsedMs: Int
    var httpStatus: Int?
    var replyOK: Bool?
    var inputTokens: Int?
    var outputTokens: Int?
    var attempts: [ProbeAttempt]?
    enum CodingKeys: String, CodingKey {
        case outcome, message, model, attempts
        case elapsedMs = "elapsed_ms", httpStatus = "http_status", replyOK = "reply_ok"
        case inputTokens = "input_tokens", outputTokens = "output_tokens"
    }
}

struct EngineError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

struct ProbeRequest {
    let profileID: String
    let revision: Int
    let alias: String
    let upstream: String
    let token: String
    var draft: Profile? = nil
    var apiKey: String = ""
    var keyURL: String = ""
}

struct ProbeKey: Hashable {
    let profileID: String
    let revision: Int
    let alias: String
    init(_ profile: Profile, _ alias: String) {
        profileID = profile.id; revision = profile.revision ?? 0; self.alias = alias
    }
}

struct ProbeState {
    enum Phase { case queued, running, cancelling, finished }
    var request: ProbeRequest
    var phase: Phase = .queued
    var result: ProbeResult?
    var cancelFailed = false
    var pending: Bool { phase != .finished }
}

/// Shared by all launcher windows. The queue limits total concurrent requests,
/// while revision + token identities fence stale results and cancellation.
@MainActor final class ProbeCoordinator: ObservableObject {
    static let concurrency = 4
    @Published private(set) var states: [ProbeKey: ProbeState] = [:]
    private var queue: [ProbeKey] = []
    private var running: [ProbeKey: String] = [:]
    private let request: (ProbeRequest) async throws -> ProbeResult
    private let cancelRequest: (String) async throws -> Void

    init(request: @escaping (ProbeRequest) async throws -> ProbeResult = { value in
        var input: [String:Any] = ["action":"probe", "id":value.profileID, "model":value.alias,
                                  "revision":value.revision, "probe_id":value.token]
        if let draft=value.draft {
            var object=try JSONSerialization.jsonObject(with:JSONEncoder().encode(draft)) as! [String:Any]
            object["api_key"]=value.apiKey; object["key_url"]=value.keyURL; input["profile"]=object; input["action"]="probe_draft"
        }
        let data = try await Engine.call(input)
        return try JSONDecoder().decode(ProbeResult.self, from:data)
    }, cancel: @escaping (String) async throws -> Void = { token in
        _ = try await Engine.call(["action":"cancel_probe", "probe_id":token])
    }) {
        self.request = request; cancelRequest = cancel
    }

    func state(_ profile: Profile, alias: String) -> ProbeState? { states[ProbeKey(profile, alias)] }
    func pendingCount(_ profile: Profile) -> Int { states.filter { $0.key.profileID == profile.id && $0.value.pending }.count }

    func start(_ profile: Profile, aliases: [String], draft: Profile? = nil, apiKey: String = "", keyURL: String = "") {
        guard profile.kind == "api" else { return }
        for alias in aliases {
            guard let mapping = profile.models.first(where: { $0.alias == alias }) else { continue }
            let key = ProbeKey(profile, alias)
            // Also fence an older revision that is still winding down.
            guard !states.contains(where: { $0.key.profileID == profile.id && $0.key.alias == alias && $0.value.pending }) else { continue }
            let value = ProbeRequest(profileID:profile.id, revision:profile.revision ?? 0, alias:alias,
                                     upstream:mapping.upstream, token:UUID().uuidString.replacingOccurrences(of:"-",with:"").lowercased(), draft:draft, apiKey:apiKey, keyURL:keyURL)
            states[key] = ProbeState(request:value)
            queue.append(key)
        }
        pump()
    }

    func cancel(_ profile: Profile, aliases: [String]? = nil, revisionOnly: Bool = false) {
        let keys = states.keys.filter { $0.profileID == profile.id && (!revisionOnly || $0.revision == (profile.revision ?? 0)) && (aliases == nil || aliases!.contains($0.alias)) }
        for key in keys {
            guard let value = states[key], value.pending else { continue }
            if value.phase == .queued {
                queue.removeAll { $0 == key }
                states[key]?.phase = .finished
                states[key]?.request.draft = nil; states[key]?.request.apiKey = ""
                states[key]?.result = ProbeResult(outcome:"cancelled", message:"已取消，未发送请求。", model:value.request.upstream, elapsedMs:0)
            } else if value.phase == .running {
                states[key]?.phase = .cancelling
                Task {
                    do { try await cancelRequest(value.request.token) }
                    catch {
                        guard states[key]?.request.token == value.request.token, states[key]?.phase == .cancelling else { return }
                        states[key]?.cancelFailed = true
                    }
                }
            }
        }
    }

    private func pump() {
        while running.count < Self.concurrency && !queue.isEmpty {
            let key = queue.removeFirst()
            guard let value = states[key], value.phase == .queued else { continue }
            states[key]?.phase = .running
            running[key] = value.request.token
            Task { await execute(key, value.request) }
        }
    }

    private func execute(_ key: ProbeKey, _ value: ProbeRequest) async {
        let result: ProbeResult
        do { result = try await request(value) }
        catch {
            result = ProbeResult(outcome:"preflight_error", message:(error as? EngineError)?.message ?? "测活组件未正常返回，未自动重试。", model:value.upstream, elapsedMs:0)
        }
        guard running[key] == value.token else { return }
        running.removeValue(forKey:key)
        if states[key]?.request.token == value.token {
            states[key]?.phase = .finished
            states[key]?.request.draft = nil; states[key]?.request.apiKey = ""
            states[key]?.result = result
        }
        pump()
    }
}

enum Engine {
    static func root(distribution: Distribution = .current,
                     environment: [String: String] = ProcessInfo.processInfo.environment,
                     bundleInfo: [String: Any] = Bundle.main.infoDictionary ?? [:],
                     home: URL = FileManager.default.homeDirectoryForCurrentUser) -> URL {
        Distribution.fixtureRoot(distribution: distribution, environment: environment, bundleInfo: bundleInfo) ?? distribution.dataRoot(home: home)
    }

    static func call(_ input: [String: Any]) async throws -> Data {
        let request = try JSONSerialization.data(withJSONObject: input)
        guard let resources = Bundle.main.resourceURL else { throw EngineError(message: "应用资源缺失。") }
        let script = resources.appendingPathComponent("engine/main.py").path
        let env = ProcessInfo.processInfo.environment
        let info = Bundle.main.infoDictionary ?? [:]
        let testRoot = Distribution.fixtureRoot(environment: env, bundleInfo: info)
        let root = root(environment: env, bundleInfo: info)
        let external = testRoot.flatMap { Distribution.fixtureExternal(root: $0, environment: env, bundleInfo: info) }
        return try await Task.detached {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: try PythonRuntime.resolve())
            process.arguments = ["-I", "-B", script, "--root", root.path]
            if let external { process.arguments! += ["--external", external.path] }
            else if testRoot != nil {
                process.arguments! += ["--test-external", root.deletingLastPathComponent().appendingPathComponent("external").path]
            }
            process.environment = ["HOME": FileManager.default.homeDirectoryForCurrentUser.path,
                                   "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin", "LANG": "en_US.UTF-8"]
            let output = Pipe(); let stdin = Pipe()
            process.standardOutput = output; process.standardInput = stdin
            process.standardError = FileHandle.nullDevice
            do { try process.run() }
            catch { throw EngineError(message: "无法启动 Python 组件。请确认已安装 Python 3.11 或更高版本，并重新打开应用。") }
            stdin.fileHandleForWriting.write(request)
            try? stdin.fileHandleForWriting.close()
            let data = output.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            guard process.terminationStatus == 0,
                  let envelope = try JSONSerialization.jsonObject(with: data) as? [String:Any] else {
                throw EngineError(message: "组件未正常返回，请检查 Python 和应用资源。")
            }
            guard envelope["ok"] as? Bool == true else {
                throw EngineError(message: envelope["error"] as? String ?? "操作失败。")
            }
            return try JSONSerialization.data(withJSONObject: envelope["result"] ?? [:])
        }.value
    }
}

@MainActor final class LauncherModel: ObservableObject {
    private let invoke: ([String:Any]) async throws -> Data
    init(invoke: @escaping ([String:Any]) async throws -> Data = Engine.call) {
        self.invoke=invoke; self.quotas=QuotaCoordinator(invoke:invoke)
    }
    @Published var snapshot: Snapshot?
    @Published var selection: String?
    @Published var storage = "auto"
    @Published var busy = false
    @Published var message: String?
    @Published var error: String?
    let probes = ProbeCoordinator()
    let quotas: QuotaCoordinator
    private var refreshGeneration = 0
    private var watchedLaunches: Set<String> = []
    private var expectedStops: Set<String> = []
    private var latestLaunch: String?
    var selected: Profile? { snapshot?.profiles.first { $0.id == selection } }
    var resolvedStorage: String { storage == "auto" ? (snapshot?.storages.first { $0.available }?.id ?? "") : storage }
    var resolvedLocation: StorageLocation? { snapshot?.storages.first { $0.id == resolvedStorage } }
    var currentRunning: Bool {
        if let p = selected, p.kind == "official", isRunning(p.id) { return true }
        return snapshot?.jobs.contains { $0.profileID == selection && $0.storage == resolvedStorage && ["running","starting"].contains($0.status) } ?? false
    }
    func isRunning(_ id: String) -> Bool { snapshot?.busyProfiles.contains(id) ?? false }
    func name(_ alias: String) -> String { snapshot?.models.first { $0.id == alias }?.name ?? alias }
    private func beginMutation() {
        busy = true; error = nil
        // Polls already in flight must not publish a pre-commit snapshot or
        // classify an exit before the stop response confirms its signal.
        refreshGeneration += 1
    }
    @discardableResult func refresh() async -> Bool {
        refreshGeneration += 1
        let generation = refreshGeneration
        do {
            let next = try JSONDecoder().decode(Snapshot.self, from: await invoke(["action":"status"]))
            guard generation == refreshGeneration else { return false }
            if snapshot != next { snapshot = next }
            quotas.sync(profiles:next.profiles,accounts:next.accounts)
            if selection == nil { selection = snapshot?.profiles.first?.id }
            for job in next.jobs where watchedLaunches.contains(job.job) {
                if job.status == "error", !(expectedStops.contains(job.job) && job.exitCode == -15) {
                    error = job.message ?? "独立窗口异常退出。"; message = nil
                }
                if job.status == "running", latestLaunch == job.job,
                   message == "正在启动独立窗口，请等待运行状态更新。" {
                    message = "独立窗口已启动；模型和续聊效果请在窗口内验收。"
                }
                // The child records running before exec. Keep watching until
                // its terminal state, including failures immediately after exec.
                if job.status == "error" || (job.status == "exited" && job.exitCode != nil) {
                    watchedLaunches.remove(job.job); expectedStops.remove(job.job)
                }
            }
            // Preserve an unavailable manual choice; never silently switch its destination.
            return true
        } catch { if generation == refreshGeneration { self.error = error.localizedDescription }; return false }
    }
    func save(_ profile: Profile, key: String) async -> Bool {
        guard snapshot != nil else { error="尚未读取配置状态，请先刷新成功后再保存。"; return false }
        beginMutation(); defer { busy = false }
        do {
            var object = try JSONSerialization.jsonObject(with: JSONEncoder().encode(profile)) as! [String:Any]
            object["api_key"] = key
            let saved = try JSONDecoder().decode(Profile.self, from: await invoke(["action":"save","profile":object]))
            // Commit responses are authoritative even when a later status read
            // fails. Deferred editors must never reopen the old revision.
            if var next = snapshot {
                if let index=next.profiles.firstIndex(where:{ $0.id==saved.id }) { next.profiles[index]=saved }
                else { next.profiles.append(saved) }
                snapshot = next
            }
            selection = saved.id
            let refreshed = await refresh()
            message = refreshed ? "已保存，供下次启动使用。" : "配置已保存；状态刷新失败，请先刷新确认。"
            return snapshot?.profiles.contains { $0.id == saved.id && $0.revision == saved.revision } == true
        } catch { self.error = error.localizedDescription; return false }
    }
    @discardableResult func action(_ input: [String:Any], message: String? = nil, refreshAfter: Bool = true) async -> Bool {
        beginMutation(); defer { busy = false }
        let stopping = input["action"] as? String == "stop" ? input["job"] as? String : nil
        do {
            let data = try await invoke(input)
            if let stopping, watchedLaunches.contains(stopping),
               let response=try JSONSerialization.jsonObject(with:data) as? [String:Any],
               response["signal_sent"] as? Bool == true { expectedStops.insert(stopping) }
            var authoritative = false
            if input["action"] as? String == "save_storages", var next = snapshot {
                let saved = try JSONDecoder().decode(StorageSaveResult.self,from:data)
                next.storages=saved.storages; next.storageRevision=saved.revision
                next.removedStorages=saved.removedStorages; snapshot=next; authoritative=true
            }
            if input["action"] as? String == "save_profile_order", var next=snapshot,
               let object=try JSONSerialization.jsonObject(with:data) as? [String:Any],
               let raw=object["profiles"], let revision=object["profile_order_revision"] as? String {
                next.profiles=try JSONDecoder().decode([Profile].self,from:JSONSerialization.data(withJSONObject:raw))
                next.profileOrderRevision=revision; snapshot=next; authoritative=true
            }
            // Sorting returns the complete committed collection and revision.
            // Release the UI immediately; the normal poll will refresh unrelated
            // login/job state. Unknown responses still require a status read.
            if authoritative && !refreshAfter {
                // A newly opened window can start a status read while this
                // mutation is in flight. It must not publish pre-commit state.
                refreshGeneration += 1
                self.message=message; return true
            }
            let refreshed = await refresh()
            guard refreshed || authoritative else {
                self.error="操作已完成，但状态刷新失败；请先刷新确认，不要重复执行。"
                return false
            }
            self.message = refreshed ? message : "设置已保存；状态刷新失败，请先刷新确认。"
            return true
        } catch {
            if let stopping { expectedStops.remove(stopping) }
            self.error = error.localizedDescription; return false
        }
    }
    func launch() async {
        guard let selection else { return }
        beginMutation(); defer { busy = false }
        do {
            let job = try JSONDecoder().decode(Job.self, from: await invoke(["action":"launch","id":selection,"storage":storage]))
            if job.status == "error" { throw EngineError(message: job.message ?? "启动失败。") }
            watchedLaunches.insert(job.job); latestLaunch = job.job
            message = job.status == "starting" ? "正在启动独立窗口，请等待运行状态更新。" : "独立窗口已启动；模型和续聊效果请在窗口内验收。"
            await refresh()
        } catch { self.error = error.localizedDescription }
    }
    func saveStorage(_ item: StorageLocation, revision: Int) async -> Bool {
        var items = snapshot?.storages ?? []
        if let index = items.firstIndex(where: { $0.id == item.id }) { items[index] = item }
        else { items.insert(item, at: items.firstIndex(where: { $0.protected }) ?? items.count) }
        return await saveStorages(items, revision: revision)
    }
    func saveStorages(_ items: [StorageLocation], revision: Int, refreshAfter: Bool = true) async -> Bool {
        guard snapshot != nil else { error="尚未读取位置状态，请先刷新成功后再保存。"; return false }
        do {
            let object = try JSONSerialization.jsonObject(with: JSONEncoder().encode(items))
            return await action(["action":"save_storages","storages":object,"revision":revision], message:"会话位置已保存。", refreshAfter:refreshAfter)
        } catch { self.error = error.localizedDescription; return false }
    }
    func login(_ p: Profile) async {
        beginMutation(); defer { busy = false }
        do {
            let data = try await invoke(["action":"login_start","id":p.id])
            let value = try JSONSerialization.jsonObject(with:data) as? [String:String]
            guard let raw = value?["auth_url"], let url = URL(string:raw), url.scheme == "https", url.host == "auth.openai.com" else {
                throw EngineError(message:"登录未返回可验证的 OpenAI 授权地址。")
            }
            guard NSWorkspace.shared.open(url) else { throw EngineError(message:"浏览器未能打开。请取消本次登录后重试。") }
            // The live account card owns login progress; a global banner would
            // otherwise keep asking for authorization after it has completed.
            message = nil
            await refresh()
        } catch { self.error = error.localizedDescription }
    }
}
