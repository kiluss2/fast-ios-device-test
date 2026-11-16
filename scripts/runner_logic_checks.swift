import Foundation

// Checks for the device runner's pure logic (PromptRules.swift, Settle.swift, Trail.swift,
// Gestures.swift, Requests.swift). The
// runner itself only runs on an iPhone, so test_fast_ui.RunnerLogicTests compiles these files
// with this one for the Mac and runs the result.
@main
struct RunnerLogicChecks {
    static var failures = 0

    static func check(_ condition: Bool, _ name: String) {
        if !condition {
            failures += 1
            print("FAIL \(name)")
        }
    }

    static func main() {
        checkPromptRules()
        checkSettle()
        checkTrail()
        checkGestures()
        checkRequests()
        if failures > 0 { exit(1) }
        print("OK")
    }

    static func checkPromptRules() {
        let tracking = "Allow “App” to track your activity across other companies’ apps and websites?"
        let notifications = "“App” Would Like to Send You Notifications"
        let location = "Allow “App” to use your location?"
        let rules: [Any] = [
            ["title_contains": "notifications", "tap": "Allow"],
            ["title_contains": "track", "tap": "Ask App Not to Track"],
        ]
        check(PromptRules.choice(title: tracking, buttons: ["Ask App Not to Track", "Allow"], entries: rules)
              == "Ask App Not to Track", "a button name shared by two prompts follows the tracking rule")
        check(PromptRules.choice(title: notifications, buttons: ["Don’t Allow", "Allow"], entries: rules)
              == "Allow", "the notifications rule picks Allow")
        check(PromptRules.choice(title: location, buttons: ["Allow Once", "Allow While Using App", "Don’t Allow"],
                                 entries: [["title_contains": "Location", "tap": "Always Allow"], "Don’t Allow"])
              == nil, "a bare label never answers a prompt a rule was written for")
        check(PromptRules.choice(title: notifications, buttons: ["Don’t Allow", "Allow"], entries: ["Allow", "Don’t Allow"])
              == "Allow", "bare labels answer in the order they are declared")
        check(PromptRules.choice(title: notifications, buttons: ["Don’t Allow", "Allow"], entries: [])
              == nil, "nothing declared taps nothing")

        let rating = ["Enjoying App?", "Tap a star to rate it on the App Store.", "1 star unselected"]
        let ratingRule: [Any] = [["title_contains": "Enjoying", "tap": "Not Now"]]
        check(PromptRules.screenChoice(texts: rating, buttons: ["1 star unselected", "Not Now"], entries: ratingRule)
              == "Not Now", "a rule answers a prompt drawn inside the app (StoreKit's rating prompt)")
        check(PromptRules.screenChoice(texts: rating, buttons: ["Not Now"], entries: ["Not Now"])
              == nil, "a bare label never taps a button of the app's own screen")
        check(PromptRules.screenChoice(texts: ["Welcome"], buttons: ["Not Now"], entries: ratingRule)
              == nil, "a rule whose title is not on screen taps nothing")
        check(PromptRules.screenChoice(texts: rating, buttons: ["Submit"], entries: ratingRule)
              == nil, "a rule whose button is missing taps nothing")
    }

    static func checkSettle() {
        var clock = 0.0
        var reads = [1, 2, 2, 3]
        let settled = Settle.stable(first: 0, timeout: 5, read: { reads.removeFirst() }, same: ==,
                                    now: { clock }, sleep: { clock += $0 })
        check(settled == 2 && reads == [3], "settles on the first value read twice in a row")

        reads = [5, 9]
        let unchanged = Settle.stable(first: 5, timeout: 5, read: { reads.removeFirst() }, same: ==,
                                      now: { clock }, sleep: { clock += $0 })
        check(unchanged == 5 && reads == [9], "one read is enough when it agrees with the first value")

        clock = 0
        var count = 0
        let moving = Settle.stable(first: 0, timeout: 1, interval: 0.1, read: { count += 1; return count }, same: ==,
                                   now: { clock }, sleep: { clock += $0 })
        check(moving == count && (10...12).contains(count), "a value that never settles stops at the timeout")
    }

