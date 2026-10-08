import AppKit
import Foundation

/// Downloads a release DMG, checks it, and swaps the app bundle after quit.
enum Installer {

    static var bundleURL: URL { Bundle.main.bundleURL }

    /// Only a real, writable .app can replace itself. `swift run` during
    /// development has no bundle, and a copy in a folder the user cannot
    /// write to (another account's /Applications) has to go the manual way.
    static var canReplaceSelf: Bool {
        bundleURL.pathExtension == "app"
            && FileManager.default.isWritableFile(atPath: bundleURL.deletingLastPathComponent().path)
    }

    @MainActor
    static func install(from dmg: URL, version: String, fallback: URL) {
        let panel = ProgressPanel(title: L("Fundus \(version) wird geladen…", "Downloading Fundus \(version)…"))
        panel.show()
        Task {
            do {
                let staged = try await prepare(dmg: dmg, panel: panel)
                await MainActor.run {
                    panel.close()
                    relaunch(into: staged)
                }
            } catch {
                await MainActor.run {
                    panel.close()
                    let a = NSAlert()
                    a.messageText = L("Update nicht installiert", "Update not installed")
                    a.informativeText = L("\(error.localizedDescription)\n\nDie Release-Seite öffnet sich, dort liegt die DMG zum Herunterladen.",
                                          "\(error.localizedDescription)\n\nThe release page will open; the DMG is there to download.")
                    a.alertStyle = .warning
                    a.runModal()
                    NSWorkspace.shared.open(fallback)
                }
            }
        }
    }

    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    /// Download → mount → verify → copy next to the current bundle.
    /// Returns the staged .app, ready to be moved into place.
    static func prepare(dmg: URL, panel: ProgressPanel) async throws -> URL {
        let work = FileManager.default.temporaryDirectory
            .appendingPathComponent("fundus-update-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: work, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: work) }

        let (tmp, resp) = try await URLSession.shared.download(from: dmg)
        guard (resp as? HTTPURLResponse)?.statusCode == 200 else {
            throw Failure(message: L("Download fehlgeschlagen.", "Download failed."))
        }
        let image = work.appendingPathComponent("Fundus.dmg")
        try FileManager.default.moveItem(at: tmp, to: image)
        await MainActor.run { panel.status(L("Wird geprüft…", "Verifying…")) }

        let mount = work.appendingPathComponent("mnt", isDirectory: true)
        try FileManager.default.createDirectory(at: mount, withIntermediateDirectories: true)
        try run("/usr/bin/hdiutil", ["attach", image.path, "-nobrowse", "-readonly",
                                     "-noautoopen", "-mountpoint", mount.path])
        defer { _ = try? run("/usr/bin/hdiutil", ["detach", mount.path, "-force"]) }

        guard let app = try FileManager.default.contentsOfDirectory(at: mount, includingPropertiesForKeys: nil)
            .first(where: { $0.pathExtension == "app" }) else {
            throw Failure(message: L("Keine App in der DMG gefunden.", "No app found in the DMG."))
        }
        // Same signer as the running app, and a valid signature. Gatekeeper
        // assessment on top: the release must be notarised.
        try run("/usr/bin/codesign", ["--verify", "--deep", "--strict", app.path])
        guard let mine = teamID(of: bundleURL), let theirs = teamID(of: app), mine == theirs,
              mine.allSatisfy({ $0.isLetter || $0.isNumber }) else {
            throw Failure(message: L("Die Signatur der neuen Version passt nicht zu dieser App.",
                                     "The new version is not signed by the same developer."))
        }
        try run("/usr/sbin/spctl", ["--assess", "--type", "execute", app.path])

        let staged = bundleURL.deletingLastPathComponent()
            .appendingPathComponent(".Fundus-update-\(UUID().uuidString).app")
        do {
            try run("/usr/bin/ditto", [app.path, staged.path])
            // Checked again on the copy that will actually run: same team,
            // same bundle identifier, and really a newer version.
            let ident = Bundle.main.bundleIdentifier ?? "app.fundus.desktop"
            let requirement = "anchor apple generic and certificate leaf[subject.OU] = \"\(mine)\" and identifier \"\(ident)\""
            try run("/usr/bin/codesign", ["--verify", "--deep", "--strict", "-R=\(requirement)", staged.path])
            let newVersion = Bundle(url: staged)?.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0"
            guard Updater.isNewer(newVersion, than: Updater.currentVersion) else {
                throw Failure(message: L("Die geladene Version ist nicht neuer als die installierte.",
                                         "The downloaded version is not newer than the installed one."))
            }
        } catch {
            try? FileManager.default.removeItem(at: staged)
            throw error
        }
        return staged
    }

    static func teamID(of app: URL) -> String? {
        guard let out = try? run("/usr/bin/codesign", ["-dv", "--verbose=2", app.path]) else { return nil }
        for line in out.split(separator: "\n") where line.hasPrefix("TeamIdentifier=") {
            let id = String(line.dropFirst("TeamIdentifier=".count))
            return id == "not set" ? nil : id
        }
        return nil
    }

    /// Quit, swap, relaunch. The swap runs in a detached shell that waits for
    /// this process to exit, so the running bundle is never modified while it
    /// is executing. On any failure the old bundle is put back.
    @MainActor
    static func relaunch(into staged: URL) {
        let target = bundleURL.path
        let backup = target + ".old"
        let script = """
        while kill -0 \(ProcessInfo.processInfo.processIdentifier) 2>/dev/null; do sleep 0.3; done
        rm -rf "$3"
        # Old app aside first. If even that fails, nothing has changed yet:
        # drop the new copy and start the old app again.
        mv "$1" "$3" || { rm -rf "$2"; open "$1"; exit 1; }
        if mv "$2" "$1"; then
          rm -rf "$3"
          xattr -dr com.apple.quarantine "$1" 2>/dev/null
        else
          mv "$3" "$1"; rm -rf "$2"
        fi
        open "$1"
        """
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/sh")
        p.arguments = ["-c", script, "fundus-update", target, staged.path, backup]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do {
            try p.run()
            NSApp.terminate(nil)
        } catch {
            try? FileManager.default.removeItem(at: staged)
        }
    }

    @discardableResult
    static func run(_ exe: String, _ args: [String]) throws -> String {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: exe)
        p.arguments = args
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        try p.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        let out = String(data: data, encoding: .utf8) ?? ""
        guard p.terminationStatus == 0 else {
            throw Failure(message: "\((exe as NSString).lastPathComponent): \(out.prefix(300))")
        }
        return out
    }
}

/// A small floating panel with a spinner and one line of status.
@MainActor
final class ProgressPanel {
    private let panel: NSPanel
    private let label: NSTextField

    init(title: String) {
        panel = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 340, height: 96),
                        styleMask: [.titled], backing: .buffered, defer: false)
        panel.title = "Fundus"
        let view = NSView(frame: panel.contentView!.bounds)
        let spin = NSProgressIndicator(frame: NSRect(x: 24, y: 40, width: 16, height: 16))
        spin.style = .spinning
        spin.controlSize = .small
        spin.startAnimation(nil)
        label = NSTextField(labelWithString: title)
        label.frame = NSRect(x: 52, y: 38, width: 270, height: 20)
        view.addSubview(spin)
        view.addSubview(label)
        panel.contentView = view
        panel.center()
    }

    func show() { panel.makeKeyAndOrderFront(nil) }
    func status(_ s: String) { label.stringValue = s }
    func close() { panel.orderOut(nil) }
}
