import Network
import XCTest

// Runs fastDeviceTest flows on a physical device. `testFlow` runs one flow passed as
// base64 JSON in FAST_FLOW; `testServe` stays alive and takes newline-delimited JSON
// requests on FAST_PORT so later flows and single actions skip the xcodebuild launch.
// Selectors and action names mirror the Simulator engine so one flow runs on both.
final class FlowRunner: XCTestCase {
    struct Failure: Error { let message: String }

    struct Node {
        let label: String
        let type: String
        let frame: CGRect
        // Frame of the nearest enclosing cell; nil outside lists.
        let cell: CGRect?
        // accessibilityIdentifier and the control's value ("1" for a switch that is on).
        let identifier: String
        let value: String
        let enabled: Bool
    }

    // `element_type` names a gesture can target by XCUIElement query (pinch, rotate, two_finger_tap,
    // clear_text, set_slider).
    static let elementTypes: [String: XCUIElement.ElementType] = [
        "Button": .button, "StaticText": .staticText, "TextField": .textField, "SecureTextField": .secureTextField,
        "SearchField": .searchField, "TextView": .textView, "Image": .image, "Cell": .cell, "Switch": .switch,
        "RadioButton": .radioButton, "Link": .link, "Tab": .tab, "Slider": .slider, "Map": .map,
        "ScrollView": .scrollView, "WebView": .webView, "Table": .table, "CollectionView": .collectionView, "Other": .other,
    ]

    // Keys `press_key` and `key_combo` name, as fast_ui.KEY_CODES does on the Simulator.
    static let namedKeys: [String: XCUIKeyboardKey] = [
        "delete": .delete, "return": .return, "tab": .tab, "escape": .escape, "space": .space,
        "up": .upArrow, "down": .downArrow, "left": .leftArrow, "right": .rightArrow,
    ]

    // The selector keys an action may carry, shared with the Simulator engine.
    static let selectorKeys = ["label", "label_contains", "label_is_empty", "id", "value", "element_type", "enabled", "frame"]

    private var apps: [String: XCUIApplication] = [:]
    private var current: XCUIApplication?
    // `optional_permission_labels` of the request being served (see PromptRules), and what
    // the runner tapped on its own; both start empty on every request.
    private var promptEntries: [Any] = []
    private var dismissed: [[String: String]] = []
    private var lastLabels: [String] = []
    private var screenBounds = CGRect.infinite
    private var inlineScreenshots = false
    private var screenshots: [[String: String]] = []
    // What the request saw and did, for the screen map (Trail.swift), and the bundle id of
    // `current`, which XCUIApplication does not expose.
    private var trail = Trail()
    private var currentBundleID = ""
    // A flow that turned the iPhone sideways turns it back when it ends.
    private var orientationChanged = false
    private let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
    // While an action runs, an XCTest failure (a gesture it cannot place) fails that step
    // instead of ending the test, which would take the persistent runner down with it.
    private var isRunningAction = false
    private var actionIssue: String?

    override func record(_ issue: XCTIssue) {
        guard isRunningAction else { return super.record(issue) }
        actionIssue = actionIssue ?? issue.compactDescription
    }

    func testFlow() throws {
        continueAfterFailure = false
        var report: [String: Any]
        do {
            report = runFlow(try decode(ProcessInfo.processInfo.environment["FAST_FLOW"]))
        } catch {
            report = ["status": "fail", "step": "prepare", "error": message(error)]
        }
        if !dismissed.isEmpty { report["dismissed"] = dismissed }
        report["trail"] = trail.finish()
        attachText("zz_result.json", json(report))
    }

    func testServe() throws {
        continueAfterFailure = true
        inlineScreenshots = true
        let environment = ProcessInfo.processInfo.environment
        let port = NWEndpoint.Port(rawValue: UInt16(environment["FAST_PORT"] ?? "") ?? 8765) ?? 8765
        let idleLimit = Double(environment["FAST_IDLE"] ?? "300") ?? 300
        let server = try RequestServer(port: port)
        server.start()
        var lastRequest = Date()
        var isStopped = false
        while !isStopped && Date().timeIntervalSince(lastRequest) < idleLimit {
            guard let request = server.next() else {
                RunLoop.current.run(until: Date().addingTimeInterval(0.05))
                continue
            }
            lastRequest = Date()
            screenshots = []
            var response = handle(request.body, isStopped: &isStopped)
            response["screenshots"] = screenshots
            response["trail"] = trail.finish()
            if !dismissed.isEmpty { response["dismissed"] = dismissed }
            request.reply(json(response, pretty: false))
        }
        server.stop()
    }
}

// MARK: - Requests
private extension FlowRunner {
    // XCUIDevice has no public lock button. `pressLockButton` is the private method WebDriverAgent
    // uses on a physical iPhone; looked up at runtime, so an Xcode without it refuses with the reason.
    func pressLockButton() throws {
        let selector = NSSelectorFromString("pressLockButton")
        guard XCUIDevice.shared.responds(to: selector) else {
            throw Failure(message: "this Xcode's XCUIDevice has no pressLockButton; lock the iPhone by hand")
        }
        XCUIDevice.shared.perform(selector)
    }

    func handle(_ body: Data, isStopped: inout Bool) -> [String: Any] {
        // A request taps only the prompts it declares itself: labels from an earlier flow
        // would otherwise answer a prompt that a later `do` meant to look at.
        promptEntries = []
        dismissed = []
        var idleNote: [String: Any] = [:]
        do {
            guard let request = try JSONSerialization.jsonObject(with: body) as? [String: Any],
                  let operation = request["op"] as? String else {
                throw Failure(message: "request needs an op")
            }
            // A `do` goes on from the screen the previous one ended on; a flow starts clean.
            trail.start(carry: operation == "action" || operation == "actions", now: Date().timeIntervalSinceReferenceDate)
            promptEntries = request["optional_permission_labels"] as? [Any] ?? []
            let skipIdle = (request["wait_for_idle"] as? Bool) == false
            if skipIdle { idleNote["wait_for_idle"] = Quiescence.disable() ? false : "unsupported" }
            defer { if skipIdle { Quiescence.restore() } }
            switch operation {
            case "ping":
                return ["status": "pass", "pong": true]
            case "stop":
                isStopped = true
                return ["status": "pass"]
            case "lock":
                // Only as the runner's last request (`fastDeviceTest lock`): nothing on a locked
                // iPhone can run until someone types its passcode.
                try pressLockButton()
                isStopped = true
                return ["status": "pass"]
            case "flow":
                return runFlow(request["flow"] as? [String: Any] ?? [:])
            case "action":
                let action = request["action"] as? [String: Any] ?? [:]
                if let bundleID = action["app"] as? String { use(bundleID) }
                guard current != nil else { throw Failure(message: "no current app: add \"app\" to the action") }
                let result = try execute(action, index: 1) ?? [:]
                if !Requests.endsWithRead(action) { finalRead() }
                return ["status": "pass", "result": result, "labels": lastLabels].merging(idleNote) { first, _ in first }
            case "actions":
                return runActions(request["actions"] as? [[String: Any]] ?? []).merging(idleNote) { first, _ in first }
            default:
                throw Failure(message: "unknown op '\(operation)'")
            }
        } catch {
            if current != nil { _ = snapshotNodes() }
            return ["status": "fail", "error": message(error), "labels": lastLabels].merging(idleNote) { first, _ in first }
        }
    }

