import SwiftUI

/// The model catalog and its probe controls remain in the launch overview.
struct ApiProbeView: View {
    @ObservedObject var model: LauncherModel
    @ObservedObject var probes: ProbeCoordinator
    let profile: Profile
    var body: some View {
        HarborCard(title: "全部模型映射 · \(profile.models.count)", subtitle: "启动后全部可选。映射名称不代表上游模型具备相同能力。") {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("测活范围：当前 API 的全部 \(profile.models.count) 个模型").font(.callout)
                    Text("最多 4 项并发 · 每项总限时 60 秒 · 限额不足时按需补测").font(.caption).foregroundStyle(.secondary)
                }
                Spacer(minLength: 4)
                if probes.pendingCount(profile) > 0 {
                    Text("\(probes.pendingCount(profile)) 项进行中").font(.caption).foregroundStyle(.secondary)
                    Button("取消") { probes.cancel(profile) }
                        .accessibilityLabel("取消当前 API 的全部测活")
                } else {
                    Button("全部测活") { probes.start(profile, aliases:profile.models.map(\.alias)) }
                        .buttonStyle(.borderedProminent).disabled(model.busy || profile.models.isEmpty)
                        .accessibilityLabel("全部测活：当前 API 的全部模型")
                        .help("仅测试当前 API 的全部模型；只有输出限额不足时才自动补测")
                }
            }
            Text("仅输入 Reply OK.，不携带历史或工具。从 32 token 起步，必要时逐档增加至 8192，最多尝试 5 次；其他错误不自动重试。").font(.caption).foregroundStyle(.secondary).fixedSize(horizontal:false,vertical:true)
            ForEach(profile.models) { mapping in
                Divider()
                modelRow(mapping)
            }
            Text("测活通过表示完成了一次短文本请求；桌面工具与续聊仍需单独验收。用量由上游报告。").font(.caption).foregroundStyle(.secondary).fixedSize(horizontal:false,vertical:true)
        }
    }

    func modelRow(_ mapping: ModelMapping) -> some View {
        let state = probes.state(profile, alias:mapping.alias)
        return VStack(alignment:.leading, spacing:10) {
            HStack(alignment:.top, spacing:12) {
                VStack(alignment:.leading, spacing:5) {
                    Text(model.name(mapping.alias)).font(.body.weight(.medium))
                    Text("→ " + mapping.upstream).font(.callout.monospaced()).foregroundStyle(.secondary)
                        .textSelection(.enabled).fixedSize(horizontal:false,vertical:true)
                    Text(mapping.reasoning == "off" ? "推理：服务默认值 · 不传参数" : "推理：" + mapping.levels.joined(separator:" / "))
                        .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal:false,vertical:true)
                }
                Spacer(minLength:0)
                if let state, state.pending {
                    Button(state.phase == .cancelling ? "取消中…" : "取消") { probes.cancel(profile, aliases:[mapping.alias]) }
                        .disabled(state.phase == .cancelling).accessibilityLabel("取消测活 " + mapping.upstream)
                } else {
                    Button("测活") { probes.start(profile, aliases:[mapping.alias]) }
                        .disabled(model.busy).accessibilityLabel("测活 " + mapping.upstream)
                }
            }
            if let state {
                if state.pending {
                    HStack(spacing:8) {
                        ProgressView().controlSize(.small)
                        Text(state.phase == .queued ? "排队中" : state.phase == .cancelling ? "正在取消…" : "测活中 · 按需补测，总限时 60 秒")
                    }.font(.caption).foregroundStyle(.secondary)
                    if state.cancelFailed {
                        Text("取消未确认；本次测活总限时 60 秒。").font(.caption).foregroundStyle(.orange)
                    }
                } else if let result = state.result {
                    ProbeResultView(result:result)
                }
            }
        }.padding(.vertical,3)
    }
}

struct ProbeResultView: View {
    let result: ProbeResult
    var title: String {
        switch result.outcome {
        case "success": return "测活通过"
        case "incomplete": return "已响应 · 回复未完成"
        case "budget_exhausted": return "未通过 · 已达测活限额"
        case "timeout": return "超时 · 已停止等待"
        case "cancelled": return "已取消"
        case "http_error": return "请求被拒绝"
        case "network_error": return "网络连接失败"
        default: return "未通过"
        }
    }
    var body: some View {
        VStack(alignment:.leading, spacing:6) {
            HStack(spacing:10) {
                Label(title, systemImage:result.outcome == "success" ? "checkmark.circle.fill" : result.outcome == "cancelled" ? "minus.circle" : "exclamationmark.circle")
                    .foregroundStyle(result.outcome == "success" ? Color.green : result.outcome == "cancelled" ? .secondary : .orange)
                if result.elapsedMs > 0 { Text("\(Double(result.elapsedMs) / 1000, specifier:"%.2f") 秒").foregroundStyle(.secondary) }
                if let status = result.httpStatus { Text("HTTP \(status)").foregroundStyle(.secondary) }
                if let attempts = result.attempts, attempts.count > 1 { Text("尝试 \(attempts.count) 次").foregroundStyle(.secondary) }
            }.font(.callout)
            if result.outcome != "success" {
                Text(result.message).font(.caption).foregroundStyle(.secondary).fixedSize(horizontal:false,vertical:true)
            }
            DisclosureGroup("详情") {
                VStack(alignment:.leading, spacing:5) {
                    if result.outcome == "success" { Text(result.message).fixedSize(horizontal:false,vertical:true) }
                    if let input = result.inputTokens, let output = result.outputTokens { Text("上游用量：输入 \(input) / 输出 \(output) token") }
                    if let attempts = result.attempts, !attempts.isEmpty {
                        Text("输出限额：" + attempts.map { String($0.maxOutputTokens) }.joined(separator:" → ") + " token")
                        ForEach(Array(attempts.enumerated()),id:\.offset) { index, attempt in
                            HStack {
                                Text("第 \(index+1) 次 · \(attempt.maxOutputTokens) token · \(Double(attempt.elapsedMs)/1000,specifier:"%.2f") 秒")
                                if let status = attempt.httpStatus { Text("HTTP \(status)") }
                            }
                        }
                    }
                    if result.replyOK == true { Text("回复 OK") }
                }.frame(maxWidth:.infinity,alignment:.leading).textSelection(.enabled)
            }.font(.caption).foregroundStyle(.secondary)
        }
    }
}
