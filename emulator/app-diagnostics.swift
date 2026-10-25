import AppKit
import Darwin
import Foundation

/// Decoupled builds only: the app's own log, the clean-exit marker, the crash notice and Help > Collect Diagnostics.
/// The app is started through LaunchServices, so its stdout is lost; without these files a bug report would have
/// only the guest console. Nothing here is sent anywhere: the log stays in the build folder and the diagnostics zip
/// is written locally for the user to attach to a report.
///
/// Files, all owner-only:
///   <build>/logs/app-<time>-<pid>.log  events before a session exists (import screen, library actions)
///   <session>/app.log (+ .1)           everything from the moment the session exists (2 MB, one rotation)
///   <session>/clean-exit               written when the app quits normally (also after a refused start)
///   <session>/app.pid                  the app's process id, so a later launch can tell "still running" from "died"
///   <session>/qemu-stderr.txt          the last 200 lines of QEMU's stderr and its exit status

/// Personal identifiers out of log lines: the home folder becomes ~, the account name <user>, the host name <host>
/// and e-mail addresses <email>. The collector redacts again when it builds the zip.
enum LogRedactor {
    static let generic: Set<String> = ["root", "admin", "user", "guest", "test", "daemon", "nobody", "shell", "host", "localhost", "mac"]

    static let homes: [String] = {
        let home = NSHomeDirectory()
        let resolved = URL(fileURLWithPath: home).resolvingSymlinksInPath().path
        return Array(Set([home, resolved])).filter { $0.count > 1 }.sorted { $0.count > $1.count }
    }()

    static let words: [(regex: NSRegularExpression, replacement: String)] = {
        var result: [(NSRegularExpression, String)] = []
        var host = [CChar](repeating: 0, count: 256)
        gethostname(&host, host.count - 1)
        let hostName = String(cString: host)
        let candidates = [(NSUserName(), "<user>"), (hostName, "<host>"),
                          (String(hostName.split(separator: ".").first ?? ""), "<host>")]
        for (word, replacement) in candidates where word.count >= 3 && !generic.contains(word.lowercased()) {
            let pattern = "(?<![A-Za-z0-9])" + NSRegularExpression.escapedPattern(for: word) + "(?![A-Za-z0-9])"
            if let regex = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]) {
                result.append((regex, replacement))
            }
        }
        return result
    }()

    static let email = try? NSRegularExpression(pattern: "[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\\.[A-Za-z0-9-]+)+")

    static func text(_ input: String) -> String {
        var text = input
        if let email = email {
            text = email.stringByReplacingMatches(in: text, range: NSRange(text.startIndex..., in: text), withTemplate: "<email>")
        }
        for home in homes { text = text.replacingOccurrences(of: home, with: "~") }
        for (regex, replacement) in words {
            text = regex.stringByReplacingMatches(in: text, range: NSRange(text.startIndex..., in: text),
                                                  withTemplate: NSRegularExpression.escapedTemplate(for: replacement))
        }
        return text
    }
}

/// Small file logger: timestamped lines appended with one write each (so a crash loses at most the line being
/// written), a 2 MB cap and one rotation. Does nothing until `start` (builds with firmware config call it).
enum AppLog {
    static let limit = 2 * 1024 * 1024
    static let queue = DispatchQueue(label: "sp16.applog")
    static var enabled = false
    static var buildDirectory: String?
    static var sessionDirectory: String?
    static var path: String?
    static var fd: Int32 = -1
    static var size = 0
    static var early: [String] = []      // lines logged before the session exists, replayed into its app.log
    static var exitMarked = false
    static let stamp: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    /// Begin logging into <build>/logs (the build folder is the parent of the sessions folder).
    static func start(buildDirectory build: String) {
        queue.sync {
            guard !enabled else { return }
            enabled = true
            buildDirectory = build
            let logs = build + "/logs"
            try? FileManager.default.createDirectory(atPath: logs, withIntermediateDirectories: true,
                                                     attributes: [.posixPermissions: 0o700])
            prune(logs)
            let name = String(format: "app-%@-%d.log", fileStamp(), getpid())
            open(logs + "/" + name)
        }
        log("app start pid=\(getpid()) version=\(version()) macOS=\(ProcessInfo.processInfo.operatingSystemVersionString)")
    }

