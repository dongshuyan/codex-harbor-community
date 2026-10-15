import SwiftUI

enum CenterPage: String { case overview = "启动总览", api = "编辑 API", storage = "编辑位置", newAPI = "新建 API / 账号", recovery = "可恢复列表" }
struct Removal { var kind: String; var id: String; var name: String; var revision: Int; var permanent = false }

enum EditorDraft: Equatable {
    case profile(Profile)
    case storage(StorageLocation, revision: Int)
}

enum EditorTarget {
    case profile(String)
    case storage(String)

    func resolve(in snapshot: Snapshot?) -> EditorDraft? {
        guard let snapshot else { return nil }
        switch self {
        case .profile(let id):
            return snapshot.profiles.first { $0.id == id }.map(EditorDraft.profile)
        case .storage(let id):
            guard let location = snapshot.storages.first(where: { $0.id == id }) else { return nil }
            return .storage(location, revision: snapshot.storageRevision)
        }
    }
}

func orderedStorageLocations(_ items: [StorageLocation]) -> [StorageLocation] {
    items.filter { !$0.protected } + items.filter { $0.protected }
}

func reorderedStorageLocations(_ items: [StorageLocation], moving id: String, offset: Int) -> [StorageLocation]? {
    var movable = items.filter { !$0.protected }
    guard let index = movable.firstIndex(where: { $0.id == id }), movable.indices.contains(index + offset) else { return nil }
    movable.swapAt(index, index + offset)
    return movable + items.filter { $0.protected }
}

