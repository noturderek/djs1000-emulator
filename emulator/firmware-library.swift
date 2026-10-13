import CryptoKit
import Foundation

/// Decoupled builds (docs/architecture.md, M3/M4): the firmware library written by `native.py firmware import`
/// (or the app's import screen). Each entry is <library>/<id>/{manifest.json, entry.json, firmware.sqfs, data/}.
/// Before boot the app re-checks the D2 policy against the profiles bundled in Resources/profiles and the image
/// SHA-256 against the manifest; the guest checks the image and application hashes again.
struct FirmwareConfig: Decodable {
    let library: String
    let allow_unverified: Bool?
}

struct FirmwareEntry {
    let id: String
    let name: String
    let kind: String
    let version: String
    let appPath: String
    let appSHA256: String
    let baseAppSHA256: String
    let imageSHA256: String
    let imageBytes: Int
    let directory: URL
    let status: String          // "verified" or "unverified"
    let profile: String?
    let reason: String
    let manifest: [String: Any]

    var imagePath: String { directory.appendingPathComponent("firmware.sqfs").path }
    var dataDirectory: String { directory.appendingPathComponent("data").path }
}

enum FirmwareLibraryError: Error, CustomStringConvertible {
    case invalid(String)
    var description: String { if case .invalid(let text) = self { return text }; return "invalid firmware" }
}

enum FirmwareLibrary {
    static let idPattern = "^[a-z0-9][a-z0-9.-]{0,95}$"
    static let shaPattern = "^[0-9a-f]{64}$"
    static let versionPattern = "^[0-9]\\.[0-9]{2}$"
    static let pathPattern = "^/([A-Za-z0-9._-]+/?)*$"

    static func matches(_ text: String, _ pattern: String) -> Bool {
        text.range(of: pattern, options: .regularExpression) != nil
    }

    /// Profiles bundled with the app: [app_sha256: (id, status, version)].
    static func bundledProfiles() -> [String: (id: String, status: String, version: String)] {
        var result: [String: (String, String, String)] = [:]
        guard let folder = Bundle.main.url(forResource: "profiles", withExtension: nil),
              let names = try? FileManager.default.contentsOfDirectory(atPath: folder.path) else { return [:] }
        for name in names.sorted() where name.hasSuffix(".json") {
            guard let data = try? Data(contentsOf: folder.appendingPathComponent(name)),
                  let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let id = object["id"] as? String, let status = object["status"] as? String,
                  let firmware = object["firmware"] as? [String: Any],
                  let sha = firmware["app_sha256"] as? String, let version = firmware["version"] as? String,
                  matches(sha, shaPattern), ["verified", "candidate"].contains(status) else { continue }
            result[sha] = (id, status, version)
        }
        return result
    }

    static func ids(library: String) -> [String] {
        guard let names = try? FileManager.default.contentsOfDirectory(atPath: library) else { return [] }
        return names.filter { name in
            var isDirectory: ObjCBool = false
            let manifest = library + "/" + name + "/manifest.json"
            return matches(name, idPattern) && FileManager.default.fileExists(atPath: manifest, isDirectory: &isDirectory)
                && !isDirectory.boolValue
        }.sorted()
    }

    static func selected(library: String) -> String? {
        if let data = try? Data(contentsOf: URL(fileURLWithPath: library + "/selected.json")),
           let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let id = object["id"] as? String, ids(library: library).contains(id) { return id }
        let all = ids(library: library)
        return all.count == 1 ? all[0] : nil
    }

    static func select(library: String, id: String) {
        guard matches(id, idPattern),
              let data = try? JSONSerialization.data(withJSONObject: ["id": id], options: [.sortedKeys]) else { return }
        let path = library + "/selected.json"
        try? data.write(to: URL(fileURLWithPath: path), options: .atomic)
        chmod(path, 0o600)
    }

