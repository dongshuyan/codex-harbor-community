import SwiftUI

struct ProfileEditor: View {
    @ObservedObject var model: LauncherModel
    @Binding var profile: Profile
    @Binding var key: String
    @StateObject private var metadata=MetadataController()
    @State private var keyURL=""
    private var normalizedURL:String {(profile.url ?? "").trimmingCharacters(in:.whitespacesAndNewlines).replacingOccurrences(of:#"/+$"#,with:"",options:.regularExpression)}
    @State private var draftProbeID=UUID().uuidString.replacingOccurrences(of:"-",with:"").lowercased()
    private var discoveryID: String {
        // Kept only in memory; credentials are never logged or put in argv.
        ([profile.id,String(profile.revision ?? 0),profile.url ?? "",key,keyURL] + profile.models.map{$0.upstream+"/"+$0.parameters.mode}).joined(separator:"\u{0}")
    }
    private var canDiscover: Bool { profile.kind=="api" && !(profile.url ?? "").isEmpty && ((!key.isEmpty && keyURL==normalizedURL) || (key.isEmpty && profile.hasKey==true)) }
    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            Text(profile.revision == nil ? (profile.kind == "official" ? "添加官方账号" : "添加自定义 API") : "编辑 · " + profile.name).font(.title2.weight(.semibold))
            if model.isRunning(profile.id) {
                Text("此来源正在使用。为保护运行中的窗口，请先退出对应实例，再保存修改。").font(.callout).foregroundStyle(.orange)
            }
            HarborCard(title: "基本信息", subtitle: profile.kind == "official" ? "先保存名称，再通过 OpenAI 网页登录" : "API Key 仅保存在本机；不进入源码或 Git") {
                labeled("名称") { TextField("例如：我的 Router", text: $profile.name).textFieldStyle(.roundedBorder) }
                if profile.kind == "api" {
                    Divider()
                    labeled("基础 URL") { TextField("https://api.example.com/v1", text: Binding(get: { profile.url ?? "" }, set: { profile.url = $0 })).textFieldStyle(.roundedBorder) }
                    Text("填写服务商的 Responses 基础地址，不带 /responses 后缀。保存新地址表示将此 API 的 Key 用于该地址。").font(.caption).foregroundStyle(.secondary)
                    Divider()
                    labeled("API Key") { SecureField(profile.hasKey == true ? "留空保留已保存的 Key" : "粘贴 API Key", text: $key).textFieldStyle(.roundedBorder).textContentType(.password) }
                    if !key.isEmpty && keyURL != normalizedURL {
                        Text("基础 URL 已改变。确认完整地址后，才会把此 Key 用于获取参数或测活。").font(.caption).foregroundStyle(.orange)
                        Button("确认此地址并获取") {keyURL=normalizedURL}
                    }
                    Divider()
                    HStack { Text("协议").foregroundStyle(.secondary); Spacer(); Text("OpenAI Responses").fontWeight(.medium) }
                } else {
                    Text("授权完成后，登录文件放在这个账号专属的本机目录。一个实际账号请只建一个条目；切换会话位置无需复制凭据。").font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
            }
            if profile.kind == "api" {
                HStack {
                    VStack(alignment: .leading, spacing: 5) {
                        Text("模型映射").font(.headline)
                        Text("启动时启用全部映射；不在这里选启动模型。").font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("添加模型", systemImage: "plus") {
                        let used = Set(profile.models.map(\.alias))
                        if let next = model.snapshot?.models.first(where: { !used.contains($0.id) }) {
                            profile.models.append(ModelMapping(alias: next.id))
                        }
                    }.disabled(profile.models.count >= (model.snapshot?.models.count ?? 0))
                }
                if metadata.loading { Label("正在获取模型信息…",systemImage:"arrow.down.circle").font(.callout).foregroundStyle(.secondary) }
                if let message=metadata.message { Text(message).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal:false,vertical:true) }
                ForEach($profile.models) { $mapping in
                    MappingEditor(mapping:$mapping,options:model.snapshot?.models ?? [],profile:profile,apiKey:key,
                                  probeID:profile.id.isEmpty ? draftProbeID : profile.id,keyURL:keyURL,probes:model.probes,
                                  loading:metadata.loading,refresh:{Task {await fetch([mapping.upstream])}}) {
                        profile.models.removeAll { $0.id == mapping.id }
                    }
                }
            }
        }.disabled(model.busy)
        .onChange(of:key) {_,value in keyURL=value.isEmpty ? "" : normalizedURL; resetAutomaticValues()}
        .onChange(of:profile.url) {_,_ in keyURL="";resetAutomaticValues()}
        .task(id:discoveryID) {
            metadata.invalidate()
            guard canDiscover else {return}
            do {try await Task.sleep(for:.milliseconds(700))} catch {return}
            await fetch(profile.models.filter{$0.parameters.mode=="auto"}.map(\.upstream))
        }
        .onDisappear { metadata.invalidate() }
    }
    func resetAutomaticValues() {for index in profile.models.indices where profile.models[index].parameters.mode=="auto" {profile.models[index].usePreset()}}
    func fetch(_ names:[String]) async {
        let names=names.map{$0.trimmingCharacters(in:.whitespacesAndNewlines)}.filter{!$0.isEmpty}
        guard canDiscover, !names.isEmpty else {return}
        let identity=discoveryID
        if let result=await metadata.load(profile,key:key,names:names,keyURL:keyURL),identity==discoveryID {
            for index in profile.models.indices where profile.models[index].parameters.mode=="auto" {
                let name=profile.models[index].upstream.trimmingCharacters(in:.whitespacesAndNewlines)
                if let value=result.models[name] {profile.models[index].apply(value)}
            }
        }
    }
    func labeled<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 7) { Text(title).font(.callout.weight(.medium)); content() }
    }
}