    static func fileStamp() -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        return formatter.string(from: Date())
    }

    static func version() -> String {
        let info = Bundle.main.infoDictionary
        let commit = (try? Data(contentsOf: Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/build-info.json")))
            .flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }?["git_commit"] as? String
        return "\(info?["CFBundleShortVersionString"] ?? "?") commit=\(commit ?? "unknown") bundle=\(Bundle.main.bundleIdentifier ?? "?")"
    }

    /// Keep the 20 newest build-level logs.
    static func prune(_ folder: String) {
        guard let names = try? FileManager.default.contentsOfDirectory(atPath: folder) else { return }
        let old = names.filter { $0.hasPrefix("app-") && $0.contains(".log") }.sorted()
        for name in old.dropLast(19) { try? FileManager.default.removeItem(atPath: folder + "/" + name) }
    }

    static func open(_ file: String) {
        if fd >= 0 { Darwin.close(fd) }
        path = file
        fd = Darwin.open(file, O_WRONLY | O_CREAT | O_APPEND, 0o600)
        var info = stat()
        size = fd >= 0 && fstat(fd, &info) == 0 ? Int(info.st_size) : 0
    }

    /// The session folder exists: from here on the log lives in <session>/app.log (earlier lines are replayed first).
    static func attach(session: String) {
        guard enabled else { return }
        let earlier: [String] = queue.sync {
            let lines = early
            early = []
            sessionDirectory = session
            return lines
        }
        log("session folder \(URL(fileURLWithPath: session).lastPathComponent) created; further events are in its app.log")
        queue.sync {
            open(session + "/app.log")
            for line in earlier { write(line) }
            let pid = session + "/app.pid"
            try? "\(getpid())\n".write(toFile: pid, atomically: true, encoding: .utf8)
            chmod(pid, 0o600)
        }
    }

    static func log(_ message: String) {
        guard enabled else { return }
        let line = stamp.string(from: Date()) + " " + LogRedactor.text(message).replacingOccurrences(of: "\n", with: "\n    ") + "\n"
        queue.async {
            if sessionDirectory == nil { early.append(line) }
            write(line)
        }
    }

    /// Caller holds `queue`.
    static func write(_ line: String) {
        guard fd >= 0, let path = path else { return }
        let bytes = Array(line.utf8)
        if size + bytes.count > limit {
            Darwin.close(fd)
            rename(path, path + ".1")
            fd = Darwin.open(path, O_WRONLY | O_CREAT | O_APPEND, 0o600)
            size = 0
            guard fd >= 0 else { return }
        }
        let written = bytes.withUnsafeBytes { Darwin.write(fd, $0.baseAddress, bytes.count) }
        if written > 0 { size += written }
    }

    /// A normal end: the next launch finds this file and does not offer diagnostics.
    static func markCleanExit(_ note: String) {
        guard enabled else { return }
        log("exit: \(note)")
        queue.sync {
            guard !exitMarked, let session = sessionDirectory else { return }
            exitMarked = true
            let marker = session + "/clean-exit"
            try? (ISO8601DateFormatter().string(from: Date()) + " " + note + "\n").write(toFile: marker, atomically: true, encoding: .utf8)
            chmod(marker, 0o600)
        }
    }
}

/// QEMU's stderr: still copied into console.txt as before, and kept (last 200 lines plus the exit status) in
/// qemu-stderr.txt. The file is appended as it arrives, so it survives an app crash, then rewritten at the end.
final class StderrCapture {
    let pipe = Pipe()
    let console: FileHandle?
    let file: String
    let queue = DispatchQueue(label: "sp16.qemu-stderr")
    var lines: [String] = []
    var partial = ""
    var written = 0
    var eof = false
    var exitText: String?
    var finished = false

