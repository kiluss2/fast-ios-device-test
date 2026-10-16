import CoreLocation
import Network
import UIKit
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
        // What an empty text field shows; its value is the placeholder too.
        let placeholder: String
    }

    // The selector keys an action may carry, shared with the Simulator engine.
    static let selectorKeys = ["label", "label_contains", "label_is_empty", "id", "value", "element_type", "enabled", "frame"]

    private var apps: [String: XCUIApplication] = [:]
    private var current: XCUIApplication?
    private var promptLabels: [String] = []
    private var lastLabels: [String] = []
    private var screenBounds = CGRect.infinite
    private var inlineScreenshots = false
    private var screenshots: [[String: String]] = []
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
        let report: [String: Any]
        do {
            report = runFlow(try decode(ProcessInfo.processInfo.environment["FAST_FLOW"]))
        } catch {
            report = ["status": "fail", "step": "prepare", "error": message(error)]
        }
        attachText("zz_result.json", json(report))
    }

    func testServe() throws {
        continueAfterFailure = true
        inlineScreenshots = true
        let environment = ProcessInfo.processInfo.environment
        let port = NWEndpoint.Port(rawValue: UInt16(environment["FAST_PORT"] ?? "") ?? 8765) ?? 8765
        let idleLimit = Double(environment["FAST_IDLE"] ?? "1800") ?? 1800
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
            request.reply(json(response, pretty: false))
        }
        server.stop()
    }
}

