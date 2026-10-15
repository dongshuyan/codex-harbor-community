import AppKit
import SwiftUI

/// Tracks only in-memory editing callbacks. No draft or API Key is written to disk.
@MainActor final class DraftLedger {
    static let shared = DraftLedger()
    struct Entry { var dirty: Bool; var save: () async -> Bool }
    private var entries: [UUID: Entry] = [:]
    var hasDirty: Bool { entries.values.contains { $0.dirty } }
    func update(_ id: UUID, dirty: Bool, save: @escaping () async -> Bool) { entries[id] = Entry(dirty:dirty,save:save) }
    func remove(_ id: UUID) { entries.removeValue(forKey:id) }
    func saveAll() async -> Bool {
        for entry in Array(entries.values) where entry.dirty {
            if !(await entry.save()) { return false }
        }
        return true
    }
}

@MainActor private func draftDecision(quitting: Bool) -> NSApplication.ModalResponse {
    let alert = NSAlert()
    alert.alertStyle = .warning
    alert.messageText = "还有未保存的修改"
    alert.informativeText = "保存后再" + (quitting ? "退出" : "关闭") + "，或明确放弃本次修改。保存失败时会保留编辑页面。"
    alert.addButton(withTitle:quitting ? "保存并退出" : "保存并关闭")
    alert.addButton(withTitle:quitting ? "放弃并退出" : "放弃并关闭")
    alert.addButton(withTitle:"继续编辑")
    return alert.runModal()
}

@MainActor final class HarborAppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard DraftLedger.shared.hasDirty else { return .terminateNow }
        switch draftDecision(quitting:true) {
        case .alertFirstButtonReturn:
            Task { sender.reply(toApplicationShouldTerminate:await DraftLedger.shared.saveAll()) }
            return .terminateLater
        case .alertSecondButtonReturn: return .terminateNow
        default: return .terminateCancel
        }
    }
}

/// Keep SwiftUI's original delegate, forwarding every unhandled selector.
/// Closing a window and quitting the app are separate AppKit entry points.
struct DraftProtection: NSViewRepresentable {
    var dirty: Bool
    var save: () async -> Bool
    func makeCoordinator() -> Coordinator { Coordinator() }
    func makeNSView(context: Context) -> GuardView {
        let view = GuardView()
        view.attach = { [weak coordinator = context.coordinator] window in coordinator?.attach(window) }
        return view
    }
    func updateNSView(_ nsView: GuardView, context: Context) {
        context.coordinator.update(dirty:dirty,save:save)
        context.coordinator.attach(nsView.window)
    }
    static func dismantleNSView(_ nsView: GuardView, coordinator: Coordinator) { coordinator.detach() }

    final class GuardView: NSView {
        var attach: ((NSWindow?) -> Void)?
        override func viewDidMoveToWindow() { super.viewDidMoveToWindow(); attach?(window) }
    }
    @MainActor final class Coordinator: NSObject, NSWindowDelegate {
        let id = UUID()
        weak var window: NSWindow?
        var previous: NSWindowDelegate?
        var dirty = false
        var allowClose = false
        var saving = false
        var save: () async -> Bool = { true }
        func update(dirty: Bool, save: @escaping () async -> Bool) {
            self.dirty = dirty; self.save = save
            DraftLedger.shared.update(id,dirty:dirty,save:save)
        }
        func attach(_ target: NSWindow?) {
            guard let target, window !== target else { return }
            detach(); window = target; previous = target.delegate; target.delegate = self
            DraftLedger.shared.update(id,dirty:dirty,save:save)
        }
        func detach() {
            if let window, window.delegate === self { window.delegate = previous }
            window = nil; previous = nil; DraftLedger.shared.remove(id)
        }
        override func responds(to selector: Selector!) -> Bool {
            super.responds(to:selector) || previous?.responds(to:selector) == true
        }
        override func forwardingTarget(for selector: Selector!) -> Any? {
            if previous?.responds(to:selector) == true { return previous }
            return super.forwardingTarget(for:selector)
        }
        func windowShouldClose(_ sender: NSWindow) -> Bool {
            guard !saving else { return false }
            guard dirty && !allowClose else { return previous?.windowShouldClose?(sender) ?? true }
            switch draftDecision(quitting:false) {
            case .alertFirstButtonReturn:
                saving = true
                Task {
                    let success = await save(); saving = false
                    if success { allowClose = true; sender.performClose(nil) }
                }
                return false
            case .alertSecondButtonReturn: return previous?.windowShouldClose?(sender) ?? true
            default: return false
            }
        }
        func windowWillClose(_ notification: Notification) {
            DraftLedger.shared.remove(id)
            previous?.windowWillClose?(notification)
        }
    }
}
