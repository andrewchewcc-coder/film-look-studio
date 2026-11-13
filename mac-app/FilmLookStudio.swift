// Film Look Studio as a real Mac app: its own window (no browser bars), Dock icon and menu bar.
// It starts the local server with mac-app/launch.sh (unchanged), then shows http://127.0.0.1:8765 in a
// WKWebView. Built by make_app.sh with swiftc; the project folder comes from Info.plist (FLSProjectDir).
//   Window: size and place remembered. Close the window or ⌘Q = quit (the server stops when nothing runs).
//   Links to other sites and "open in a new tab" go to your normal browser; downloads go to ~/Downloads.
import Cocoa
import WebKit

let appURL = URL(string: "http://127.0.0.1:8765/")!

// The page's web view, remembering the last mouse-down so a drag that starts in the page (a photo dragged out
// to the Desktop, a folder, a mail) can be handed to macOS as a real file drag.
final class DragWebView: WKWebView {
    var lastDown: NSEvent?
    override func mouseDown(with event: NSEvent) { lastDown = event; super.mouseDown(with: event) }
}

// Drags files that don't exist yet: the app's server makes each JPG copy when the drop happens (a RAW can
// take ~15 s the first time), and Finder shows progress. Always copies, never the originals.
final class FileDragSource: NSObject, NSDraggingSource, NSFilePromiseProviderDelegate {
    let queue: OperationQueue = { let q = OperationQueue(); q.maxConcurrentOperationCount = 2; return q }()
    func draggingSession(_ s: NSDraggingSession, sourceOperationMaskFor c: NSDraggingContext) -> NSDragOperation { .copy }
    func filePromiseProvider(_ p: NSFilePromiseProvider, fileNameForType t: String) -> String {
        ((p.userInfo as? [String: String])?["name"]) ?? "photo.jpg"
    }
    func operationQueue(for p: NSFilePromiseProvider) -> OperationQueue { queue }
    func filePromiseProvider(_ p: NSFilePromiseProvider, writePromiseTo url: URL, completionHandler: @escaping (Error?) -> Void) {
        guard let str = (p.userInfo as? [String: String])?["url"], let src = URL(string: str, relativeTo: appURL) else {
            return completionHandler(NSError(domain: "FLS", code: 1))
        }
        var req = URLRequest(url: src); req.timeoutInterval = 300
        let done = DispatchSemaphore(value: 0)
        var failure: Error?
        URLSession.shared.downloadTask(with: req) { tmp, resp, err in
            if let err = err { failure = err }
            else if let tmp = tmp, (resp as? HTTPURLResponse)?.statusCode == 200 {
                do { try? FileManager.default.removeItem(at: url); try FileManager.default.moveItem(at: tmp, to: url) }
                catch { failure = error }
            } else { failure = NSError(domain: "FLS", code: 2) }
            done.signal()
        }.resume()
        done.wait()
        completionHandler(failure)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate, WKDownloadDelegate,
                         WKScriptMessageHandler {
    var window: NSWindow!
    var web: DragWebView!
    let dragSource = FileDragSource()
    var note: NSTextField!

    func applicationDidFinishLaunching(_ n: Notification) {
        buildMenu()
        let cfg = WKWebViewConfiguration()
        cfg.userContentController.add(self, name: "fls")              // the page's Quit button tells us to close
        cfg.preferences.isElementFullscreenEnabled = true
        web = DragWebView(frame: .zero, configuration: cfg)
        web.uiDelegate = self
        web.navigationDelegate = self
        web.allowsMagnification = true
        web.isHidden = true

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1440, height: 900),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                          backing: .buffered, defer: false)
        window.title = "Film Look Studio"
        window.titlebarAppearsTransparent = false
        window.minSize = NSSize(width: 900, height: 600)
        window.center()
        window.setFrameAutosaveName("FilmLookStudioMain")             // remembers size and place
        let box = NSView()
        note = NSTextField(labelWithString: "Starting Film Look Studio…")
        note.font = .systemFont(ofSize: 15)
        note.textColor = .secondaryLabelColor
        note.translatesAutoresizingMaskIntoConstraints = false
        web.translatesAutoresizingMaskIntoConstraints = false
        box.addSubview(web); box.addSubview(note)
        NSLayoutConstraint.activate([
            web.leadingAnchor.constraint(equalTo: box.leadingAnchor), web.trailingAnchor.constraint(equalTo: box.trailingAnchor),
            web.topAnchor.constraint(equalTo: box.topAnchor), web.bottomAnchor.constraint(equalTo: box.bottomAnchor),
            note.centerXAnchor.constraint(equalTo: box.centerXAnchor), note.centerYAnchor.constraint(equalTo: box.centerYAnchor)])
        window.contentView = box
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        startServer()
    }