    static func screen(_ labels: String...) -> [TrailNode] {
        labels.map { TrailNode(label: $0, type: "Button", id: "") }
    }

    static func kinds(_ entries: [[String: Any]]) -> [String] {
        entries.map { entry in
            if let gesture = entry["gesture"] as? [String: Any] { return gesture["type"] as? String ?? "?" }
            let screen = entry["screen"] as? [String: Any]
            let nodes = screen?["nodes"] as? [[String: Any]] ?? []
            return nodes.compactMap { $0["label"] as? String }.joined(separator: "+")
        }
    }

    static func checkTrail() {
        var trail = Trail()
        trail.start(carry: false, now: 0)
        trail.read(app: "app", title: "", nodes: screen("A"), now: 0)
        trail.read(app: "app", title: "", nodes: screen("B"), now: 0)
        trail.startAction()
        trail.touch()
        trail.read(app: "app", title: "", nodes: screen("C"), now: 0)
        trail.gesture(["type": "tap_nearest", "target": ["label": "B"], "anchor": ["label": "x"], "timeout": 3])
        trail.startAction()
        trail.touch()
        trail.gesture(["type": "tap_point", "x": 1, "y": 2])
        trail.startAction()
        trail.gesture(["type": "type_text", "text": "secret"])
        var entries = trail.finish()
        check(kinds(entries) == ["B", "tap_nearest", "C", "tap_point", "type_text"],
              "a gesture starts from the read before its touch; reads after it are where it led")
        check(entries[1]["from"] == nil && entries[3]["from"] == nil && entries[4]["from"] is NSNull,
              "a gesture with no read since the previous one has no starting screen")
        check((entries[1]["gesture"] as? [String: Any]).map { $0.keys.sorted() } == ["anchor", "target", "type"],
              "tap_nearest keeps its selectors and drops timing")
        check((entries[4]["gesture"] as? [String: Any])?["text"] == nil, "typed text is never kept")
        check(trail.finish().isEmpty, "finish hands a request's entries over once")

        trail.start(carry: false, now: 0)
        trail.read(app: "app", title: "Title", nodes: [
            TrailNode(label: "q", type: "Other", id: ""), TrailNode(label: "", type: "Image", id: ""),
            TrailNode(label: "Go", type: "Button", id: ""), TrailNode(label: "", type: "Other", id: "close"),
            TrailNode(label: "", type: "StaticText", id: ""),
        ], now: 0)
        entries = trail.finish()
        let stored = (entries.first?["screen"] as? [String: Any])
        check((stored?["nodes"] as? [[String: Any]])?.count == 2 && stored?["title"] as? String == "Title"
              && stored?["app"] as? String == "app", "only selectable nodes with a label or an id are kept")

        let long = "Reply to the message about tomorrow's meeting at the office"
        trail.start(carry: false, now: 0)
        trail.read(app: "app", title: "", nodes: [TrailNode(label: long, type: "StaticText", id: ""),
                                                  TrailNode(label: long, type: "Button", id: "")]
                   + (0..<200).map { TrailNode(label: "Text \($0)", type: "StaticText", id: "") }
                   + (0..<10).map { TrailNode(label: "Go \($0)", type: "Button", id: "") }, now: 0)
        trail.startAction()
        trail.gesture(["type": "open_url", "url": "myapp://settings"])
        entries = trail.finish()
        let sent = (entries.first?["screen"] as? [String: Any])?["nodes"] as? [[String: Any]] ?? []
        check(sent.count == 150 && sent.first?["label_contains"] as? String == String(long.prefix(40))
              && sent.first?["label"] == nil && sent[1]["label"] as? String == "Go 0",
              "long text stays on the iPhone: long StaticText dropped, long labels cut, 150 nodes, controls first")
        check((entries.last?["gesture"] as? [String: Any])?["url"] as? String == "myapp://settings",
              "a path through open_url keeps its url")
        check(Trail.title(["General"]) == "General" && Trail.title(["General", "General"]) == "General"
              && Trail.title(["Settings", "General"]) == "" && Trail.title([]) == "",
              "two different navigation titles on screen name no screen")

        trail.start(carry: false, now: 0)
        for index in 0..<60 {
            trail.read(app: "app", title: "", nodes: screen("S\(index)"), now: 0)
            trail.startAction()
            trail.gesture(["type": "tap_label", "label": "S\(index)"])
        }
        entries = trail.finish()
        check(entries.count == 50 && kinds(entries).last == "tap_label" && kinds(entries).first == "S35",
              "a long request keeps its last 50 entries")

        trail.start(carry: false, now: 0)
        trail.read(app: "app", title: "", nodes: screen("Home"), now: 0)
        _ = trail.finish()
        trail.start(carry: true, now: 10)
        check(trail.finish().isEmpty, "a carried screen is not reported again without a gesture")
        trail.start(carry: true, now: 119)
        trail.startAction()
        trail.touch()
        trail.gesture(["type": "tap_point", "x": 1, "y": 2])
        entries = trail.finish()
        check(kinds(entries) == ["Home", "tap_point"] && entries[1]["from"] == nil,
              "the next request starts from the screen the last one ended on")
        trail.start(carry: true, now: 240)
        trail.startAction()
        trail.gesture(["type": "swipe", "direction": "up"])
        check(trail.finish().first?["from"] is NSNull, "a screen older than two minutes is not carried")
    }

