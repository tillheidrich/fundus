import AppKit
import Foundation

/// Finds a newer release on GitHub and installs it in place.
///
/// The install is deliberately conservative, because a half-replaced app is
/// worse than no update: the DMG is downloaded and mounted read-only, the app
/// inside must pass `codesign --verify` and carry the same Team ID as the
/// running app, and only then is it copied next to the current bundle. The
/// swap itself happens after this process has quit, by a small script that
/// moves the old bundle aside, moves the new one in, and puts the old one back
/// if anything fails. Whenever a step is not possible (no DMG asset, a folder
/// the user cannot write to, a signature that does not match), the app falls
/// back to opening the release page, which is what it always did.
///
/// The feed is the GitHub releases API by default: no infrastructure to run,
/// and it is where an open-source project's downloads already live. Override
/// with the FundusUpdateFeed key in Info.plist to point somewhere else.
enum Updater {

    struct Release {
        let version: String
        let url: URL          // release page, the fallback
        let notes: String
        let dmg: URL?         // installable asset, if the release has one
    }

    static var currentVersion: String {
        (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String) ?? "0"
    }

    static var feedURL: URL? {
        // Environment first: lets the install path be tested end to end
        // against a local feed without publishing a release.
        // Debug builds only: in a release build anyone who can set this
        // user's environment could point the updater at their own feed.
        #if DEBUG
        if let s = ProcessInfo.processInfo.environment["FUNDUS_UPDATE_FEED"], let u = URL(string: s) {
            return u
        }
        #endif
        if let s = Bundle.main.infoDictionary?["FundusUpdateFeed"] as? String,
           let u = URL(string: s) { return u }
        return URL(string: "https://api.github.com/repos/tillheidrich/fundus/releases/latest")
    }

    // MARK: - Version comparison

    /// Compares dotted numeric versions, ignoring a leading "v" and anything
    /// after a dash. `git describe` produces tags like `v0.2.0-4-gc0ffee` for
    /// untagged builds, and treating that as newer than `v0.2.0` would nag a
    /// developer about an update to the thing they are sitting on.
    static func isNewer(_ candidate: String, than current: String) -> Bool {
        func parts(_ s: String) -> [Int] {
            let core = s.trimmingCharacters(in: .whitespaces)
                .trimmingCharacters(in: CharacterSet(charactersIn: "vV"))
                .split(separator: "-").first.map(String.init) ?? s
            return core.split(separator: ".").map { Int($0.filter(\.isNumber)) ?? 0 }
        }
        let a = parts(candidate), b = parts(current)
        for i in 0..<max(a.count, b.count) {
            let x = i < a.count ? a[i] : 0
            let y = i < b.count ? b[i] : 0
            if x != y { return x > y }
        }
        return false
    }

    // MARK: - Checking

    /// The callback always arrives on the main thread — it ends in an alert.
    ///
    /// `async` rather than a completion handler because Swift 6 is right to
    /// object: a closure handed to URLSession runs on a background thread and
    /// then wants to touch AppKit. Awaiting from a `@MainActor` caller makes
    /// the hop explicit instead of hoping DispatchQueue.main.async is enough.
    static func check() async -> Release? {
        guard let url = feedURL else { return nil }
        var req = URLRequest(url: url)
        req.timeoutInterval = 12
        req.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")

        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              (resp as? HTTPURLResponse)?.statusCode == 200,
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let tag = obj["tag_name"] as? String,
              let page = obj["html_url"] as? String,
              let pageURL = URL(string: page)
        else { return nil }

        // Drafts and prereleases are not offers to the general public.
        if (obj["draft"] as? Bool) == true || (obj["prerelease"] as? Bool) == true {
            return nil
        }
        guard isNewer(tag, than: currentVersion) else { return nil }
        let assets = (obj["assets"] as? [[String: Any]]) ?? []
        let dmg = assets.lazy
            .compactMap { $0["browser_download_url"] as? String }
            .first { $0.lowercased().hasSuffix(".dmg") }
            .flatMap(URL.init(string:))
        return Release(version: tag, url: pageURL, notes: (obj["body"] as? String) ?? "", dmg: dmg)
    }

    // MARK: - Presenting

    private static let skippedKey = "FundusSkippedVersion"
    private static let lastCheckKey = "FundusLastUpdateCheck"

    /// Quiet check on launch: at most once a day, silent when up to date, and
    /// silent about a version the user already declined.
    @MainActor
    static func checkInBackground() {
        let d = UserDefaults.standard
        let last = d.double(forKey: lastCheckKey)
        if Date().timeIntervalSince1970 - last < 24 * 3600 { return }
        d.set(Date().timeIntervalSince1970, forKey: lastCheckKey)

        Task { @MainActor in
            guard let rel = await check() else { return }
            if d.string(forKey: skippedKey) == rel.version { return }
            present(rel, manual: false)
        }
    }

    /// Menu-driven check: says something either way, because silence after
    /// clicking a button reads as broken.
    @MainActor
    static func checkNow() {
        Task { @MainActor in
            if let rel = await check() {
                present(rel, manual: true)
            } else {
                let a = NSAlert()
                a.messageText = L("Fundus ist aktuell", "Fundus is up to date")
                a.informativeText = "Version \(currentVersion)."
                a.addButton(withTitle: "OK")
                a.runModal()
            }
        }
    }

    @MainActor
    private static func present(_ rel: Release, manual: Bool) {
        let a = NSAlert()
        a.messageText = L("Version \(rel.version) ist verfügbar", "Version \(rel.version) is available")
        var text = L("Installiert ist \(currentVersion).", "Installed: \(currentVersion).")
        if !rel.notes.isEmpty {
            text += "\n\n" + rel.notes.prefix(600)
        }
        a.informativeText = text
        let canInstall = rel.dmg != nil && Installer.canReplaceSelf
        a.addButton(withTitle: canInstall ? L("Installieren und neu starten", "Install and Relaunch")
                                          : L("Herunterladen", "Download"))
        a.addButton(withTitle: L("Später", "Later"))
        if !manual { a.addButton(withTitle: L("Diese Version überspringen", "Skip This Version")) }

        switch a.runModal() {
        case .alertFirstButtonReturn:
            if canInstall, let dmg = rel.dmg {
                Installer.install(from: dmg, version: rel.version, fallback: rel.url)
            } else {
                NSWorkspace.shared.open(rel.url)
            }
        case .alertThirdButtonReturn:
            UserDefaults.standard.set(rel.version, forKey: skippedKey)
        default:
            break
        }
    }
}