    // Stops at the first failing step, like a flow, but keeps the app state it found.
    func runActions(_ actions: [[String: Any]]) -> [String: Any] {
        var results: [[String: Any]] = []
        for (offset, action) in actions.enumerated() {
            let index = offset + 1
            do {
                if let result = try execute(action, index: index) {
                    results.append(["step": index].merging(result) { first, _ in first })
                }
            } catch {
                if current != nil { _ = snapshotNodes() }
                return ["status": "fail", "step": index, "error": message(error), "results": results, "labels": lastLabels]
            }
        }
        if !Requests.endsWithRead(actions.last ?? [:]) { finalRead() }
        return ["status": "pass", "results": results, "labels": lastLabels]
    }

    func runFlow(_ flow: [String: Any]) -> [String: Any] {
        var results: [[String: Any]] = []
        var step = "launch"
        let setup = flow["setup"] as? [String: Any] ?? [:]
        let previousAppearance = XCUIDevice.shared.appearance
        defer { XCUIDevice.shared.appearance = previousAppearance }
        orientationChanged = false
        defer { if orientationChanged { XCUIDevice.shared.orientation = .portrait } }
        let skipIdle = (setup["wait_for_idle"] as? Bool) == false
        let idleNote: [String: Any] = skipIdle ? ["wait_for_idle": Quiescence.disable() ? false : "unsupported"] : [:]
        defer { if skipIdle { Quiescence.restore() } }
        do {
            let app = flow["app"] as? [String: Any] ?? [:]
            guard let bundleID = app["bundle_id"] as? String else { throw Failure(message: "missing app.bundle_id") }
            promptEntries = setup["optional_permission_labels"] as? [Any] ?? []
            switch setup["appearance"] as? String {
            case "dark": XCUIDevice.shared.appearance = .dark
            case "light": XCUIDevice.shared.appearance = .light
            default: break
            }

            let target = use(bundleID)
            target.launchArguments = setup["launch_arguments"] as? [String] ?? []
            let launches = setup["launch"] as? Bool ?? true
            trail.gesture(["type": launches ? "launch" : "activate"])
            if launches {
                target.launch()
            } else {
                target.activate()
            }
            awaitForeground(target)

            let actions = flow["actions"] as? [[String: Any]] ?? []
            for (offset, action) in actions.enumerated() {
                let index = offset + 1
                step = String(format: "%02d_%@", index, action["type"] as? String ?? "unknown")
                if let result = try execute(action, index: index) {
                    results.append(["step": index].merging(result) { first, _ in first })
                }
            }
            return ["status": "pass", "results": results, "labels": lastLabels].merging(idleNote) { first, _ in first }
        } catch {
            capture("failure")
            if let current { attachText("failure-ui.txt", current.debugDescription) }
            return ["status": "fail", "step": step, "error": message(error), "results": results, "labels": lastLabels]
                .merging(idleNote) { first, _ in first }
        }
    }
}

// MARK: - Actions
private extension FlowRunner {
    // Runs one action; a gesture that succeeded joins the screen map's trail.
    func execute(_ action: [String: Any], index: Int) throws -> [String: Any]? {
        actionIssue = nil
        isRunningAction = true
        defer { isRunningAction = false }
        trail.startAction()
        let result = try perform(action, index: index)
        if let actionIssue { throw Failure(message: "XCTest: \(actionIssue)") }
        let kind = action["type"] as? String ?? ""
        if Trail.gestureKinds.contains(kind), result?["optional_missing"] == nil {
            trail.gesture(action)
        }
        // A guard on the screen the step leads to: a multi-step request stops at the first
        // surprise instead of acting on a screen the agent did not plan for. An optional tap that
        // found nothing changed no screen, so there is nothing to expect.
        if action["expect"] != nil && !(action["expect"] is [String: Any]) {
            throw Failure(message: "step \(index): expect must be a selector object")
        }
        if let expected = action["expect"] as? [String: Any], result?["optional_missing"] == nil {
            do {
                _ = try waitNodes(number(action["expect_timeout"]) ?? 2) { nodes in nodes.contains { self.matches($0, expected) } }
            } catch {
                let shown = (try? JSONSerialization.data(withJSONObject: expected, options: [.sortedKeys]))
                    .flatMap { String(data: $0, encoding: .utf8) } ?? "\(expected)"
                throw Failure(message: "step \(index): expected \(shown) after \(kind); visible labels: \(lastLabels)")
            }
        }
        return result
    }