    static func load(library: String, id: String) throws -> FirmwareEntry {
        guard matches(id, idPattern) else { throw FirmwareLibraryError.invalid("invalid firmware id") }
        let directory = URL(fileURLWithPath: library).appendingPathComponent(id)
        guard let data = try? Data(contentsOf: directory.appendingPathComponent("manifest.json")),
              let manifest = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw FirmwareLibraryError.invalid("firmware \(id) has no readable manifest")
        }
        guard manifest["schema"] as? Int == 1, manifest["product"] as? String == "sp16",
              manifest["id"] as? String == id,
              let kind = manifest["kind"] as? String, ["stock", "mod"].contains(kind),
              let app = manifest["app_sha256"] as? String, matches(app, shaPattern),
              let appPath = manifest["app_path"] as? String, matches(appPath, pathPattern), !appPath.contains(".."),
              let image = manifest["image"] as? [String: Any], image["file"] as? String == "firmware.sqfs",
              let imageSHA = image["sha256"] as? String, matches(imageSHA, shaPattern),
              let imageBytes = image["bytes"] as? Int, imageBytes > 0, imageBytes % 4096 == 0,
              let update = manifest["update"] as? [String: Any],
              let version = update["trailer_version"] as? String, matches(version, versionPattern) else {
            throw FirmwareLibraryError.invalid("firmware \(id) has an invalid manifest")
        }
        var base = app
        var modOK = true
        if kind == "mod" {
            guard let mod = manifest["mod"] as? [String: Any], let baseSHA = mod["base_app_sha256"] as? String,
                  matches(baseSHA, shaPattern), let baseID = mod["base_id"] as? String, matches(baseID, idPattern) else {
                throw FirmwareLibraryError.invalid("firmware \(id) has an invalid mod section")
            }
            base = baseSHA
            modOK = mod["verified"] as? Bool == true
                && (try? load(library: library, id: baseID))?.appSHA256 == baseSHA
        }
        var name = id
        if let entryData = try? Data(contentsOf: directory.appendingPathComponent("entry.json")),
           let entry = try? JSONSerialization.jsonObject(with: entryData) as? [String: Any],
           let text = entry["name"] as? String, !text.isEmpty { name = String(text.prefix(80)) }
        let profile = bundledProfiles()[base]
        var status = "unverified", reason = ""
        if profile == nil { reason = "no compatibility profile for this firmware" }
        else if profile!.version != version { reason = "update version differs from the profile's version" }
        else if profile!.status != "verified" { reason = "profile \(profile!.id) is a candidate, not verified" }
        else if !modOK { reason = "mod was not verified against a base firmware in the library" }
        else { status = "verified" }
        return FirmwareEntry(id: id, name: name, kind: kind, version: version, appPath: appPath, appSHA256: app,
                             baseAppSHA256: base,
                             imageSHA256: imageSHA, imageBytes: imageBytes, directory: directory, status: status,
                             profile: profile?.id, reason: reason, manifest: manifest)
    }

    /// Integrity: size and SHA-256 of the stored image must equal the manifest.
    static func verifyImage(_ entry: FirmwareEntry) throws {
        let path = entry.imagePath
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: path),
              attributes[.type] as? FileAttributeType == .typeRegular,
              (attributes[.size] as? NSNumber)?.intValue == entry.imageBytes,
              let handle = FileHandle(forReadingAtPath: path) else {
            throw FirmwareLibraryError.invalid("firmware image of \(entry.id) is missing or its size changed")
        }
        defer { try? handle.close() }
        var hasher = SHA256()
        while true {
            let chunk = handle.readData(ofLength: 1 << 20)
            if chunk.isEmpty { break }
            hasher.update(data: chunk)
        }
        let digest = hasher.finalize().map { String(format: "%02x", $0) }.joined()
        guard digest == entry.imageSHA256 else {
            throw FirmwareLibraryError.invalid("firmware image of \(entry.id) does not match its manifest")
        }
    }

    /// Fill the QEMU placeholders for a checked entry. Every value was validated against a strict pattern above.
    static func substitute(_ arguments: [String], entry: FirmwareEntry) -> [String] {
        let values = ["@FWIMAGE@": entry.imagePath, "@FWSHA@": entry.imageSHA256, "@FWBYTES@": String(entry.imageBytes),
                      "@FWAPP@": entry.appSHA256, "@FWBASE@": entry.baseAppSHA256, "@FWVER@": entry.version,
                      "@FWAPPPATH@": entry.appPath]
        return arguments.map { argument in
            var text = argument
            for (key, value) in values { text = text.replacingOccurrences(of: key, with: value) }
            return text
        }
    }

    static func report(_ entry: FirmwareEntry) -> String {
        var lines = ["Firmware \(entry.name) (\(entry.id), version \(entry.version), \(entry.kind)) cannot start:",
                     entry.reason]
        if let compat = entry.manifest["compat"] as? [String: Any] {
            for key in ["devices_unknown", "pcms_unknown", "ioctls_unknown_candidates"] {
                if let list = compat[key] as? [String], !list.isEmpty {
                    lines.append("\(key.replacingOccurrences(of: "_", with: " ")): \(list.joined(separator: ", "))")
                }
            }
        }
        lines.append("Only versions with a verified compatibility profile run (design decision D2). "
                     + "Run `native.py firmware check` for the full report.")
        return lines.joined(separator: "\n")
    }
}