    // MARK: server

    func projectDir() -> String { Bundle.main.object(forInfoDictionaryKey: "FLSProjectDir") as? String ?? "" }

    func startServer() {
        let script = projectDir() + "/mac-app/launch.sh"
        DispatchQueue.global().async {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/bin/bash")
            p.arguments = [script, "start"]
            var ok = false
            do { try p.run(); p.waitUntilExit(); ok = p.terminationStatus == 0 } catch { ok = false }
            DispatchQueue.main.async {
                if ok { self.web.load(URLRequest(url: appURL)) } else { self.startFailed() }
            }
        }
    }

    func startFailed() {
        note.stringValue = "Film Look Studio didn't start."
        let a = NSAlert()
        a.messageText = "Film Look Studio didn't start"
        a.informativeText = "The first time, macOS asks whether it may use your Documents folder (where the app lives) and your external drives (your photos). Click Allow for both — the boxes can hide behind other windows.\n\nIf you clicked Don't Allow: open Privacy Settings › Files and Folders, find Film Look Studio and switch on Documents Folder and Removable Volumes. Then open the app again."
        a.addButton(withTitle: "OK"); a.addButton(withTitle: "Open Privacy Settings")
        if a.runModal() == .alertSecondButtonReturn {
            NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders")!)
        }
    }

    func webView(_ w: WKWebView, didFinish nav: WKNavigation!) { w.isHidden = false; note.isHidden = true }

    func webView(_ w: WKWebView, didFail nav: WKNavigation!, withError e: Error) { retryLoad() }
    func webView(_ w: WKWebView, didFailProvisionalNavigation nav: WKNavigation!, withError e: Error) { retryLoad() }
    func retryLoad() {                                                 // server restarting: try again shortly
        note.isHidden = false; note.stringValue = "Waiting for Film Look Studio…"
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { self.web.load(URLRequest(url: appURL)) }
    }

