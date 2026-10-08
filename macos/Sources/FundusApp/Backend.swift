import CryptoKit
import Foundation
import Security

/// Supervises the Python server that does the actual work.
///
/// The app is a window around it, the same way ClipGrab is a window around
/// yt-dlp. Everything interesting — extractors, transcripts, ffmpeg — already
/// exists and is tested; wrapping it beats reimplementing it in Swift.
final class Backend {

    enum Phase {
        case preparing(String)
        case running(URL)
        case failed(String)
    }

    private var process: Process?
    let port: UInt16
    private let support: URL

    /// Shared with the server through the environment and injected into the
    /// WebView, so a request from our own page can be told apart from one any
    /// web page could make to the same port. New on every launch.
    let localToken: String = {
        var bytes = [UInt8](repeating: 0, count: 32)
        _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        return bytes.map { String(format: "%02x", $0) }.joined()
    }()
    private let serverDir: URL

    var onPhase: ((Phase) -> Void)?

    init() {
        // A stable port when it is free, because an AI client configured for
        // the MCP endpoint (http://127.0.0.1:8765/mcp) needs an address that
        // survives a restart. Taken by something else → any free port, and
        // MCP clients then need the address shown in the app.
        // A server from the previous run may still be winding down (it exits
        // within a few seconds once its parent is gone), so wait briefly for
        // the port before giving up on it.
        var free = Backend.portIsFree(Backend.preferredPort)
        for _ in 0..<12 where !free {
            Thread.sleep(forTimeInterval: 0.5)
            free = Backend.portIsFree(Backend.preferredPort)
        }
        self.port = free ? Backend.preferredPort : Backend.freePort()
        let base = FileManager.default.urls(for: .applicationSupportDirectory,
                                            in: .userDomainMask)[0]
        // Both overrides exist for one purpose: reproducing a bare Mac on a
        // developed one. Without them the only way to test the first-run path
        // is to delete a working installation, which nobody does twice.
        if let dir = ProcessInfo.processInfo.environment["FUNDUS_SUPPORT_DIR"], !dir.isEmpty {
            self.support = URL(fileURLWithPath: dir, isDirectory: true)
        } else {
            self.support = base.appendingPathComponent("Fundus", isDirectory: true)
        }
        // Resources/server inside the bundle when built as an app; the repo
        // itself when run from `swift run` during development.
        if let res = Bundle.main.resourceURL?.appendingPathComponent("server"),
           FileManager.default.fileExists(atPath: res.appendingPathComponent("main.py").path) {
            self.serverDir = res
        } else {
            self.serverDir = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
                .deletingLastPathComponent()
        }
    }

    var url: URL { URL(string: "http://127.0.0.1:\(port)/")! }

    /// The first page load carries the token; the server answers with an
    /// HttpOnly cookie, which every later request from the WebView (links,
    /// downloads, images) then carries on its own.
    var appURL: URL { URL(string: "http://127.0.0.1:\(port)/?t=\(localToken)")! }

    // MARK: - Lifecycle

