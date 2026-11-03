import Foundation

// The screen map's record of one request: the latest tree read before each gesture, the
// gestures that succeeded, and the read the request ended on. fast_device.py merges it with
// fast_ui_map.py; the rules mirror fast_ui_map.Trail on the Simulator.
struct TrailNode: Equatable {
    let label: String
    let type: String
    let id: String
}

struct Trail {
    struct Screen: Equatable {
        let app: String
        let title: String
        let nodes: [TrailNode]
    }

    enum Entry {
        case screen(Screen)
        case gesture([String: Any], fresh: Bool)
    }

    // fast_ui_map.KEPT_TYPES; other nodes count only when they carry an id.
    static let keptTypes: Set<String> = [
        "Button", "Cell", "Switch", "RadioButton", "Tab", "Link", "TextField", "SecureTextField",
        "TextView", "Slider", "Heading", "StaticText",
    ]
    // fast_device's TRAIL_GESTURES and fast_ui_map.GESTURE_KEYS.
    static let gestureKinds: Set<String> = [
        "tap_label", "tap_first", "tap_nearest", "tap_point", "long_press", "swipe", "scroll_to",
        "type_text", "launch", "activate", "terminate", "open_url", "double_tap", "drag", "edge_swipe", "pinch",
        "rotate", "orientation", "press_key", "press_button", "set_slider", "set_picker", "shake", "two_finger_tap",
        "two_finger_drag", "key_combo", "clear_text", "pull_to_refresh", "set_switch", "tap_alert", "dismiss_keyboard",
    ]
    static let interactiveTypes = keptTypes.subtracting(["StaticText", "Heading"])
    static let gestureKeys = [
        "label", "label_contains", "id", "element_type", "target", "anchor", "x", "y", "direction", "from", "to", "url",
        "hold", "edge", "at", "scale", "radians", "value", "index", "key", "count", "button", "modifiers", "taps", "dy", "spread",
    ]
    // fast_ui_map.LABEL_LIMIT and NODES_PER_SCREEN: long text never leaves the iPhone whole.
    static let labelLimit = 40
    static let nodesPerScreen = 150
    static let maxEntries = 50
    // A `do` that taps a point right after another `do` starts from the screen the last one
    // ended on, unless the agent took longer than this to send it.
    static let carryAge = 120.0

    private var entries: [Entry] = []
    private var latest: Screen?
    private var latestIsCarried = false
    private var touched = false
    private var before: Screen?
    private var lastRead: (screen: Screen, at: Double)?

    mutating func start(carry: Bool, now: Double) {
        entries = []
        touched = false
        before = nil
        latest = nil
        latestIsCarried = false
        if carry, let last = lastRead, now - last.at <= Trail.carryAge {
            latest = last.screen
            latestIsCarried = true
        }
    }

    // A screen's title, or none when two different navigation bars are on screen (a sheet over
    // a pushed screen, a hidden tab's bar): no title is better than the wrong one.
    static func title(_ titles: [String]) -> String {
        let distinct = Set(titles.filter { !$0.isEmpty })
        return distinct.count == 1 ? distinct.first ?? "" : ""
    }

    mutating func read(app: String, title: String, nodes: [TrailNode], now: Double) {
        var seen = Set<String>()
        let selectable = nodes.filter { node in
            guard !node.id.isEmpty || Trail.keptTypes.contains(node.type) else { return false }
            guard !node.label.isEmpty || !node.id.isEmpty else { return false }
            if node.type == "StaticText" && node.id.isEmpty && node.label.count > Trail.labelLimit { return false }
            return seen.insert("\(node.label.prefix(Trail.labelLimit))\u{1F}\(node.id)").inserted
        }
        let controls = selectable.filter { Trail.interactiveTypes.contains($0.type) }
        let others = selectable.filter { !Trail.interactiveTypes.contains($0.type) }
        let kept = Array((controls + others).prefix(Trail.nodesPerScreen))
        let screen = Screen(app: app, title: title, nodes: kept)
        latest = screen
        latestIsCarried = false
        lastRead = (screen, now)
    }

    mutating func startAction() {
        touched = false
        before = nil
    }

    // A finger went down: the read before it is where the action's gesture started.
    mutating func touch() {
        touched = true
        before = latest
        latest = nil
    }

    mutating func gesture(_ action: [String: Any]) {
        let start = touched ? before : latest
        if !touched { latest = nil }
        if let start { append(start) }
        var kept: [String: Any] = ["type": action["type"] ?? ""]
        for key in Trail.gestureKeys where action[key] != nil { kept[key] = action[key] }
        entries.append(.gesture(kept, fresh: start != nil))
        touched = false
        before = nil
    }

    // The request's entries as JSON objects, once; the next call returns nothing new.
    mutating func finish() -> [[String: Any]] {
        if let latest, !latestIsCarried { append(latest) }
        latest = nil
        defer { entries = [] }
        return entries.suffix(Trail.maxEntries).map { entry in
            switch entry {
            case .screen(let screen):
                let nodes = screen.nodes.map { node -> [String: Any] in
                    let label = node.label.count > Trail.labelLimit
                        ? ["label_contains": String(node.label.prefix(Trail.labelLimit))] : ["label": node.label]
                    return label.merging(["type": node.type, "id": node.id]) { first, _ in first }
                }
                return ["screen": ["app": screen.app, "title": screen.title, "nodes": nodes]]
            case .gesture(let action, let fresh):
                return fresh ? ["gesture": action] : ["gesture": action, "from": NSNull()]
            }
        }
    }

    private mutating func append(_ screen: Screen) {
        if case .screen(let last)? = entries.last, last == screen { return }
        entries.append(.screen(screen))
    }
}
