import AppKit
import SwiftUI

struct SidebarItem {
    let id: String
    var movable = true
    let version: String
    let content: AnyView
}

/// Own the native table instead of relying on SwiftUI List's private delegate.
/// AppKit handles drag cancellation, autoscroll and keyboard navigation; Harbor
/// owns rounded selection and the earlier, card-edge reorder threshold.
struct SortableSidebar: NSViewRepresentable {
    var items: [SidebarItem]
    var selected: String?
    var enabled: Bool
    var canMove: Bool
    var revision: String
    var onSelect: (String) -> Void
    var onMove: ([String]) async -> Bool

    func makeCoordinator() -> Coordinator { Coordinator(self) }
    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.drawsBackground = false; scroll.hasVerticalScroller = true
        scroll.hasHorizontalScroller = false; scroll.autohidesScrollers = true
        let table = SidebarTable()
        table.headerView = nil; table.style = .plain
        table.backgroundColor = .clear; table.selectionHighlightStyle = .none
        table.focusRingType = .none; table.intercellSpacing = NSSize(width: 0, height: 10)
        table.allowsMultipleSelection = false; table.allowsEmptySelection = true
        table.columnAutoresizingStyle = .lastColumnOnlyAutoresizingStyle
        let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier("card"))
        column.resizingMask = .autoresizingMask; table.addTableColumn(column)
        table.delegate = context.coordinator; table.dataSource = context.coordinator
        table.registerForDraggedTypes([Coordinator.pasteboardType])
        table.setDraggingSourceOperationMask(.move, forLocal: true)
        table.setDraggingSourceOperationMask([], forLocal: false)
        table.draggingDestinationFeedbackStyle = .gap
        scroll.documentView = table
        context.coordinator.table = table
        table.resized = { [weak coordinator = context.coordinator] in coordinator?.render() }
        table.trackingEnded = { [weak coordinator = context.coordinator] in coordinator?.endMouseTracking() }
        context.coordinator.update(self)
        return scroll
    }
    func updateNSView(_ view: NSScrollView, context: Context) { context.coordinator.update(self) }

    final class SidebarTable: NSTableView {
        var resized: (() -> Void)?
        var trackingEnded: (() -> Void)?
        var trackingMouse = false
        var mouseDownPoint: NSPoint?
        override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
        override func mouseDown(with event: NSEvent) {
            mouseDownPoint = convert(event.locationInWindow, from: nil)
            trackingMouse = true
            super.mouseDown(with: event)
            trackingMouse = false; trackingEnded?()
        }
        override func setFrameSize(_ newSize: NSSize) {
            let changed = abs(frame.width - newSize.width) > 1
            super.setFrameSize(newSize)
            if changed { resized?() }
        }
    }

    final class CardRow: NSTableRowView {
        override func drawSelection(in dirtyRect: NSRect) {}
        override var interiorBackgroundStyle: NSView.BackgroundStyle { .normal }
    }

    final class CardCell: NSTableCellView {
        let host = NSHostingView(rootView: AnyView(EmptyView()))
        override init(frame: NSRect) {
            super.init(frame: frame)
            host.autoresizingMask = [.width, .height]; addSubview(host)
        }
        required init?(coder: NSCoder) { nil }
        override func layout() { super.layout(); host.frame = bounds }
    }

    @MainActor final class Coordinator: NSObject, NSTableViewDataSource, NSTableViewDelegate {
        static let pasteboardType = NSPasteboard.PasteboardType("local.codex-harbor.sidebar-card")
        var parent: SortableSidebar
        weak var table: SidebarTable?
        var order: [String] = []
        let measuringHost = NSHostingView(rootView: AnyView(EmptyView()))
        var renderedVersions: [String: String] = [:]
        var renderedWidth: CGFloat = 0
        var renderedOrder: [String] = []
        var heights: [String: CGFloat] = [:]
        var drag: SidebarDragOrder?
        var dragRevision = ""
        var accepted = false
        var saving = false
        var rendering = false
        init(_ parent: SortableSidebar) { self.parent = parent }
        var movableIDs: [String] { parent.items.filter(\.movable).map(\.id) }

        func update(_ next: SortableSidebar) {
            parent = next
            // Polls may update login badges while dragging, but must not undo the
            // preview. A changed revision still invalidates the eventual drop.
            guard drag == nil, !saving, table?.trackingMouse != true else { return }
            order = next.items.map(\.id); render()
        }

        func endMouseTracking() {
            guard drag == nil, !saving else { return }
            order = parent.items.map(\.id); render(force: true)
        }

        func render(force: Bool = false) {
            guard let table, !rendering, drag == nil, !table.trackingMouse else { return }
            rendering = true; defer { rendering = false }
            let width = max(100, table.bounds.width - 4)
            var changed = force || order != renderedOrder || width != renderedWidth
            for item in parent.items {
                let version = item.version + "|\(parent.selected == item.id)|\(parent.enabled)|\(parent.canMove)"
                guard renderedVersions[item.id] != version || width != renderedWidth else { continue }
                measuringHost.rootView = cardContent(item)
                measuringHost.frame.size.width = width + 4
                heights[item.id] = max(44, measuringHost.fittingSize.height)
                renderedVersions[item.id] = version; changed = true
            }
            heights = heights.filter { order.contains($0.key) }
            renderedVersions = renderedVersions.filter { order.contains($0.key) }
            if changed { table.reloadData() }
            renderedOrder = order; renderedWidth = width; syncSelection()
        }

        func cardContent(_ item: SidebarItem) -> AnyView {
            AnyView(item.content.padding(.horizontal, 2).padding(.vertical, 1)
                .frame(width: max(104, table?.bounds.width ?? 104)).environment(\.isEnabled, parent.enabled))
        }

        func syncSelection() {
            guard let table else { return }
            let index = parent.selected.flatMap { order.firstIndex(of: $0) }
            let wasRendering = rendering; rendering = true
            table.selectRowIndexes(index.map { IndexSet(integer: $0) } ?? [], byExtendingSelection: false)
            rendering = wasRendering
        }

        func numberOfRows(in tableView: NSTableView) -> Int { order.count }
        func tableView(_ tableView: NSTableView, heightOfRow row: Int) -> CGFloat { heights[order[row]] ?? 100 }
        func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
            guard let item = parent.items.first(where: { $0.id == order[row] }) else { return nil }
            let identifier = NSUserInterfaceItemIdentifier("sidebar-card")
            let cell = tableView.makeView(withIdentifier: identifier, owner: nil) as? CardCell ?? CardCell()
            cell.identifier = identifier; cell.host.rootView = cardContent(item)
            return cell
        }
        func tableView(_ tableView: NSTableView, rowViewForRow row: Int) -> NSTableRowView? { CardRow() }
        func tableView(_ tableView: NSTableView, shouldSelectRow row: Int) -> Bool {
            parent.enabled && !saving && parent.items.first(where: { $0.id == order[row] })?.movable == true
        }
        func tableViewSelectionDidChange(_ notification: Notification) {
            guard !rendering, drag == nil, let table, order.indices.contains(table.selectedRow) else { return }
            let id = order[table.selectedRow]
            if id != parent.selected { parent.onSelect(id) }
            // Navigation can be deferred by the unsaved draft dialog.
            // Never let the table silently select a different underlying API.
            syncSelection()
        }
        func tableView(_ tableView: NSTableView, pasteboardWriterForRow row: Int) -> NSPasteboardWriting? {
            guard parent.enabled, parent.canMove, !saving, order.indices.contains(row),
                  parent.items.first(where: { $0.id == order[row] })?.movable == true else { return nil }
            // AppKit may call willBegin after mouseDown has already returned.
            // Freeze now, before that gap can trigger a SwiftUI/table refresh.
            let frames = movableIDs.compactMap { id in order.firstIndex(of: id).map { tableView.frameOfCell(atColumn: 0, row: $0) } }
            let startY = table?.mouseDownPoint?.y ?? tableView.frameOfCell(atColumn: 0, row: row).midY
            guard let transaction = SidebarDragOrder(ids: movableIDs, frames: frames, moving: order[row], pointerY: startY) else { return nil }
            drag = transaction; dragRevision = parent.revision; accepted = false
            let item = NSPasteboardItem(); item.setString(order[row], forType: Self.pasteboardType)
            return item
        }
        func tableView(_ tableView: NSTableView, draggingSession session: NSDraggingSession,
                       willBeginAt screenPoint: NSPoint, forRowIndexes rowIndexes: IndexSet) {
            session.animatesToStartingPositionsOnCancelOrFail = true
            // Keep AppKit's preview frame and cursor offset. Replacing them here
            // mixes source/gap coordinates and makes the image fly in from below.
        }
        func tableView(_ tableView: NSTableView, validateDrop info: NSDraggingInfo,
                       proposedRow row: Int, proposedDropOperation operation: NSTableView.DropOperation) -> NSDragOperation {
            guard info.draggingSource as? NSTableView === tableView,
                  parent.enabled, parent.canMove, parent.revision == dragRevision,
                  drag?.original == movableIDs, var transaction = drag else { return [] }
            let point = tableView.convert(info.draggingLocation, from: nil)
            _ = transaction.update(pointerY: point.y); drag = transaction
            let source = transaction.original.firstIndex(of: transaction.moving)!
            let target = transaction.order.firstIndex(of: transaction.moving)!
            // AppKit previews the gap. Never mutate rows during validateDrop:
            // its drag engine is still tracking those original row identities.
            tableView.setDropRow(target > source ? target + 1 : target, dropOperation: .above)
            return .move
        }
        func tableView(_ tableView: NSTableView, acceptDrop info: NSDraggingInfo,
                       row: Int, dropOperation operation: NSTableView.DropOperation) -> Bool {
            guard info.draggingSource as? NSTableView === tableView else { return false }
            return acceptLocalDrop(in: tableView)
        }
        func acceptLocalDrop(in tableView: NSTableView) -> Bool {
            accepted = parent.enabled && parent.canMove &&
                parent.revision == dragRevision && drag?.result(currentIDs: movableIDs, allowed: true) != nil
            if accepted, let transaction = drag,
               let result = transaction.result(currentIDs: movableIDs, allowed: true),
               let from = order.firstIndex(of: transaction.moving), let to = result.firstIndex(of: transaction.moving) {
                // Same transaction pattern as the AppKit drag-and-drop samples:
                // update data and move the existing row once, only at drop time.
                rendering = true
                tableView.beginUpdates()
                order = result + parent.items.filter { !$0.movable }.map(\.id)
                tableView.moveRow(at: from, to: to)
                tableView.endUpdates()
                renderedOrder = order
                rendering = false
            }
            return accepted
        }
        func tableView(_ tableView: NSTableView, draggingSession session: NSDraggingSession,
                       endedAt screenPoint: NSPoint, operation: NSDragOperation) {
            finishDrag(in: tableView, operation: operation)
        }
        func finishDrag(in tableView: NSTableView, operation: NSDragOperation) {
            let result = drag?.result(currentIDs: movableIDs,
                allowed: accepted && operation == .move && parent.enabled && parent.canMove && parent.revision == dragRevision)
            drag = nil; accepted = false
            // The gap style temporarily hides source rows. Let AppKit finish its
            // drag cleanup first, then restore even a rejected/no-op drop.
            DispatchQueue.main.async { [weak self] in
                guard let self, self.drag == nil else { return }
                tableView.unhideRows(at: tableView.hiddenRowIndexes, withAnimation: [])
                self.render(force: true)
            }
            guard let result else { order = parent.items.map(\.id); render(force: true); return }
            saving = true
            let save = parent.onMove
            let revisionAtDrop = dragRevision
            Task {
                let saved = await save(result)
                saving = false
                // SwiftUI may deliver the commit snapshot on its next update.
                // Keep a successful drop in place until then, rather than
                // briefly restoring the pre-commit parent order.
                if !saved || parent.revision != revisionAtDrop { order = parent.items.map(\.id) }
                render(force: true)
            }
        }
    }
}
