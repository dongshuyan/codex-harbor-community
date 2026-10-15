import CoreGraphics
import Foundation

/// A drag previews an order in memory. The caller commits only an accepted local
/// drop. Frozen slot geometry avoids feedback when the rows move under the mouse.
struct SidebarDragOrder {
    let original: [String]
    let moving: String
    private let frames: [CGRect]
    private let source: Int
    private let startY: CGFloat
    private var lastSwapY: CGFloat
    private var lastDirection: CGFloat = 0
    private(set) var order: [String]

    init?(ids: [String], frames: [CGRect], moving: String, pointerY: CGFloat) {
        guard ids.count == frames.count, Set(ids).count == ids.count,
              let source = ids.firstIndex(of: moving),
              frames.allSatisfy({ $0.height > 0 }) else { return nil }
        original = ids; order = ids; self.frames = frames; self.moving = moving
        self.source = source; startY = pointerY; lastSwapY = pointerY
    }

    mutating func update(pointerY: CGFloat) -> [String]? {
        let delta = pointerY - startY
        var target = source
        // The leading edge needs only 12 pt of overlap, independent of where the
        // card was grabbed. The gap by itself is not a request to swap rows.
        if delta < 0 {
            for index in (0..<source).reversed() {
                if frames[source].minY + delta <= frames[index].maxY - min(12, frames[index].height / 4) { target = index }
            }
        } else if delta > 0, source + 1 < original.count {
            for index in (source + 1)..<original.count {
                if frames[source].maxY + delta >= frames[index].minY + min(12, frames[index].height / 4) { target = index }
            }
        }
        let current = order.firstIndex(of: moving)!
        guard current != target else { return nil }
        let direction: CGFloat = target > current ? 1 : -1
        // Small hand tremors at a boundary must not make the cards bounce.
        guard direction == lastDirection || abs(pointerY - lastSwapY) >= 6 else { return nil }
        var next = original; next.remove(at: source); next.insert(moving, at: target)
        order = next; lastSwapY = pointerY; lastDirection = direction
        return next
    }

    func result(currentIDs: [String], allowed: Bool) -> [String]? {
        allowed && currentIDs == original && order != original ? order : nil
    }
}