    init(console: FileHandle?, file: String) {
        self.console = console; self.file = file
        FileManager.default.createFile(atPath: file, contents: nil, attributes: [.posixPermissions: 0o600])
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard let self = self else { handle.readabilityHandler = nil; return }
            if data.isEmpty { handle.readabilityHandler = nil }
            self.queue.async { self.received(data) }
        }
    }

    func received(_ data: Data) {
        if data.isEmpty { eof = true; finishIfReady(); return }
        console?.write(data)
        if written < 1 << 20, let handle = FileHandle(forWritingAtPath: file) {
            handle.seekToEndOfFile(); handle.write(data); try? handle.close(); written += data.count
        }
        partial += String(decoding: data, as: UTF8.self)
        var parts = partial.components(separatedBy: "\n")
        partial = parts.removeLast()
        lines += parts
        if lines.count > 200 { lines.removeFirst(lines.count - 200) }
    }

    /// The process ended: record how, then write the final file once the pipe has drained (or after a second).
    func terminated(status: Int32, reason: Process.TerminationReason) {
        queue.async {
            self.exitText = "exit status \(status) (\(reason == .exit ? "exited" : "killed by signal"))"
            self.finishIfReady()
            self.queue.asyncAfter(deadline: .now() + 1) { self.finish() }
        }
    }

    func finishIfReady() { if eof && exitText != nil { finish() } }

    func finish() {
        guard !finished, let exit = exitText else { return }
        finished = true
        if !partial.isEmpty { lines.append(partial); partial = "" }
        let text = LogRedactor.text(lines.suffix(200).joined(separator: "\n"))
        let body = (text.isEmpty ? "" : text + "\n") + "[QEMU \(exit)]\n"
        try? body.write(toFile: file, atomically: true, encoding: .utf8)
        chmod(file, 0o600)
        AppLog.log("QEMU \(exit); stderr lines kept: \(lines.count)")
    }
}

/// At launch: did the previous session end without the clean-exit marker, or did macOS write a crash report for
/// this app since then? Only sessions that have an app.log (written by this feature) count, so a session from an
/// older build of the app never triggers it.
enum CrashCheck {
    /// Where macOS writes crash reports, and this app's bundle id (matched inside the report's header).
    static var reportsDirectory = NSHomeDirectory() + "/Library/Logs/DiagnosticReports"
    static var bundleID = Bundle.main.bundleIdentifier ?? "-"

    static func isRunning(pid: Int32) -> Bool {
        guard pid > 1, kill(pid, 0) == 0 || errno == EPERM else { return false }
        var buffer = [CChar](repeating: 0, count: 4096)
        return proc_pidpath(pid, &buffer, UInt32(buffer.count)) > 0 && String(cString: buffer).contains("DJS1KReplicator")
    }

    static func modified(_ path: String) -> Date? {
        (try? FileManager.default.attributesOfItem(atPath: path))?[.modificationDate] as? Date
    }

    /// A short description of what was found, or nil. `current` is this launch's own session (not checked).
    static func findProblem(build: String, current: String?) -> (text: String, session: String?)? {
        let fm = FileManager.default
        let sessions = build + "/sessions"
        let names = ((try? fm.contentsOfDirectory(atPath: sessions)) ?? []).filter { $0.hasPrefix("native-") }
        let ranked = names.compactMap { name -> (String, Date)? in
            let folder = sessions + "/" + name
            guard folder != current, let date = modified(folder + "/runtime.json") ?? modified(folder) else { return nil }
            return (folder, date)
        }.sorted { $0.1 > $1.1 }
        let previous = ranked.first
        var reasons: [String] = []
        if let (folder, _) = previous, fm.fileExists(atPath: folder + "/app.log"),
           !fm.fileExists(atPath: folder + "/clean-exit"), !fm.fileExists(atPath: folder + "/crash-notified") {
            let pid = (try? String(contentsOfFile: folder + "/app.pid", encoding: .utf8)).flatMap { Int32($0.trimmingCharacters(in: .whitespacesAndNewlines)) } ?? 0
            if !isRunning(pid: pid) { reasons.append("the last session ended without a clean exit") }
        }
        let threshold = [previous?.1, modified(build + "/logs/crash-ack")].compactMap { $0 }.max() ?? .distantPast
        let reports = reportsDirectory
        let id = bundleID
        for name in ((try? fm.contentsOfDirectory(atPath: reports)) ?? []).sorted()
        where (name.hasPrefix("DJS1KReplicator-") || name.hasPrefix("SP16Emulator-")) && name.hasSuffix(".ips") {
            let path = reports + "/" + name
            guard let date = modified(path), date > threshold,
                  let handle = FileHandle(forReadingAtPath: path) else { continue }
            let head = String(decoding: handle.readData(ofLength: 8192), as: UTF8.self)
            try? handle.close()
            if head.contains("\"\(id)\"") { reasons.append("macOS wrote a crash report (\(name))"); break }
        }
        guard !reasons.isEmpty else { return nil }
        return (reasons.joined(separator: "; "), previous?.0)
    }