    func perform(_ action: [String: Any], index: Int) throws -> [String: Any]? {
        guard let kind = action["type"] as? String else { throw Failure(message: "action \(index) has no type") }
        if let bundleID = action["app"] as? String { use(bundleID) }
        guard let app = current else { throw Failure(message: "no current app") }
        let timeout = number(action["timeout"]) ?? 6
        let postDelay = number(action["post_delay"]) ?? 0.15

        switch kind {
        case "activate":
            app.activate()
            awaitForeground(app)
        case "launch":
            if let arguments = action["launch_arguments"] as? [String] { app.launchArguments = arguments }
            app.launch()
            awaitForeground(app)
        case "terminate":
            app.terminate()
        case "wait":
            _ = try waitNodes(timeout) { nodes in nodes.contains { self.matches($0, action) } }
        case "wait_absent":
            do {
                _ = try waitNodes(timeout) { nodes in !nodes.contains { self.matches($0, action) } }
            } catch {
                throw Failure(message: "'\(action["label"] ?? "selector")' is still on screen after \(timeout)s; visible labels: \(lastLabels)")
            }
        case "assert_absent":
            let deadline = Date().addingTimeInterval(number(action["seconds"]) ?? 3)
            repeat {
                let nodes = try waitNodes(timeout) { _ in true }
                if nodes.contains(where: { matches($0, action) }) {
                    throw Failure(message: "'\(action["label"] ?? "selector")' appeared while asserting absence; visible labels: \(lastLabels)")
                }
                Thread.sleep(forTimeInterval: 0.3)
            } while Date() < deadline
        case "long_press" where action["x"] != nil && action["y"] != nil:
            press(number(action["x"]) ?? 0, number(action["y"]) ?? 0, number(action["duration"]) ?? 1, postDelay)
        case "double_tap" where action["x"] != nil && action["y"] != nil:
            doubleTap(number(action["x"]) ?? 0, number(action["y"]) ?? 0, postDelay)
        case "tap_label", "long_press", "double_tap":
            guard action["label"] != nil || action["label_contains"] != nil || action["id"] != nil else {
                throw Failure(message: kind == "tap_label"
                    ? "missing 'label' or 'id' in action \(index)"
                    : "\(kind) needs 'label', 'id' or x and y in action \(index)")
            }
            var selector: [String: Any] = [:]
            for key in FlowRunner.selectorKeys { selector[key] = action[key] }
            if selector["element_type"] == nil { selector["element_type"] = "Button" }
            let explicitType = action["element_type"] != nil
            let node: Node?
            if action["optional"] as? Bool ?? false {
                node = resolveTapTarget(snapshotNodes(), selector, explicitType: explicitType)
            } else {
                let nodes = try waitNodes(timeout) { nodes in
                    self.resolveTapTarget(nodes, selector, explicitType: explicitType) != nil
                }
                node = resolveTapTarget(nodes, selector, explicitType: explicitType)
            }
            if let found = node {
                let target = try settled(found) { self.resolveTapTarget($0, selector, explicitType: explicitType) }
                if kind == "long_press" {
                    press(target.frame.midX, target.frame.midY, number(action["duration"]) ?? 1, postDelay)
                } else if kind == "double_tap" {
                    doubleTap(target.frame.midX, target.frame.midY, postDelay)
                } else {
                    tap(target.frame.midX, target.frame.midY, postDelay)
                }
                if let expected = selector["element_type"] as? String, !typeMatches(target.type, expected) {
                    return ["fallback_type": target.type]
                }
            } else {
                return ["optional_missing": true]
            }
        case "tap_first":
            let nodes = try waitNodes(timeout) { nodes in nodes.contains { self.matches($0, action) } }
            let keys = action["sort"] as? [String] ?? ["y", "x"]
            func first(_ nodes: [Node]) -> Node? { sorted(nodes.filter { matches($0, action) }, by: keys).first }
            guard let found = first(nodes) else {
                throw Failure(message: "no node matched tap_first at action \(index)")
            }
            let node = try settled(found, first)
            tap(node.frame.midX, node.frame.midY, postDelay)
        case "tap_nearest":
            try tapNearest(action, timeout: timeout, postDelay: postDelay)
        case "tap_point":
            tap(number(action["x"]) ?? 0, number(action["y"]) ?? 0, postDelay)
        case "swipe":
            try swipe(action)
            Thread.sleep(forTimeInterval: postDelay)
        case "scroll_to":
            let limit = Int(number(action["max_swipes"]) ?? 6)
            var swipes = 0
            while !snapshotNodes().contains(where: { matches($0, action) }) {
                guard swipes < limit else {
                    throw Failure(message: "'\(action["label"] ?? action["label_contains"] ?? "selector")' not found after \(limit) swipes; visible labels: \(lastLabels)")
                }
                try swipe(["direction": action["direction"] ?? "up"])
                Thread.sleep(forTimeInterval: 0.4)
                swipes += 1
            }
        case "type_text":
            guard let text = action["text"] as? String else { throw Failure(message: "type_text needs text") }
            try requireKeyboardFocus(app, kind)
            app.typeText(text)
            Thread.sleep(forTimeInterval: postDelay)
        case "dismiss_keyboard":
            try dismissKeyboard(app, button: action["button"] as? String)
        case "pull_to_refresh":
            if screenBounds.isInfinite { _ = snapshotNodes() }
            let area = hasSelector(action) ? try resolveNode(action, timeout: timeout).frame : screenBounds
            guard !area.isInfinite else { throw Failure(message: "pull_to_refresh has no screen size: the current app is not running") }
            let x = Double(area.midX)
            trail.touch()
            point(x, Double(area.minY + area.height * 0.25)).press(
                forDuration: 0.05, thenDragTo: point(x, Double(area.minY + area.height * 0.75)),
                withVelocity: .slow, thenHoldForDuration: 0.3
            )
            Thread.sleep(forTimeInterval: max(postDelay, 1.0))
        case "set_switch":
            guard let on = action["on"] as? Bool else { throw Failure(message: "set_switch needs on: true or false in action \(index)") }
            return try setSwitch(action, on: on, timeout: timeout, postDelay: postDelay)
        case "tap_alert":
            guard let label = action["button"] as? String else { throw Failure(message: "tap_alert needs button in action \(index)") }
            return try tapAlert(label, app: app, systemOnly: action["system"] as? Bool ?? false,
                                optional: action["optional"] as? Bool ?? false, timeout: timeout, postDelay: postDelay)
        case "assert_value":
            let attribute = action["attribute"] as? String ?? "value"
            guard ["value", "label"].contains(attribute) else { throw Failure(message: "attribute must be value or label") }
            _ = try checkText("", action)  // a step without equals/contains/… fails here, before any polling
            var complaint = "no node matched the selector"
            do {
                _ = try waitNodes(timeout) { nodes in
                    let read: (Node) -> String = { attribute == "label" ? $0.label : $0.value }
                    let candidates = self.sorted(nodes.filter { self.matches($0, action) }, by: ["y", "x"])
                    // A row's container often carries the control's id (or label) without its value:
                    // read the top-left match that has the attribute at all.
                    guard let node = candidates.first(where: { !read($0).isEmpty }) ?? candidates.first else {
                        complaint = "no node matched the selector"
                        return false
                    }
                    // Not `try?`: it flattens a throw and a pass into the same nil.
                    do {
                        guard let failure = try self.checkText(read(node), action) else {
                            return true
                        }
                        complaint = "\(attribute) of the \(node.type) \(failure)"
                    } catch {
                        complaint = self.message(error)
                    }
                    return false
                }
            } catch {
                throw Failure(message: "\(complaint) after \(timeout)s; visible labels: \(lastLabels)")
            }
        case "assert_count":
            guard action["count"] != nil || action["min"] != nil || action["max"] != nil else {
                throw Failure(message: "assert_count needs count, min or max")
            }
            var seen = 0
            do {
                _ = try waitNodes(timeout) { nodes in
                    seen = self.distinctFrames(nodes.filter { self.matches($0, action) }).count
                    return self.countFailure(seen, action) == nil
                }
            } catch {
                throw Failure(message: "\(seen) nodes matched, \(countFailure(seen, action) ?? "") after \(timeout)s; visible labels: \(lastLabels)")
            }
            return ["count": seen]
        case "pause":
            Thread.sleep(forTimeInterval: number(action["seconds"]) ?? 1)
        case "screenshot":
            clearPrompts()
            let name = action["name"] as? String ?? String(format: "step-%02d", index)
            guard action["stable"] as? Bool != false else {
                capture(name)
                return ["screenshot": name]
            }
            let (shot, stable) = stableScreenshot(
                timeout: number(action["stable_timeout"]) ?? 3,
                ratio: number(action["stable_ratio"]) ?? 0.005
            )
            capture(name, shot)
            return ["screenshot": name, "stable": stable]
        case "dump":
            let nodes = try waitNodes(timeout) { _ in true }
            let name = action["name"] as? String ?? String(format: "step-%02d", index)
            attachText("\(name)-ui.txt", app.debugDescription)
            return ["name": name, "labels": visibleLabels(nodes), "nodes": visibleNodes(nodes)]
        case "drag":
            let start = try pointOf(action["from"], timeout: timeout, index: index)
            let end = try pointOf(action["to"], timeout: timeout, index: index)
            trail.touch()
            // Resting on the start point first is what a drag and drop or a reorder needs.
            point(start.x, start.y).press(forDuration: max(number(action["hold"]) ?? 0.6, 0.05),
                                          thenDragTo: point(end.x, end.y), withVelocity: velocity(from: start, to: end, action),
                                          thenHoldForDuration: 0.2)
            Thread.sleep(forTimeInterval: postDelay)
        case "edge_swipe":
            if screenBounds.isInfinite { _ = snapshotNodes() }
            guard !screenBounds.isInfinite else {
                throw Failure(message: "edge_swipe has no screen size: the current app is not running")
            }
            guard let points = Gestures.edgePoints(edge: action["edge"] as? String ?? "", at: number(action["at"]) ?? 0.5,
                                                   distance: number(action["distance"]) ?? 0.8, screen: screenBounds) else {
                throw Failure(message: "edge must be left, right, top or bottom in action \(index)")
            }
            trail.touch()
            point(points.start.x, points.start.y).press(
                forDuration: 0.05, thenDragTo: point(points.end.x, points.end.y),
                withVelocity: velocity(from: (Double(points.start.x), Double(points.start.y)),
                                       to: (Double(points.end.x), Double(points.end.y)), action),
                thenHoldForDuration: 0)
            Thread.sleep(forTimeInterval: postDelay)
        case "pinch":
            let target = try element(action, in: app, timeout: timeout)
            let scale = number(action["scale"]) ?? 2
            guard scale > 0 else { throw Failure(message: "pinch scale must be above 0 in action \(index)") }
            trail.touch()
            target.pinch(withScale: scale, velocity: scale >= 1 ? 1 : -1)
            Thread.sleep(forTimeInterval: postDelay)
        case "rotate":
            let target = try element(action, in: app, timeout: timeout)
            let radians = number(action["radians"]) ?? Double.pi / 2
            trail.touch()
            target.rotate(radians, withVelocity: radians >= 0 ? 1 : -1)
            Thread.sleep(forTimeInterval: postDelay)
        case "orientation":
            let orientations: [String: UIDeviceOrientation] = [
                "portrait": .portrait, "portrait_upside_down": .portraitUpsideDown,
                "landscape_left": .landscapeLeft, "landscape_right": .landscapeRight,
            ]
            guard let orientation = orientations[action["value"] as? String ?? ""] else {
                throw Failure(message: "orientation value must be one of \(orientations.keys.sorted()) in action \(index)")
            }
            XCUIDevice.shared.orientation = orientation
            orientationChanged = orientation != .portrait
            Thread.sleep(forTimeInterval: max(postDelay, 0.5))
        case "press_key":
            guard let key = FlowRunner.namedKeys[action["key"] as? String ?? ""] else {
                throw Failure(message: "key must be one of \(FlowRunner.namedKeys.keys.sorted()) in action \(index)")
            }
            let count = max(Int(number(action["count"]) ?? 1), 1)
            try requireKeyboardFocus(app, kind)
            if [XCUIKeyboardKey.delete, .return, .space].contains(key) {
                app.typeText(String(repeating: key.rawValue, count: count))
            } else {
                // typeText writes an arrow, tab or escape into the field as a character ("13←2").
                for _ in 0..<count { app.typeKey(key.rawValue, modifierFlags: []) }
            }
            Thread.sleep(forTimeInterval: postDelay)
        case "press_button":
            switch action["button"] as? String {
            case "siri":
                guard let text = action["text"] as? String, !text.isEmpty else {
                    throw Failure(message: "press_button siri on an iPhone needs `text`: XCUITest starts Siri with that request")
                }
                XCUIDevice.shared.siriService.activate(voiceRecognitionText: text)
            case "home":
                // XCUIDevice.press(.home) does nothing on iOS 26.7.1 (iPhone 11); bringing
                // SpringBoard forward goes home, and the next steps read the Home Screen.
                use("com.apple.springboard").activate()
            case "volume_up": XCUIDevice.shared.press(.volumeUp)
            case "volume_down": XCUIDevice.shared.press(.volumeDown)
            case "lock":
                throw Failure(message: "press_button lock is refused inside a flow on an iPhone: the next steps would wait for its passcode; lock it at the end with `fastDeviceTest lock`")
            default:
                throw Failure(message: "button must be home, siri, volume_up, volume_down or lock in action \(index)")
            }
            Thread.sleep(forTimeInterval: max(postDelay, 0.5))
        case "set_slider":
            guard action["id"] != nil || action["label"] != nil || action["label_contains"] != nil else {
                throw Failure(message: "set_slider needs 'id' or 'label' in action \(index)")
            }
            let slider = try element(action, in: app, type: .slider, timeout: timeout)
            trail.touch()
            slider.adjust(toNormalizedSliderPosition: CGFloat(number(action["value"]) ?? 0))
            Thread.sleep(forTimeInterval: postDelay)
        case "set_picker":
            guard let value = action["value"] as? String else { throw Failure(message: "set_picker needs a value in action \(index)") }
            let wheel = app.pickerWheels.element(boundBy: Int(number(action["index"]) ?? 0))
            guard wheel.waitForExistence(timeout: timeout) else {
                throw Failure(message: "no picker wheel \(Int(number(action["index"]) ?? 0)) on screen; visible labels: \(lastLabels)")
            }
            trail.touch()
            // A value the wheel does not have is a UI Automation failure that ends the whole test,
            // and the persistent runner with it (XCTExpectFailure does not stop that); the launcher
            // then reports XCTest's message, which lists the wheel's values.
            wheel.adjust(toPickerWheelValue: value)
            Thread.sleep(forTimeInterval: postDelay)
            let shown = wheel.value as? String ?? ""
            guard shown == value || shown.hasPrefix(value + " ") else {
                throw Failure(message: "set_picker could not turn wheel \(Int(number(action["index"]) ?? 0)) to '\(value)'; it shows '\(shown)' (use the wheel's own text, such as '07')")
            }
        case "open_url":
            guard let text = action["url"] as? String, let url = URL(string: text) else {
                throw Failure(message: "open_url needs a url in action \(index)")
            }
            XCUIDevice.shared.system.open(url)
            Thread.sleep(forTimeInterval: max(postDelay, 1))
        case "two_finger_tap":
            // XCUITest cannot place two fingers on the whole app ("unable to compute coordinates"),
            // and that UI Automation failure ends the runner, so a target is required.
            guard ["id", "label", "label_contains", "element_type"].contains(where: { action[$0] != nil }) else {
                throw Failure(message: "two_finger_tap needs a selector or element_type (Map, ScrollView, WebView, Image) in action \(index)")
            }
            let target = try element(action, in: app, timeout: timeout)
            trail.touch()
            target.tap(withNumberOfTaps: max(Int(number(action["taps"]) ?? 1), 1), numberOfTouches: 2)
            Thread.sleep(forTimeInterval: postDelay)
        case "two_finger_drag":
            // Two fingers side by side, `dy` (fraction of the target's height, negative = up, default
            // -0.25) apart from start to end; `spread` is the gap between the fingers in points; `at` (0-1, default
            // 0.5) is how far down the target the travel is centred: a sheet under the fingers drags instead.
            let hasTarget = ["id", "label", "label_contains", "element_type"].contains(where: { action[$0] != nil })
            let frame = hasTarget ? try element(action, in: app, timeout: timeout).frame : app.frame
            let dy = Double(frame.height) * (number(action["dy"]) ?? -0.25)
            let spread = number(action["spread"]) ?? 60
            let midX = Double(frame.midX), midY = Double(frame.minY) + Double(frame.height) * (number(action["at"]) ?? 0.5)
            trail.touch()
            if let reason = MultiTouch.drag(
                fingers: [-spread / 2, spread / 2].map {
                    (from: CGPoint(x: midX + $0, y: midY - dy / 2), to: CGPoint(x: midX + $0, y: midY + dy / 2))
                },
                duration: number(action["duration"]) ?? 0.6) {
                throw Failure(message: "two_finger_drag is not available: \(reason) in action \(index)")
            }
            Thread.sleep(forTimeInterval: postDelay)
        case "key_combo":
            let flags: [String: XCUIElement.KeyModifierFlags] = [
                "command": .command, "shift": .shift, "option": .option, "control": .control,
            ]
            let names = action["modifiers"] as? [String] ?? []
            guard !names.isEmpty, names.allSatisfy({ flags[$0] != nil }) else {
                throw Failure(message: "modifiers must be a list of \(flags.keys.sorted()) in action \(index)")
            }
            let name = action["key"] as? String ?? ""
            guard let key = FlowRunner.namedKeys[name]?.rawValue ?? (name.count == 1 ? name : nil) else {
                throw Failure(message: "key must be one character or one of \(FlowRunner.namedKeys.keys.sorted()) in action \(index)")
            }
            try requireKeyboardFocus(app, kind)
            app.typeKey(key, modifierFlags: XCUIElement.KeyModifierFlags(names.compactMap { flags[$0] }))
            Thread.sleep(forTimeInterval: postDelay)
        case "clear_text":
            let field = try textField(action, in: app, timeout: timeout)
            trail.touch()
            // Near the trailing edge, then select all and delete: the caret and the text's length
            // stop mattering. A secure field may refuse select-all; then delete what it holds.
            field.coordinate(withNormalizedOffset: CGVector(dx: 0.85, dy: 0.5)).tap()
            try requireKeyboardFocus(app, kind)
            func left() -> String {
                let text = field.value as? String ?? ""
                return text == field.placeholderValue ? "" : text
            }
            if !left().isEmpty {
                app.typeKey("a", modifierFlags: .command)
                app.typeText(XCUIKeyboardKey.delete.rawValue)
            }
            if !left().isEmpty { app.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: left().count)) }
            Thread.sleep(forTimeInterval: postDelay)
            guard left().isEmpty else { throw Failure(message: "clear_text left '\(left())' in the field") }
        case "shake":
            throw Failure(message: "shake has no XCUITest API on an iPhone; use the Simulator")
        default:
            throw Failure(message: "unsupported action type '\(kind)' on device")
        }
        return nil
    }
}