// MARK: - Requests
private extension FlowRunner {
    func handle(_ body: Data, isStopped: inout Bool) -> [String: Any] {
        do {
            guard let request = try JSONSerialization.jsonObject(with: body) as? [String: Any],
                  let operation = request["op"] as? String else {
                throw Failure(message: "request needs an op")
            }
            let skipIdle = (request["wait_for_idle"] as? Bool) == false
            if skipIdle { Quiescence.disable() }
            defer { if skipIdle { Quiescence.restore() } }
            switch operation {
            case "ping":
                return ["status": "pass", "pong": true]
            case "stop":
                isStopped = true
                return ["status": "pass"]
            case "flow":
                return runFlow(request["flow"] as? [String: Any] ?? [:])
            case "action":
                let action = request["action"] as? [String: Any] ?? [:]
                if let bundleID = action["app"] as? String { current = application(bundleID) }
                guard current != nil else { throw Failure(message: "no current app: add \"app\" to the action") }
                let result = try execute(action, index: 1) ?? [:]
                _ = snapshotNodes()
                return ["status": "pass", "result": result, "labels": lastLabels]
            case "actions":
                return runActions(request["actions"] as? [[String: Any]] ?? [])
            default:
                throw Failure(message: "unknown op '\(operation)'")
            }
        } catch {
            if current != nil { _ = snapshotNodes() }
            return ["status": "fail", "error": message(error), "labels": lastLabels]
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
        _ = snapshotNodes()
        return ["status": "pass", "results": results, "labels": lastLabels]
    }

    func runFlow(_ flow: [String: Any]) -> [String: Any] {
        var results: [[String: Any]] = []
        var step = "launch"
        let setup = flow["setup"] as? [String: Any] ?? [:]
        let previousAppearance = XCUIDevice.shared.appearance
        defer { XCUIDevice.shared.appearance = previousAppearance }
        let skipIdle = (setup["wait_for_idle"] as? Bool) == false
        let idleNote: [String: Any] = skipIdle ? ["wait_for_idle": Quiescence.disable() ? false : "unsupported"] : [:]
        defer { if skipIdle { Quiescence.restore() } }
        do {
            let app = flow["app"] as? [String: Any] ?? [:]
            guard let bundleID = app["bundle_id"] as? String else { throw Failure(message: "missing app.bundle_id") }
            promptLabels = setup["optional_permission_labels"] as? [String] ?? []
            switch setup["appearance"] as? String {
            case "dark": XCUIDevice.shared.appearance = .dark
            case "light": XCUIDevice.shared.appearance = .light
            default: break
            }

            let target = application(bundleID)
            current = target
            target.launchArguments = setup["launch_arguments"] as? [String] ?? []
            if setup["launch"] as? Bool ?? true {
                target.launch()
            } else {
                target.activate()
            }

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
    func execute(_ action: [String: Any], index: Int) throws -> [String: Any]? {
        actionIssue = nil
        isRunningAction = true
        defer { isRunningAction = false }
        let result = try perform(action, index: index)
        if let actionIssue { throw Failure(message: "XCTest: \(actionIssue)") }
        return result
    }

    func perform(_ action: [String: Any], index: Int) throws -> [String: Any]? {
        guard let kind = action["type"] as? String else { throw Failure(message: "action \(index) has no type") }
        if let bundleID = action["app"] as? String { current = application(bundleID) }
        guard let app = current else { throw Failure(message: "no current app") }
        let timeout = number(action["timeout"]) ?? 6
        let postDelay = number(action["post_delay"]) ?? 0.15

        switch kind {
        case "activate":
            app.activate()
        case "launch":
            if let arguments = action["launch_arguments"] as? [String] { app.launchArguments = arguments }
            app.launch()
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
        case "tap_label":
            guard action["label"] != nil || action["label_contains"] != nil || action["id"] != nil else {
                throw Failure(message: "missing 'label' or 'id' in action \(index)")
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
            if let node {
                tap(node.frame.midX, node.frame.midY, postDelay)
                if let expected = selector["element_type"] as? String, !typeMatches(node.type, expected) {
                    return ["fallback_type": node.type]
                }
            }
        case "tap_first":
            let nodes = try waitNodes(timeout) { nodes in nodes.contains { self.matches($0, action) } }
            let keys = action["sort"] as? [String] ?? ["y", "x"]
            guard let node = sorted(nodes.filter { matches($0, action) }, by: keys).first else {
                throw Failure(message: "no node matched tap_first at action \(index)")
            }
            tap(node.frame.midX, node.frame.midY, postDelay)
        case "tap_nearest":
            try tapNearest(action, timeout: timeout, postDelay: postDelay)
        case "tap_point":
            tap(number(action["x"]) ?? 0, number(action["y"]) ?? 0, postDelay)
        case "swipe":
            // On an element (a row to swipe-delete) the swipe stays inside its frame.
            let area = hasSelector(action) ? try resolveNode(action, timeout: timeout).frame : nil
            try swipe(action, in: area)
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
            let field = try focusField(action, timeout: timeout)
            if action["clear"] as? Bool ?? false { try clearText(field) }
            app.typeText(text)
            Thread.sleep(forTimeInterval: postDelay)
        case "clear_text":
            try clearText(try focusField(action, timeout: timeout))
        case "press_key":
            guard let key = (action["key"] as? String)?.lowercased(), let typed = FlowRunner.keys[key] else {
                throw Failure(message: "press_key key must be one of \(FlowRunner.keys.keys.sorted().joined(separator: ", ")); arrow keys need Simulator")
            }
            app.typeText(String(repeating: typed, count: Int(number(action["count"]) ?? 1)))
            Thread.sleep(forTimeInterval: postDelay)
        case "dismiss_keyboard":
            try dismissKeyboard(app, button: action["button"] as? String, timeout: timeout)
        case "long_press":
            let (x, y) = try resolvePoint(action, timeout: timeout, context: "long_press")
            point(x, y).press(forDuration: number(action["duration"]) ?? 1.0)
            Thread.sleep(forTimeInterval: postDelay)
        case "double_tap":
            let (x, y) = try resolvePoint(action, timeout: timeout, context: "double_tap")
            point(x, y).doubleTap()
            Thread.sleep(forTimeInterval: postDelay)
        case "two_finger_tap":
            try element(action, app: app).twoFingerTap()
            Thread.sleep(forTimeInterval: postDelay)
        case "drag":
            let start = try resolvePoint(action["from"], timeout: timeout, context: "drag 'from'")
            let end = try resolvePoint(action["to"], timeout: timeout, context: "drag 'to'")
            let distance = hypot(end.0 - start.0, end.1 - start.1)
            let speed = distance / max(number(action["duration"]) ?? 0.8, 0.05)
            point(start.0, start.1).press(
                forDuration: number(action["hold"]) ?? 0.6, thenDragTo: point(end.0, end.1),
                withVelocity: XCUIGestureVelocity(rawValue: CGFloat(speed)), thenHoldForDuration: 0.3
            )
            Thread.sleep(forTimeInterval: postDelay)
        case "pinch":
            let scale = number(action["scale"]) ?? 2
            guard scale > 0 else { throw Failure(message: "pinch scale must be above 0 (below 1 zooms out)") }
            let velocity = number(action["velocity"]) ?? (scale >= 1 ? 1 : -1)
            try element(action, app: app).pinch(withScale: CGFloat(scale), velocity: CGFloat(velocity))
            Thread.sleep(forTimeInterval: postDelay)
        case "rotate_gesture":
            let radians = (number(action["degrees"]) ?? 90) * .pi / 180
            try element(action, app: app).rotate(CGFloat(radians), withVelocity: CGFloat(radians >= 0 ? 1 : -1))
            Thread.sleep(forTimeInterval: postDelay)
        case "edge_swipe", "back":
            let edge = kind == "back" ? "left" : action["edge"] as? String ?? "left"
            guard let (from, to) = FlowRunner.edges[edge] else { throw Failure(message: "edge must be left, right, top or bottom") }
            if screenBounds.isInfinite { _ = snapshotNodes() }
            let bounds = screenBounds
            func at(_ fraction: (Double, Double)) -> XCUICoordinate {
                point(Double(bounds.minX) + Double(bounds.width) * fraction.0, Double(bounds.minY) + Double(bounds.height) * fraction.1)
            }
            at(from).press(forDuration: 0.05, thenDragTo: at(to))
            Thread.sleep(forTimeInterval: max(postDelay, 0.6))
        case "pull_to_refresh":
            if screenBounds.isInfinite { _ = snapshotNodes() }
            let area = hasSelector(action) ? try resolveNode(action, timeout: timeout).frame : screenBounds
            let x = Double(area.midX)
            point(x, Double(area.minY + area.height * 0.25)).press(
                forDuration: 0.05, thenDragTo: point(x, Double(area.minY + area.height * 0.75)),
                withVelocity: .slow, thenHoldForDuration: 0.3
            )
            Thread.sleep(forTimeInterval: max(postDelay, 1.0))
        case "set_switch":
            guard let on = action["on"] as? Bool else { throw Failure(message: "set_switch needs on: true or false") }
            return try setSwitch(action, on: on, timeout: timeout, postDelay: postDelay)
        case "set_slider":
            guard let percent = number(action["percent"]), (0...100).contains(percent) else {
                throw Failure(message: "set_slider needs percent 0-100")
            }
            let slider = try query(action, in: app.sliders, type: "Slider", timeout: timeout)
            slider.adjust(toNormalizedSliderPosition: CGFloat(percent / 100))
            Thread.sleep(forTimeInterval: postDelay)
            let value = (slider.value as? String) ?? ""
            // UIKit reports "50%", SwiftUI the fraction "0.5"; anything else (a step number) is not checked.
            let trimmed = value.trimmingCharacters(in: .whitespaces)
            let reported = trimmed.hasSuffix("%") ? Double(trimmed.dropLast()) : Double(trimmed).flatMap { (0...1).contains($0) ? $0 * 100 : nil }
            let tolerance = number(action["tolerance"]) ?? 2
            if let reported, abs(reported - percent) > tolerance {
                throw Failure(message: "slider is at \(value), expected \(percent)% (±\(tolerance))")
            }
            return ["value": value]
        case "set_picker":
            guard let option = action["option"] as? String else {
                throw Failure(message: "set_picker on a device needs option (the row text); index is Simulator-only")
            }
            let wheel = try query(action, in: app.pickerWheels, type: "PickerWheel", timeout: timeout)
            wheel.adjust(toPickerWheelValue: option)
            Thread.sleep(forTimeInterval: postDelay)
        case "tap_alert":
            guard let label = action["button"] as? String else { throw Failure(message: "tap_alert needs button") }
            try tapAlert(label, app: app, timeout: timeout)
            Thread.sleep(forTimeInterval: postDelay)
        case "assert_value":
            let attribute = action["attribute"] as? String ?? "value"
            guard ["value", "label"].contains(attribute) else { throw Failure(message: "attribute must be value or label") }
            _ = try checkText("", action)  // a flow without equals/contains/… fails here, before any polling
            var complaint = "no node matched the selector"
            do {
                _ = try waitNodes(timeout) { nodes in
                    guard let node = self.sorted(nodes.filter { self.matches($0, action) }, by: ["y", "x"]).first else {
                        complaint = "no node matched the selector"
                        return false
                    }
                    // Not `try?`: it flattens a throw and a pass into the same nil.
                    do {
                        guard let failure = try self.checkText(attribute == "label" ? node.label : node.value, action) else {
                            return true
                        }
                        complaint = "\(attribute) \(failure)"
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
        case "press_button":
            guard let name = action["button"] as? String, let button = FlowRunner.buttons[name] else {
                throw Failure(message: "press_button on a device takes \(FlowRunner.buttons.keys.sorted().joined(separator: ", ")); lock and siri need Simulator")
            }
            XCUIDevice.shared.press(button)
            Thread.sleep(forTimeInterval: max(postDelay, 1.0))
        case "set_orientation":
            guard let name = action["orientation"] as? String, let orientation = FlowRunner.orientations[name] else {
                throw Failure(message: "orientation must be \(FlowRunner.orientations.keys.sorted().joined(separator: ", "))")
            }
            XCUIDevice.shared.orientation = orientation
            Thread.sleep(forTimeInterval: max(postDelay, 1.0))
            screenBounds = .infinite
        case "set_appearance":
            switch action["appearance"] as? String {
            case "dark": XCUIDevice.shared.appearance = .dark
            case "light": XCUIDevice.shared.appearance = .light
            default: throw Failure(message: "appearance must be light or dark")
            }
        case "set_location":
            if action["clear"] as? Bool ?? false {
                XCUIDevice.shared.location = nil
            } else {
                guard let latitude = number(action["latitude"]), let longitude = number(action["longitude"]) else {
                    throw Failure(message: "set_location needs latitude and longitude, or clear: true")
                }
                XCUIDevice.shared.location = XCUILocation(location: CLLocation(latitude: latitude, longitude: longitude))
            }
        case "set_permission":
            guard let verb = action["action"] as? String else {
                throw Failure(message: "set_permission needs action: grant, revoke or reset")
            }
            guard verb == "reset" else {
                throw Failure(message: "a device can only reset a permission (the app asks again); grant and revoke need Simulator")
            }
            guard let service = action["service"] as? String, let resource = FlowRunner.resources[service] else {
                throw Failure(message: "service must be \(FlowRunner.resources.keys.sorted().joined(separator: ", "))")
            }
            // Resetting terminates the app when it is running.
            app.resetAuthorizationStatus(for: resource)
        case "open_url":
            guard let string = action["url"] as? String, let url = URL(string: string) else {
                throw Failure(message: "open_url needs url")
            }
            // The system's handler for the URL, like `simctl openurl`; `app.open` keeps it in the current app.
            XCUIDevice.shared.system.open(url)
            Thread.sleep(forTimeInterval: max(postDelay, 1.0))
        case "set_clipboard":
            withRunnerInFront { UIPasteboard.general.string = action["text"] as? String ?? "" }
        case "assert_clipboard":
            if let failure = try checkText(withRunnerInFront { UIPasteboard.general.string ?? "" }, action) {
                throw Failure(message: "clipboard \(failure)")
            }
        case "push_notification", "status_bar":
            throw Failure(message: "\(kind) uses simctl and runs on Simulator only")
        case "pause":
            Thread.sleep(forTimeInterval: number(action["seconds"]) ?? 1)
        case "screenshot":
            let name = action["name"] as? String ?? String(format: "step-%02d", index)
            capture(name)
            return ["screenshot": name]
        case "dump":
            let nodes = try waitNodes(timeout) { _ in true }
            let name = action["name"] as? String ?? String(format: "step-%02d", index)
            attachText("\(name)-ui.txt", app.debugDescription)
            return ["name": name, "labels": visibleLabels(nodes), "nodes": visibleNodes(nodes)]
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
        func resolveAndTap() throws {
            let nodes = try waitNodes(timeout) { nodes in
                nodes.contains { self.matches($0, target) } && nodes.contains { self.matches($0, anchor) }
            }
            let targets = nodes.filter { matches($0, target) }
            // XCUITest nests a text in containers with the same label and frame; those are one anchor.
            let anchorValues = Set(nodes.filter { matches($0, anchor) }.map { value($0, axis).rounded() })
            guard anchorValues.count == 1, let anchorValue = anchorValues.first else {
                throw Failure(message: "tap_nearest requires one anchor and at least one target; got \(anchorValues.count)/\(targets.count)")
            }
            guard let nearest = targets.min(by: { abs(value($0, axis) - anchorValue) < abs(value($1, axis) - anchorValue) }) else {
                throw Failure(message: "tap_nearest found no target")
            }
            tap(nearest.frame.midX, nearest.frame.midY, postDelay)
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

    // `from`/`to` points, or a `direction` the finger moves in ("up" scrolls content down)
    // across `area` (an element's frame) or else the whole screen.
    func swipe(_ action: [String: Any], in area: CGRect? = nil) throws {
        if let from = action["from"] as? [Any], let to = action["to"] as? [Any],
           let fromX = number(from.first), let fromY = number(from.last),
           let toX = number(to.first), let toY = number(to.last) {
            point(fromX, fromY).press(forDuration: 0.05, thenDragTo: point(toX, toY))
            return
        }
        if area == nil && screenBounds.isInfinite { _ = snapshotNodes() }
        let bounds = area ?? screenBounds
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
        point(points.0, points.1).press(forDuration: 0.05, thenDragTo: point(points.2, points.3))
    }
}

// MARK: - Controls and targets
private extension FlowRunner {
    // Typed text for press_key; arrow keys have no software-keyboard equivalent.
    static let keys: [String: String] = [
        "return": "\n", "enter": "\n", "delete": XCUIKeyboardKey.delete.rawValue,
        "backspace": XCUIKeyboardKey.delete.rawValue, "tab": "\t", "space": " ",
        "escape": XCUIKeyboardKey.escape.rawValue,
    ]
    static let buttons: [String: XCUIDevice.Button] = [
        "home": .home, "volume_up": .volumeUp, "volume_down": .volumeDown, "action": .action, "camera": .camera,
    ]
    static let orientations: [String: UIDeviceOrientation] = [
        "portrait": .portrait, "portrait_upside_down": .portraitUpsideDown,
        "landscape_left": .landscapeLeft, "landscape_right": .landscapeRight,
    ]
    // The service names `simctl privacy` uses, where both exist.
    static let resources: [String: XCUIProtectedResource] = [
        "contacts": .contacts, "calendar": .calendar, "reminders": .reminders, "photos": .photos,
        "microphone": .microphone, "camera": .camera, "media-library": .mediaLibrary, "homekit": .homeKit,
        "location": .location, "bluetooth": .bluetooth, "health": .health, "user-tracking": .userTracking,
        "local-network": .localNetwork, "keyboard-network": .keyboardNetwork, "focus": .focus,
    ]
    // Start and end of an edge swipe as fractions of the screen, as in the Simulator engine.
    static let edges: [String: ((Double, Double), (Double, Double))] = [
        "left": ((0.005, 0.5), (0.8, 0.5)), "right": ((0.995, 0.5), (0.2, 0.5)),
        "top": ((0.5, 0.003), (0.5, 0.6)), "bottom": ((0.5, 0.997), (0.5, 0.5)),
    ]

    func hasSelector(_ action: [String: Any]) -> Bool {
        FlowRunner.selectorKeys.contains { action[$0] != nil }
    }

    // The top-left node matching the action's selector keys, waiting for it like `wait`.
    func resolveNode(_ selector: [String: Any], timeout: Double, accept: @escaping (Node) -> Bool = { _ in true }) throws -> Node {
        let found: (Node) -> Bool = { self.matches($0, selector) && accept($0) }
        let nodes = try waitNodes(timeout) { nodes in nodes.contains(where: found) }
        guard let node = sorted(nodes.filter(found), by: ["y", "x"]).first else {
            throw Failure(message: "no node matched the selector")
        }
        return node
    }

    // A gesture target: [x, y], a mapping with x/y, or a selector mapping.
    func resolvePoint(_ spec: Any?, timeout: Double, context: String) throws -> (Double, Double) {
        if let pair = spec as? [Any], pair.count == 2, let x = number(pair[0]), let y = number(pair[1]) {
            return (x, y)
        }
        if let mapping = spec as? [String: Any] {
            if let x = number(mapping["x"]), let y = number(mapping["y"]) { return (x, y) }
            if hasSelector(mapping) {
                let frame = try resolveNode(mapping, timeout: timeout).frame
                return (Double(frame.midX), Double(frame.midY))
            }
        }
        throw Failure(message: "\(context) needs x/y, an [x, y] point or a selector (label, id, …)")
    }

    // An XCUIElement for gestures XCUITest only offers on elements (pinch, rotate, two-finger
    // tap): the one carrying the selector's id or label, else the whole app.
    func element(_ action: [String: Any], app: XCUIApplication) throws -> XCUIElement {
        guard hasSelector(action) else { return app }
        let node = try resolveNode(action, timeout: number(action["timeout"]) ?? 6)
        let candidates = app.descendants(matching: .any)
        let element = !node.identifier.isEmpty
            ? candidates.matching(identifier: node.identifier).firstMatch
            : candidates.matching(NSPredicate(format: "label == %@", node.label)).firstMatch
        return element.exists ? element : app
    }

    // The element of `type` the selector names by id or label, else the first one on screen.
    // The full selector picks the node (as on the Simulator); the element is then found by
    // that node's identifier, else by its frame, so label_contains, value or frame bounds count.
    func query(_ action: [String: Any], in elements: XCUIElementQuery, type: String, timeout: Double) throws -> XCUIElement {
        let node = try resolveNode(action, timeout: timeout) { $0.type == type }
        if !node.identifier.isEmpty {
            let element = elements.matching(identifier: node.identifier).firstMatch
            if element.exists { return element }
        }
        if let element = elements.allElementsBoundByIndex.first(where: { $0.exists && $0.frame.integral == node.frame.integral }) {
            return element
        }
        throw Failure(message: "the matching \(type) is not reachable as an element; visible labels: \(lastLabels)")
    }

    // Taps the field the action names, if any, and returns the field that has keyboard focus.
    func focusField(_ action: [String: Any], timeout: Double) throws -> XCUIElement? {
        guard let app = current else { return nil }
        if hasSelector(action) {
            let frame = try resolveNode(action, timeout: timeout).frame
            // Near the trailing edge, so the cursor lands after the existing text, but left of a
            // button inside the field (a search bar's Dictate or clear button), whose touch area
            // is ~44 pt wide however small its glyph.
            var x = Double(frame.minX + frame.width * 0.9)
            for button in snapshotNodes() where button.type == "Button" && button.frame.width < frame.width / 2
                && frame.contains(CGPoint(x: button.frame.midX, y: button.frame.midY)) {
                x = min(x, Double(min(button.frame.minX, button.frame.midX - 22)) - 8)
            }
            point(max(x, Double(frame.minX) + 4), Double(frame.midY)).tap()
            Thread.sleep(forTimeInterval: 0.5)
        }
        let focused = app.descendants(matching: .any).matching(NSPredicate(format: "hasKeyboardFocus == true")).firstMatch
        return focused.exists ? focused : nil
    }

    // One Delete per character after moving to the end; an empty field reports its placeholder.
    func clearText(_ field: XCUIElement?) throws {
        guard let app = current else { return }
        guard let field else { throw Failure(message: "no text field has keyboard focus; name the field with a selector") }
        let text = (field.value as? String) ?? ""
        guard !text.isEmpty, text != (field.placeholderValue ?? "") else { return }
        app.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: text.count + 2))
    }

    func dismissKeyboard(_ app: XCUIApplication, button: String?, timeout: Double) throws {
        guard app.keyboards.firstMatch.exists else { return }
        if let button {
            let target = [app.keyboards.buttons[button], app.toolbars.buttons[button], app.buttons[button]]
                .first { $0.exists }
            guard let target else { throw Failure(message: "no '\(button)' button on screen; visible labels: \(lastLabels)") }
            target.tap()
        } else {
            // The first Return can be spent accepting an autocorrect suggestion.
            for _ in 0..<3 where app.keyboards.firstMatch.exists {
                app.typeText("\n")
                Thread.sleep(forTimeInterval: 0.4)
            }
        }
        if app.keyboards.firstMatch.exists {
            throw Failure(message: "the keyboard is still up; name the field's dismiss button with \"button\"")
        }
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
            throw Failure(message: "switch did not turn \(on ? "on" : "off"); visible labels: \(lastLabels)")
        }
        return ["toggled": true]
    }

    // System alerts belong to SpringBoard, an app's own alerts to the app.
    func tapAlert(_ label: String, app: XCUIApplication, timeout: Double) throws {
        let deadline = Date().addingTimeInterval(timeout)
        repeat {
            for owner in [springboard, app] {
                let button = owner.alerts.buttons[label]
                if button.exists {
                    button.tap()
                    return
                }
            }
            Thread.sleep(forTimeInterval: 0.2)
        } while Date() < deadline
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
        func walk(_ element: XCUIElementSnapshot, cell: CGRect?) {
            let value = element.value.map { ($0 as? String) ?? String(describing: $0) } ?? ""
            nodes.append(Node(label: element.label, type: typeName(element.elementType), frame: element.frame,
                              cell: cell, identifier: element.identifier, value: value, enabled: element.isEnabled,
                              placeholder: element.placeholderValue ?? ""))
            let childCell = element.elementType == .cell ? element.frame : cell
            element.children.forEach { walk($0, cell: childCell) }
        }
        walk(root, cell: nil)
        lastLabels = visibleLabels(nodes)
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

    // Permission alerts on a device belong to SpringBoard, not the app under test.
    func dismissPrompt() -> Bool {
        guard !promptLabels.isEmpty else { return false }
        for owner in [springboard] + [current].compactMap({ $0 }) {
            for label in promptLabels {
                let button = owner.alerts.buttons[label]
                if button.exists {
                    button.tap()
                    Thread.sleep(forTimeInterval: 0.3)
                    return true
                }
            }
        }
        return false
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
        let frames = Set(candidates.map {
            "\($0.frame.minX.rounded()),\($0.frame.minY.rounded()),\($0.frame.width.rounded()),\($0.frame.height.rounded())"
        })
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
        case "TextField": return ["TextField", "SecureTextField", "TextView", "SearchField"].contains(actual)
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
        case .searchField: return "SearchField"
        case .image: return "Image"
        case .cell: return "Cell"
        case .switch: return "Switch"
        case .radioButton: return "RadioButton"
        case .link: return "Link"
        case .tab: return "Tab"
        case .alert: return "Alert"
        case .slider: return "Slider"
        case .pickerWheel: return "PickerWheel"
        case .picker: return "Picker"
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
                (!node.label.isEmpty || !node.identifier.isEmpty || node.type != "Other") && node.frame.width > 0 && node.frame.height > 0
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
        point(x, y).tap()
        Thread.sleep(forTimeInterval: postDelay)
    }

    // iOS keeps the pasteboard from background processes, and the runner is one: it comes to
    // the front for the access, then the current app is activated again (it sees a resign/become active).
    func withRunnerInFront<T>(_ body: () -> T) -> T {
        guard let bundleID = Bundle.main.bundleIdentifier else { return body() }
        XCUIApplication(bundleIdentifier: bundleID).activate()
        defer { current?.activate() }
        return body()
    }

    func number(_ value: Any?) -> Double? {
        (value as? NSNumber)?.doubleValue
    }

    func message(_ error: Error) -> String {
        (error as? Failure)?.message ?? "\(error)"
    }

    // Server mode returns PNGs in the reply; one-shot mode leaves them in the xcresult.
    func capture(_ name: String) {
        let screenshot = XCUIScreen.main.screenshot()
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
