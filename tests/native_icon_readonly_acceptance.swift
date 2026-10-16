// Compile with Distribution.swift; the Python runner supplies synthetic apps only.
import AppKit
import Darwin
import Foundation

// Distribution's unrelated Python resolver refers to this shared error value.
// Avoid compiling the UI model or constructing any account/runtime state here.
struct EngineError: Error {
    var message: String
}

@main struct NativeIconReadOnlyAcceptance {
    static func main() throws {
        let arguments = CommandLine.arguments
        guard arguments.count == 4,
              let root = ProcessInfo.processInfo.environment["ICON_FIXTURE_ROOT"],
              root.hasPrefix("/private/tmp/harbor-icon-readonly-"),
              arguments[2].hasPrefix(root + "/") else {
            fatalError("Only the isolated icon fixture is permitted")
        }
        let mode = arguments[1]
        let app = URL(fileURLWithPath: arguments[2], isDirectory: true)
        let dark = arguments[3] == "dark"
        let image: NSImage?
        if mode == "canary" {
            // This request must be intercepted before reaching the kernel.
            let icon = app.appendingPathComponent("Contents/Resources/icon-codex-light.png")
            var attributes = attrlist()
            attributes.bitmapcount = UInt16(ATTR_BIT_MAP_COUNT)
            attributes.commonattr = UInt32(ATTR_CMN_USERACCESS)
            var buffer: UInt64 = 0
            let result = getattrlist(icon.path, &attributes, &buffer, MemoryLayout.size(ofValue: buffer), 0)
            let blocked = result == -1 && errno == EACCES
            print("{\"canary_blocked\":\(blocked)}")
            if !blocked { exit(1) }
            return
        } else if mode == "legacy-url" {
            image = CodexApplicationResources.iconURL(dark: dark, applications: [app])
                .flatMap(NSImage.init(contentsOf:))
        } else if mode == "production" {
            image = CodexApplicationResources.iconData(dark: dark, applications: [app])
                .flatMap(NSImage.init(data:))
        } else {
            fatalError("Unknown icon fixture mode")
        }
        var frame = CGRect(x: 0, y: 0, width: 2, height: 2)
        let decoded = image?.cgImage(forProposedRect: &frame, context: nil, hints: nil)
        var pixels = [UInt8](repeating: 0, count: 16)
        let rendered = pixels.withUnsafeMutableBytes { bytes -> Bool in
            guard let decoded,
                  let context = CGContext(data: bytes.baseAddress, width: 2, height: 2,
                                          bitsPerComponent: 8, bytesPerRow: 8,
                                          space: CGColorSpaceCreateDeviceRGB(),
                                          bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue |
                                              CGBitmapInfo.byteOrder32Big.rawValue) else { return false }
            context.draw(decoded, in: CGRect(x: 0, y: 0, width: 2, height: 2))
            return true
        }
        let result: [String: Any] = ["mode": mode, "loaded": image != nil,
                                   "rendered": rendered, "pixels": pixels,
                                   "width": decoded?.width ?? 0, "height": decoded?.height ?? 0]
        print(String(decoding: try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]),
                     as: UTF8.self))
    }
}