// MARK: - Gestures
private extension FlowRunner {
    // Port of the Simulator engine's tap_nearest: pick the target closest to one anchor
    // along `axis`, so a row's trailing button (an ⓘ, a Delete) is found by the row's text.
    func tapNearest(_ action: [String: Any], timeout: Double, postDelay: Double) throws {
        guard let target = action["target"] as? [String: Any], let anchor = action["anchor"] as? [String: Any] else {
            throw Failure(message: "tap_nearest needs target and anchor selectors")
        }
        let axis = action["axis"] as? String ?? "y"
        func nearest(_ nodes: [Node]) throws -> Node {
            let targets = nodes.filter { matches($0, target) }
            // XCUITest nests a text in containers with the same label and frame; those are one anchor.
            let anchorValues = Set(nodes.filter { matches($0, anchor) }.map { value($0, axis).rounded() })
            guard anchorValues.count == 1, let anchorValue = anchorValues.first else {
                throw Failure(message: "tap_nearest requires one anchor and at least one target; got \(anchorValues.count)/\(targets.count)")
            }
            guard let nearest = targets.min(by: { abs(value($0, axis) - anchorValue) < abs(value($1, axis) - anchorValue) }) else {
                throw Failure(message: "tap_nearest found no target")
            }
            return nearest
        }
        func resolveAndTap() throws {
            let nodes = try waitNodes(timeout) { nodes in
                nodes.contains { self.matches($0, target) } && nodes.contains { self.matches($0, anchor) }
            }
            let node = try settled(try nearest(nodes)) { try? nearest($0) }
            tap(node.frame.midX, node.frame.midY, postDelay)
        }

        try resolveAndTap()
        guard let success = action["success"] as? [String: Any] else { return }
        let dismissals = action["dismiss"] as? [[String: Any]]
            ?? (action["dismiss"] as? [String: Any]).map { [$0] } ?? []
        let nodes = try waitNodes(timeout) { nodes in
            nodes.contains { self.matches($0, success) }
                || dismissals.contains { selector in nodes.contains { self.matches($0, selector) } }
        }
        if nodes.contains(where: { matches($0, success) }) { return }
        let blockers = dismissals.flatMap { selector in nodes.filter { matches($0, selector) } }
        guard let blocker = sorted(blockers, by: ["y", "x"]).first else {
            throw Failure(message: "tap_nearest reached neither success nor dismiss selector")
        }
        tap(blocker.frame.midX, blocker.frame.midY, postDelay)
        try resolveAndTap()
        _ = try waitNodes(timeout) { nodes in nodes.contains { self.matches($0, success) } }
    }

