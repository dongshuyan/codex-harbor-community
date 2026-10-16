import Foundation
import SwiftUI

struct QuotaWindow: Codable, Equatable, Identifiable {
    var id: String
    var remainingPercent: Double? = nil
    var durationMinutes: Int? = nil
    var resetsAt: Double? = nil
    enum CodingKeys: String, CodingKey {
        case id
        case remainingPercent = "remaining_percent", durationMinutes = "duration_minutes", resetsAt = "resets_at"
    }
    var durationLabel: String {
        guard let minutes=durationMinutes, minutes>0 else { return "额度窗口" }
        if minutes % 1440 == 0 { return "\(minutes/1440) 天" }
        if minutes % 60 == 0 { return "\(minutes/60) 小时" }
        return "\(minutes) 分钟"
    }
    var remainingText: String {
        guard let value=remainingPercent, value.isFinite else { return "未知" }
        return value.formatted(.number.precision(.fractionLength(0...1))) + "%"
    }
    func resetPassed(now: Double = Date().timeIntervalSince1970) -> Bool {
        resetsAt.map { $0 <= now } ?? false
    }
    func resetText(now:Double = Date().timeIntervalSince1970) -> String {
        guard let value=resetsAt, value.isFinite, value>0 else { return "重置时间未返回" }
        return Self.beijingTime(value) + (resetPassed(now:now) ? " 北京时间 · 等待更新" : " 北京时间重置")
    }
    static func beijingTime(_ value:Double) -> String {
        let formatter=DateFormatter()
        formatter.locale=Locale(identifier:"en_US_POSIX")
        formatter.timeZone=TimeZone(identifier:"Asia/Shanghai")
        formatter.dateFormat="yyyy-MM-dd HH:mm"
        return formatter.string(from:Date(timeIntervalSince1970:value))
    }
    var remainingColor: Color {
        guard let value=remainingPercent, value.isFinite else{return .secondary}
        return value<=10 ? .red : value<=30 ? .yellow : .green
    }
}

struct QuotaBucket: Codable, Equatable, Identifiable {
    var id: String
    var name: String
    var windows: [QuotaWindow]
}

struct QuotaResult: Codable, Equatable {
    var state: String
    var credentialRevision: String? = nil
    var checkedAt: Double? = nil
    var buckets: [QuotaBucket]? = nil
    enum CodingKeys: String, CodingKey {
        case state, buckets
        case credentialRevision = "credential_revision", checkedAt = "checked_at"
    }
}

struct QuotaState: Equatable {
    var revision: String
    var status = "idle"
    var result: QuotaResult? = nil
    var lastAttempt: Double? = nil
    var pending: Bool { status == "loading" || status == "queued" }
    var message: String {
        switch status {
        case "loading": return "正在查询额度…"
        case "queued": return "等待查询额度…"
        case "ready": return "额度已更新"
        case "empty": return "服务未返回额度窗口"
        case "timeout": return "查询超时，可稍后刷新"
        case "auth_required": return "登录信息需更新；打开此账号的 Codex 后刷新"
        case "unsupported": return "当前 Codex 版本不支持额度查询"
        case "busy": return "账号正在处理其他操作，稍后刷新"
        case "changed": return "登录已更新，等待刷新"
        case "unavailable": return "暂时无法获取额度，可稍后刷新"
        default: return "额度待查询"
        }
    }
    func stale(now: Double = Date().timeIntervalSince1970) -> Bool {
        guard let time=result?.checkedAt else { return true }
        return status != "ready" || now-time>=300 || now<time ||
            (result?.buckets?.contains { $0.windows.contains { $0.resetPassed(now:now) } } ?? false)
    }
    var updatedText: String {
        guard let time=result?.checkedAt else { return "尚无查询结果" }
        return "更新于 " + QuotaWindow.beijingTime(time) + " 北京时间"
    }
    var sidebarVersion: String {
        // Native reusable cells refresh only when their contents change.
        String(describing:self)
    }
}

