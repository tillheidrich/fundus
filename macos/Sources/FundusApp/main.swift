import AppKit
import WebKit

// ── Fundus for macOS ─────────────────────────────────────────────────────────
// A native window around the local server. The point is not the chrome — it is
// that the machine running the downloads has a home IP, which is the single
// thing that decides whether YouTube cooperates. Everything else the local
// mode gains (browser cookies, Neural Engine transcription, no disk pressure)
// follows from running on a laptop instead of in a datacenter.

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate,
                         NSMenuItemValidation {

    private var window: NSWindow!
    private var web: WKWebView!
    private var status: NSTextField!
    private var spinner: NSProgressIndicator!
    private let backend = Backend()
    private var clipTimer: Timer?
    private var clipCount = NSPasteboard.general.changeCount
    private var clipSeen = Set<String>()
    static let clipKey = "watchClipboard"

    func applicationDidFinishLaunching(_ note: Notification) {
        buildWindow()
        backend.onPhase = { [weak self] phase in
            DispatchQueue.main.async { self?.apply(phase) }
        }
        backend.start()
        // After the window is up, not before: an update prompt in front of a
        // blank window looks like the app failed to start.
        DispatchQueue.main.asyncAfter(deadline: .now() + 4) {
            Updater.checkInBackground()
        }
        if UserDefaults.standard.bool(forKey: AppDelegate.clipKey) { startClipboardWatch() }
    }

    // MARK: - Clipboard (opt-in)
    // Off by default, and it reads nothing while off: a clipboard holds
    // passwords and private messages as often as links. When on, it looks
    // only while Fundus is in the background — a link copied inside the app
    // (the MCP address, a transcript) is not something to fetch — and passes
    // on nothing but http(s) addresses.

    @objc func toggleClipboardWatch(_ sender: Any?) {
        let on = !UserDefaults.standard.bool(forKey: AppDelegate.clipKey)
        UserDefaults.standard.set(on, forKey: AppDelegate.clipKey)
        on ? startClipboardWatch() : stopClipboardWatch()
    }

    func validateMenuItem(_ item: NSMenuItem) -> Bool {
        if item.action == #selector(toggleClipboardWatch(_:)) {
            item.state = UserDefaults.standard.bool(forKey: AppDelegate.clipKey) ? .on : .off
        }
        return true
    }

    private func startClipboardWatch() {
        clipCount = NSPasteboard.general.changeCount   // ignore what was there before
        clipTimer?.invalidate()
        clipTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            self?.pollClipboard()
        }
    }

    private func stopClipboardWatch() {
        clipTimer?.invalidate()
        clipTimer = nil
    }

    private func pollClipboard() {
        let pb = NSPasteboard.general
        guard pb.changeCount != clipCount else { return }
        clipCount = pb.changeCount
        guard !NSApp.isActive, let web = web, !web.isHidden,
              let text = pb.string(forType: .string), text.count < 20_000 else { return }
        let urls = text.split(whereSeparator: { $0.isWhitespace })
            .map(String.init)
            .filter { s in
                guard let u = URL(string: s), let scheme = u.scheme?.lowercased(),
                      scheme == "http" || scheme == "https", u.host != nil else { return false }
                return true
            }
            .filter { clipSeen.insert($0).inserted }
        if clipSeen.count > 500 { clipSeen.removeAll() }   // a set of links, not an archive
        guard !urls.isEmpty,
              let json = try? JSONSerialization.data(withJSONObject: urls),
              let arg = String(data: json, encoding: .utf8) else { return }
        web.evaluateJavaScript("window.fundusClipboard && window.fundusClipboard(\(arg))")
    }

    @objc func checkForUpdates(_ sender: Any?) { Updater.checkNow() }

    // The server holds no state worth losing, but it does hold running
    // ffmpeg children — let it wind them down rather than orphaning them.
    func applicationWillTerminate(_ note: Notification) { backend.stop() }
    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }

    // MARK: - Window

    private func buildWindow() {
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1120, height: 780),
            // No .fullSizeContentView: with it the content reaches under the
            // title bar, the web view sits on top of the only drag area the
            // window has, and swallows the mouse events — the window cannot
            // be moved at all. A real title bar is the honest trade.
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        window.title = "Fundus"
        // Dragging an empty patch of background also moves the window, which
        // costs nothing and helps when the title bar is off screen.
        window.isMovableByWindowBackground = true
        window.setFrameAutosaveName("FundusMain")
        window.minSize = NSSize(width: 420, height: 560)   // the UI is mobile-first; let it be narrow
        // Matches --paper so the window does not flash white before the page
        // paints, and does not glare in dark surroundings either.
        window.backgroundColor = NSColor(srgbRed: 0.957, green: 0.969, blue: 0.984, alpha: 1)

        let container = NSView(frame: window.contentView!.bounds)
        container.autoresizingMask = [.width, .height]

        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()          // keeps the session cookie between launches

        // Teach our own page to present the shared token on every request it
        // makes. Without this the server's cross-site check would reject the
        // app's own form posts — the protection has to let the app through
        // while still turning away a page on the open web.
        let token = backend.localToken
        let inject = """
        (function () {
          const T = '\(token)';
          const orig = window.fetch;
          window.fetch = function (input, init) {
            init = init || {};
            const h = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
            h.set('X-Fundus-Token', T);
            init.headers = h;
            return orig.call(this, input, init);
          };
          const open = XMLHttpRequest.prototype.open;
          XMLHttpRequest.prototype.open = function () {
            const r = open.apply(this, arguments);
            this.setRequestHeader('X-Fundus-Token', T);
            return r;
          };
        })();
        """
        config.userContentController.addUserScript(
            WKUserScript(source: inject, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        web = WKWebView(frame: container.bounds, configuration: config)
        web.autoresizingMask = [.width, .height]
        web.navigationDelegate = self
        web.uiDelegate = self
        web.isHidden = true
        if web.responds(to: Selector(("setAllowsBackForwardNavigationGestures:"))) {
            web.allowsBackForwardNavigationGestures = false   // it is an app, not a browser
        }
        container.addSubview(web)

        spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.startAnimation(nil)
        spinner.frame = NSRect(x: container.bounds.midX - 8, y: container.bounds.midY + 24, width: 16, height: 16)
        spinner.autoresizingMask = [.minXMargin, .maxXMargin, .minYMargin, .maxYMargin]
        container.addSubview(spinner)

        status = NSTextField(labelWithString: L("Wird gestartet…", "Starting…"))
        status.alignment = .center
        status.font = .systemFont(ofSize: 12)
        status.textColor = .secondaryLabelColor
        status.lineBreakMode = .byWordWrapping
        status.maximumNumberOfLines = 12
        status.frame = NSRect(x: 40, y: container.bounds.midY - 130, width: container.bounds.width - 80, height: 150)
        status.autoresizingMask = [.width, .minXMargin, .maxXMargin, .minYMargin, .maxYMargin]
        container.addSubview(status)

        window.contentView = container
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func apply(_ phase: Backend.Phase) {
        switch phase {
        case .preparing(let msg):
            status.stringValue = msg
        case .running:
            web.load(URLRequest(url: backend.appURL))
        case .failed(let msg):
            spinner.stopAnimation(nil)
            spinner.isHidden = true
            status.stringValue = L("Start fehlgeschlagen", "Start failed") + "\n\n" + msg
        }
    }

    func webView(_ w: WKWebView, didFinish nav: WKNavigation!) {
        spinner.stopAnimation(nil)
        spinner.isHidden = true
        status.isHidden = true
        web.isHidden = false
    }

    func webView(_ w: WKWebView, didFail nav: WKNavigation!, withError error: Error) {
        apply(.failed(error.localizedDescription + "\n\n" + backend.recentLog()))
    }

    /// Links to anywhere but the local server open in the real browser. The
    /// page links out to YouTube, Instagram and an extension store; loading
    /// those inside the app window would be confusing and pointless.
    func webView(_ w: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void) {
        // Own server means own port too: another local service on a different
        // port must not be loaded in here, where the page script hands every
        // request the app's token.
        if let url = action.request.url,
           url.scheme?.hasPrefix("http") == true,
           !((url.host == "127.0.0.1" || url.host == "localhost")
             && url.port == Int(backend.port)) {
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    /// target="_blank" arrives here rather than through the policy handler.
    func webView(_ w: WKWebView, createWebViewWith config: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url { NSWorkspace.shared.open(url) }
        return nil
    }

    // ── Downloads ────────────────────────────────────────────────────────────
    //
    // Es gab hier gar nichts, und deshalb ist jedes „Speichern unter" mit
    // einer Fehlermeldung abgebrochen. Eine WKWebView lädt nichts von selbst
    // herunter: ohne diese drei Methoden bricht sie die Navigation zu einer
    // ZIP-Datei einfach ab, und das sieht für den Nutzer aus wie ein Fehler
    // der Seite. Im Browser lief dasselbe, weil der Browser es mitbringt.

    /// Antworten, die das Fenster nicht anzeigen kann oder soll, werden
    /// Downloads. Beide Bedingungen sind nötig: eine ZIP scheitert schon an
    /// `canShowMIMEType`, eine Markdown-Datei nicht — die würde sonst als Text
    /// im Fenster landen, obwohl der Server sie als Anhang ausliefert.
    func webView(_ w: WKWebView, decidePolicyFor response: WKNavigationResponse,
                 decisionHandler: @escaping @MainActor (WKNavigationResponsePolicy) -> Void) {
        let isAttachment = (response.response as? HTTPURLResponse)?
            .value(forHTTPHeaderField: "Content-Disposition")?
            .lowercased().contains("attachment") ?? false
        decisionHandler(!response.canShowMIMEType || isAttachment ? .download : .allow)
    }

    func webView(_ w: WKWebView, navigationResponse: WKNavigationResponse,
                 didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ w: WKWebView, navigationAction: WKNavigationAction,
                 didBecome download: WKDownload) {
        download.delegate = self
    }
}

extension AppDelegate: WKDownloadDelegate {

    /// Wohin die Datei soll.
    ///
    /// Ein Speichern-Dialog, nicht still nach ~/Downloads: der Nutzer hat
    /// „Speichern unter" angeklickt, also soll er den Ort bestimmen. Der
    /// vorgeschlagene Name kommt vom Server und ist dort schon eindeutig
    /// gemacht; falls am gewählten Ort trotzdem etwas gleich heißt, hängt
    /// macOS selbst eine Nummer an — das ist die einzige Stelle, an der eine
    /// Zählnummer richtig ist, weil sie den Namen nicht ersetzt.
    func download(_ download: WKDownload,
                  decideDestinationUsing response: URLResponse,
                  suggestedFilename: String,
                  completionHandler: @escaping @MainActor (URL?) -> Void) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = suggestedFilename
        panel.directoryURL = FileManager.default.urls(for: .downloadsDirectory,
                                                      in: .userDomainMask).first
        panel.canCreateDirectories = true
        // Der Dialog muss zum Fenster gehören. Als eigenständiges Fenster
        // kann er hinter der App verschwinden, und dann hängt der Download
        // an einem Dialog, den niemand sieht.
        guard let window = NSApp.mainWindow else {
            completionHandler(panel.runModal() == .OK ? panel.url : nil)
            return
        }
        panel.beginSheetModal(for: window) { result in
            completionHandler(result == .OK ? panel.url : nil)
        }
    }

    func downloadDidFinish(_ download: WKDownload) {
        guard let url = download.progress.fileURL else { return }
        // Im Finder zeigen statt öffnen: was hier ankommt, ist meist ein
        // Archiv oder eine Datei für ein anderes Programm.
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }

    func download(_ download: WKDownload, didFailWithError error: Error,
                  resumeData: Data?) {
        // Einen Abbruch durch den Nutzer nicht als Fehler melden: wer im
        // Dialog auf Abbrechen klickt, hat keinen Fehler gemacht.
        if (error as NSError).code == NSUserCancelledError { return }
        let alert = NSAlert()
        alert.messageText = L("Download fehlgeschlagen", "Download failed")
        alert.informativeText = error.localizedDescription
        alert.alertStyle = .warning
        alert.runModal()
    }
}

// Menu bar. Without one, copy and paste do not work in the web view — the
// shortcuts are routed through the menu, not the responder chain.
func buildMenu() -> NSMenu {
    let root = NSMenu()

    let appItem = NSMenuItem()
    let appMenu = NSMenu()
    appMenu.addItem(withTitle: L("Über Fundus", "About Fundus"), action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
    appMenu.addItem(.separator())
    appMenu.addItem(withTitle: L("Nach Updates suchen…", "Check for Updates…"),
                    action: #selector(AppDelegate.checkForUpdates(_:)), keyEquivalent: "")
    appMenu.addItem(.separator())
    appMenu.addItem(withTitle: L("Fundus ausblenden", "Hide Fundus"), action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
    appMenu.addItem(withTitle: L("Beenden", "Quit Fundus"), action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
    appItem.submenu = appMenu
    root.addItem(appItem)

    let editItem = NSMenuItem()
    let edit = NSMenu(title: L("Bearbeiten", "Edit"))
    edit.addItem(withTitle: L("Widerrufen", "Undo"), action: Selector(("undo:")), keyEquivalent: "z")
    edit.addItem(withTitle: L("Wiederholen", "Redo"), action: Selector(("redo:")), keyEquivalent: "Z")
    edit.addItem(.separator())
    edit.addItem(withTitle: L("Ausschneiden", "Cut"), action: #selector(NSText.cut(_:)), keyEquivalent: "x")
    edit.addItem(withTitle: L("Kopieren", "Copy"), action: #selector(NSText.copy(_:)), keyEquivalent: "c")
    edit.addItem(withTitle: L("Einsetzen", "Paste"), action: #selector(NSText.paste(_:)), keyEquivalent: "v")
    edit.addItem(withTitle: L("Alles auswählen", "Select All"), action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
    edit.addItem(.separator())
    edit.addItem(withTitle: L("Links aus der Zwischenablage übernehmen", "Offer Links from the Clipboard"),
                 action: #selector(AppDelegate.toggleClipboardWatch(_:)), keyEquivalent: "")
    editItem.submenu = edit
    root.addItem(editItem)

    let viewItem = NSMenuItem()
    let view = NSMenu(title: L("Darstellung", "View"))
    view.addItem(withTitle: L("Neu laden", "Reload"), action: Selector(("reload:")), keyEquivalent: "r")
    view.addItem(withTitle: L("Vollbild", "Full Screen"), action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f")
    viewItem.submenu = view
    root.addItem(viewItem)

    return root
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.mainMenu = buildMenu()
app.run()