    // `from`/`to` points, or a `direction` the finger moves in ("up" scrolls content down).
    func swipe(_ action: [String: Any]) throws {
        if let from = action["from"] as? [Any], let to = action["to"] as? [Any],
           let fromX = number(from.first), let fromY = number(from.last),
           let toX = number(to.first), let toY = number(to.last) {
            trail.touch()
            point(fromX, fromY).press(forDuration: 0.05, thenDragTo: point(toX, toY))
            return
        }
        if screenBounds.isInfinite { _ = snapshotNodes() }
        let bounds = screenBounds
        let x = Double(bounds.midX), y = Double(bounds.midY)
        let top = Double(bounds.minY + bounds.height * 0.3), bottom = Double(bounds.minY + bounds.height * 0.7)
        let left = Double(bounds.minX + bounds.width * 0.2), right = Double(bounds.minX + bounds.width * 0.8)
        let points: (Double, Double, Double, Double)
        switch action["direction"] as? String {
        case "up": points = (x, bottom, x, top)
        case "down": points = (x, top, x, bottom)
        case "left": points = (right, y, left, y)
        case "right": points = (left, y, right, y)
        default: throw Failure(message: "swipe needs from/to [x, y] or direction up|down|left|right")
        }
        trail.touch()
        point(points.0, points.1).press(forDuration: 0.05, thenDragTo: point(points.2, points.3))
    }
}

// MARK: - Controls and checks
private extension FlowRunner {
    func hasSelector(_ action: [String: Any]) -> Bool {
        FlowRunner.selectorKeys.contains { action[$0] != nil }
    }

    // The top-left node matching the action's selector keys (and `accept`), waiting for it like `wait`.
    func resolveNode(_ selector: [String: Any], timeout: Double, accept: @escaping (Node) -> Bool = { _ in true }) throws -> Node {
        let found: (Node) -> Bool = { self.matches($0, selector) && accept($0) }
        let nodes: [Node]
        do {
            nodes = try waitNodes(timeout) { nodes in nodes.contains(where: found) }
        } catch {
            throw Failure(message: "no node matched the selector after \(timeout)s; visible labels: \(lastLabels)")
        }
        guard let node = sorted(nodes.filter(found), by: ["y", "x"]).first else {
            throw Failure(message: "no node matched the selector; visible labels: \(lastLabels)")
        }
        return node
    }

    func dismissKeyboard(_ app: XCUIApplication, button: String?) throws {
        guard app.keyboards.firstMatch.exists else { return }
        if let button {
            let target = [app.keyboards.buttons[button], app.toolbars.buttons[button], app.buttons[button]]
                .first { $0.exists }
            guard let target else { throw Failure(message: "no '\(button)' button on screen; visible labels: \(lastLabels)") }
            trail.touch()
            target.tap()
        } else {
            // The first Return can be spent accepting an autocorrect suggestion.
            for _ in 0..<3 {
                app.typeText("\n")
                if keyboardGone(app, within: 0.6) { break }
            }
        }
        // The keyboard is still sliding down for ~0.3 s after the tap that closed it.
        if !keyboardGone(app, within: 2) {
            throw Failure(message: "the keyboard is still up; name the field's dismiss button with \"button\"")
        }
    }

    func keyboardGone(_ app: XCUIApplication, within seconds: Double) -> Bool {
        let deadline = Date().addingTimeInterval(seconds)
        while app.keyboards.firstMatch.exists {
            guard Date() < deadline else { return false }
            Thread.sleep(forTimeInterval: 0.1)
        }
        return true
    }

    // Taps only when the switch is not already in the wanted state, then waits for the change.
    func setSwitch(_ action: [String: Any], on: Bool, timeout: Double, postDelay: Double) throws -> [String: Any] {
        let isSwitch: (Node) -> Bool = { ["Switch", "CheckBox"].contains($0.type) && ["0", "1"].contains($0.value) }
        let node = try resolveNode(action, timeout: timeout, accept: isSwitch)
        let wanted = on ? "1" : "0"
        guard node.value != wanted else { return ["toggled": false] }
        // A SwiftUI Toggle spans its row and flips only when the switch itself is hit.
        let x = node.frame.width > 120 ? node.frame.maxX - 36 : node.frame.midX
        tap(Double(x), Double(node.frame.midY), postDelay)
        let origin = node.frame.origin
        do {
            _ = try waitNodes(timeout) { nodes in
                nodes.contains { isSwitch($0) && $0.value == wanted && abs($0.frame.minX - origin.x) < 4 && abs($0.frame.minY - origin.y) < 4 }
            }
        } catch {
            throw Failure(message: "switch did not turn \(on ? "on" : "off") after \(timeout)s; visible labels: \(lastLabels)")
        }
        return ["toggled": true]
    }

