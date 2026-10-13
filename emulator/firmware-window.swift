import AppKit
import Foundation

/// Decoupled builds (docs/architecture.md, M4): the import screen shown on first run and the firmware library
/// window. Both drive the bundled firmware library tool (Resources/fwlib/firmware.py, the same code path as
/// `native.py firmware`), so the key is read and sent to the import VM by that tool and never touches the app.
enum FirmwareTool {
    static var script: String? { Bundle.main.url(forResource: "fwlib", withExtension: nil)?.appendingPathComponent("firmware.py").path }
    static let python = "/usr/bin/python3"

    /// Run the tool; `progress` gets each JSON progress line from stderr. Returns the parsed stdout document.
    static func run(_ arguments: [String], progress: ((String) -> Void)? = nil) -> [String: Any] {
        guard let script = script, FileManager.default.isExecutableFile(atPath: python) else {
            return ["status": "error", "error": "Python 3 (/usr/bin/python3, Xcode Command Line Tools) is required to import firmware"]
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.arguments = ["-E", "-s", script] + arguments  // -I would drop the script folder (fwlib) from sys.path
        process.environment = ["PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"]
        let output = Pipe(), errors = Pipe()
        process.standardOutput = output; process.standardError = errors
        errors.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            for line in text.split(separator: "\n") { progress?(String(line)) }
        }
        do { try process.run() } catch { return ["status": "error", "error": "cannot run the firmware tool: \(error)"] }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        errors.fileHandleForReading.readabilityHandler = nil
        return (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? ["status": "error", "error": "no result from the firmware tool"]
    }

    /// Boot assets of this build (QEMU, base kernel and initramfs) from the bundled default configuration.
    static func boot(_ config: Configuration) -> [String] {
        var result = ["--qemu", config.qemu]
        for flag in ["-kernel", "-initrd"] {
            if let index = config.arguments.firstIndex(of: flag), index + 1 < config.arguments.count {
                result += ["--" + flag.dropFirst(), config.arguments[index + 1]]
            }
        }
        return result
    }

    static func describe(_ result: [String: Any]) -> String {
        var lines: [String] = []
        if let error = result["error"] as? String { lines.append(error) }
        if let report = result["report"] as? [String: Any] {
            lines.append("Status: \(report["status"] ?? "?")  Profile: \(report["profile"] ?? "none")")
            if let reason = report["reason"] as? String, !reason.isEmpty { lines.append("Reason: \(reason)") }
            for item in report["checks"] as? [[String: Any]] ?? [] {
                lines.append("[\(item["result"] ?? "?")] \(item["check"] ?? "")")
            }
        }
        return lines.joined(separator: "\n")
    }

    /// Start a new instance on the chosen firmware, then quit this one (its clean stop runs as usual).
    static func relaunch(firmware id: String) {
        let open = Process()
        open.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        open.arguments = ["-n", Bundle.main.bundlePath, "--args", "--firmware", id]
        try? open.run()
        open.waitUntilExit()
        NSApp.terminate(nil)
    }
}

/// First run (or File > Import): choose the SP-16 update and the key (key file or the official GPL source archive parts).
final class FirmwareImportWindow: NSObject, NSWindowDelegate {
    let config: Configuration
    let library: String
    let window: NSWindow
    let updateField = NSTextField(), keyField = NSTextField(), nameField = NSTextField()
    let keyKind = NSSegmentedControl(labels: ["Key file", "GPL archive"], trackingMode: .selectOne, target: nil, action: nil)
    let status = NSTextField(labelWithString: "")
    let report = NSTextView()
    let importButton = NSButton(title: "Import", target: nil, action: nil)
    let startButton = NSButton(title: "Start", target: nil, action: nil)
    var keyPaths: [String] = []
    var importedID: String?
    var onClose: (() -> Void)?

