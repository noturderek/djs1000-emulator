// Renders the macOS app icon iconset: a dark rounded tile with a metal rim and a logo.
//
// usage: make-icon <legacy-logo.svg> <output.iconset>
// The logo is a vector file such as assets/neutral-pads.svg, recoloured at load time (flat fills only).
// native.py runs this during `build` and turns the iconset into an .icns with iconutil.
import AppKit

let arguments = CommandLine.arguments
guard arguments.count == 3 else {
    FileHandle.standardError.write(Data("usage: make-icon <legacy-logo.svg> <output.iconset>\n".utf8))
    exit(2)
}

let canvas: CGFloat = 1024
let tile: CGFloat = 920            // matches the CDJ-3000 emulator's tile, so both sit alike in the Dock
let tileRadius: CGFloat = 210
let wordmarkWidth: CGFloat = 640
let smallWordmarkWidth: CGFloat = 780   // used up to 64 px so the wordmark stays readable in the Dock/Finder list sizes

let srgb = CGColorSpace(name: CGColorSpace.sRGB)!
func color(_ hex: UInt32, _ alpha: CGFloat = 1) -> CGColor {
    CGColor(colorSpace: srgb, components: [CGFloat((hex >> 16) & 255) / 255, CGFloat((hex >> 8) & 255) / 255,
                                           CGFloat(hex & 255) / 255, alpha])!
}

guard var logoText = try? String(contentsOfFile: arguments[1], encoding: .utf8) else {
    FileHandle.standardError.write(Data("cannot read \(arguments[1])\n".utf8)); exit(1)
}
logoText = logoText.replacingOccurrences(of: "fill:#[0-9a-fA-F]{6}", with: "fill:#eceeef", options: .regularExpression)
logoText = logoText.replacingOccurrences(of: "fill=\"#[0-9a-fA-F]{6}\"", with: "fill=\"#eceeef\"", options: .regularExpression)
guard let logo = NSImage(data: Data(logoText.utf8)), logo.size.width > 0 else {
    FileHandle.standardError.write(Data("cannot parse \(arguments[1])\n".utf8)); exit(1)
}

func gradient(_ stops: [(CGFloat, CGColor)]) -> CGGradient {
    CGGradient(colorsSpace: srgb, colors: stops.map { $0.1 } as CFArray, locations: stops.map { $0.0 })!
}

/// Draws the icon at `pixels` x `pixels` by scaling the 1024-point design, so every size is rendered, not resampled.
func render(_ pixels: Int) -> NSBitmapImageRep {
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixels, pixelsHigh: pixels, bitsPerSample: 8,
                               samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                               bytesPerRow: 0, bitsPerPixel: 0)!
    let graphics = NSGraphicsContext(bitmapImageRep: rep)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = graphics
    defer { NSGraphicsContext.restoreGraphicsState() }
    let ctx = graphics.cgContext
    ctx.scaleBy(x: CGFloat(pixels) / canvas, y: CGFloat(pixels) / canvas)
    let origin = (canvas - tile) / 2
    let frame = CGRect(x: origin, y: origin, width: tile, height: tile)
    let body = CGPath(roundedRect: frame, cornerWidth: tileRadius, cornerHeight: tileRadius, transform: nil)

    // Soft drop shadow under the tile.
    ctx.saveGState()
    ctx.setShadow(offset: CGSize(width: 0, height: -14), blur: 34, color: color(0x000000, 0.5))
    ctx.setFillColor(color(0x1b1c1f))
    ctx.addPath(body); ctx.fillPath()
    ctx.restoreGState()

    // Body: dark top-to-bottom gradient (y is up in this context).
    ctx.saveGState()
    ctx.addPath(body); ctx.clip()
    ctx.drawLinearGradient(gradient([(0, color(0x16171a)), (1, color(0x2d2e33))]),
                           start: CGPoint(x: 0, y: frame.minY), end: CGPoint(x: 0, y: frame.maxY), options: [])
    // Faint top sheen.
    ctx.drawLinearGradient(gradient([(0, color(0xffffff, 0)), (1, color(0xffffff, 0.07))]),
                           start: CGPoint(x: 0, y: frame.midY), end: CGPoint(x: 0, y: frame.maxY), options: [])
    ctx.restoreGState()

    // Metal rim: light at the top, dark at the bottom.
    ctx.saveGState()
    let rim = CGPath(roundedRect: frame.insetBy(dx: 4, dy: 4), cornerWidth: tileRadius - 4, cornerHeight: tileRadius - 4, transform: nil)
    ctx.addPath(rim)
    ctx.setLineWidth(8)
    ctx.replacePathWithStrokedPath()
    ctx.clip()
    ctx.drawLinearGradient(gradient([(0, color(0x3a3b40)), (0.55, color(0x7b7d83)), (1, color(0xbfc1c6))]),
                           start: CGPoint(x: 0, y: frame.minY), end: CGPoint(x: 0, y: frame.maxY), options: [])
    ctx.restoreGState()

    // Wordmark, centred, with a whisper of glow so it separates from the tile.
    let width = pixels <= 64 ? smallWordmarkWidth : wordmarkWidth
    let markHeight = width * logo.size.height / logo.size.width
    let markRect = CGRect(x: (canvas - width) / 2, y: (canvas - markHeight) / 2, width: width, height: markHeight)
    ctx.saveGState()
    ctx.setShadow(offset: .zero, blur: 18, color: color(0xffffff, 0.22))
    logo.draw(in: markRect)
    ctx.restoreGState()
    return rep
}

let sizes: [(String, Int)] = [("icon_16x16", 16), ("icon_16x16@2x", 32), ("icon_32x32", 32), ("icon_32x32@2x", 64),
                              ("icon_128x128", 128), ("icon_128x128@2x", 256), ("icon_256x256", 256),
                              ("icon_256x256@2x", 512), ("icon_512x512", 512), ("icon_512x512@2x", 1024)]
let directory = URL(fileURLWithPath: arguments[2])
try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
for (name, pixels) in sizes {
    guard let png = render(pixels).representation(using: .png, properties: [:]) else { exit(1) }
    try png.write(to: directory.appendingPathComponent(name + ".png"))
}
print("wrote \(sizes.count) icon sizes to \(directory.path)")
