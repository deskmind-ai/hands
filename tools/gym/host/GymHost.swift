// A chromeless window around one web page, for the gym. In Safari the accessibility tree the planner reads was mostly
// the browser -- its sidebar (with the user's own iCloud tabs and devices), bookmarks, toolbar -- and the page's
// rows were cut at the element limit, and it would put the user's data into training rows. Here the tree is the page.
//
//   open -g -n tools/gym/host/build/GymHost.app --args <url>
import AppKit
import WebKit

final class Delegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    var windows: [NSWindow] = []
    var webs: [WKWebView] = []
    /// The page's title, as the window's: the planner and the oracle tell the two apps' windows apart by it. The page
    /// sets it from script once its task is fetched, after the load finishes, so it is followed, not read once.
    var titles: [NSKeyValueObservation] = []

    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.mainMenu = Self.menu()
        // One window per URL: a task that reads in one app and acts in another has both, as two windows of this
        // app, and the planner moves between them like two documents.
        // --display <id>: the windows open on that display (a virtual one, so the gym leaves the user's screen alone).
        var args = Array(CommandLine.arguments.dropFirst())
        var screen: NSScreen? = nil
        if let i = args.firstIndex(of: "--display"), i + 1 < args.count {
            let id = UInt32(args[i + 1]) ?? 0
            screen = NSScreen.screens.first {
                ($0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value == id
            }
            args.removeSubrange(i...(i + 1))
        }
        var urls = args.compactMap(URL.init(string:))
        if urls.isEmpty { urls = [URL(string: "about:blank")!] }
        for (i, url) in urls.enumerated() {
            let web = WKWebView(frame: NSRect(x: 0, y: 0, width: 1100, height: 760), configuration: WKWebViewConfiguration())
            // Behind the user's windows the page counts as hidden: WebKit stops painting it (screenshots come out
            // blank) and defers its accessibility tree. The gym window is always behind, so it is never occluded.
            let occlusion = NSSelectorFromString("_setWindowOcclusionDetectionEnabled:")
            if web.responds(to: occlusion) {
                typealias Setter = @convention(c) (AnyObject, Selector, Bool) -> Void
                unsafeBitCast(web.method(for: occlusion), to: Setter.self)(web, occlusion, false)
            }
            web.navigationDelegate = self
            titles.append(web.observe(\.title, options: [.new]) { web, _ in
                if let t = web.title, !t.isEmpty { web.window?.title = t }
            })
            let window = NSWindow(contentRect: web.frame, styleMask: [.titled, .closable, .resizable, .miniaturizable],
                                  backing: .buffered, defer: false)
            window.contentView = web
            window.title = "Gym"
            window.center()
            if let f = screen?.visibleFrame {
                window.setFrameOrigin(NSPoint(x: f.midX - window.frame.width / 2, y: f.midY - window.frame.height / 2))
            }
            window.setFrameOrigin(NSPoint(x: window.frame.origin.x + CGFloat(i * 40), y: window.frame.origin.y - CGFloat(i * 40)))
            window.orderFront(nil)   // not key, not activated: the gym runs behind whatever the user is doing
            web.load(URLRequest(url: url))
            windows.append(window)
            webs.append(web)
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if let t = webView.title, !t.isEmpty { webView.window?.title = t }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        FileHandle.standardError.write("GymHost: \(error)\n".data(using: .utf8)!)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        FileHandle.standardError.write("GymHost: \(error)\n".data(using: .utf8)!)
    }

    // Key equivalents reach the web view through the main menu: without an Edit menu, command-V (how hands types into
    // a web field) does nothing.
    static func menu() -> NSMenu {
        let main = NSMenu()
        let appItem = NSMenuItem()
        appItem.submenu = NSMenu()
        appItem.submenu!.addItem(withTitle: "Quit GymHost", action: #selector(NSApplication.terminate(_:)),
                                 keyEquivalent: "q")
        main.addItem(appItem)
        let editItem = NSMenuItem()
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        main.addItem(editItem)
        return main
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }
}

@main
enum GymHostMain {
    static let delegate = Delegate()
    static func main() {
        let app = NSApplication.shared
        app.delegate = delegate
        app.setActivationPolicy(.regular)
        app.run()
    }
}
