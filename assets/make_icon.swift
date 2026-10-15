// Deterministic vector artwork. Generates only Harbor assets; no app/UI state.
import AppKit

let output = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
let iconset = output.appendingPathComponent("CodexHarborIcon.iconset", isDirectory: true)
try FileManager.default.createDirectory(at: iconset, withIntermediateDirectories: true)

func render(_ pixels: Int) -> Data {
    let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels,
        bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
        colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
    let context = NSGraphicsContext(bitmapImageRep: bitmap)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = context
    context.cgContext.scaleBy(x: CGFloat(pixels) / 1024, y: CGFloat(pixels) / 1024)

    let tile = NSBezierPath(roundedRect: NSRect(x: 80, y: 80, width: 864, height: 864),
                            xRadius: 202, yRadius: 202)
    NSColor(srgbRed: 0.09, green: 0.23, blue: 0.42, alpha: 1).setFill()
    tile.fill()
    // Two independent piers joined by one harbor: API and conversation storage.
    NSColor(srgbRed: 0.96, green: 0.98, blue: 1.0, alpha: 1).setFill()
    NSBezierPath(roundedRect: NSRect(x: 258, y: 304, width: 138, height: 454),
                 xRadius: 39, yRadius: 39).fill()
    NSBezierPath(roundedRect: NSRect(x: 628, y: 304, width: 138, height: 454),
                 xRadius: 39, yRadius: 39).fill()
    NSBezierPath(roundedRect: NSRect(x: 340, y: 478, width: 345, height: 122),
                 xRadius: 35, yRadius: 35).fill()
    NSColor(srgbRed: 0.30, green: 0.84, blue: 0.78, alpha: 1).setStroke()
    let water = NSBezierPath()
    water.move(to: NSPoint(x: 258, y: 250))
    water.curve(to: NSPoint(x: 512, y: 247), controlPoint1: NSPoint(x: 339, y: 191), controlPoint2: NSPoint(x: 422, y: 304))
    water.curve(to: NSPoint(x: 766, y: 250), controlPoint1: NSPoint(x: 602, y: 190), controlPoint2: NSPoint(x: 685, y: 307))
    water.lineWidth = 44; water.lineCapStyle = .round; water.stroke()
    NSGraphicsContext.restoreGraphicsState()
    return bitmap.representation(using: .png, properties: [:])!
}

for size in [16, 32, 128, 256, 512] {
    try render(size).write(to: iconset.appendingPathComponent("icon_\(size)x\(size).png"))
    try render(size * 2).write(to: iconset.appendingPathComponent("icon_\(size)x\(size)@2x.png"))
}
try render(1024).write(to: output.appendingPathComponent("CodexHarborIcon.png"))