    static func checkGestures() {
        let screen = CGRect(x: 0, y: 0, width: 400, height: 800)
        func pair(_ edge: String, _ at: Double, _ distance: Double) -> [Double] {
            guard let points = Gestures.edgePoints(edge: edge, at: at, distance: distance, screen: screen) else { return [] }
            return [points.start.x, points.start.y, points.end.x, points.end.y].map { Double($0) }
        }
        check(pair("left", 0.5, 0.5) == [1, 400, 200, 400] && pair("top", 0.9, 0.5) == [360, 1, 360, 400]
              && pair("bottom", 0.5, 0.25) == [200, 799, 200, 600] && pair("right", 0.25, 0.5) == [399, 200, 200, 200],
              "edge swipes start one point inside the edge, as on the Simulator")
        check(Gestures.edgePoints(edge: "middle", at: 0.5, distance: 0.5, screen: screen) == nil, "an unknown edge has no points")

        var trail = Trail()
        trail.start(carry: false, now: 0)
        trail.startAction()
        trail.gesture(["type": "press_key", "key": "delete", "count": 3, "timeout": 2])
        trail.gesture(["type": "drag", "from": [1, 2], "to": [3, 4], "hold": 1])
        trail.gesture(["type": "key_combo", "key": "a", "modifiers": ["command"]])
        trail.gesture(["type": "press_button", "button": "siri", "text": "call mum"])
        let kept = trail.finish().compactMap { ($0["gesture"] as? [String: Any]).map { $0.keys.sorted() } }
        check(Trail.gestureKinds.isSuperset(of: ["double_tap", "drag", "edge_swipe", "pinch", "rotate", "orientation",
                                                 "press_key", "press_button", "set_slider", "set_picker", "shake",
                                                 "two_finger_tap", "two_finger_drag", "key_combo", "clear_text"])
              && kept == [["count", "key", "type"], ["from", "hold", "to", "type"], ["key", "modifiers", "type"],
                          ["button", "type"]],
              "the new gestures join the screen map with their parameters")
    }

    static func checkRequests() {
        check(Requests.endsWithRead(["type": "wait", "label": "A"]) && Requests.endsWithRead(["type": "dump"])
              && Requests.endsWithRead(["type": "scroll_to", "label": "A"])
              && Requests.endsWithRead(["type": "tap_label", "label": "A", "expect": ["label": "B"]])
              && Requests.endsWithRead(["type": "tap_nearest", "success": ["label": "B"]]),
              "a step that reads the tree last leaves a fresh read: the request needs no closing one")
        check(!Requests.endsWithRead(["type": "tap_label", "label": "A"]) && !Requests.endsWithRead(["type": "swipe"])
              && !Requests.endsWithRead(["type": "screenshot"]) && !Requests.endsWithRead([:]),
              "a gesture, a screenshot or nothing still needs the closing read")
    }
}