    func start() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            do {
                try FileManager.default.createDirectory(at: self.support,
                                                        withIntermediateDirectories: true)
                let python = try self.ensureEnvironment()
                try self.launch(python: python)
                self.waitUntilReady()
            } catch {
                self.onPhase?(.failed(error.localizedDescription))
            }
        }
    }

    func stop() {
        // SIGTERM, not terminate(): uvicorn shuts its workers down cleanly on
        // it, and a half-written download is worse than a slow quit.
        guard let p = process, p.isRunning else { return }
        kill(p.processIdentifier, SIGTERM)
        let deadline = Date().addingTimeInterval(4)
        while p.isRunning && Date() < deadline { usleep(100_000) }
        if p.isRunning { p.terminate() }
    }

    // MARK: - Environment

    /// Returns the interpreter to run the server with, building the virtual
    /// environment on first launch. This is the slow path — a minute or two,
    /// once — so it reports progress rather than appearing to hang.
    private func ensureEnvironment() throws -> URL {
        let venv = support.appendingPathComponent("venv", isDirectory: true)
        let python = venv.appendingPathComponent("bin/python3")
        let marker = venv.appendingPathComponent(".deps-ok")

        if FileManager.default.fileExists(atPath: marker.path),
           FileManager.default.fileExists(atPath: python.path) {
            return python
        }

        let system = try ensureInterpreter()

        if !FileManager.default.fileExists(atPath: python.path) {
            onPhase?(.preparing(L("Python-Umgebung wird angelegt…", "Creating the Python environment…")))
            try Backend.run(system, ["-m", "venv", venv.path])
        }

        onPhase?(.preparing(L("Abhängigkeiten werden geladen — das dauert beim ersten Start ein bis zwei Minuten…", "Installing components — the first start takes a minute or two…")))
        try Backend.run(python, ["-m", "pip", "install", "--upgrade", "pip", "--quiet"])
        try Backend.run(python, ["-m", "pip", "install", "--quiet", "-r",
                                 serverDir.appendingPathComponent("requirements.txt").path])
        // Keine Extraktoren hier (requirements-extractors.txt, yt-dlp,
        // gallery-dl, Deno). Seit 1.1.0 installiert die App sie nur noch auf
        // ausdrücklichen Wunsch: im System-Bereich, mit Einwilligung, über
        // POST /api/media/install. Eine bestehende Installation, die yt-dlp
        // schon hat, behält es — dieser Pfad läuft ohnehin nur beim ersten
        // Start (Marker .deps-ok).

        // Neural Engine transcription. Apple Silicon only, and entirely
        // optional — a failure here costs speed, not function.
        if Backend.isAppleSilicon {
            onPhase?(.preparing(L("Whisper für Apple Silicon wird eingerichtet…", "Setting up Whisper for Apple Silicon…")))
            _ = try? Backend.run(python, ["-m", "pip", "install", "--quiet", "mlx-whisper"])
        }

        try ensureMediaTools(python: python, venv: venv)

        FileManager.default.createFile(atPath: marker.path, contents: nil)
        return python
    }

    /// A pinned, self-contained CPython, published by the Astral project and
    /// the same build `uv` installs. Two reasons it is here at all:
    ///
    /// macOS ships Python 3.9.6 and nothing newer. The server's own
    /// dependencies refuse it — `mcp` wants 3.10 at least — so on a Mac with
    /// no Homebrew the setup used to get as far as creating a virtual
    /// environment and then fail on the first install, which looked like the
    /// app was broken rather than the machine being bare. And the `deno`
    /// wheel has no 3.9 build, so YouTube would have stayed dead regardless.
    private enum Runtime {
        static let tag = "20261003"
        static let version = "3.12.15"
        /// Checksums taken from the downloaded assets and pinned here. An
        /// unpinned "latest" download is a standing invitation: whoever can
        /// influence that URL later gets to run code on every friend's Mac.
        static let sha256: [String: String] = [
            "aarch64": "316a463172740e71d8dca1f2730784e325f3f720941137b5d674d5801a632213",
            "x86_64":  "a8fd7a91852f19b6d959793ef41fad048631ccb2a334a9ecdf573255298f7978",
        ]
        static var arch: String { Backend.isAppleSilicon ? "aarch64" : "x86_64" }
        static var url: URL {
            // The "+" in the version is literal in the asset name and has to
            // stay percent-encoded; URL(string:) would otherwise read it as a
            // space.
            URL(string: "https://github.com/astral-sh/python-build-standalone/releases/download/"
                + "\(tag)/cpython-\(version)%2B\(tag)-\(arch)-apple-darwin-install_only.tar.gz")!
        }
    }

    /// The interpreter to build the virtual environment from: whatever the
    /// machine already has, if it is new enough, otherwise our own download.
    private func ensureInterpreter() throws -> URL {
        let forced = ProcessInfo.processInfo.environment["FUNDUS_FORCE_RUNTIME"] == "1"
        if !forced, let system = Backend.findSystemPython() { return system }

        let root = support.appendingPathComponent("python", isDirectory: true)
        let python = root.appendingPathComponent("bin/python3")
        if FileManager.default.isExecutableFile(atPath: python.path) { return python }

        onPhase?(.preparing(L("Python \(Runtime.version) wird geladen — 25 MB, nur dieses eine Mal…", "Downloading Python \(Runtime.version) — 25 MB, only this once…")))
        let archive = try download(Runtime.url, expecting: Runtime.sha256[Runtime.arch])

        onPhase?(.preparing(L("Python wird entpackt…", "Unpacking Python…")))
        let staging = support.appendingPathComponent("python.tmp", isDirectory: true)
        try? FileManager.default.removeItem(at: staging)
        try FileManager.default.createDirectory(at: staging, withIntermediateDirectories: true)
        try Backend.run(URL(fileURLWithPath: "/usr/bin/tar"),
                        ["xzf", archive.path, "-C", staging.path])
        try? FileManager.default.removeItem(at: archive)

        // The archive contains a single `python/` directory.
        let unpacked = staging.appendingPathComponent("python", isDirectory: true)
        guard FileManager.default.isExecutableFile(
                atPath: unpacked.appendingPathComponent("bin/python3").path) else {
            throw AppError.message("Das Python-Archiv sieht nicht aus wie erwartet.")
        }
        try? FileManager.default.removeItem(at: root)
        try FileManager.default.moveItem(at: unpacked, to: root)
        try? FileManager.default.removeItem(at: staging)
        return python
    }

    /// Fetch a file and refuse it unless it hashes to what we expect.
    private func download(_ url: URL, expecting sha: String?) throws -> URL {
        guard let sha else { throw AppError.message("Für diese Architektur ist keine Prüfsumme hinterlegt.") }

        let dest = support.appendingPathComponent("download-\(UUID().uuidString)")
        let sem = DispatchSemaphore(value: 0)
        let box = Box<Result<URL, Error>>()
        URLSession.shared.downloadTask(with: url) { tmp, resp, err in
            defer { sem.signal() }
            if let err { box.value = .failure(err); return }
            guard let tmp, (resp as? HTTPURLResponse)?.statusCode == 200 else {
                box.value = .failure(AppError.message(
                    "Download fehlgeschlagen (HTTP \((resp as? HTTPURLResponse)?.statusCode ?? 0))."))
                return
            }
            // The temporary file is deleted the moment this handler returns.
            do {
                try? FileManager.default.removeItem(at: dest)
                try FileManager.default.moveItem(at: tmp, to: dest)
                box.value = .success(dest)
            } catch { box.value = .failure(error) }
        }.resume()
        _ = sem.wait(timeout: .now() + 900)
        guard let result = box.value else {
            throw AppError.message("Der Download hat zu lange gedauert.")
        }
        let file = try result.get()

        var hasher = SHA256()
        let handle = try FileHandle(forReadingFrom: file)
        defer { try? handle.close() }
        while case let chunk = handle.readData(ofLength: 1 << 20), !chunk.isEmpty {
            hasher.update(data: chunk)
        }
        let got = hasher.finalize().map { String(format: "%02x", $0) }.joined()
        guard got == sha else {
            try? FileManager.default.removeItem(at: file)
            throw AppError.message(
                "Die Prüfsumme der geladenen Datei stimmt nicht.\n\n"
                + "Erwartet: \(sha)\nErhalten: \(got)")
        }
        return file
    }

    /// ffmpeg and ffprobe — needed for audio trimming and Whisper, and absent
    /// on a Mac without Homebrew. The JavaScript runtime (Deno) is no longer
    /// installed here: only yt-dlp needs it, so it comes with the opt-in
    /// install (POST /api/media/install).
    ///
    /// This used to be the whole obstacle to handing the app to someone: the
    /// Python side installed itself fine, then every download failed on a
    /// missing binary. Rather than bundling them (ffmpeg is GPL, and shipping
    /// it inside a signed app bundle brings obligations with it) they are
    /// installed as wheels into the virtual environment, which is also where
    /// everything else already lives.
    private func ensureMediaTools(python: URL, venv: URL) throws {
        let bin = venv.appendingPathComponent("bin", isDirectory: true)

        // ffmpeg only if the machine has none. A Homebrew install is newer and
        // the user's own choice; no reason to shadow it.
        if Backend.which("ffmpeg") != nil && Backend.which("ffprobe") != nil { return }

        onPhase?(.preparing(L("ffmpeg wird geladen — rund 80 MB, nur dieses eine Mal…", "Downloading ffmpeg — about 80 MB, only this once…")))
        try Backend.run(python, ["-m", "pip", "install", "--quiet", "static-ffmpeg"])
        // The wheel carries no binaries; it fetches them for this architecture
        // on first use. Doing that here, with a status line, beats having it
        // happen inside the first download the user starts.
        let paths = try Backend.run(python, ["-c",
            "from static_ffmpeg import run\n"
            + "for p in run.get_or_fetch_platform_executables_else_raise(): print(p)"])
        // Pick the two paths out of the output rather than assuming the output
        // is only the two paths: the fetch narrates itself on stdout
        // ("Download of … completed", "Extracting …"), and this method merges
        // stdout with stderr. Counting lines here cost one failed first run.
        let found = paths.split(separator: "\n").map {
            $0.trimmingCharacters(in: .whitespaces)
        }.filter {
            $0.hasPrefix("/")
                && (($0 as NSString).lastPathComponent == "ffmpeg"
                    || ($0 as NSString).lastPathComponent == "ffprobe")
                && FileManager.default.isExecutableFile(atPath: $0)
        }
        guard found.count == 2 else {
            throw AppError.message("ffmpeg konnte nicht eingerichtet werden:\n" + paths.suffix(400))
        }
        // Symlink rather than copy: pip owns the originals, and an upgrade
        // should not leave a stale duplicate behind.
        for src in found {
            let dest = bin.appendingPathComponent((src as NSString).lastPathComponent)
            try? FileManager.default.removeItem(at: dest)
            try FileManager.default.createSymbolicLink(at: dest, withDestinationURL: URL(fileURLWithPath: src))
        }
    }

    private func launch(python: URL) throws {
        onPhase?(.preparing(L("Server wird gestartet…", "Starting the server…")))
        let p = Process()
        p.executableURL = python
        p.arguments = ["-m", "uvicorn", "main:app",
                       "--host", "127.0.0.1", "--port", String(port)]
        p.currentDirectoryURL = serverDir

        var env = ProcessInfo.processInfo.environment
        // An app launched from the Dock inherits launchd's minimal PATH —
        // /usr/bin:/bin:/usr/sbin:/sbin, with no Homebrew in it. Started from
        // a terminal it inherits the shell's PATH instead, so this breaks
        // only for the way people actually open apps, and breaks everything:
        // ffmpeg, ffprobe, yt-dlp and the JS runtime all live in /opt/homebrew.
        // venv/bin first: on a machine without Homebrew it is the only place
        // ffmpeg, ffprobe and deno exist at all (see ensureMediaTools).
        let extraPaths = [python.deletingLastPathComponent().path,
                          "/opt/homebrew/bin", "/opt/homebrew/sbin",
                          "/usr/local/bin", "\(NSHomeDirectory())/.local/bin"]
        let current = (env["PATH"] ?? "").split(separator: ":").map(String.init)
        env["PATH"] = (extraPaths.filter { !current.contains($0) } + current)
            .joined(separator: ":")
        env["LOCAL_MODE"] = "1"
        // Dies ist die Bedingung, an der der entfallende Login hängt — nicht
        // LOCAL_MODE allein.
        //
        // LOCAL_MODE steht in der .env.example und lässt sich in einem
        // öffentlichen Container genauso setzen. Steht dort auf derselben
        // Maschine ein nginx davor, ist `request.client` für jeden Besucher
        // aus dem Internet 127.0.0.1 — und die Instanz hätte keinen Login,
        // mit dem ersten Account (dem Administrator) als implizitem Nutzer.
        //
        // Diese Variable setzt nur die App, und sie bedeutet genau eins: der
        // Server wurde von mir gestartet, auf 127.0.0.1, für diesen einen
        // Menschen. Niemand schreibt sie versehentlich in eine .env.
        env["FUNDUS_DESKTOP"] = "1"
        // Shared secret between app and server, so requests from our own
        // WebView are distinguishable from requests any web page could make
        // to the same port. In the environment, not in argv — argv is visible
        // to every process on the machine via ps.
        env["LOCAL_TOKEN"] = localToken
        // The server exits when this process is gone, even after a force quit.
        env["FUNDUS_PARENT_PID"] = String(ProcessInfo.processInfo.processIdentifier)
        // Data lives outside the bundle: a reinstall or an update replaces
        // the app, and taking the user's accounts with it would be rude.
        env["DATA_DIR"] = support.appendingPathComponent("data").path
        env["DOWNLOAD_DIR"] = support.appendingPathComponent("downloads").path
        env["TMP_DIR"] = support.appendingPathComponent("tmp").path
        env["WHISPER_MODEL_DIR"] = support.appendingPathComponent("models").path
        env["PYTHONUNBUFFERED"] = "1"
        p.environment = env

        // Keep stderr: when the server refuses to start, its last lines are
        // the only useful thing to show.
        let pipe = Pipe()
        p.standardError = pipe
        p.standardOutput = pipe
        logTail(pipe)

        try p.run()
        process = p
    }

    private func waitUntilReady() {
        let health = URL(string: "http://127.0.0.1:\(port)/health")!
        let deadline = Date().addingTimeInterval(90)
        while Date() < deadline {
            if let p = process, !p.isRunning {
                onPhase?(.failed("Der Server hat sich beendet.\n\n" + recentLog()))
                return
            }
            var req = URLRequest(url: health)
            req.timeoutInterval = 2
            let sem = DispatchSemaphore(value: 0)
            // The semaphore already orders the write before the read, but the
            // compiler cannot see that across the closure boundary — and a
            // captured `var` mutated from a URLSession thread is a real race
            // the day someone adds an early return above the wait.
            let ok = Flag()
            URLSession.shared.dataTask(with: req) { _, resp, _ in
                ok.set((resp as? HTTPURLResponse)?.statusCode == 200)
                sem.signal()
            }.resume()
            _ = sem.wait(timeout: .now() + 3)
            if ok.value { onPhase?(.running(url)); return }
            usleep(400_000)
        }
        onPhase?(.failed("Der Server antwortet nicht.\n\n" + recentLog()))
    }

    // MARK: - Logging

    /// Separate from Backend so the pipe handler can capture something the
    /// compiler can prove is safe to touch from another thread. Backend
    /// itself is not Sendable, and making it so would be a lie — it owns a
    /// Process and a callback that belong to the launching thread.
    final class LogBuffer: @unchecked Sendable {
        private let lock = NSLock()
        private var lines: [String] = []

        func append(_ chunk: String) {
            lock.lock(); defer { lock.unlock() }
            lines.append(contentsOf: chunk.split(separator: "\n").map(String.init))
            if lines.count > 200 { lines.removeFirst(lines.count - 200) }
        }

        func tail(_ n: Int = 15) -> String {
            lock.lock(); defer { lock.unlock() }
            return lines.suffix(n).joined(separator: "\n")
        }
    }

    private let log = LogBuffer()

    private func logTail(_ pipe: Pipe) {
        let buffer = log      // value capture, no self
        pipe.fileHandleForReading.readabilityHandler = { handle in
            guard let s = String(data: handle.availableData, encoding: .utf8), !s.isEmpty else { return }
            buffer.append(s)
            FileHandle.standardError.write(Data(s.utf8))
        }
    }

    func recentLog() -> String { log.tail() }

    // MARK: - Helpers

    enum AppError: LocalizedError {
        case message(String)
        var errorDescription: String? { if case .message(let m) = self { return m }; return nil }
    }

    /// A boolean that may be written from one thread and read from another.
    final class Flag: @unchecked Sendable {
        private let lock = NSLock()
        private var flag = false
        func set(_ v: Bool) { lock.lock(); flag = v; lock.unlock() }
        var value: Bool { lock.lock(); defer { lock.unlock() }; return flag }
    }

    static var isAppleSilicon: Bool {
        var size = 0
        sysctlbyname("hw.optional.arm64", nil, &size, nil, 0)
        var value: Int32 = 0
        var vsize = MemoryLayout<Int32>.size
        guard sysctlbyname("hw.optional.arm64", &value, &vsize, nil, 0) == 0 else { return false }
        return value == 1
    }

    /// A Python already on the machine that is new enough to be worth using.
    ///
    /// Two filters, both learned the hard way. Each candidate has to actually
    /// answer: Apple's `/usr/bin/python3` is a stub that exists whether or not
    /// the Command Line Tools do, and an existence check on it sends the app
    /// into a venv creation that pops an unexplained system dialog and fails.
    /// And it has to be 3.11 or newer: Apple ships 3.9.6, which the server's
    /// own dependencies reject outright.
    static func findSystemPython() -> URL? {
        let candidates = [
            "/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3",
        ]
        for c in candidates where FileManager.default.isExecutableFile(atPath: c) {
            let url = URL(fileURLWithPath: c)
            guard let out = try? run(url, ["-c", "import sys; print(sys.version_info[:2])"]) else { continue }
            let nums = out.split(whereSeparator: { !$0.isNumber }).compactMap { Int($0) }
            if nums.count >= 2, nums[0] == 3, nums[1] >= 11 { return url }
        }
        return nil
    }

    /// Somewhere a completion handler can leave its answer. Same role as
    /// `Flag`, for a value rather than a boolean.
    final class Box<T>: @unchecked Sendable {
        private let lock = NSLock()
        private var stored: T?
        var value: T? {
            get { lock.lock(); defer { lock.unlock() }; return stored }
            set { lock.lock(); stored = newValue; lock.unlock() }
        }
    }

    /// An executable on the PATH a launched server would actually see — which
    /// is not this process's PATH when the app came from the Dock.
    static func which(_ name: String) -> String? {
        // Forcing the bundled runtime means pretending the machine has nothing,
        // tools included — otherwise the one path worth rehearsing, a Mac
        // without Homebrew, is the one path the rehearsal skips.
        if ProcessInfo.processInfo.environment["FUNDUS_FORCE_RUNTIME"] == "1" { return nil }
        let dirs = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
                    "\(NSHomeDirectory())/.local/bin"]
        for d in dirs {
            let p = (d as NSString).appendingPathComponent(name)
            if FileManager.default.isExecutableFile(atPath: p) { return p }
        }
        return nil
    }

    static let preferredPort: UInt16 = 8765

    static func portIsFree(_ port: UInt16) -> Bool {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        addr.sin_port = port.bigEndian
        return withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        } == 0
    }

    /// Ask the kernel for an unused port, then hand it to uvicorn. There is a
    /// race between closing and rebinding, but it is a local loopback socket
    /// and the alternative is a hardcoded port that collides.
    static func freePort() -> UInt16 {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        addr.sin_port = 0
        let bound = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                bind(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0 else { return 8765 }
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        _ = withUnsafeMutablePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(fd, $0, &len) }
        }
        return UInt16(bigEndian: addr.sin_port)
    }

    @discardableResult
    static func run(_ exe: URL, _ args: [String]) throws -> String {
        let p = Process()
        p.executableURL = exe
        p.arguments = args
        // Same reason as in launch(): pip builds shell out to compilers and
        // git, and from a Dock launch it would not find them.
        var env = ProcessInfo.processInfo.environment
        let current = (env["PATH"] ?? "").split(separator: ":").map(String.init)
        env["PATH"] = (["/opt/homebrew/bin", "/usr/local/bin"].filter { !current.contains($0) }
                       + current).joined(separator: ":")
        p.environment = env
        let out = Pipe()
        p.standardOutput = out
        p.standardError = out
        try p.run()
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        let text = String(data: data, encoding: .utf8) ?? ""
        if p.terminationStatus != 0 {
            throw AppError.message("\(exe.lastPathComponent) \(args.first ?? "") fehlgeschlagen:\n"
                                   + text.suffix(600))
        }
        return text
    }
}
