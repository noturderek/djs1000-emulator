// Offscreen panel render with the app's own renderer (no window, works while the screen is locked).
// Build: swiftc -O -parse-as-library -o build/render-panel emulator/render-panel.swift emulator/panel-view.swift
// Run:   build/render-panel LAYOUT.json LAMPS.json LCD.png OUT.png [WIDTH]
// LAMPS.json maps control/lamp ids to [r, g, b] (panel_map.decode_lamps of a TX frame); LCD.png is an 800x480 snapshot.
import AppKit

@main
enum RenderPanel {
    static func main() {
        let a = CommandLine.arguments
        let layout = try! PanelLayout.load(from: URL(fileURLWithPath: a[1]))
        let lampJSON = try! JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: a[2]))) as! [String: [Int]]
        let width = CGFloat(a.count > 5 ? Double(a[5])! : Double(layout.canvas.width))
        let size = CGSize(width: width, height: width * layout.canvas.height / layout.canvas.width)
        let t = PanelTransform.fit(canvas: CGSize(width: layout.canvas.width, height: layout.canvas.height), in: CGRect(origin: .zero, size: size))
        let renderer = PanelRenderer(layout: layout)
        var state = PanelRenderState()
        state.feedbackLive = true
        for (k, v) in lampJSON { state.lamps[UInt8(k)!] = PanelView.Lamp(r: UInt8(v[0]), g: UInt8(v[1]), b: UInt8(v[2])) }
        if let img = NSImage(contentsOfFile: a[3]), let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) { state.lcdImage = cg }
        let staticImage = renderer.makeStaticLayer(viewSize: size, scale: 1, transform: t, lcdOnly: false)!
        let ctx = CGContext(data: nil, width: Int(size.width), height: Int(size.height), bitsPerComponent: 8, bytesPerRow: 0,
                            space: CGColorSpace(name: CGColorSpace.sRGB)!,
                            bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue)!
        // match NSView's flipped drawing: origin top-left
        ctx.translateBy(x: 0, y: size.height); ctx.scaleBy(x: 1, y: -1)
        NSGraphicsContext.current = NSGraphicsContext(cgContext: ctx, flipped: true)
        ctx.saveGState()
        ctx.translateBy(x: 0, y: size.height); ctx.scaleBy(x: 1, y: -1)
        ctx.draw(staticImage, in: CGRect(origin: .zero, size: size))
        ctx.restoreGState()
        ctx.translateBy(x: t.origin.x, y: t.origin.y); ctx.scaleBy(x: t.scale, y: t.scale)
        renderer.drawDynamic(ctx, state: state, scale: t.scale, dirty: CGRect(x: -1e5, y: -1e5, width: 2e5, height: 2e5))
        let rep = NSBitmapImageRep(cgImage: ctx.makeImage()!)
        try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: a[4]))
        print("wrote", a[4], Int(size.width), Int(size.height))
    }
}