    /// Record that the user was told, so the same crash is not announced on every launch.
    static func acknowledge(build: String, session: String?) {
        if let session = session { try? "shown\n".write(toFile: session + "/crash-notified", atomically: true, encoding: .utf8) }
        let ack = build + "/logs/crash-ack"
        try? FileManager.default.createDirectory(atPath: build + "/logs", withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        try? "ack\n".write(toFile: ack, atomically: true, encoding: .utf8)
        chmod(ack, 0o600)
    }
}

/// Help > Collect Diagnostics…: runs the bundled diagnostics tool (the same code as `native.py diagnostics`) and
/// reveals the zip in Finder. A non-blocking panel offers it after an unclean exit.
final class DiagnosticsController: NSObject {
    static let shared = DiagnosticsController()
    var buildDirectory: String?
    var busy = false
    var panel: NSPanel?

    @objc func collect() {
        guard let build = buildDirectory else { return }
        guard !busy else { return }
        busy = true
        AppLog.log("collect diagnostics requested")
        DispatchQueue.global(qos: .userInitiated).async {
            let result = FirmwareTool.run(["--output", build], tool: "diagnostics.py")
            DispatchQueue.main.async {
                self.busy = false
                if result["status"] as? String == "diagnostics_written", let zip = result["zip"] as? String {
                    AppLog.log("diagnostics written: \(zip) files=\(result["files"] ?? "?")")
                    NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: zip)])
                    self.show(title: "Diagnostics collected",
                              text: "The zip is selected in Finder:\n\(zip)\n\nNothing was sent anywhere. Attach it to your bug report if you want to share it. It contains no firmware, update or key; see README.txt inside.",
                              collect: false)
                } else {
                    AppLog.log("diagnostics failed: \(result["error"] ?? result)")
                    self.show(title: "Diagnostics failed", text: "\(result["error"] ?? "no result from the diagnostics tool")", collect: false)
                }
            }
        }
    }

    /// The crash notice (offer = true) and the result messages: a small panel that does not block the emulator.
    func show(title: String, text: String, collect offer: Bool) {
        panel?.close()
        let panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 480, height: 200),
                            styleMask: [.titled, .closable, .utilityWindow],
                            backing: .buffered, defer: false)
        panel.isReleasedWhenClosed = false
        panel.title = title
        panel.becomesKeyOnlyIfNeeded = true
        let label = NSTextField(wrappingLabelWithString: text)
        label.widthAnchor.constraint(equalToConstant: 440).isActive = true
        var views: [NSView] = [label]
        if offer {
            let collectButton = NSButton(title: "Collect Diagnostics…", target: self, action: #selector(collectFromNotice))
            collectButton.keyEquivalent = "\r"
            let later = NSButton(title: "Not Now", target: panel, action: #selector(NSWindow.performClose(_:)))
            let row = NSStackView(views: [later, collectButton]); row.orientation = .horizontal
            views.append(row)
        } else {
            views.append(NSButton(title: "OK", target: panel, action: #selector(NSWindow.performClose(_:))))
        }
        let stack = NSStackView(views: views)
        stack.orientation = .vertical; stack.alignment = .trailing; stack.spacing = 12
        stack.edgeInsets = NSEdgeInsets(top: 14, left: 14, bottom: 14, right: 14)
        panel.contentView = stack
        panel.center()
        panel.orderFront(nil)
        self.panel = panel
    }

    @objc func collectFromNotice() {
        panel?.close()
        collect()
    }

    /// Launch-time check; shows the notice at most once per finding.
    func checkPreviousRun(current: String?) {
        guard let build = buildDirectory, let problem = CrashCheck.findProblem(build: build, current: current) else { return }
        AppLog.log("crash notice shown: \(problem.text)")
        CrashCheck.acknowledge(build: build, session: problem.session)
        show(title: "Emulator did not shut down cleanly",
             text: "The emulator did not shut down cleanly last time. Collect diagnostics?\n\nFound: \(problem.text).\n\nThis writes one zip with logs and system facts for a bug report, to your Desktop. Nothing is sent anywhere, and the zip contains no firmware, update or key.",
             collect: true)
    }
}

/// The Help menu shared by the main window and the import/library-only screens.
func helpMenuItem() -> NSMenuItem {
    let item = NSMenuItem()
    let menu = NSMenu(title: "Help")
    let collect = NSMenuItem(title: "Collect Diagnostics…", action: #selector(DiagnosticsController.collect), keyEquivalent: "")
    collect.target = DiagnosticsController.shared
    menu.addItem(collect)
    item.submenu = menu
    NSApp.helpMenu = menu
    return item
}