    // System alerts belong to SpringBoard, an app's own alerts to the app; `systemOnly` skips the app's.
    func tapAlert(_ label: String, app: XCUIApplication, systemOnly: Bool, optional: Bool,
                  timeout: Double, postDelay: Double) throws -> [String: Any] {
        let owners = systemOnly ? [springboard] : [springboard, app]
        let deadline = Date().addingTimeInterval(timeout)
        repeat {
            for owner in owners {
                let alert = alertContainer(owner)
                let button = alert.buttons[label]
                if alert.exists, button.exists {
                    let title = alert.label
                    trail.touch()
                    button.tap()
                    Thread.sleep(forTimeInterval: postDelay)
                    return ["alert": title, "system": owner === springboard]
                }
            }
            Thread.sleep(forTimeInterval: 0.2)
        } while Date() < deadline
        // A permission prompt iOS chose not to show (already answered) is no failure of the flow.
        if optional { return ["skipped": true] }
        _ = snapshotNodes()
        throw Failure(message: "no alert with a '\(label)' button after \(timeout)s; visible labels: \(lastLabels)")
    }

    // Why `actual` fails equals/not_equals/contains/matches; nil when it passes.
    func checkText(_ actual: String, _ action: [String: Any]) throws -> String? {
        let checks = ["equals", "not_equals", "contains", "matches"].filter { action[$0] != nil }
        guard !checks.isEmpty else { throw Failure(message: "needs equals, not_equals, contains or matches") }
        for key in checks {
            let expected = "\(action[key]!)"
            switch key {
            case "equals" where actual != expected: return "is '\(actual)', expected '\(expected)'"
            case "not_equals" where actual == expected: return "is still '\(actual)'"
            case "contains" where !actual.contains(expected): return "is '\(actual)', expected it to contain '\(expected)'"
            case "matches" where actual.range(of: expected, options: .regularExpression) == nil:
                return "is '\(actual)', expected it to match /\(expected)/"
            default: continue
            }
        }
        return nil
    }

    func countFailure(_ count: Int, _ action: [String: Any]) -> String? {
        if let wanted = number(action["count"]), count != Int(wanted) { return "expected \(Int(wanted))" }
        if let minimum = number(action["min"]), count < Int(minimum) { return "expected at least \(Int(minimum))" }
        if let maximum = number(action["max"]), count > Int(maximum) { return "expected at most \(Int(maximum))" }
        return nil
    }

    // XCUITest nests a text in containers with the same frame; those count once.
    func distinctFrames(_ nodes: [Node]) -> [Node] {
        var seen = Set<String>()
        return nodes.filter { node in
            seen.insert("\(node.frame.minX.rounded()),\(node.frame.minY.rounded()),\(node.frame.width.rounded()),\(node.frame.height.rounded())").inserted
        }
    }
}

// MARK: - Hierarchy
private extension FlowRunner {
    @discardableResult
    func use(_ bundleID: String) -> XCUIApplication {
        let app = application(bundleID)
        current = app
        currentBundleID = bundleID
        return app
    }

    func application(_ bundleID: String) -> XCUIApplication {
        if let app = apps[bundleID] { return app }
        let app = XCUIApplication(bundleIdentifier: bundleID)
        apps[bundleID] = app
        return app
    }

    func snapshotNodes() -> [Node] {
        guard let root = try? current?.snapshot() else { return [] }
        screenBounds = root.frame
        var nodes: [Node] = []
        var bars: [(frame: CGRect, title: String)] = []
        func walk(_ element: XCUIElementSnapshot, cell: CGRect?) {
            if element.elementType == .navigationBar {
                bars.append((element.frame, element.identifier.isEmpty ? element.label : element.identifier))
            }
            let value = element.value.map { ($0 as? String) ?? String(describing: $0) } ?? ""
            nodes.append(Node(label: element.label, type: typeName(element.elementType), frame: element.frame,
                              cell: cell, identifier: element.identifier, value: value, enabled: element.isEnabled))
            let childCell = element.elementType == .cell ? element.frame : cell
            element.children.forEach { walk($0, cell: childCell) }
        }
        walk(root, cell: nil)
        lastLabels = visibleLabels(nodes)
        let title = Trail.title(bars.filter { screenBounds.contains(CGPoint(x: $0.frame.midX, y: $0.frame.midY)) }.map(\.title))
        trail.read(app: currentBundleID, title: title,
                   nodes: nodes.filter(isOnScreen).map { TrailNode(label: $0.label, type: $0.type, id: $0.identifier) },
                   now: Date().timeIntervalSinceReferenceDate)
        return nodes
    }

    func waitNodes(_ timeout: Double, until predicate: ([Node]) -> Bool) throws -> [Node] {
        let deadline = Date().addingTimeInterval(timeout)
        var nodes: [Node] = []
        repeat {
            if dismissPrompt() { continue }
            nodes = snapshotNodes()
            if predicate(nodes) { return nodes }
            Thread.sleep(forTimeInterval: 0.2)
        } while Date() < deadline
        throw Failure(message: "timed out waiting for UI selector; visible labels: \(lastLabels)")
    }

    // Permission alerts on a device belong to SpringBoard, not the app under test. One
    // snapshot gives the alert's title and buttons; PromptRules picks the button.
    func dismissPrompt() -> Bool {
        guard !promptEntries.isEmpty else { return false }
        for owner in [springboard] + [current].compactMap({ $0 }) {
            let alert = alertContainer(owner)
            guard alert.exists, let snapshot = try? alert.snapshot() else { continue }
            let parts = descendants(of: snapshot)
            let title = snapshot.label.isEmpty
                ? parts.first { $0.elementType == .staticText }?.label ?? ""
                : snapshot.label
            let buttons = parts.filter { $0.elementType == .button }.map(\.label)
            guard let choice = PromptRules.choice(title: title, buttons: buttons, entries: promptEntries) else { continue }
            alert.buttons[choice].firstMatch.tap()
            // An alert still animating in can ignore the tap and get it again: report it once.
            let entry = ["title": title, "tapped": choice]
            if dismissed.last != entry { dismissed.append(entry) }
            Thread.sleep(forTimeInterval: 0.3)
            return true
        }
        return false
    }

    // iOS 27 reports a UIAlertController as a sheet (iOS 26 and earlier: an alert); one query
    // covers both, since this runs on every poll of a wait.
    func alertContainer(_ owner: XCUIApplication) -> XCUIElement {
        owner.descendants(matching: .any).matching(NSPredicate(
            format: "elementType == %lu OR elementType == %lu",
            XCUIElement.ElementType.alert.rawValue, XCUIElement.ElementType.sheet.rawValue
        )).firstMatch
    }

    func descendants(of element: XCUIElementSnapshot) -> [XCUIElementSnapshot] {
        element.children.flatMap { [$0] + descendants(of: $0) }
    }

    // With the idle wait off a target can still be sliding in (a push, a sheet): tap it once
    // two snapshots agree on its frame, or at its latest frame after 1.5 s.
    func settled(_ node: Node, _ resolve: ([Node]) -> Node?) throws -> Node {
        guard Quiescence.isActive else { return node }
        let last = Settle.stable(first: Optional(node), timeout: 1.5, read: { resolve(self.snapshotNodes()) },
                                 same: { lhs, rhs in
                                     guard let lhs, let rhs else { return false }
                                     return self.frameKey(lhs) == self.frameKey(rhs)
                                 })
        guard let last else {
            throw Failure(message: "the target left the screen while it settled; visible labels: \(lastLabels)")
        }
        return last
    }

