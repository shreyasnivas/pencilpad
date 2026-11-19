// PencilPad menu bar app. Keeps pencilpad.py running in the background, starts
// at login, and shows the connection state. Build and install with app/build.sh.
//
// The Python script runs as a child of this app, so macOS asks for the
// Accessibility permission for PencilPad itself rather than for a terminal.

import Cocoa
import ApplicationServices
import Speech

// "PencilPad --transcribe file.wav": print what was said in a recording, then exit.
// The Python script runs this for the Dictate key when the iPad's microphone is used.
// Exit code 3 means Speech Recognition is not allowed for PencilPad.
if CommandLine.arguments.count == 3, CommandLine.arguments[1] == "--transcribe" {
    let file = URL(fileURLWithPath: CommandLine.arguments[2])
    SFSpeechRecognizer.requestAuthorization { status in
        guard status == .authorized else { exit(3) }
        guard let recognizer = SFSpeechRecognizer() ?? SFSpeechRecognizer(locale: Locale(identifier: "en-US")),
              recognizer.isAvailable else { exit(4) }
        let request = SFSpeechURLRecognitionRequest(url: file)
        request.shouldReportPartialResults = false
        if recognizer.supportsOnDeviceRecognition { request.requiresOnDeviceRecognition = true }
        if #available(macOS 13, *) { request.addsPunctuation = true }
        recognizer.recognitionTask(with: request) { result, error in
            if let result = result, result.isFinal {
                print(result.bestTranscription.formattedString)
                exit(0)
            }
            if error != nil { exit(0) }      // nothing was said
        }
    }
    RunLoop.main.run()
}

let home = FileManager.default.homeDirectoryForCurrentUser
let agentLabel = "io.github.pencilpad.app"
let agentPlist = home.appendingPathComponent("Library/LaunchAgents/\(agentLabel).plist")
let logURL = home.appendingPathComponent("Library/Logs/PencilPad.log")