struct Dashboard: View {
    @ObservedObject var model: LauncherModel
    @ObservedObject private var probes: ProbeCoordinator
    @ObservedObject private var quotas: QuotaCoordinator
    @State private var page: CenterPage = .overview
    @State private var editorGeneration=UUID()
    @State private var draft: Profile?
    @State private var initialProfile: Profile?
    @State private var key = ""
    @State private var location: StorageLocation?
    @State private var initialLocation: StorageLocation?
    @State private var storageRevision = 0
    @State private var pending: (() -> Void)?
    @State private var askUnsaved = false
    @State private var removal: Removal?
    init(model: LauncherModel) { self.model = model; self.probes = model.probes; self.quotas = model.quotas }
    var dirty: Bool {
        draft != initialProfile || !key.isEmpty || location?.id != initialLocation?.id ||
        location?.name != initialLocation?.name || location?.path != initialLocation?.path ||
        location?.enabled != initialLocation?.enabled
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            HStack(spacing: 0) {
                apiSidebar.frame(width: 235)
                Divider()
                center.frame(maxWidth: .infinity)
                Divider()
                storageSidebar.frame(width: 255)
            }
        }.tint(Color(red: 0.16, green: 0.36, blue: 0.77))
        .background(DraftProtection(dirty:dirty,save:{ await saveDraft() }).frame(width:0,height:0))
        .alert("保留未保存的修改？", isPresented: $askUnsaved) {
            Button("保存并继续") { Task { if await saveDraft() { pending?(); pending = nil } } }
            Button("放弃修改", role: .destructive) { clearDraft(); pending?(); pending = nil }
            Button("继续编辑", role: .cancel) { pending = nil }
        } message: { Text("切换后，尚未保存的配置不会生效。API Key 只在本机保存。") }
        .confirmationDialog(removal?.permanent == true ? "永久删除此条目？" : "移到可恢复列表？", isPresented: Binding(get: { removal != nil }, set: { if !$0 { removal = nil } }), titleVisibility: .visible) {
            if let target = removal {
                Button((target.permanent ? "永久删除“" : "移除“") + target.name + "”", role: .destructive) {
                    Task { await remove(target) }
                }
            }
            Button("取消", role: .cancel) { removal = nil }
        } message: {
            Text(removal?.kind == "storage" ? "只移除位置登记，磁盘上的目录和所有会话都保留。" : removal?.permanent == true ? "删除此配置和专属凭据后不能恢复；会话记录保留。下次使用需要重新配置或登录。" : "配置及凭据暂时保留，可在可恢复列表中恢复；会话记录保留。")
        }
    }

    var header: some View {
        HStack(spacing: 12) {
            Image(systemName: "square.split.2x2.fill").font(.title2).foregroundStyle(Color.accentColor)
            Text(Distribution.current.appName).font(.title3.weight(.semibold))
            Text("账号与会话，各自选择").font(.callout).foregroundStyle(.secondary)
            Spacer()
            Text(Distribution.current.versionLabel).font(.caption).padding(.horizontal, 10).padding(.vertical, 6)
                .background(Color.accentColor.opacity(0.08), in: Capsule())
            Button { Task { await model.refresh() } } label: { Image(systemName: "arrow.clockwise") }
                .buttonStyle(.borderless).help("刷新状态").accessibilityLabel("刷新状态").disabled(model.busy)
        }.padding(.horizontal, 22).padding(.vertical, 17)
    }

    var apiSidebar: some View {
        let profiles=model.snapshot?.profiles ?? []
        let revision=model.snapshot?.profileOrderRevision ?? ""
        return VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text("API 来源").font(.subheadline.weight(.semibold)).foregroundStyle(.secondary)
                Spacer()
                Button { quotas.refresh() } label: { Image(systemName:"arrow.clockwise") }
                    .buttonStyle(.borderless).help("刷新所有官方账号额度")
                    .accessibilityLabel("刷新所有官方账号额度")
                    .disabled(model.busy || quotas.states.isEmpty || quotas.pendingCount>0)
            }
            AddSourceButton(title: "新建 API / 账号", detail: "官方登录或自定义 API") {
                navigate { clearDraft(); page = .newAPI }
            }.disabled(model.busy)
            SortableSidebar(items: profiles.map { p in
                SidebarItem(id: p.id, version: "\(p.name)|\(p.kind)|\(p.models.count)|\(model.snapshot?.accounts[p.id]?.accountEmail ?? "")|\(model.isRunning(p.id))|\(probes.pendingCount(p))|\(quotas.states[p.id]?.sidebarVersion ?? "")",
                            content: AnyView(apiRow(p)))
            },
                            selected: model.selection, enabled: !model.busy, canMove: !dirty,
                            revision: revision, onSelect: { id in
                guard !model.busy, id != model.selection else { return }
                navigate {
                    guard model.snapshot?.profiles.contains(where: { $0.id == id }) == true else { return }
                    clearDraft(); model.selection = id; page = .overview
                }
            }, onMove: { ids in await saveAPIOrder(ids, revision: revision) })
            Text("按住卡片拖动排序").font(.caption2).foregroundStyle(.secondary)
            Button { navigate { clearDraft(); page = .recovery } } label: {
                Label("可恢复列表 · \((model.snapshot?.removedProfiles.count ?? 0) + (model.snapshot?.removedStorages.count ?? 0))", systemImage: "arrow.uturn.backward.circle")
            }.buttonStyle(.borderless).disabled(model.busy)
        }.padding(16).background(Color(nsColor: .windowBackgroundColor))
    }

    func apiRow(_ p: Profile) -> some View {
        SelectionCard(selected: p.id == model.selection,
                      accessibilityLabel: p.name + "，" + (p.kind == "official" ? "GPT 官方账号" : "Responses") + (model.snapshot?.accounts[p.id]?.accountEmail.map { "，" + $0 } ?? ""),
                      action: nil) {
            VStack(alignment: .leading, spacing: 7) {
                HStack(alignment: .top, spacing: 9) {
                    AccountSourceIcon(official:p.kind == "official")
                        .font(.title3).foregroundStyle(p.id == model.selection ? Color.accentColor : .secondary)
                    VStack(alignment: .leading, spacing: 5) {
                        Text(p.kind == "official" ? (model.snapshot?.accounts[p.id]?.accountEmail ?? p.name) : p.name).font(.body.weight(.semibold)).foregroundStyle(.primary).fixedSize(horizontal:false,vertical:true)
                        if p.kind != "official" {
                            Text("Responses · \(p.models.count) 个模型").font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    Spacer(minLength: 0)
                    Image(systemName: "checkmark.circle.fill").foregroundStyle(Color.accentColor)
                        .opacity(p.id == model.selection ? 1 : 0)
                        .accessibilityHidden(true)
                }
                if p.kind=="official" {
                    Divider()
                    QuotaSummary(state:quotas.states[p.id],compact:true)
                }
            }
        } actions: {
            HStack {
                if model.isRunning(p.id) { Label("使用中", systemImage: "circle.fill").font(.caption2).foregroundStyle(.green).allowsHitTesting(false) }
                if probes.pendingCount(p) > 0 { Text("测活中 · \(probes.pendingCount(p))").font(.caption2).foregroundStyle(.secondary) }
                Spacer()
                if p.kind=="official" {
                    Button { quotas.refresh(ids:[p.id]) } label: { Image(systemName:"arrow.clockwise") }
                        .help("刷新 \(p.name) 的额度").accessibilityLabel("刷新 \(p.name) 的额度")
                        .disabled(quotas.states[p.id]==nil || quotas.states[p.id]?.pending==true)
                }
                Button { navigate { edit(.profile(p.id)) } } label: { Image(systemName: "pencil") }
                    .help("编辑 \(p.name)").accessibilityLabel("编辑 \(p.name)")
                    .disabled(probes.pendingCount(p) > 0)
                Button(role: .destructive) { navigate { removal = Removal(kind: "api", id: p.id, name: p.name, revision: p.revision ?? 0) } } label: { Image(systemName: "trash") }
                    .help("移除 \(p.name)").accessibilityLabel("移除 \(p.name)").disabled(model.isRunning(p.id) || probes.pendingCount(p) > 0)
            }.buttonStyle(.borderless)
        }.disabled(model.busy)
        .contextMenu {
            Button("移到最前") { Task { await moveAPIFirst(p.id) } }.disabled(model.busy || dirty)
        }
    }

    var storageSidebar: some View {
        let snapshot=model.snapshot
        let locations=orderedStorageLocations(snapshot?.storages ?? [])
        let movable=locations.filter{!$0.protected}
        let revision=snapshot?.storageRevision ?? 0
        return VStack(alignment: .leading, spacing: 16) {
            Text("会话位置").font(.subheadline.weight(.semibold)).foregroundStyle(.secondary)
            AddSourceButton(title: "新建会话位置", detail: "为一个文件夹命名") {
                navigate { clearDraft(); location = StorageLocation(); initialLocation = location; storageRevision = model.snapshot?.storageRevision ?? 0; page = .storage }
            }.disabled(model.busy)
                    SelectionCard(selected: model.storage == "auto", accessibilityLabel: "自动选择，按优先级使用首个可用位置",
                                  action: { navigate { clearDraft(); model.storage = "auto"; page = .overview } }) {
                            HStack {
                                Image(systemName: "arrow.triangle.branch")
                                VStack(alignment: .leading, spacing: 4) {
                                    Text("自动选择").font(.body.weight(.semibold))
                                    Text("按下面的顺序，使用首个可用位置").font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                                }
                                Spacer(minLength: 0)
                            }
                    } actions: { EmptyView() }
                    .disabled(model.busy)
            SortableSidebar(items: Array(locations.enumerated()).map { index, s in
                SidebarItem(id: s.id, movable: !s.protected, version: "\(s.name)|\(s.path)|\(s.reason)|\(s.available)|\(s.protected)|\(index)",
                            content: AnyView(storageRow(s, index: index)))
            }, selected: model.storage == "auto" ? nil : model.storage,
                            enabled: !model.busy, canMove: !dirty, revision: String(revision), onSelect: { id in
                guard !model.busy, id != model.storage, movable.contains(where: { $0.id == id }) else { return }
                navigate {
                    guard model.snapshot?.storages.contains(where: { $0.id == id && !$0.protected }) == true else { return }
                    clearDraft(); model.storage = id; page = .overview
                }
            }, onMove: { ids in await saveStorageOrder(ids, revision: revision) })
            Text("按住卡片拖动，调整自动选择的优先级。").font(.caption2).foregroundStyle(.secondary)
            Text("各位置的历史独立保存，不自动同步。现有 Codex 位置直接沿用原历史，账号与 API 配置由 Harbor 独立保存。").font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }.padding(16).background(Color(nsColor: .windowBackgroundColor))
    }

    func storageRow(_ s: StorageLocation, index: Int) -> some View {
        SelectionCard(selected: model.storage == s.id, accessibilityLabel: s.name + "，" + s.reason,
                      action: nil) {
                HStack(alignment: .top, spacing: 9) {
                    Image(systemName: s.protected ? "lock" : "externaldrive").font(.title3)
                    VStack(alignment: .leading, spacing: 5) {
                        Text(s.name).font(.body.weight(.semibold)).lineLimit(2)
                        Text(s.path).font(.caption2.monospaced()).foregroundStyle(.secondary)
                            .fixedSize(horizontal:false,vertical:true).help(s.path)
                        Text(s.protected ? "仅登记 · 未启用" : s.reason).font(.caption)
                            .foregroundStyle(s.available ? Color.green : .secondary)
                    }
                    Spacer(minLength: 0)
                }
        } actions: {
            if !s.protected {
                HStack(spacing: 10) {
                    Text("优先级 \(index + 1)").font(.caption2).foregroundStyle(.secondary).allowsHitTesting(false)
                    Spacer(minLength: 0)
                    Button { navigate { edit(.storage(s.id)) } } label: { Image(systemName: "pencil") }.accessibilityLabel("编辑位置 \(s.name)")
                    Button(role: .destructive) { navigate { removal = Removal(kind: "storage", id: s.id, name: s.name, revision: model.snapshot?.storageRevision ?? 0) } } label: { Image(systemName: "trash") }.accessibilityLabel("移除位置 \(s.name)")
                }.buttonStyle(.borderless)
            }
        }.disabled(model.busy || s.protected)
        .contextMenu {
            Button("提高优先级") {Task {await reorder(s,offset:-1)}}.disabled(model.busy || dirty || s.protected || index==0)
            Button("降低优先级") {Task {await reorder(s,offset:1)}}.disabled(model.busy || dirty || s.protected || index+1 >= (model.snapshot?.storages.filter{!$0.protected}.count ?? 0))
        }
    }

    var center: some View {
        VStack(spacing: 0) {
            HStack(spacing: 18) {
                tab(.overview)
                tab(.api)
                tab(.storage)
                Spacer()
                if dirty { Text("未保存").font(.caption).foregroundStyle(.orange) }
            }.padding(.horizontal, 26).padding(.vertical, 16)
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    if let error = model.error {
                        HStack(alignment: .top) {
                            Image(systemName: "exclamationmark.circle").foregroundStyle(.orange)
                            Text(error).font(.callout).fixedSize(horizontal: false, vertical: true)
                            Spacer()
                            Button { model.error = nil } label: { Image(systemName: "xmark") }.buttonStyle(.borderless).accessibilityLabel("关闭错误提示")
                        }.padding(14).background(Color.orange.opacity(0.08), in: RoundedRectangle(cornerRadius: 10))
                    }
                    if let message = model.message { Text(message).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true) }
                    switch page {
                    case .overview: overview
                    case .api:
                        if let current=draft {
                            let generation=editorGeneration
                            ProfileEditor(model:model,profile:Binding(get:{draft ?? current},set:{if editorGeneration==generation && draft != nil {draft=$0}}),key:$key)
                        }
                    case .storage:
                        if location != nil {
                            StorageEditor(model: model, location: Binding(get: { location! }, set: { location = $0 }), isNew: !(model.snapshot?.storages.contains { $0.id == location?.id } ?? false), revision: storageRevision) { saved in
                                location = saved; initialLocation = saved; storageRevision = model.snapshot?.storageRevision ?? 0
                            }
                        }
                    case .newAPI: newAPI
                    case .recovery: recovery
                    }
                }.padding(26).frame(maxWidth: .infinity, alignment: .leading)
            }.background(Color(nsColor: .textBackgroundColor))
            Divider()
            footer
        }
    }

    func tab(_ target: CenterPage) -> some View {
        Button {
            navigate {
                if target == .api, let p = model.selected { edit(.profile(p.id)) }
                else if target == .storage, let s = model.resolvedLocation { edit(.storage(s.id)) }
                else { clearDraft(); page = target == .overview ? target : .newAPI }
            }
        } label: {
            VStack(spacing: 7) {
                Text(target.rawValue).font(.body.weight(page == target ? .semibold : .regular))
                Capsule().fill(page == target ? Color.accentColor : .clear).frame(height: 2)
            }
        }.buttonStyle(.plain).foregroundStyle(page == target ? Color.accentColor : .secondary).disabled(model.busy || (target == .storage && model.resolvedLocation == nil) || (target == .api && (model.selected == nil || model.selected.map { probes.pendingCount($0) > 0 } == true)))
    }

    var footer: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 5) {
                Text((model.selected?.name ?? "选择 API") + "  ×  " + (model.resolvedLocation?.name ?? "选择位置")).font(.callout.weight(.semibold)).lineLimit(1)
                Text(dirty ? "请先保存修改，再启动" : "全部已配置模型 · 独立 Codex 窗口").font(.caption).foregroundStyle(.secondary)
            }
            Spacer(minLength: 4)
            if page == .api || page == .storage {
                Button("取消") { navigate { clearDraft(); page = .overview } }.disabled(model.busy)
                Button("保存") { Task { _ = await saveDraft() } }.buttonStyle(.borderedProminent).disabled(model.busy || !dirty || (draft.map { model.isRunning($0.id) } ?? false))
            } else {
                Button { Task { await model.launch() } } label: {
                    Label(model.currentRunning ? "已在运行" : "启动 Codex", systemImage: "arrow.up.right.square")
                        .padding(.vertical, 5)
                }.buttonStyle(.borderedProminent)
                    .disabled(model.busy || dirty || model.selected == nil || model.resolvedLocation?.available != true || model.currentRunning)
            }
        }.padding(19)
    }

    var overview: some View {
        VStack(alignment: .leading, spacing: 22) {
            VStack(alignment: .leading, spacing: 7) {
                Text("准备好，打开你的工作空间").font(.title2.weight(.semibold))
                Text("从两侧选择来源，在这里核对。具体模型在启动后的 Codex 中切换。").font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            HarborCard(title: "本次启动", subtitle: "API 与会话位置互相独立") {
                summaryRow("API 来源", model.selected?.name ?? "尚未选择", icon: "network")
                Divider()
                summaryRow("会话位置", model.resolvedLocation?.name ?? "没有可用位置", icon: "externaldrive")
                if let s = model.resolvedLocation {
                    Text(s.path).font(.caption.monospaced()).foregroundStyle(.secondary).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                    if !s.available { Text(s.reason + "；不会自动改用别处。").font(.callout).foregroundStyle(.orange) }
                }
            }
            if let p = model.selected {
                if p.kind == "api" { ApiProbeView(model:model, probes:probes, profile:p) }
                else { accountCard(p) }
            } else {
                HarborCard(title: "先添加一个来源") { Text("点击左上角“新建 API / 账号”，选择官方账号或自定义 Responses API。").foregroundStyle(.secondary) }
            }
            runningInstances
        }
    }

    func summaryRow(_ label: String, _ value: String, icon: String) -> some View {
        HStack { Label(label, systemImage: icon).foregroundStyle(.secondary); Spacer(); Text(value).fontWeight(.medium) }
    }

    func accountCard(_ p: Profile) -> some View {
        let state = model.snapshot?.accounts[p.id]
        return HarborCard(title: "官方账号", subtitle: "此账号实际可用的全部模型由 Codex 提供") {
            HStack {
                VStack(alignment: .leading, spacing: 5) {
                    Text(state?.displayTitle ?? "尚未登录")
                    if let email = state?.accountEmail {
                        Text(email).font(.callout).foregroundStyle(.secondary).textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if let detail = state?.displayDetail { Text(detail).font(.caption).foregroundStyle(.secondary) }
                }
                Spacer()
                if state?.state == "waiting" || state?.state == "starting" {
                    Button("取消登录") { Task { await model.action(["action":"login_cancel","id":p.id]) } }
                } else {
                    Button(state?.credentialPresent == true ? "重新授权" : "在浏览器登录") { Task { await model.login(p) } }
                        .disabled(model.busy || model.isRunning(p.id))
                }
            }
            Text("同一账号一次使用一个位置；不同账号可同时运行。授权成功仅证明登录完成，模型调用仍需独立验收。").font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            Divider()
            HStack {
                Text("剩余额度").font(.headline)
                Spacer()
                Button(quotas.states[p.id]?.pending==true ? "查询中…" : "刷新额度") { quotas.refresh(ids:[p.id]) }
                    .disabled(model.busy || quotas.states[p.id]==nil || quotas.states[p.id]?.pending==true)
            }
            QuotaSummary(state:quotas.states[p.id])
            Text("来自 Codex 官方额度接口。窗口在前台时每 5 分钟更新；重置时间统一显示完整日期与北京时间。只查询额度，不发送模型请求。未返回的周期不会补成满额。")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal:false,vertical:true)
        }
    }

    var newAPI: some View {
        VStack(alignment: .leading, spacing: 20) {
            Text("你要添加哪一种来源？").font(.title2.weight(.semibold))
            Text("先选择类型，再填写名称或连接信息。").foregroundStyle(.secondary)
            AddSourceButton(title: "GPT 官方账号", detail: "通过 OpenAI 网页授权，凭据仅保存在本机") { newProfile("official") }
            AddSourceButton(title: "自定义 API", detail: "Responses 协议 · 多模型映射 · 独立测活") { newProfile("api") }
        }
    }

    var recovery: some View {
        VStack(alignment: .leading, spacing: 20) {
            Text("可恢复列表").font(.title2.weight(.semibold))
            Text("移除先保留一份。永久删除只清除该条目，始终保留会话记录。").foregroundStyle(.secondary)
            HarborCard(title: "API 与账号") {
                if model.snapshot?.removedProfiles.isEmpty != false { Text("没有已移除的来源").foregroundStyle(.secondary) }
                ForEach(model.snapshot?.removedProfiles ?? []) { p in
                    HStack {
                        Text(p.name); Spacer()
                        Button("恢复") { Task { await model.action(["action":"restore","id":p.id,"revision":p.revision ?? 0], message:"来源已恢复。") } }
                        Button("永久删除", role: .destructive) { removal = Removal(kind:"api",id:p.id,name:p.name,revision:p.revision ?? 0,permanent:true) }
                    }
                }
            }
            HarborCard(title: "会话位置登记") {
                if model.snapshot?.removedStorages.isEmpty != false { Text("没有已移除的位置").foregroundStyle(.secondary) }
                ForEach(model.snapshot?.removedStorages ?? []) { s in
                    VStack(alignment: .leading, spacing: 6) {
                        HStack {
                            Text(s.name); Spacer()
                            Button("恢复") { Task { await model.action(["action":"restore_storage","id":s.id,"revision":model.snapshot?.storageRevision ?? 0], message:"位置登记已恢复。") } }
                            Button("永久删除", role: .destructive) { removal = Removal(kind:"storage",id:s.id,name:s.name,revision:model.snapshot?.storageRevision ?? 0,permanent:true) }
                        }
                        Text(s.path).font(.caption.monospaced()).foregroundStyle(.secondary)
                    }
                }
            }
        }.disabled(model.busy)
    }

    @ViewBuilder var runningInstances: some View {
        let jobs = model.snapshot?.jobs.filter { ["running","starting"].contains($0.status) } ?? []
        if !jobs.isEmpty {
            HarborCard(title: "运行中的窗口") {
                ForEach(jobs) { j in
                    HStack {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(model.snapshot?.profiles.first { $0.id == j.profileID }?.name ?? "独立窗口")
                            Text(model.snapshot?.storages.first { $0.id == j.storage }?.name ?? j.storage).font(.caption).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button("退出实例") { Task { await model.action(["action":"stop","job":j.job], message:"已请求退出这个实例。") } }.disabled(model.busy || j.status == "starting")
                    }
                }
                Text("续聊同一会话前先退出原窗口。另一窗口的列表可能需要重开后刷新。").font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    func navigate(_ action: @escaping () -> Void) {
        if dirty { pending = action; askUnsaved = true } else { action() }
    }
    func clearDraft() { editorGeneration=UUID(); draft = nil; initialProfile = nil; key = ""; location = nil; initialLocation = nil; model.message = nil }
    func edit(_ target: EditorTarget) {
        guard let current = target.resolve(in: model.snapshot) else {
            model.error = "这个条目已改变或移除，请刷新后重新选择。"
            return
        }
        clearDraft()
        switch current {
        case .profile(let p):
            model.selection = p.id; draft = p; initialProfile = p; page = .api
        case .storage(let s, let revision):
            location = s; initialLocation = s; storageRevision = revision; page = .storage
        }
    }
    func newProfile(_ kind: String) { clearDraft(); draft = Profile(kind:kind,models:kind == "api" ? [ModelMapping()] : []); initialProfile = draft; page = .api }
    func saveDraft() async -> Bool {
        guard !model.busy else { model.error="当前操作尚未完成，请稍后保存。"; return false }
        if let p = draft {
            guard await model.save(p,key:key) else { return false }
            clearDraft(); page = .overview; return true
        }
        if let s = location {
            guard await model.saveStorage(s,revision:storageRevision) else { return false }
            clearDraft(); page = .overview; return true
        }
        return true
    }
    func saveAPIOrder(_ ids:[String],revision:String) async -> Bool {
        guard !model.busy, !dirty, let snapshot=model.snapshot else{return false}
        guard revision==snapshot.profileOrderRevision else{model.error="API 列表已改变，请重新拖动。";return false}
        return await model.action(["action":"save_profile_order","ids":ids,"revision":revision],message:"API 顺序已保存。",refreshAfter:false)
    }
    func moveAPIFirst(_ id:String) async {
        guard let snapshot=model.snapshot,let target=snapshot.profiles.first?.id else{return}
        guard let ids=reorderedIDs(snapshot.profiles.map(\.id),moving:id,to:target) else{return}
        _ = await saveAPIOrder(ids,revision:snapshot.profileOrderRevision ?? "")
    }
    func saveStorageOrder(_ ids:[String],revision:Int) async -> Bool {
        guard !model.busy, !dirty, let snapshot=model.snapshot else{return false}
        guard revision==snapshot.storageRevision else{model.error="会话位置已改变，请重新拖动。";return false}
        let movable=snapshot.storages.filter{!$0.protected}
        guard ids.count==movable.count,Set(ids)==Set(movable.map(\.id)) else{model.error="会话位置已改变，请重新拖动。";return false}
        let lookup=Dictionary(uniqueKeysWithValues:movable.map{($0.id,$0)})
        return await model.saveStorages(ids.compactMap{lookup[$0]}+snapshot.storages.filter{$0.protected},revision:snapshot.storageRevision,refreshAfter:false)
    }
    func reorder(_ s: StorageLocation, offset: Int) async {
        guard !model.busy, !dirty, let snapshot = model.snapshot,
              let items = reorderedStorageLocations(snapshot.storages, moving: s.id, offset: offset) else { return }
        _ = await model.saveStorages(items,revision:snapshot.storageRevision,refreshAfter:false)
    }
    func remove(_ target: Removal) async {
        let action = target.kind == "storage" ? (target.permanent ? "purge_storage" : "remove_storage") : (target.permanent ? "purge" : "remove")
        let ok = await model.action(["action":action,"id":target.id,"revision":target.revision],message:target.permanent ? "条目已永久删除，会话目录保留。" : "已移入可恢复列表。")
        if ok && target.kind == "api" && model.selection == target.id { model.selection = model.snapshot?.profiles.first?.id; clearDraft(); page = .overview }
        removal = nil
    }
}

struct AccountSourceIcon: View {
    var official: Bool
    @Environment(\.colorScheme) private var scheme
    private var vendorIcon: NSImage? {
        CodexApplicationResources.iconURL(dark: scheme == .dark).flatMap(NSImage.init(contentsOf:))
    }
    var body: some View {
        if official, let icon=vendorIcon {
            Image(nsImage:icon).resizable().scaledToFit().frame(width:22,height:22).accessibilityLabel("Codex")
        } else {
            Image(systemName:official ? "terminal" : "network").accessibilityLabel(official ? "Codex" : "Router")
        }
    }
}