    // With the idle wait off `launch()` and `activate()` return before the app has drawn its
    // screen: wait until it is frontmost and its layout stops moving. Only frames count, so a
    // ticking clock or a counter does not keep it waiting.
    func awaitForeground(_ app: XCUIApplication) {
        guard Quiescence.isActive else { return }
        _ = app.wait(for: .runningForeground, timeout: 10)
        _ = Settle.stable(first: layout(snapshotNodes()), timeout: 2, read: { self.layout(self.snapshotNodes()) },
                          same: { !$0.isEmpty && $0 == $1 })
    }

    // The read a request ends on: its labels and the screen map's destination. With the idle
    // wait off it would land mid-animation (a push still sliding in shows both screens), so it
    // waits for the layout to stop moving, as a launch does.
    func finalRead() {
        guard Quiescence.isActive else {
            _ = snapshotNodes()
            return
        }
        _ = Settle.stable(first: layout(snapshotNodes()), timeout: 1.5, read: { self.layout(self.snapshotNodes()) },
                          same: { !$0.isEmpty && $0 == $1 })
    }

    func layout(_ nodes: [Node]) -> [String] {
        nodes.map { "\($0.type) \(frameKey($0))" }
    }

    func frameKey(_ node: Node) -> String {
        "\(node.frame.minX.rounded()),\(node.frame.minY.rounded()),\(node.frame.width.rounded()),\(node.frame.height.rounded())"
    }

    // A screenshot reads no tree, so a prompt raised after the flow's last wait (during a
    // `pause`) would end up in the image; the 8 s cap matches the Simulator engine.
    func clearPrompts() {
        let deadline = Date().addingTimeInterval(8)
        while dismissPrompt(), Date() < deadline {}
    }

    // Port of the Simulator engine's resolve_tap_target: the first full match, else — when the
    // flow did not pin element_type — the one node of any type with that label.
    func resolveTapTarget(_ nodes: [Node], _ selector: [String: Any], explicitType: Bool) -> Node? {
        if let node = sorted(nodes.filter { matches($0, selector) }, by: ["y", "x"]).first { return node }
        if explicitType { return nil }
        var loose = selector
        loose["element_type"] = nil
        // XCUITest nests a text in containers with the same label and frame; those are one
        // target, and the innermost (last in walk order) is the one to tap. The application
        // and window roots carry the app's name and are never a target.
        let candidates = nodes.filter { matches($0, loose) && !["Application", "Window"].contains($0.type) }
        let frames = Set(candidates.map(frameKey))
        guard frames.count == 1 else { return nil }
        return candidates.last
    }

    func matches(_ node: Node, _ selector: [String: Any]) -> Bool {
        if let label = selector["label"] as? String, node.label != label { return false }
        if let fragment = selector["label_contains"] as? String, !node.label.contains(fragment) { return false }
        if let identifier = selector["id"] as? String, node.identifier != identifier { return false }
        if let value = selector["value"], node.value != "\(value)" { return false }
        if selector["label_is_empty"] as? Bool ?? false, !node.label.isEmpty { return false }
        if let enabled = selector["enabled"] as? Bool, node.enabled != enabled { return false }
        if let type = selector["element_type"] as? String, !typeMatches(node.type, type) { return false }
        let limits = selector["frame"] as? [String: Any] ?? [:]
        let values: [String: Double] = [
            "x": node.frame.minX, "y": node.frame.minY,
            "width": node.frame.width, "height": node.frame.height,
        ]
        for (name, actual) in values {
            if let minimum = number(limits["min_\(name)"]), actual < minimum { return false }
            if let maximum = number(limits["max_\(name)"]), actual > maximum { return false }
        }
        return node.frame.width > 0 && node.frame.height > 0 && isOnScreen(node)
    }

    // SwiftUI keeps hidden tabs and off-screen pages in the tree; only a node whose centre is
    // on screen can receive the tap. Text in a list row that is scrolled away reports a made-up
    // frame near the top of the list, so it also has to sit inside its own cell.
    func isOnScreen(_ node: Node) -> Bool {
        let centre = CGPoint(x: node.frame.midX, y: node.frame.midY)
        guard screenBounds.contains(centre) else { return false }
        return node.cell.map { $0.insetBy(dx: -8, dy: -8).contains(centre) } ?? true
    }

    // AXe names (Simulator flows) mapped onto XCUIElement types.
    func typeMatches(_ actual: String, _ expected: String) -> Bool {
        switch expected {
        case "RadioButton": return ["Button", "RadioButton"].contains(actual)
        case "Heading": return ["StaticText", "Other"].contains(actual)
        case "TextField": return ["TextField", "SecureTextField", "TextView"].contains(actual)
        default: return actual == expected
        }
    }

    func typeName(_ type: XCUIElement.ElementType) -> String {
        switch type {
        case .button: return "Button"
        case .staticText: return "StaticText"
        case .textField: return "TextField"
        case .secureTextField: return "SecureTextField"
        case .textView: return "TextView"
        case .image: return "Image"
        case .cell: return "Cell"
        case .switch: return "Switch"
        case .radioButton: return "RadioButton"
        case .link: return "Link"
        case .tab: return "Tab"
        case .alert: return "Alert"
        case .sheet: return "Sheet"
        case .application: return "Application"
        case .window: return "Window"
        default: return "Other"
        }
    }

    func visibleLabels(_ nodes: [Node], limit: Int = 40) -> [String] {
        var seen = Set<String>()
        var labels: [String] = []
        for node in sorted(nodes, by: ["y", "x"])
        where !node.label.isEmpty && isOnScreen(node) {
            if seen.insert(node.label).inserted { labels.append(node.label) }
            if labels.count == limit { break }
        }
        return labels
    }

    // Frames use the selector keys (x, y, width, height), so a dump feeds `frame` bounds,
    // `tap_point` and `tap_nearest` directly.
    func visibleNodes(_ nodes: [Node], limit: Int = 200) -> [[String: Any]] {
        sorted(nodes, by: ["y", "x"])
            .filter { node in
                (!node.label.isEmpty || node.type != "Other") && node.frame.width > 0 && node.frame.height > 0
                    && isOnScreen(node)
            }
            .prefix(limit)
            .map { node in
                var entry: [String: Any] = [
                    "label": node.label, "type": node.type,
                    "frame": [
                        "x": Double(node.frame.minX.rounded()), "y": Double(node.frame.minY.rounded()),
                        "width": Double(node.frame.width.rounded()), "height": Double(node.frame.height.rounded()),
                    ],
                ]
                if !node.identifier.isEmpty { entry["id"] = node.identifier }
                if !node.value.isEmpty { entry["value"] = node.value }
                if !node.enabled { entry["enabled"] = false }
                return entry
            }
    }

    func sorted(_ nodes: [Node], by keys: [String]) -> [Node] {
        nodes.sorted { lhs, rhs in
            for key in keys {
                let left = value(lhs, key)
                let right = value(rhs, key)
                if left != right { return left < right }
            }
            return false
        }
    }

    func value(_ node: Node, _ key: String) -> Double {
        switch key {
        case "x": return node.frame.minX
        case "width": return node.frame.width
        case "height": return node.frame.height
        default: return node.frame.minY
        }
    }
}

