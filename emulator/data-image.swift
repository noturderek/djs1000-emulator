import Foundation
import Darwin

/// Persistent per-profile emulated eMMC: a sparse raw image plus a one-session lock.
/// The lock is an flock(2) on a file in the same directory, so the kernel releases it when the holder
/// dies (kill -9, crash, power loss); the PID written inside is only for the refusal message.
enum DataImageError: Error, CustomStringConvertible {
    case inUse(pid: Int32?)
    case io(String)

    var description: String {
        switch self {
        case .inUse(let pid):
            return "this profile's data image is already in use by another session"
                + (pid.map { " (pid \($0))" } ?? "")
        case .io(let message): return message
        }
    }
}

enum DataImage {
    static let imageName = "emmc.img"
    static let lockName = "session.lock"
    static let defaultSize: UInt64 = 1 << 30

    static func ensureDirectory(_ directory: String) throws {
        do { try FileManager.default.createDirectory(atPath: directory, withIntermediateDirectories: true,
                                                     attributes: [.posixPermissions: 0o700]) }
        catch { throw DataImageError.io("cannot create data directory: \(error.localizedDescription)") }
    }

    /// Create the directory (0700) and a sparse image of `size` bytes if absent. An existing image is never
    /// touched, so formatted contents survive. Returns the image path.
    static func prepare(directory: String, size: UInt64 = defaultSize) throws -> String {
        try ensureDirectory(directory)
        let path = directory + "/" + imageName
        let fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0o600)
        if fd < 0 {
            if errno == EEXIST { return path }
            throw DataImageError.io("cannot create \(path): errno \(errno)")
        }
        defer { close(fd) }
        if ftruncate(fd, off_t(size)) != 0 {
            unlink(path)
            throw DataImageError.io("cannot size \(path): errno \(errno)")
        }
        return path
    }

    /// Take the exclusive session lock; the returned descriptor must stay open for the session (it is
    /// close-on-exec so QEMU does not inherit it).
    static func acquireLock(directory: String) throws -> Int32 {
        try ensureDirectory(directory)
        let path = directory + "/" + lockName
        let fd = open(path, O_RDWR | O_CREAT | O_CLOEXEC, 0o600)
        if fd < 0 { throw DataImageError.io("cannot open \(path): errno \(errno)") }
        if flock(fd, LOCK_EX | LOCK_NB) != 0 {
            let busy = errno == EWOULDBLOCK
            var buffer = [UInt8](repeating: 0, count: 32)
            let count = pread(fd, &buffer, buffer.count, 0)
            close(fd)
            guard busy else { throw DataImageError.io("cannot lock \(path): errno \(errno)") }
            let pid = count > 0 ? Int32(String(decoding: buffer[0..<count], as: UTF8.self)
                .trimmingCharacters(in: .whitespacesAndNewlines)) : nil
            throw DataImageError.inUse(pid: pid)
        }
        _ = ftruncate(fd, 0)
        let text = Array("\(getpid())\n".utf8)
        _ = pwrite(fd, text, text.count, 0)
        return fd
    }

    /// Ask the guest to stop the OEM app, sync and unmount (message over the dedicated control port), and wait
    /// at most `timeout` seconds for its "done" reply. Returns true only on a confirmed clean unmount.
    static func requestCleanStop(socketPath: String, timeout: TimeInterval = 5) -> Bool {
        return requestControl(socketPath: socketPath, message: "shutdown", until: ["done"], timeout: timeout) != nil
    }

    /// Send one line on the control port and return the first of `tokens` the guest answers with, or nil on
    /// connection failure or timeout.
    static func requestControl(socketPath: String, message line: String, until tokens: [String],
                               timeout: TimeInterval) -> String? {
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        if fd < 0 { return nil }
        defer { close(fd) }
        var address = sockaddr_un()
        address.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(socketPath.utf8)
        guard bytes.count < MemoryLayout.size(ofValue: address.sun_path) else { return nil }
        withUnsafeMutableBytes(of: &address.sun_path) { raw in
            for (index, byte) in bytes.enumerated() { raw[index] = byte }
        }
        let connected = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        if connected != 0 { return nil }
        let message = Array((line + "\n").utf8)
        if write(fd, message, message.count) != message.count { return nil }
        let deadline = Date().addingTimeInterval(timeout)
        var reply = Data()
        while Date() < deadline {
            var poller = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
            let remaining = Int32(max(1, deadline.timeIntervalSinceNow * 1000))
            if poll(&poller, 1, min(remaining, 200)) <= 0 { continue }
            var chunk = [UInt8](repeating: 0, count: 64)
            let count = read(fd, &chunk, chunk.count)
            if count <= 0 { return nil }
            reply.append(contentsOf: chunk[0..<count])
            let text = String(decoding: reply, as: UTF8.self)
            if let hit = tokens.first(where: { text.contains($0) }) { return hit }
        }
        return nil
    }

    /// Validate a user-supplied USB image: absolute, a regular file (no symlink) owned by this user, and free of
    /// the comma QEMU would parse as an option separator.
    static func validateUSBImage(_ path: String) throws {
        guard path.hasPrefix("/"), !path.contains(",") else { throw DataImageError.io("USB image must be an absolute path without commas") }
        var info = stat()
        guard lstat(path, &info) == 0 else { throw DataImageError.io("USB image \(path) does not exist") }
        guard (info.st_mode & S_IFMT) == S_IFREG, info.st_uid == getuid() else {
            throw DataImageError.io("USB image must be a regular file owned by you (no symlinks)")
        }
    }
}