final class App: NSObject, NSApplicationDelegate {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let statusLine = NSMenuItem(title: "Starting…", action: nil, keyEquivalent: "")
    let detailLine = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    let permission = NSMenuItem(title: "Allow Accessibility…", action: #selector(openAccessibility), keyEquivalent: "")
    let loginItem = NSMenuItem(title: "Start at Login", action: #selector(toggleLogin), keyEquivalent: "")

    var child: Process?
    var quitting = false
    var startedTrusted = false
    var quickExits = 0
    var lastStart = Date.distantPast
    var timer: Timer?

    // where the script lives: written into the app by build.sh
    let repo: String = (Bundle.main.object(forInfoDictionaryKey: "PencilPadRepo") as? String) ?? ""
    // the port: PENCILPAD_PORT, else the one the script saved on its first run (read each
    // time, so a port it chose then is picked up), else 8765
    var port: String {
        if let p = ProcessInfo.processInfo.environment["PENCILPAD_PORT"], !p.isEmpty { return p }
        let saved = try? String(contentsOf: home.appendingPathComponent(".pencilpad/port"), encoding: .utf8)
        return Int((saved ?? "").trimmingCharacters(in: .whitespacesAndNewlines)).map(String.init) ?? "8765"
    }
    var token: String {
        let t = try? String(contentsOf: home.appendingPathComponent(".pencilpad-token"), encoding: .utf8)
        return (t ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    }
    var base: String { "http://127.0.0.1:\(port)/\(token)" }

    func applicationDidFinishLaunching(_ note: Notification) {
        setIcon(connected: false)
        let menu = NSMenu()
        menu.autoenablesItems = false
        statusLine.isEnabled = false
        detailLine.isEnabled = false
        detailLine.isHidden = true
        permission.target = self
        permission.isHidden = true
        menu.addItem(statusLine)
        menu.addItem(detailLine)
        menu.addItem(permission)
        menu.addItem(.separator())
        menu.addItem(make("Show Setup Code…", #selector(showSetup), "s"))
        menu.addItem(make("Copy iPad Link", #selector(copyLink), "c"))
        menu.addItem(make("Pair with Another Mac…", #selector(pairMac), ""))
        menu.addItem(.separator())
        menu.addItem(make("Restart PencilPad", #selector(restart), "r"))
        loginItem.target = self
        menu.addItem(loginItem)
        menu.addItem(make("Show Log", #selector(showLog), ""))
        menu.addItem(.separator())
        menu.addItem(make("Quit PencilPad", #selector(quit), "q"))
        item.menu = menu
        refreshLoginItem()
        startChild()
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.poll() }
    }

    func make(_ title: String, _ action: Selector, _ key: String) -> NSMenuItem {
        let m = NSMenuItem(title: title, action: action, keyEquivalent: key)
        m.target = self
        return m
    }

    func setIcon(connected: Bool) {
        guard let button = item.button else { return }
        let names = connected ? ["applepencil.tip", "applepencil", "pencil.tip"] : ["applepencil", "pencil.tip", "pencil"]
        var image: NSImage?
        for n in names where image == nil { image = NSImage(systemSymbolName: n, accessibilityDescription: "PencilPad") }
        if let image = image {
            image.isTemplate = true
            button.image = image
            button.title = ""
        } else {
            button.title = "✎"
        }
        button.appearsDisabled = !connected
    }

    // ---- the Python process
    func startChild() {
        let python = repo + "/.venv/bin/python"
        let script = repo + "/pencilpad.py"
        guard FileManager.default.isExecutableFile(atPath: python),
              FileManager.default.fileExists(atPath: script) else {
            show("PencilPad files not found", detail: "Expected them in \(repo). Rebuild with app/build.sh.")
            return
        }
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        let p = Process()
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = [script]
        p.currentDirectoryURL = URL(fileURLWithPath: repo)
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        env["PENCILPAD_NO_OPEN"] = "1"
        env["PENCILPAD_EXIT_WITH_PARENT"] = "1"
        if let me = Bundle.main.executablePath {
            env["PENCILPAD_TRANSCRIBE"] = "'" + me.replacingOccurrences(of: "'", with: "'\\''") + "' --transcribe"
        }
        p.environment = env
        if let log = try? FileHandle(forWritingTo: logURL) {
            log.truncateFile(atOffset: 0)
            p.standardOutput = log
            p.standardError = log
        }
        p.terminationHandler = { [weak self] _ in DispatchQueue.main.async { self?.childExited() } }
        startedTrusted = AXIsProcessTrusted()
        lastStart = Date()
        do { try p.run(); child = p } catch { show("Could not start PencilPad", detail: "\(error.localizedDescription)") }
    }

    func childExited() {
        child = nil
        if quitting { return }
        // exits straight away when the port is taken (another copy is running): do not spin
        quickExits = Date().timeIntervalSince(lastStart) < 5 ? quickExits + 1 : 0
        if quickExits >= 3 { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in
            if let s = self, s.child == nil, !s.quitting { s.startChild() }
        }
    }

    func stopChild() {
        guard let p = child, p.isRunning else { return }
        p.terminate()
        p.waitUntilExit()
    }

    // ---- status
    func show(_ title: String, detail: String? = nil) {
        statusLine.title = title
        detailLine.title = detail ?? ""
        detailLine.isHidden = detail == nil
    }

    func poll() {
        let trusted = AXIsProcessTrusted()
        permission.isHidden = trusted
        if trusted && !startedTrusted && child != nil {     // permission was just granted
            restart()
            return
        }
        guard !token.isEmpty, let url = URL(string: base + "/state") else {
            show(child == nil ? "PencilPad is not running" : "Starting…"); setIcon(connected: false); return
        }
        var req = URLRequest(url: url)
        req.timeoutInterval = 1.5
        URLSession.shared.dataTask(with: req) { [weak self] data, _, _ in
            let json = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            DispatchQueue.main.async { self?.update(json, trusted: trusted) }
        }.resume()
    }

    func update(_ state: [String: Any]?, trusted: Bool) {
        guard let state = state else {
            show(child == nil ? "PencilPad is not running" : "Starting…")
            setIcon(connected: false)
            return
        }
        let clients = state["clients"] as? Int ?? 0
        let access = state["access"] as? Bool ?? false
        if !access || !trusted {
            show("Needs Accessibility permission", detail: "The pointer will not move until it is allowed.")
        } else if clients > 0 {
            show("iPad connected")
        } else {
            show("Waiting for your iPad", detail: "Open PencilPad on the iPad, or choose Show Setup Code.")
        }
        setIcon(connected: clients > 0 && access)
    }

    // ---- menu actions
    @objc func showSetup() { if let u = URL(string: "http://localhost:\(port)/\(token)/connect") { NSWorkspace.shared.open(u) } }

    @objc func copyLink() {
        guard let url = URL(string: base + "/state") else { return }
        URLSession.shared.dataTask(with: url) { [weak self] data, _, _ in
            guard let self = self else { return }
            let json = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
            let link = json?["url"] as? String ?? ""
            DispatchQueue.main.async {
                guard !link.isEmpty else { return }
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(link, forType: .string)
                self.show("Link copied")
            }
        }.resume()
    }

    // Use the same link as another Mac, so one pad on the iPad can reach both.
    @objc func pairMac() {
        let alert = NSAlert()
        alert.messageText = "Pair with Another Mac"
        alert.informativeText = "On the other Mac, choose Copy iPad Link from the PencilPad menu, then paste the link here. "
            + "This Mac will then use the same link, and the pad on your iPad can switch between the two."
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        field.placeholderString = "http://…"
        if let clip = NSPasteboard.general.string(forType: .string), clip.hasPrefix("http") { field.stringValue = clip }
        alert.accessoryView = field
        alert.addButton(withTitle: "Pair")
        alert.addButton(withTitle: "Cancel")
        NSApp.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: repo + "/.venv/bin/python")
        p.arguments = [repo + "/pencilpad.py", "--pair", field.stringValue]
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        do { try p.run() } catch { show("Could not pair", detail: "\(error.localizedDescription)"); return }
        p.waitUntilExit()
        let out = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
        let done = NSAlert()
        done.messageText = p.terminationStatus == 0 ? "Paired" : "Could not pair"
        done.informativeText = p.terminationStatus == 0
            ? "Open PencilPad on the iPad and tap the Mac's name at the top to switch between your Macs."
            : out.trimmingCharacters(in: .whitespacesAndNewlines)
        done.runModal()
        if p.terminationStatus == 0 { restart() }
    }

    @objc func restart() {
        quickExits = 0
        if child == nil { startChild() } else { stopChild() }   // the exit handler starts it again
    }

    @objc func openAccessibility() {
        let opts = ["AXTrustedCheckOptionPrompt": true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(opts)
        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")!)
    }

    @objc func showLog() { NSWorkspace.shared.open(logURL) }

    func refreshLoginItem() { loginItem.state = FileManager.default.fileExists(atPath: agentPlist.path) ? .on : .off }

    @objc func toggleLogin() {
        if FileManager.default.fileExists(atPath: agentPlist.path) {
            try? FileManager.default.removeItem(at: agentPlist)
        } else {
            let plist: [String: Any] = [
                "Label": agentLabel,
                "ProgramArguments": [Bundle.main.executablePath ?? ""],
                "RunAtLoad": true,
                "ProcessType": "Interactive",
            ]
            try? FileManager.default.createDirectory(at: agentPlist.deletingLastPathComponent(), withIntermediateDirectories: true)
            (plist as NSDictionary).write(to: agentPlist, atomically: true)
        }
        refreshLoginItem()
    }

    @objc func quit() {
        quitting = true
        stopChild()
        NSApp.terminate(nil)
    }

    func applicationWillTerminate(_ note: Notification) {
        quitting = true
        stopChild()
    }
}

// one copy only
let me = NSRunningApplication.current
if NSRunningApplication.runningApplications(withBundleIdentifier: Bundle.main.bundleIdentifier ?? agentLabel)
    .contains(where: { $0 != me }) {
    exit(0)
}
let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