    init(config: Configuration, library: String, firstRun: Bool) {
        self.config = config; self.library = library
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 640, height: 460), styleMask: [.titled, .closable],
                          backing: .buffered, defer: false)
        super.init()
        window.title = firstRun ? "Import SP-16 firmware" : "Import firmware"
        window.delegate = self
        let intro = NSTextField(wrappingLabelWithString: firstRun
            ? "This emulator ships without any firmware. Choose your SP-16 update (.UPD) and its key: the key file, or the official GPL source archive (the .tar.gz, or both official zip parts in order). The key is used only for this import and is never stored."
            : "Choose an SP-16 update (.UPD) and its key file or GPL archive parts.")
        updateField.placeholderString = "SP-16 update (.UPD)"; updateField.isEditable = false
        keyField.placeholderString = "Key file or GPL archive parts"; keyField.isEditable = false
        nameField.placeholderString = "Name (optional)"
        keyKind.selectedSegment = 0
        keyKind.target = self; keyKind.action = #selector(keyKindChanged)
        let chooseUpdate = NSButton(title: "Choose…", target: self, action: #selector(chooseUpdate))
        let chooseKey = NSButton(title: "Choose…", target: self, action: #selector(chooseKey))
        importButton.target = self; importButton.action = #selector(startImport); importButton.keyEquivalent = "\r"
        startButton.target = self; startButton.action = #selector(startEmulator); startButton.isHidden = true
        report.isEditable = false; report.font = .monospacedSystemFont(ofSize: 11, weight: .regular)
        let scroll = NSScrollView(); scroll.documentView = report; scroll.hasVerticalScroller = true
        scroll.heightAnchor.constraint(equalToConstant: 200).isActive = true
        func row(_ views: [NSView]) -> NSStackView { let s = NSStackView(views: views); s.orientation = .horizontal; return s }
        let stack = NSStackView(views: [intro, row([updateField, chooseUpdate]), keyKind, row([keyField, chooseKey]),
                                        nameField, row([importButton, startButton, status]), scroll])
        stack.orientation = .vertical; stack.alignment = .leading; stack.spacing = 10
        stack.edgeInsets = NSEdgeInsets(top: 16, left: 16, bottom: 16, right: 16)
        for field in [updateField, keyField] { field.widthAnchor.constraint(equalToConstant: 480).isActive = true }
        nameField.widthAnchor.constraint(equalToConstant: 300).isActive = true
        intro.widthAnchor.constraint(equalToConstant: 600).isActive = true
        scroll.widthAnchor.constraint(equalToConstant: 600).isActive = true
        window.contentView = stack
        window.center()
        updateField.setAccessibilityLabel("Update file"); keyField.setAccessibilityLabel("Key source")
    }

    func show() { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
    func windowWillClose(_ notification: Notification) { onClose?() }

    @objc func keyKindChanged() { keyPaths = []; keyField.stringValue = "" }

    @objc func chooseUpdate() {
        let panel = NSOpenPanel(); panel.canChooseDirectories = false; panel.allowsMultipleSelection = false
        panel.message = "Choose the SP-16 update (.UPD)"
        if panel.runModal() == .OK, let url = panel.url { updateField.stringValue = url.path }
    }

    @objc func chooseKey() {
        let panel = NSOpenPanel(); panel.canChooseDirectories = false
        panel.allowsMultipleSelection = keyKind.selectedSegment == 1
        panel.message = keyKind.selectedSegment == 0 ? "Choose the key file"
            : "Choose the GPL archive (.tar.gz) or both zip parts; they are used in name order"
        if panel.runModal() == .OK {
            keyPaths = panel.urls.map { $0.path }.sorted()
            keyField.stringValue = keyPaths.map { ($0 as NSString).lastPathComponent }.joined(separator: ", ")
        }
    }

    @objc func startImport() {
        guard !updateField.stringValue.isEmpty, !keyPaths.isEmpty else { status.stringValue = "Choose the update and the key first."; return }
        var arguments = ["import", "--library", library] + FirmwareTool.boot(config) + ["--update", updateField.stringValue]
        arguments += keyKind.selectedSegment == 0 ? ["--key-file", keyPaths[0]] : ["--gpl-archive"] + keyPaths
        if !nameField.stringValue.isEmpty { arguments += ["--name", nameField.stringValue] }
        importButton.isEnabled = false
        status.stringValue = "Importing… (the update is decrypted and checked inside the emulator VM)"
        report.string = ""
        DispatchQueue.global(qos: .userInitiated).async {
            let result = FirmwareTool.run(arguments) { line in
                DispatchQueue.main.async { self.status.stringValue = "Importing… \(line)" }
            }
            DispatchQueue.main.async { self.finish(result) }
        }
    }

    func finish(_ result: [String: Any]) {
        importButton.isEnabled = true
        report.string = FirmwareTool.describe(result)
        if result["status"] as? String == "firmware_imported", let id = result["id"] as? String {
            importedID = id
            let boots = (result["report"] as? [String: Any])?["boots"] as? Bool ?? false
            status.stringValue = boots ? "Imported \(id)." : "Imported \(id), but it cannot start: no verified compatibility profile."
            startButton.isHidden = !boots
            if boots { _ = FirmwareTool.run(["select", "--library", library, "--id", id]) }
        } else {
            status.stringValue = "Import refused."
        }
    }

    @objc func startEmulator() {
        guard let id = importedID else { return }
        FirmwareTool.relaunch(firmware: id)
    }
}

/// Firmware > Library: list, select (restarts on that firmware), rename and remove imports; each has its own data.
final class FirmwareLibraryWindow: NSObject, NSTableViewDataSource, NSTableViewDelegate, NSWindowDelegate {
    let config: Configuration
    let library: String
    let current: String?
    let window: NSWindow
    let table = NSTableView()
    let status = NSTextField(labelWithString: "")
    var entries: [[String: Any]] = []
    var importWindow: FirmwareImportWindow?
    var onClose: (() -> Void)?

    init(config: Configuration, library: String, current: String?) {
        self.config = config; self.library = library; self.current = current
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 720, height: 360), styleMask: [.titled, .closable, .resizable],
                          backing: .buffered, defer: false)
        super.init()
        window.title = "Firmware library"
        window.delegate = self
        for (id, title, width) in [("name", "Name", 220.0), ("version", "Version", 70.0), ("kind", "Kind", 60.0),
                                   ("status", "Status", 90.0), ("id", "Id", 240.0)] {
            let column = NSTableColumn(identifier: NSUserInterfaceItemIdentifier(id))
            column.title = title; column.width = CGFloat(width)
            table.addTableColumn(column)
        }
        table.dataSource = self; table.delegate = self
        table.setAccessibilityLabel("Imported firmware")
        let scroll = NSScrollView(); scroll.documentView = table; scroll.hasVerticalScroller = true
        scroll.heightAnchor.constraint(equalToConstant: 240).isActive = true
        scroll.widthAnchor.constraint(equalToConstant: 690).isActive = true
        let buttons = NSStackView(views: [
            NSButton(title: "Start selected", target: self, action: #selector(startSelected)),
            NSButton(title: "Rename…", target: self, action: #selector(renameSelected)),
            NSButton(title: "Remove…", target: self, action: #selector(removeSelected)),
            NSButton(title: "Import…", target: self, action: #selector(importFirmware))])
        let stack = NSStackView(views: [scroll, buttons, status])
        stack.orientation = .vertical; stack.alignment = .leading
        stack.edgeInsets = NSEdgeInsets(top: 12, left: 12, bottom: 12, right: 12)
        window.contentView = stack
        window.center()
        reload()
    }

    func show() { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true) }
    func windowWillClose(_ notification: Notification) { onClose?() }

    func reload() {
        let result = FirmwareTool.run(["list", "--library", library])
        entries = result["firmware"] as? [[String: Any]] ?? []
        if let error = result["error"] as? String { status.stringValue = error }
        table.reloadData()
    }

    func numberOfRows(in tableView: NSTableView) -> Int { entries.count }

    func tableView(_ tableView: NSTableView, viewFor tableColumn: NSTableColumn?, row: Int) -> NSView? {
        guard let key = tableColumn?.identifier.rawValue else { return nil }
        var text = "\(entries[row][key] ?? "")"
        if key == "name", entries[row]["id"] as? String == current { text += " (running)" }
        return NSTextField(labelWithString: text)
    }

    var selected: [String: Any]? { table.selectedRow >= 0 && table.selectedRow < entries.count ? entries[table.selectedRow] : nil }

    @objc func startSelected() {
        guard let entry = selected, let id = entry["id"] as? String else { return }
        guard entry["status"] as? String == "verified" || config.firmware?.allow_unverified == true else {
            status.stringValue = "\(id) cannot start: \(entry["reason"] ?? "no verified compatibility profile")"
            return
        }
        _ = FirmwareTool.run(["select", "--library", library, "--id", id])
        FirmwareTool.relaunch(firmware: id)
    }

    @objc func renameSelected() {
        guard let id = selected?["id"] as? String else { return }
        let alert = NSAlert(); alert.messageText = "Rename \(id)"
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
        field.stringValue = selected?["name"] as? String ?? ""
        alert.accessoryView = field; alert.addButton(withTitle: "Rename"); alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let result = FirmwareTool.run(["rename", "--library", library, "--id", id, "--name", field.stringValue])
        status.stringValue = result["error"] as? String ?? "Renamed."
        reload()
    }

    @objc func removeSelected() {
        guard let id = selected?["id"] as? String else { return }
        guard id != current else { status.stringValue = "The running firmware cannot be removed."; return }
        let alert = NSAlert(); alert.messageText = "Remove \(id)?"
        alert.informativeText = "Its imported image, manifest and its own projects and settings (data image) are deleted."
        alert.addButton(withTitle: "Remove"); alert.addButton(withTitle: "Cancel")
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let result = FirmwareTool.run(["remove", "--library", library, "--id", id])
        status.stringValue = result["error"] as? String ?? "Removed."
        reload()
    }

    @objc func importFirmware() {
        let importer = FirmwareImportWindow(config: config, library: library, firstRun: false)
        importer.onClose = { [weak self] in self?.importWindow = nil; self?.reload() }
        importWindow = importer
        importer.show()
    }
}

/// App delegate for a decoupled build that has no usable firmware yet: only the import screen, no VM.
final class FirmwareSetupApplication: NSObject, NSApplicationDelegate {
    let importer: FirmwareImportWindow
    init(config: Configuration, library: String) {
        importer = FirmwareImportWindow(config: config, library: library, firstRun: true)
        super.init()
        importer.onClose = { NSApp.terminate(nil) }
    }
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        let menu = NSMenu(), appItem = NSMenuItem()
        menu.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        NSApp.mainMenu = menu
        importer.show()
    }
}

/// App delegate for a decoupled build with imports but none selected: the library window to pick one.
final class FirmwareLibrarySetupApplication: NSObject, NSApplicationDelegate {
    let libraryWindow: FirmwareLibraryWindow
    init(config: Configuration, library: String) {
        libraryWindow = FirmwareLibraryWindow(config: config, library: library, current: nil)
        super.init()
        libraryWindow.onClose = { NSApp.terminate(nil) }
    }
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        let menu = NSMenu(), appItem = NSMenuItem()
        menu.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        NSApp.mainMenu = menu
        libraryWindow.show()
    }
}