@MainActor final class QuotaCoordinator: ObservableObject {
    @Published private(set) var states: [String:QuotaState] = [:]
    private let invoke: ([String:Any]) async throws -> Data
    private var order: [String] = []
    private var queue: [(id:String, revision:String)] = []
    private var active: Set<String> = []
    init(invoke: @escaping ([String:Any]) async throws -> Data = Engine.call) { self.invoke=invoke }
    var pendingCount: Int { states.values.filter(\.pending).count }
    func sync(profiles:[Profile], accounts:[String:LoginState]) {
        order=profiles.filter { $0.kind=="official" && accounts[$0.id]?.credentialPresent==true &&
            accounts[$0.id]?.credentialRevision != nil && !["starting","waiting"].contains(accounts[$0.id]?.state ?? "") }.map(\.id)
        let allowed=Set(order)
        var next=states.filter { allowed.contains($0.key) }
        for id in order {
            let revision=accounts[id]!.credentialRevision!
            if next[id]?.revision != revision { next[id]=QuotaState(revision:revision) }
        }
        if states != next { states=next }
        queue.removeAll { states[$0.id]?.revision != $0.revision }
    }
    func refreshDue(now:Double = Date().timeIntervalSince1970) {
        refresh(ids:order.filter { id in
            guard let state=states[id], !state.pending else{return false}
            guard let attempted=state.lastAttempt else{return true}
            return now-attempted>=300 || now<attempted
        },now:now)
    }
    func refresh(ids:[String]? = nil, now:Double = Date().timeIntervalSince1970) {
        for id in ids ?? order {
            guard var state=states[id], !state.pending, !active.contains(id) else{continue}
            // A manual click bypasses the cache; pending/active guards suppress duplicates.
            state.status="queued";state.lastAttempt=now;states[id]=state
            queue.append((id,state.revision))
        }
        pump()
    }
    private func pump() {
        while active.count<10 && !queue.isEmpty {
            let item=queue.removeFirst()
            guard states[item.id]?.revision==item.revision else{continue}
            active.insert(item.id);states[item.id]?.status="loading"
            Task {
                let result: QuotaResult
                do {
                    result=try JSONDecoder().decode(QuotaResult.self,from:await invoke(["action":"quota_read","id":item.id]))
                } catch { result=QuotaResult(state:"unavailable") }
                active.remove(item.id)
                if var state=states[item.id], state.revision==item.revision {
                    if let revision=result.credentialRevision, revision != item.revision {
                        state.status="changed";state.result=nil
                    } else {
                        state.status=result.state
                        if result.state=="ready" || result.state=="empty" {state.result=result}
                        if result.state=="auth_required" || result.state=="changed" {state.result=nil}
                    }
                    states[item.id]=state
                }
                pump()
            }
        }
    }
}

struct QuotaSummary: View {
    var state: QuotaState?
    var compact = false
    var body: some View {
        // Expiry remains visible without a new account/selection publication.
        // This redraw clock does not dispatch network requests.
        TimelineView(.periodic(from:.now,by:30)) { context in
            contents(now:context.date.timeIntervalSince1970)
        }
    }
    private func contents(now:Double) -> some View {
        VStack(alignment:.leading,spacing:compact ? 6 : 12) {
            if let state, let buckets=state.result?.buckets, !buckets.isEmpty {
                ForEach(compact ? Array(buckets.prefix(1)) : buckets) { bucket in
                    VStack(alignment:.leading,spacing:6) {
                        if bucket.id != "codex" || !compact && buckets.count>1 { Text(bucket.name).font(.caption.weight(.medium)) }
                        ForEach(bucket.windows) { window in
                            VStack(alignment:.leading,spacing:3) {
                                HStack {
                                    Text(window.durationLabel + "剩余")
                                    Spacer(minLength:6)
                                    Text(window.remainingText).monospacedDigit().fontWeight(.medium)
                                }
                                if let remaining=window.remainingPercent {
                                    GeometryReader { geometry in
                                        ZStack(alignment:.leading) {
                                            Capsule().fill(Color.secondary.opacity(0.16))
                                            Capsule().fill(state.stale(now:now) ? Color.secondary : window.remainingColor)
                                                .frame(width:geometry.size.width * max(0,min(100,remaining))/100)
                                        }
                                    }.frame(height:6)
                                        .accessibilityElement(children:.ignore)
                                        .accessibilityLabel(window.durationLabel + "剩余 " + window.remainingText)
                                }
                                Text(window.resetText(now:now)).foregroundStyle(.secondary)
                                    .fixedSize(horizontal:false,vertical:true)
                            }.help(window.resetText(now:now))
                        }
                    }
                }
                if compact, buckets.count>1 {Text("另有 \(buckets.count-1) 个额度分组 · 见总览").foregroundStyle(.secondary)}
                Text((state.stale(now:now) ? "上次结果 · " : "") + state.updatedText)
                    .foregroundStyle(.secondary).fixedSize(horizontal:false,vertical:true)
            }
            if state?.status != "ready" {Text(state?.message ?? "登录后查询额度").foregroundStyle(.secondary).fixedSize(horizontal:false,vertical:true)}
        }.font(compact ? .caption2 : .callout)
    }
}
