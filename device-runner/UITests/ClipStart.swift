import Foundation

// Where a recorded clip should begin, as device time (the clock XCTest stamps its screen
// recording with). The relaunch at the head of a flow is setup, not evidence: by default the
// clip starts once the leading launch / activate / terminate steps are done; a step marked
// `"clip_start": true` moves the start to just before it; `setup.clip: "full"` keeps it all.
struct ClipStart {
    static let openingKinds: Set<String> = ["launch", "activate", "terminate"]

    private var time: Double?
    private var isLeading = true
    private var isMarked = false

    mutating func opened() {
        time = Date().timeIntervalSince1970
    }

    mutating func before(_ action: [String: Any]) {
        guard (action["clip_start"] as? Bool) == true, !isMarked else { return }
        time = Date().timeIntervalSince1970
        isMarked = true
    }

    mutating func after(_ action: [String: Any]) {
        guard isLeading, !isMarked else { return }
        if Self.openingKinds.contains(action["type"] as? String ?? "") {
            time = Date().timeIntervalSince1970
        } else {
            isLeading = false
        }
    }

    func report(_ setup: [String: Any]) -> [String: Any] {
        guard let time, (setup["clip"] as? String) != "full" else { return [:] }
        return ["clip_start": time]
    }
}
