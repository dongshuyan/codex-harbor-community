import SwiftUI
import AppKit

struct StorageEditor: View {
    @ObservedObject var model: LauncherModel
    @Binding var location: StorageLocation
    let isNew: Bool
    let revision: Int
    var initialized: (StorageLocation) -> Void
    var existingHome: Bool { location.preserveMissing ?? ["original-local", "original-external"].contains(location.id) }
    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            Text(isNew ? "添加会话位置" : "编辑 · " + location.name).font(.title2.weight(.semibold))
            HarborCard(title: "名称与路径", subtitle: existingHome ? "直接使用此目录中的现有 Codex 历史；保留原配置与登录文件" : "保存只登记路径，不迁移或导入任何历史") {
                Text("位置名称").font(.callout.weight(.medium))
                TextField("例如：工作移动盘", text: $location.name).textFieldStyle(.roundedBorder)
                Divider()
                Text("目录路径").font(.callout.weight(.medium))
                TextField("绝对路径", text: $location.path).textFieldStyle(.roundedBorder).font(.callout.monospaced())
                HStack {
                    Text("更改仅更新路径登记，不移动、复制或删除原位置的文件。").font(.caption).foregroundStyle(.secondary)
                    Spacer()
                    Button("选择文件夹") { chooseFolder() }
                }
                Divider()
                Toggle("启用此位置", isOn: $location.enabled)
                Text("启用后可手动选择，也参与自动选择。自动选择按右侧优先级从上往下查找。").font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }.disabled(location.protected || model.busy)
            HarborCard(title: "使用规则") {
                Text("不同位置的历史独立保存，连接移动盘后不会自动合并。更改顺序不影响已经打开的窗口。").font(.callout).foregroundStyle(.secondary)
                Divider()
                Text(existingHome ? "历史目录离线时不会建立空目录。手动选中离线位置会停止启动；自动选择会使用下一个可用位置。" : "目录不存在时，先保存登记，再回到本页点击“建立目录”。手动选中的目录不可用时，启动会停止，不会切换到别处。").font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                if !isNew && !location.protected && !location.exists && !existingHome {
                    Button("建立已保存的目录") { Task {
                        if await model.action(["action":"initialize_storage","id":location.id,"revision":revision,"path":location.path],message:"已建立保存的目录。") {
                            if let saved = model.snapshot?.storages.first(where: { $0.id == location.id }) { initialized(saved) }
                        }
                    } }.disabled(model.busy || model.snapshot?.storages.first(where: { $0.id == location.id })?.path != location.path || model.snapshot?.storages.first(where: { $0.id == location.id })?.name != location.name || model.snapshot?.storages.first(where: { $0.id == location.id })?.enabled != location.enabled)
                }
            }
            if location.protected { Text("原版目录仅登记，保持停用；本次测试不会修改它。").foregroundStyle(.secondary) }
        }
    }
    func chooseFolder() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false; panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false; panel.canCreateDirectories = false
        panel.prompt = "选择此文件夹"
        if panel.runModal() == .OK, let url = panel.url { location.path = url.path }
    }
}