    // MARK: quitting

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }

    func applicationShouldTerminate(_ s: NSApplication) -> NSApplication.TerminateReply {
        // Ask the server to stop. While an export, book or upload is running it says no: that work
        // finishes in the background and the server then stops by itself. Short wait: never hangs.
        var req = URLRequest(url: appURL.appendingPathComponent("api/quit"))
        req.httpMethod = "POST"; req.timeoutInterval = 1.5
        var busy: [String] = []
        let done = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: req) { data, _, _ in
            if let d = data, let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
               (j["ok"] as? Bool) == false { busy = j["busy"] as? [String] ?? [] }
            done.signal()
        }.resume()
        _ = done.wait(timeout: .now() + 1.5)
        if !busy.isEmpty {
            let a = NSAlert()
            a.messageText = "Still working on: " + busy.joined(separator: ", ")
            a.informativeText = "It will finish in the background, then Film Look Studio stops by itself. Open the app again any time to see how it's going."
            a.addButton(withTitle: "Quit"); a.addButton(withTitle: "Cancel")
            if a.runModal() != .alertFirstButtonReturn { return .terminateCancel }
        }
        return .terminateNow
    }

    func userContentController(_ c: WKUserContentController, didReceive m: WKScriptMessage) {
        if (m.body as? String) == "quit" { NSApp.terminate(nil); return }
        if let d = m.body as? [String: Any], let files = d["drag"] as? [[String: String]], !files.isEmpty,
           let ev = web.lastDown {                                        // the page says: a photo is being dragged out
            let at = web.convert(ev.locationInWindow, from: nil)
            let icon = NSWorkspace.shared.icon(forFileType: "jpg"); icon.size = NSSize(width: 56, height: 56)
            var items: [NSDraggingItem] = []
            for (i, f) in files.prefix(30).enumerated() {
                let p = NSFilePromiseProvider(fileType: "public.jpeg", delegate: dragSource)
                p.userInfo = ["name": f["name"] ?? "photo.jpg", "url": f["url"] ?? ""]
                let it = NSDraggingItem(pasteboardWriter: p)
                let k = CGFloat(min(i, 4)) * 4
                it.setDraggingFrame(NSRect(x: at.x - 28 + k, y: at.y - 28 - k, width: 56, height: 56), contents: icon)
                items.append(it)
            }
            web.beginDraggingSession(with: items, event: ev, source: dragSource)
        }
    }

    // MARK: links, new windows, dialogs, file choosers, downloads

    func webView(_ w: WKWebView, decidePolicyFor a: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let u = a.request.url, a.targetFrame?.isMainFrame ?? true, u.host != appURL.host, u.scheme != "blob", u.scheme != "about" {
            NSWorkspace.shared.open(u); decisionHandler(.cancel); return  // other sites: your normal browser
        }
        decisionHandler(a.shouldPerformDownload ? .download : .allow)
    }

    func webView(_ w: WKWebView, decidePolicyFor r: WKNavigationResponse, decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let attach = ((r.response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition") ?? "").lowercased().hasPrefix("attachment")
        decisionHandler(attach || !r.canShowMIMEType ? .download : .allow)
    }

    func webView(_ w: WKWebView, createWebViewWith c: WKWebViewConfiguration, for a: WKNavigationAction, windowFeatures f: WKWindowFeatures) -> WKWebView? {
        if let u = a.request.url { NSWorkspace.shared.open(u) }        // previews, PDFs, WordPress: in your browser
        return nil
    }

    func webView(_ w: WKWebView, navigationAction a: WKNavigationAction, didBecome d: WKDownload) { d.delegate = self }
    func webView(_ w: WKWebView, navigationResponse r: WKNavigationResponse, didBecome d: WKDownload) { d.delegate = self }

    func download(_ d: WKDownload, decideDestinationUsing r: URLResponse, suggestedFilename name: String,
                  completionHandler: @escaping (URL?) -> Void) {
        let dir = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask)[0]
        var dest = dir.appendingPathComponent(name), n = 2
        let base = (name as NSString).deletingPathExtension, ext = (name as NSString).pathExtension
        while FileManager.default.fileExists(atPath: dest.path) {         // never overwrite: "name 2.cube"
            dest = dir.appendingPathComponent(ext.isEmpty ? "\(base) \(n)" : "\(base) \(n).\(ext)"); n += 1
        }
        completionHandler(dest)
    }

    func downloadDidFinish(_ d: WKDownload) {
        if let u = d.progress.fileURL { NSWorkspace.shared.activateFileViewerSelecting([u]) }
    }

    func webView(_ w: WKWebView, runJavaScriptAlertPanelWithMessage m: String, initiatedByFrame f: WKFrameInfo,
                 completionHandler: @escaping () -> Void) {
        let a = NSAlert(); a.messageText = m; a.addButton(withTitle: "OK")
        a.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ w: WKWebView, runJavaScriptConfirmPanelWithMessage m: String, initiatedByFrame f: WKFrameInfo,
                 completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert(); a.messageText = m; a.addButton(withTitle: "OK"); a.addButton(withTitle: "Cancel")
        a.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn) }
    }

    func webView(_ w: WKWebView, runJavaScriptTextInputPanelWithPrompt p: String, defaultText t: String?,
                 initiatedByFrame f: WKFrameInfo, completionHandler: @escaping (String?) -> Void) {
        let a = NSAlert(); a.messageText = p; a.addButton(withTitle: "OK"); a.addButton(withTitle: "Cancel")
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24)); field.stringValue = t ?? ""
        a.accessoryView = field
        a.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn ? field.stringValue : nil) }
    }

    func webView(_ w: WKWebView, runOpenPanelWith p: WKOpenPanelParameters, initiatedByFrame f: WKFrameInfo,
                 completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = p.allowsMultipleSelection
        panel.canChooseDirectories = p.allowsDirectories
        panel.beginSheetModal(for: window) { completionHandler($0 == .OK ? panel.urls : nil) }
    }

    // MARK: menu bar

    @objc func reloadPage(_ s: Any?) { web.reload() }
    @objc func zoomIn(_ s: Any?) { web.pageZoom = min(2.0, web.pageZoom + 0.1) }
    @objc func zoomOut(_ s: Any?) { web.pageZoom = max(0.6, web.pageZoom - 0.1) }
    @objc func zoomReset(_ s: Any?) { web.pageZoom = 1.0 }
    @objc func openInBrowser(_ s: Any?) { NSWorkspace.shared.open(appURL) }
    @objc func showLog(_ s: Any?) {
        NSWorkspace.shared.open(URL(fileURLWithPath: NSHomeDirectory() + "/Library/Logs/Film Look Studio.log"))
    }

    func buildMenu() {
        let main = NSMenu()
        func sub(_ title: String, _ items: [NSMenuItem]) {
            let it = NSMenuItem(title: title, action: nil, keyEquivalent: ""); let m = NSMenu(title: title)
            items.forEach { m.addItem($0) }; it.submenu = m; main.addItem(it)
        }
        func item(_ t: String, _ a: Selector?, _ k: String = "", _ mods: NSEvent.ModifierFlags = .command, target: AnyObject? = nil) -> NSMenuItem {
            let i = NSMenuItem(title: t, action: a, keyEquivalent: k); i.keyEquivalentModifierMask = mods; i.target = target; return i
        }
        sub("Film Look Studio", [
            item("About Film Look Studio", #selector(NSApplication.orderFrontStandardAboutPanel(_:))), .separator(),
            item("Hide Film Look Studio", #selector(NSApplication.hide(_:)), "h"),
            item("Hide Others", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option]),
            item("Show All", #selector(NSApplication.unhideAllApplications(_:))), .separator(),
            item("Quit Film Look Studio", #selector(NSApplication.terminate(_:)), "q")])
        // Edit: cut / copy / paste for text boxes. ⌘Z and ⌘C / ⌘V on photos still reach the page first.
        sub("Edit", [
            item("Undo", Selector(("undo:")), "z"), item("Redo", Selector(("redo:")), "z", [.command, .shift]), .separator(),
            item("Cut", #selector(NSText.cut(_:)), "x"), item("Copy", #selector(NSText.copy(_:)), "c"),
            item("Paste", #selector(NSText.paste(_:)), "v"), item("Select All", #selector(NSText.selectAll(_:)), "a")])
        sub("View", [
            item("Reload", #selector(reloadPage(_:)), "r", target: self), .separator(),
            item("Actual Size", #selector(zoomReset(_:)), "0", target: self),
            item("Zoom In", #selector(zoomIn(_:)), "+", target: self),
            item("Zoom Out", #selector(zoomOut(_:)), "-", target: self), .separator(),
            item("Enter Full Screen", #selector(NSWindow.toggleFullScreen(_:)), "f", [.command, .control])])
        sub("Window", [
            item("Minimise", #selector(NSWindow.performMiniaturize(_:)), "m"),
            item("Zoom", #selector(NSWindow.performZoom(_:)))])
        sub("Help", [
            item("Open in Browser Instead", #selector(openInBrowser(_:)), target: self),
            item("Show Log", #selector(showLog(_:)), target: self)])
        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