struct MappingEditor: View {
    @Binding var mapping: ModelMapping
    let options: [ModelOption]
    let profile: Profile
    let apiKey: String
    let probeID: String
    let keyURL: String
    @ObservedObject var probes: ProbeCoordinator
    let loading: Bool
    var refresh: () -> Void
    var remove: () -> Void
    @State private var draftProbeRevision=Int.random(in:(-Int.max)...(-1))
    @State private var confirmRemoval=false
    @State private var probedDraft: Profile?
    @State private var probedKey=""
    var probeProfile: Profile { var p=profile;p.id=probeID;p.revision = draftProbeRevision;return p }
    var body: some View {
        // Capture while the draft exists. Disappearance happens after the parent
        // clears its binding; callbacks must not dereference that cleared draft.
        let leavingProfile=probeProfile
        let leavingAlias=mapping.alias
        return HarborCard(title:options.first{$0.id==mapping.alias}?.name ?? "模型映射") {
            HStack {
                Picker("Codex 名称",selection:$mapping.alias){ForEach(options){Text($0.name).tag($0.id)}}.disabled(probes.pendingCount(probeProfile)>0)
                Spacer()
                Button(role:.destructive){confirmRemoval=true}label:{Image(systemName:"trash")}.buttonStyle(.borderless).accessibilityLabel("移除模型映射").disabled(probes.pendingCount(probeProfile)>0)
            }
            TextField("实际 ModelName，例如 infi/glm-5.3",text:$mapping.upstream).textFieldStyle(.roundedBorder).accessibilityLabel("实际 ModelName").disabled(probes.pendingCount(probeProfile)>0)
            Text("映射名称只用于在 Codex 中选择。参数按上面实际 ModelName 获取。").font(.caption).foregroundStyle(.secondary)
            Divider()
            ModelParameterEditor(mapping:$mapping,refresh:refresh,loading:loading)
            Divider()
            draftProbe
        }
        .onChange(of:mapping.upstream){_,_ in mapping.usePreset()}
        .onDisappear {probes.cancel(leavingProfile,aliases:[leavingAlias],revisionOnly:true)}
        .confirmationDialog("移除这个模型映射？",isPresented:$confirmRemoval,titleVisibility:.visible){
            Button("移除映射",role:.destructive,action:remove);Button("取消",role:.cancel){}
        }message:{Text("保存 API 后才会生效，不会删除会话。")}
    }
    @ViewBuilder var draftProbe: some View {
        let state=probes.state(probeProfile,alias:mapping.alias)
        HStack {
            VStack(alignment:.leading,spacing:4){
                Text("测活当前模型").font(.callout.weight(.medium))
                Text("使用此刻草稿参数，不保存。仅输入 Reply OK.，总限时 60 秒；限额不足时按需补测。").font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            if let state,state.pending {
                ProgressView().controlSize(.small)
                Button(state.phase == .cancelling ? "取消中…" : "取消") {probes.cancel(probeProfile,aliases:[mapping.alias],revisionOnly:true)}.disabled(state.phase == .cancelling)
            } else {
                Button("测活") {
                    var draft=profile;draft.models=[mapping]
                    probedDraft=draft;probedKey=apiKey
                    probes.start(probeProfile,aliases:[mapping.alias],draft:draft,apiKey:apiKey,keyURL:keyURL)
                }.disabled(mapping.upstream.isEmpty || (apiKey.isEmpty && profile.hasKey != true) || mapping.context==0)
                    .accessibilityLabel("测活草稿 " + mapping.alias)
            }
        }
        if let result=state?.result {
            var current: Profile {var p=profile;p.models=[mapping];return p}
            if probedDraft==current && probedKey==apiKey {ProbeResultView(result:result)}
            else {Text("草稿已修改；上次测活结果仅对应修改前的参数。").font(.caption).foregroundStyle(.secondary)}
        }
    }
}