// MARK: - Input and Output
private extension FlowRunner {
    func decode(_ encoded: String?) throws -> [String: Any] {
        guard let encoded, let data = Data(base64Encoded: encoded),
              let flow = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw Failure(message: "FAST_FLOW is missing or not base64 JSON")
        }
        return flow
    }

    func point(_ x: Double, _ y: Double) -> XCUICoordinate {
        let app = current ?? springboard
        return app.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(dx: x, dy: y))
    }

    func tap(_ x: Double, _ y: Double, _ postDelay: Double) {
        trail.touch()
        point(x, y).tap()
        Thread.sleep(forTimeInterval: postDelay)
    }

    // An action's `duration` as a drag speed; XCUITest's default speed without one.
    func velocity(from start: (x: Double, y: Double), to end: (x: Double, y: Double),
                  _ action: [String: Any]) -> XCUIGestureVelocity {
        guard let duration = number(action["duration"]), duration > 0 else { return .default }
        return XCUIGestureVelocity(hypot(end.x - start.x, end.y - start.y) / duration)
    }

    func doubleTap(_ x: Double, _ y: Double, _ postDelay: Double) {
        trail.touch()
        point(x, y).doubleTap()
        Thread.sleep(forTimeInterval: postDelay)
    }

    // An `[x, y]` point, or the centre of the first node (top-left first) a selector matches.
    func pointOf(_ spec: Any?, timeout: Double, index: Int) throws -> (x: Double, y: Double) {
        if let pair = spec as? [Any], pair.count == 2, let x = number(pair[0]), let y = number(pair[1]) { return (x, y) }
        guard let selector = spec as? [String: Any] else {
            throw Failure(message: "drag needs 'from' and 'to' as [x, y] or a selector in action \(index)")
        }
        let nodes = try waitNodes(timeout) { nodes in nodes.contains { self.matches($0, selector) } }
        guard let node = sorted(nodes.filter { matches($0, selector) }, by: ["y", "x"]).first else {
            throw Failure(message: "no node matched \(selector); visible labels: \(lastLabels)")
        }
        return (Double(node.frame.midX), Double(node.frame.midY))
    }

    // The XCUIElement a selector (or `element_type`) names, for gestures a screen point cannot
    // make (pinch, rotate, a slider); without one the whole app, or the first element of `type`.
    // XCUITest starts a pinch-in with the fingers near the element's edges: on a full-screen map
    // under a sheet one finger lands on the sheet, so zooming out needs a smaller element.
    func element(_ selector: [String: Any], in app: XCUIApplication, type: XCUIElement.ElementType = .any,
                 timeout: Double) throws -> XCUIElement {
        var named: XCUIElement.ElementType?
        if let name = selector["element_type"] as? String {
            // Never the whole app once a type was named: a two-finger tap there ends the runner.
            guard let known = FlowRunner.elementTypes[name] else {
                throw Failure(message: "element_type must be one of \(FlowRunner.elementTypes.keys.sorted()), not '\(name)'")
            }
            named = known
        }
        let query = app.descendants(matching: named ?? type)
        let found: XCUIElement
        if let id = selector["id"] as? String {
            found = query.matching(NSPredicate(format: "identifier == %@", id)).firstMatch
        } else if let label = selector["label"] as? String {
            found = query.matching(NSPredicate(format: "label == %@", label)).firstMatch
        } else if let fragment = selector["label_contains"] as? String {
            found = query.matching(NSPredicate(format: "label CONTAINS %@", fragment)).firstMatch
        } else if type == .any && named == nil {
            return app
        } else {
            found = query.firstMatch
        }
        guard found.waitForExistence(timeout: timeout) else {
            throw Failure(message: "no element matched the selector of \(selector["type"] ?? "action"); visible labels: \(lastLabels)")
        }
        return found
    }

    // The text field a selector names (a caption with the same label is not one), else the first
    // text field, secure field, search field or text view on screen; `element_type` overrides.
    func textField(_ selector: [String: Any], in app: XCUIApplication, timeout: Double) throws -> XCUIElement {
        if selector["element_type"] != nil { return try element(selector, in: app, timeout: timeout) }
        let named = selector["id"] != nil || selector["label"] != nil || selector["label_contains"] != nil
        let deadline = Date().addingTimeInterval(timeout)
        repeat {
            for type in [XCUIElement.ElementType.textField, .secureTextField, .searchField, .textView] {
                let field = named ? (try? element(selector, in: app, type: type, timeout: 0)) : app.descendants(matching: type).firstMatch
                if let field, field.exists { return field }
            }
            Thread.sleep(forTimeInterval: 0.2)
        } while Date() < deadline
        throw Failure(message: "no text field\(named ? " matches the selector" : "") on screen; visible labels: \(lastLabels)")
    }

    // typeText and typeKey with nothing focused are UI Automation failures, which end the runner.
    func requireKeyboardFocus(_ app: XCUIApplication, _ kind: String) throws {
        let focused = app.descendants(matching: .any).matching(NSPredicate(format: "hasKeyboardFocus == true")).firstMatch
        guard focused.exists else {
            throw Failure(message: "\(kind) needs a focused field (tap it first); visible labels: \(lastLabels)")
        }
    }

    // Hold one finger: a context menu, or a Home Screen widget's Edit Widget.
    func press(_ x: Double, _ y: Double, _ duration: Double, _ postDelay: Double) {
        trail.touch()
        point(x, y).press(forDuration: duration)
        Thread.sleep(forTimeInterval: postDelay)
    }

    func number(_ value: Any?) -> Double? {
        (value as? NSNumber)?.doubleValue
    }

    func message(_ error: Error) -> String {
        (error as? Failure)?.message ?? "\(error)"
    }

    // Server mode returns PNGs in the reply; one-shot mode leaves them in the xcresult.
    // An image taken while a popup slides or a toast fades is not evidence: shoot until two shots
    // 0.25 s apart differ by at most `ratio` of the screen below the status bar.
    func stableScreenshot(timeout: TimeInterval, ratio: Double) -> (XCUIScreenshot, Bool) {
        var previous = XCUIScreen.main.screenshot()
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            Thread.sleep(forTimeInterval: 0.25)
            let current = XCUIScreen.main.screenshot()
            if changedRatio(previous.image, current.image) <= ratio { return (current, true) }
            previous = current
        }
        return (previous, false)
    }

    func changedRatio(_ first: UIImage, _ second: UIImage) -> Double {
        guard let a = grayPixels(first), let b = grayPixels(second), a.count == b.count, !a.isEmpty else { return 1 }
        let start = a.count * 7 / 100
        var changed = 0
        for index in start..<a.count where abs(Int(a[index]) - Int(b[index])) >= 12 { changed += 1 }
        return Double(changed) / Double(a.count - start)
    }

    func grayPixels(_ image: UIImage) -> [UInt8]? {
        guard let cgImage = image.cgImage else { return nil }
        let width = max(cgImage.width / 4, 1), height = max(cgImage.height / 4, 1)
        var pixels = [UInt8](repeating: 0, count: width * height)
        let drawn = pixels.withUnsafeMutableBytes { buffer -> Bool in
            guard let context = CGContext(
                data: buffer.baseAddress, width: width, height: height, bitsPerComponent: 8, bytesPerRow: width,
                space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGImageAlphaInfo.none.rawValue
            ) else { return false }
            context.draw(cgImage, in: CGRect(x: 0, y: 0, width: width, height: height))
            return true
        }
        return drawn ? pixels : nil
    }

    func capture(_ name: String, _ shot: XCUIScreenshot? = nil) {
        let screenshot = shot ?? XCUIScreen.main.screenshot()
        if inlineScreenshots {
            screenshots.append(["name": name, "png": screenshot.pngRepresentation.base64EncodedString()])
            return
        }
        let attachment = XCTAttachment(screenshot: screenshot)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    func attachText(_ name: String, _ text: String) {
        guard !inlineScreenshots else { return }
        let attachment = XCTAttachment(string: text)
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    func json(_ object: [String: Any], pretty: Bool = true) -> String {
        let options: JSONSerialization.WritingOptions = pretty ? [.prettyPrinted] : []
        guard let data = try? JSONSerialization.data(withJSONObject: object, options: options) else { return "{}" }
        return String(decoding: data, as: UTF8.self)
    }
}
