import ApplicationServices
import Carbon
import Cocoa
import CoreAudio
import Foundation

// MARK: - JSON Output Helper

func emitJSON(_ dict: [String: Any], exitCode: Int32 = 0) -> Never {
    if let data = try? JSONSerialization.data(withJSONObject: dict, options: [.sortedKeys]),
       let str = String(data: data, encoding: .utf8) {
        print(str)
    } else {
        print("{\"ok\":false,\"error\":\"json_serialization_failed\"}")
    }
    fflush(stdout)
    exit(exitCode)
}

func failDict(_ message: String, extra: [String: Any] = [:]) -> ([String: Any], Int32) {
    var d = extra
    d["ok"] = false
    d["error"] = message
    return (d, 1)
}

func failJSON(_ message: String, extra: [String: Any] = [:]) -> Never {
    var d = extra
    d["ok"] = false
    d["error"] = message
    emitJSON(d, exitCode: 1)
}

// MARK: - CoreAudio Built-in Mic Discovery

func findBuiltInInputDevice() -> (id: AudioDeviceID, name: String)? {
    var propSize: UInt32 = 0
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDevices,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &propSize) == noErr else {
        return nil
    }
    let count = Int(propSize) / MemoryLayout<AudioDeviceID>.size
    var devices = [AudioDeviceID](repeating: 0, count: count)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &propSize, &devices) == noErr else {
        return nil
    }

    for dev in devices {
        var transport: UInt32 = 0
        var tSize = UInt32(MemoryLayout<UInt32>.size)
        var tAddr = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyTransportType,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        guard AudioObjectGetPropertyData(dev, &tAddr, 0, nil, &tSize, &transport) == noErr else { continue }
        if transport != kAudioDeviceTransportTypeBuiltIn { continue }

        var inSize: UInt32 = 0
        var inAddr = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyStreamConfiguration,
            mScope: kAudioObjectPropertyScopeInput,
            mElement: kAudioObjectPropertyElementMain
        )
        guard AudioObjectGetPropertyDataSize(dev, &inAddr, 0, nil, &inSize) == noErr, inSize > 0 else { continue }
        let buf = UnsafeMutableRawPointer.allocate(byteCount: Int(inSize), alignment: MemoryLayout<AudioBufferList>.alignment)
        defer { buf.deallocate() }
        var inChannels: UInt32 = 0
        if AudioObjectGetPropertyData(dev, &inAddr, 0, nil, &inSize, buf) == noErr {
            let abl = buf.bindMemory(to: AudioBufferList.self, capacity: 1)
            for b in UnsafeMutableAudioBufferListPointer(abl) {
                inChannels += b.mNumberChannels
            }
        }
        guard inChannels > 0 else { continue }

        var rawName: Unmanaged<CFString>?
        var nSize = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        var nAddr = AudioObjectPropertyAddress(
            mSelector: kAudioObjectPropertyName,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        let name: String
        let status = withUnsafeMutablePointer(to: &rawName) { ptr in
            AudioObjectGetPropertyData(dev, &nAddr, 0, nil, &nSize, ptr)
        }
        if status == noErr, let cf = rawName?.takeRetainedValue() {
            name = cf as String
        } else {
            name = "Built-in Mic"
        }
        return (dev, name)
    }
    return nil
}

// MARK: - Accessibility Helpers

var lastAxManualPid: pid_t = 0

func findAntigravityApp() -> NSRunningApplication? {
    RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.002))
    return NSRunningApplication.runningApplications(withBundleIdentifier: "com.google.antigravity").first
        ?? NSWorkspace.shared.runningApplications.first { $0.bundleIdentifier == "com.google.antigravity" }
}

func axAppElement(_ app: NSRunningApplication) -> AXUIElement {
    let pid = app.processIdentifier
    let axApp = AXUIElementCreateApplication(pid)
    if pid != lastAxManualPid {
        AXUIElementSetAttributeValue(axApp, "AXManualAccessibility" as CFString, kCFBooleanTrue)
        lastAxManualPid = pid
    }
    AXUIElementSetMessagingTimeout(axApp, 1.5)
    return axApp
}

func isAppFrontmost(_ app: NSRunningApplication, _ axApp: AXUIElement) -> Bool {
    axBool(axApp, kAXFrontmostAttribute) ?? app.isActive
}

func axString(_ el: AXUIElement, _ attr: String) -> String {
    var v: AnyObject?
    guard AXUIElementCopyAttributeValue(el, attr as CFString, &v) == .success else { return "" }
    if let s = v as? String { return s }
    return ""
}

func axBool(_ el: AXUIElement, _ attr: String) -> Bool? {
    var v: AnyObject?
    guard AXUIElementCopyAttributeValue(el, attr as CFString, &v) == .success else { return nil }
    if let b = v as? Bool { return b }
    if let n = v as? NSNumber { return n.boolValue }
    return nil
}

func axChildren(_ el: AXUIElement) -> [AXUIElement] {
    var c: AnyObject?
    guard AXUIElementCopyAttributeValue(el, kAXChildrenAttribute as CFString, &c) == .success,
          let arr = c as? [AXUIElement] else { return [] }
    return arr
}

func axParent(_ el: AXUIElement) -> AXUIElement? {
    var p: AnyObject?
    guard AXUIElementCopyAttributeValue(el, kAXParentAttribute as CFString, &p) == .success,
          let pe = p else { return nil }
    return (pe as! AXUIElement)
}

func axWindows(_ axApp: AXUIElement) -> [AXUIElement] {
    var v: AnyObject?
    guard AXUIElementCopyAttributeValue(axApp, kAXWindowsAttribute as CFString, &v) == .success,
          let arr = v as? [AXUIElement] else { return [] }
    return arr
}

func axPress(_ el: AXUIElement) -> Bool {
    AXUIElementPerformAction(el, kAXPressAction as CFString) == .success
}

func axElementAttr(_ el: AXUIElement, _ attr: String) -> AXUIElement? {
    var v: AnyObject?
    guard AXUIElementCopyAttributeValue(el, attr as CFString, &v) == .success, let obj = v else {
        return nil
    }
    return (obj as! AXUIElement)
}

func axSearchChecked(_ root: AXUIElement, key: String? = nil, text: String? = nil, limit: Int = 20) -> (supported: Bool, results: [AXUIElement]) {
    var pred: [String: Any] = [
        "AXDirection": "AXDirectionNext",
        "AXImmediateDescendantsOnly": false,
        "AXResultsLimit": limit
    ]
    if let key = key { pred["AXSearchKey"] = key }
    if let text = text { pred["AXSearchText"] = text }
    var res: AnyObject?
    let err = AXUIElementCopyParameterizedAttributeValue(
        root,
        "AXUIElementsForSearchPredicate" as CFString,
        pred as CFDictionary,
        &res
    )
    guard err == .success else {
        return (false, [])
    }
    return (true, (res as? [AXUIElement]) ?? [])
}

func axSearch(_ root: AXUIElement, key: String? = nil, text: String? = nil, limit: Int = 20) -> [AXUIElement] {
    axSearchChecked(root, key: key, text: text, limit: limit).results
}

func findFirst(_ root: AXUIElement, maxNodes: Int = 5000, maxDepth: Int = 60, skipNestedWebAreas: Bool = true, where predicate: (AXUIElement) -> Bool) -> AXUIElement? {
    var queue: [(AXUIElement, Int)] = [(root, 0)]
    var idx = 0
    while idx < queue.count && idx < maxNodes {
        let (node, depth) = queue[idx]
        idx += 1
        if skipNestedWebAreas && depth > 0 && axString(node, kAXRoleAttribute) == "AXWebArea" {
            continue
        }
        if predicate(node) { return node }
        if depth < maxDepth {
            for child in axChildren(node) {
                queue.append((child, depth + 1))
            }
        }
    }
    return nil
}

func findAll(_ root: AXUIElement, maxNodes: Int = 5000, maxDepth: Int = 60, skipNestedWebAreas: Bool = true, where predicate: (AXUIElement) -> Bool) -> [AXUIElement] {
    var results: [AXUIElement] = []
    var queue: [(AXUIElement, Int)] = [(root, 0)]
    var idx = 0
    while idx < queue.count && idx < maxNodes {
        let (node, depth) = queue[idx]
        idx += 1
        if skipNestedWebAreas && depth > 0 && axString(node, kAXRoleAttribute) == "AXWebArea" {
            continue
        }
        if predicate(node) { results.append(node) }
        if depth < maxDepth {
            for child in axChildren(node) {
                queue.append((child, depth + 1))
            }
        }
    }
    return results
}

func findMainWebArea(_ window: AXUIElement) -> AXUIElement? {
    // Collect top-level AXWebArea nodes without descending into any WebArea DOM subtree.
    var webAreas: [AXUIElement] = []
    var queue: [(AXUIElement, Int)] = [(window, 0)]
    var idx = 0
    while idx < queue.count && idx < 600 {
        let (node, depth) = queue[idx]
        idx += 1
        let role = axString(node, kAXRoleAttribute)
        if role == "AXWebArea" {
            webAreas.append(node)
            continue
        }
        if depth < 15 {
            for child in axChildren(node) {
                queue.append((child, depth + 1))
            }
        }
    }

    // Pass 1: Fast title match avoids deep AX traversal across unrelated browser/sidecar tabs.
    for wa in webAreas {
        let title = axString(wa, kAXTitleAttribute)
        if title.hasSuffix(" - Antigravity") || title == "Antigravity" {
            return wa
        }
    }

    // Pass 2: Fast in-process Chromium AX predicate lookup, then bounded BFS fallback.
    for wa in webAreas {
        let title = axString(wa, kAXTitleAttribute)
        if title == "Device Activation" || title == "Voice Wake" { continue }
        let hasTextField = axSearch(wa, key: "AXTextFieldSearchKey", limit: 5).contains { el in
            axString(el, kAXRoleAttribute) == "AXTextArea" && axString(el, kAXDescriptionAttribute) == "Message input"
        }
        if hasTextField {
            return wa
        }
        let hasSidebar = axSearch(wa, key: "AXLinkSearchKey", text: "New Conversation", limit: 5).contains { el in
            axString(el, kAXRoleAttribute) == "AXLink" && axString(el, kAXDescriptionAttribute) == "New Conversation"
        }
        if hasSidebar {
            return wa
        }
        let hasComposerOrSidebar = findFirst(wa, maxNodes: 2500, maxDepth: 45, where: { el in
            let r = axString(el, kAXRoleAttribute)
            let d = axString(el, kAXDescriptionAttribute)
            return (r == "AXTextArea" && d == "Message input") || (r == "AXLink" && d == "New Conversation")
        }) != nil
        if hasComposerOrSidebar {
            return wa
        }
    }
    return nil
}

func axCenter(_ el: AXUIElement) -> CGPoint? {
    var pObj: AnyObject?
    var sObj: AnyObject?
    guard AXUIElementCopyAttributeValue(el, kAXPositionAttribute as CFString, &pObj) == .success,
          AXUIElementCopyAttributeValue(el, kAXSizeAttribute as CFString, &sObj) == .success else {
        return nil
    }
    var pt = CGPoint.zero
    var sz = CGSize.zero
    AXValueGetValue(pObj as! AXValue, .cgPoint, &pt)
    AXValueGetValue(sObj as! AXValue, .cgSize, &sz)
    guard sz.width > 0 && sz.height > 0 else { return nil }
    return CGPoint(x: pt.x + sz.width / 2.0, y: pt.y + sz.height / 2.0)
}

func findMainWindowAndWebArea(_ axApp: AXUIElement) -> (window: AXUIElement, webArea: AXUIElement)? {
    var checked: [AXUIElement] = []
    if let mainWin = axElementAttr(axApp, kAXMainWindowAttribute) {
        checked.append(mainWin)
        if let wa = findMainWebArea(mainWin) {
            return (mainWin, wa)
        }
    }
    if let focusedWin = axElementAttr(axApp, kAXFocusedWindowAttribute),
       !checked.contains(where: { CFEqual($0, focusedWin) }) {
        checked.append(focusedWin)
        if let wa = findMainWebArea(focusedWin) {
            return (focusedWin, wa)
        }
    }

    let wins = axWindows(axApp)
    // Prefer AXStandardWindow first, then any remaining window with a main AXWebArea
    let ordered = wins.sorted {
        (axString($0, kAXSubroleAttribute) == "AXStandardWindow" ? 0 : 1) <
        (axString($1, kAXSubroleAttribute) == "AXStandardWindow" ? 0 : 1)
    }
    for w in ordered {
        if checked.contains(where: { CFEqual($0, w) }) { continue }
        if let wa = findMainWebArea(w) {
            return (w, wa)
        }
    }
    return nil
}

func collectText(_ el: AXUIElement, depth: Int = 0) -> String {
    let direct = axString(el, kAXValueAttribute)
    if !direct.isEmpty { return direct }
    if depth > 12 { return "" }
    var parts: [String] = []
    for child in axChildren(el) {
        let role = axString(child, kAXRoleAttribute)
        if role == "AXStaticText" {
            let v = axString(child, kAXValueAttribute)
            let t = axString(child, kAXTitleAttribute)
            if !v.isEmpty { parts.append(v) }
            else if !t.isEmpty { parts.append(t) }
        } else {
            let sub = collectText(child, depth: depth + 1)
            if !sub.isEmpty { parts.append(sub) }
        }
    }
    return parts.joined(separator: "")
}

// MARK: - CGEvent Keyboard Helpers

func sendKeyCombo(keyCode: CGKeyCode, flags: CGEventFlags) {
    let src = CGEventSource(stateID: .hidSystemState)
    guard let down = CGEvent(keyboardEventSource: src, virtualKey: keyCode, keyDown: true),
          let up = CGEvent(keyboardEventSource: src, virtualKey: keyCode, keyDown: false) else { return }
    down.flags = flags
    up.flags = flags
    down.post(tap: .cghidEventTap)
    usleep(30_000)
    up.post(tap: .cghidEventTap)
}

func pasteString(_ text: String) {
    let pb = NSPasteboard.general
    let prev = pb.string(forType: .string)
    pb.clearContents()
    pb.setString(text, forType: .string)
    sendKeyCombo(keyCode: 9, flags: .maskCommand) // Cmd+V
    usleep(250_000)
    if let prev = prev {
        pb.clearContents()
        pb.setString(prev, forType: .string)
    }
}

// MARK: - Core Actions

func activateAntigravity() -> (app: NSRunningApplication, axApp: AXUIElement, window: AXUIElement, webArea: AXUIElement)? {
    guard let app = findAntigravityApp() else { return nil }
    let axApp = axAppElement(app)

    // Fast-path when Antigravity is already active and visible to avoid spawning /usr/bin/open.
    if isAppFrontmost(app, axApp) && !app.isHidden, let pair = findMainWindowAndWebArea(axApp) {
        if axBool(pair.window, kAXMinimizedAttribute) == true {
            AXUIElementSetAttributeValue(pair.window, kAXMinimizedAttribute as CFString, kCFBooleanFalse)
        }
        AXUIElementPerformAction(pair.window, kAXRaiseAction as CFString)
        return (app, axApp, pair.window, pair.webArea)
    }

    app.unhide()
    app.activate(options: [.activateAllWindows])
    usleep(150_000)
    if !isAppFrontmost(app, axApp) || findMainWindowAndWebArea(axApp) == nil {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        proc.arguments = ["-a", "Antigravity"]
        try? proc.run()
        proc.waitUntilExit()
    }

    let deadline = Date().addingTimeInterval(4.0)
    while Date() < deadline {
        if !isAppFrontmost(app, axApp) {
            app.activate(options: [.activateAllWindows])
        }
        for w in axWindows(axApp) where axString(w, kAXSubroleAttribute) == "AXStandardWindow" {
            if axBool(w, kAXMinimizedAttribute) == true {
                AXUIElementSetAttributeValue(w, kAXMinimizedAttribute as CFString, kCFBooleanFalse)
            }
            AXUIElementPerformAction(w, kAXRaiseAction as CFString)
        }
        if let pair = findMainWindowAndWebArea(axApp) {
            if axBool(pair.window, kAXMinimizedAttribute) == true {
                AXUIElementSetAttributeValue(pair.window, kAXMinimizedAttribute as CFString, kCFBooleanFalse)
            }
            AXUIElementPerformAction(pair.window, kAXRaiseAction as CFString)
            return (app, axApp, pair.window, pair.webArea)
        }
        usleep(120_000)
    }
    return nil
}

func isModelSelector(_ el: AXUIElement) -> Bool {
    guard axString(el, kAXRoleAttribute) == "AXPopUpButton" else { return false }
    let d = axString(el, kAXDescriptionAttribute)
    return d.hasPrefix("Select model") || d.hasPrefix("Model:")
}

struct ComposerHandles {
    var msgInput: AXUIElement?
    var modelBtn: AXUIElement?
    var idleBtn: AXUIElement?
    var activeBtn: AXUIElement?
    var sendBtn: AXUIElement?
    var cancelBtn: AXUIElement?
}

func findMessageInput(_ axApp: AXUIElement?, _ webArea: AXUIElement) -> AXUIElement? {
    if let appEl = axApp,
       let focused = axElementAttr(appEl, kAXFocusedUIElementAttribute),
       axString(focused, kAXRoleAttribute) == "AXTextArea",
       axString(focused, kAXDescriptionAttribute) == "Message input" {
        return focused
    }
    let keySearch = axSearchChecked(webArea, key: "AXTextFieldSearchKey", limit: 10)
    if let hit = keySearch.results.first(where: {
        axString($0, kAXRoleAttribute) == "AXTextArea" && axString($0, kAXDescriptionAttribute) == "Message input"
    }) {
        return hit
    }
    let textSearch = axSearchChecked(webArea, text: "Message input", limit: 10)
    if let hit = textSearch.results.first(where: {
        axString($0, kAXRoleAttribute) == "AXTextArea" && axString($0, kAXDescriptionAttribute) == "Message input"
    }) {
        return hit
    }
    // Only fall back to BFS if Chromium's in-process AX search predicate is unavailable.
    if keySearch.supported || textSearch.supported {
        return nil
    }
    return findFirst(webArea, maxNodes: 2500, maxDepth: 50) { el in
        axString(el, kAXRoleAttribute) == "AXTextArea" && axString(el, kAXDescriptionAttribute) == "Message input"
    }
}

func getComposerHandles(_ axApp: AXUIElement?, _ webArea: AXUIElement) -> ComposerHandles {
    var h = ComposerHandles()

    func inspectControl(_ node: AXUIElement) {
        let r = axString(node, kAXRoleAttribute)
        if r == "AXPopUpButton" {
            if h.modelBtn == nil {
                let desc = axString(node, kAXDescriptionAttribute)
                if desc.hasPrefix("Select model") || desc.hasPrefix("Model:") {
                    h.modelBtn = node
                }
            }
        } else if r == "AXButton" {
            let dRaw = axString(node, kAXDescriptionAttribute)
            if h.idleBtn == nil && dRaw == "Record voice memo" {
                h.idleBtn = node
            }
            let d = dRaw.lowercased()
            let t = axString(node, kAXTitleAttribute).lowercased()
            let s = "\(d) \(t)"
            let isStopRec = (d == "stop recording" || s.contains("stop recording") || s.contains("stop dictation") || s.contains("stop voice memo"))
            if h.activeBtn == nil && isStopRec {
                h.activeBtn = node
            }
            if h.sendBtn == nil && (d == "send message" || d == "send" || t == "send" || d.contains("send prompt")) {
                if axBool(node, kAXEnabledAttribute) ?? true {
                    h.sendBtn = node
                }
            }
            if !isStopRec && h.cancelBtn == nil && (s.contains("cancel") || s.contains("stop")) {
                h.cancelBtn = node
            }
        }
    }

    h.msgInput = findMessageInput(axApp, webArea)
    if let mi = h.msgInput {
        var scope: AXUIElement = mi
        for _ in 1...4 {
            if let p = axParent(scope) {
                scope = p
            }
        }
        let controls = axSearch(scope, key: "AXControlSearchKey", limit: 40)
        if !controls.isEmpty {
            for ctrl in controls {
                inspectControl(ctrl)
            }
        } else {
            var q: [(AXUIElement, Int)] = [(scope, 0)]
            var i = 0
            while i < q.count && i < 200 {
                let (node, d) = q[i]
                i += 1
                let r = axString(node, kAXRoleAttribute)
                if d > 0 && r == "AXWebArea" { continue }
                if r == "AXPopUpButton" || r == "AXButton" {
                    inspectControl(node)
                }
                if d < 10 {
                    for child in axChildren(node) {
                        q.append((child, d + 1))
                    }
                }
            }
        }
        return h
    }

    // Fallback when Message input is absent: targeted predicates without full-tree BFS
    for el in axSearch(webArea, key: "AXControlSearchKey", text: "Model", limit: 10) {
        inspectControl(el)
    }
    if h.modelBtn == nil {
        for el in axSearch(webArea, key: "AXControlSearchKey", text: "Select model", limit: 10) {
            inspectControl(el)
        }
    }
    for el in axSearch(webArea, key: "AXButtonSearchKey", text: "Record voice memo", limit: 5) {
        inspectControl(el)
    }
    for el in axSearch(webArea, key: "AXButtonSearchKey", text: "Stop", limit: 10) {
        let r = axString(el, kAXRoleAttribute)
        if r == "AXButton" {
            let d = axString(el, kAXDescriptionAttribute).lowercased()
            let t = axString(el, kAXTitleAttribute).lowercased()
            let s = "\(d) \(t)"
            if h.activeBtn == nil && (d == "stop recording" || s.contains("stop recording") || s.contains("stop dictation") || s.contains("stop voice memo")) {
                h.activeBtn = el
            }
        }
    }
    for el in axSearch(webArea, key: "AXButtonSearchKey", text: "Send", limit: 5) {
        inspectControl(el)
    }
    return h
}

func getComposerState(_ app: NSRunningApplication, _ axApp: AXUIElement, _ webArea: AXUIElement) -> [String: Any] {
    let h = getComposerHandles(axApp, webArea)
    let text = h.msgInput.map { collectText($0) } ?? ""
    let modelDesc = h.modelBtn.map { axString($0, kAXDescriptionAttribute) } ?? ""
    let recActive = (h.activeBtn != nil)
    let dictDesc = h.activeBtn.map { axString($0, kAXDescriptionAttribute) } ?? (h.idleBtn.map { axString($0, kAXDescriptionAttribute) } ?? "")

    return [
        "ok": true,
        "antigravity_active": isAppFrontmost(app, axApp),
        "message_input_found": h.msgInput != nil,
        "text": text,
        "char_count": text.count,
        "recording_active": recActive,
        "dictation_button_desc": dictDesc,
        "send_button_present": h.sendBtn != nil,
        "model_desc": modelDesc
    ]
}

// MARK: - Subcommands

func runBuiltInMic() -> ([String: Any], Int32) {
    let mic = findBuiltInInputDevice()
    return ([
        "ok": mic != nil,
        "builtin_mic_id": mic?.id ?? 0,
        "builtin_mic_name": mic?.name ?? ""
    ], (mic != nil) ? 0 : 1)
}

func runQuickCheck() -> ([String: Any], Int32) {
    let trusted = AXIsProcessTrusted()
    let mic = findBuiltInInputDevice()
    let app = findAntigravityApp()
    return ([
        "ok": trusted && (mic != nil),
        "ax_trusted": trusted,
        "antigravity_running": app != nil,
        "antigravity_pid": app?.processIdentifier ?? 0,
        "builtin_mic_id": mic?.id ?? 0,
        "builtin_mic_name": mic?.name ?? ""
    ], (trusted && (mic != nil)) ? 0 : 1)
}

func runCheck() -> ([String: Any], Int32) {
    let trusted = AXIsProcessTrusted()
    let mic = findBuiltInInputDevice()
    guard let app = findAntigravityApp() else {
        return ([
            "ok": false,
            "ax_trusted": trusted,
            "antigravity_running": false,
            "builtin_mic_id": mic?.id ?? 0,
            "builtin_mic_name": mic?.name ?? "",
            "error": "antigravity_not_running"
        ], 1)
    }
    let axApp = axAppElement(app)
    let wins = axWindows(axApp)
    var msgVisible = false
    var modelDesc = ""
    if let pair = findMainWindowAndWebArea(axApp) {
        let state = getComposerState(app, axApp, pair.webArea)
        msgVisible = (state["message_input_found"] as? Bool) ?? false
        modelDesc = (state["model_desc"] as? String) ?? ""
    }
    return ([
        "ok": trusted && (mic != nil),
        "ax_trusted": trusted,
        "antigravity_running": true,
        "antigravity_pid": app.processIdentifier,
        "antigravity_active": isAppFrontmost(app, axApp),
        "window_count": wins.count,
        "message_input_visible": msgVisible,
        "model_desc": modelDesc,
        "builtin_mic_id": mic?.id ?? 0,
        "builtin_mic_name": mic?.name ?? ""
    ], (trusted && mic != nil) ? 0 : 1)
}

func runActivate() -> ([String: Any], Int32) {
    guard AXIsProcessTrusted() else { return failDict("ax_not_trusted") }
    guard let (app, axApp, _, wa) = activateAntigravity() else {
        return failDict("antigravity_activate_failed")
    }
    return ([
        "ok": true,
        "pid": app.processIdentifier,
        "active": isAppFrontmost(app, axApp),
        "webarea_title": axString(wa, kAXTitleAttribute)
    ], 0)
}

func runPrepare(project: String, modelKeyword: String, effort: String) -> ([String: Any], Int32) {
    guard AXIsProcessTrusted() else { return failDict("ax_not_trusted") }
    guard let (_, axApp, window, initialWa) = activateAntigravity() else {
        return failDict("antigravity_activate_failed")
    }
    var wa = initialWa

    func findProjectButton(_ root: AXUIElement) -> AXUIElement? {
        if let hit = axSearch(root, key: "AXButtonSearchKey", text: project, limit: 30).first(where: {
            axString($0, kAXRoleAttribute) == "AXButton" && axString($0, kAXTitleAttribute) == project
        }) {
            return hit
        }
        return findFirst(root, maxNodes: 2500, maxDepth: 40) {
            axString($0, kAXRoleAttribute) == "AXButton" && axString($0, kAXTitleAttribute) == project
        }
    }

    // 1. Find project button in sidebar; toggle sidebar if hidden
    var projBtn = findProjectButton(wa)
    if projBtn == nil {
        let hasNewConvLink = axSearch(wa, key: "AXLinkSearchKey", text: "New Conversation", limit: 5).contains {
            axString($0, kAXRoleAttribute) == "AXLink" && axString($0, kAXDescriptionAttribute) == "New Conversation"
        } || findFirst(wa, maxNodes: 600, maxDepth: 25) {
            axString($0, kAXRoleAttribute) == "AXLink" && axString($0, kAXDescriptionAttribute) == "New Conversation"
        } != nil
        if !hasNewConvLink {
            let toggleBtn = axSearch(wa, key: "AXButtonSearchKey", text: "Toggle Sidebar", limit: 5).first(where: {
                axString($0, kAXRoleAttribute) == "AXButton" && axString($0, kAXDescriptionAttribute) == "Toggle Sidebar"
            }) ?? findFirst(wa, maxNodes: 400, maxDepth: 20) {
                axString($0, kAXRoleAttribute) == "AXButton" && axString($0, kAXDescriptionAttribute) == "Toggle Sidebar"
            }
            if let toggleBtn = toggleBtn {
                _ = axPress(toggleBtn)
                usleep(350_000)
                projBtn = findProjectButton(wa)
            }
        }
    }

    let prevTitle = axString(wa, kAXTitleAttribute)
    var openedViaSidebar = false
    if let btn = projBtn {
        var cur: AXUIElement? = btn
        for _ in 1...4 {
            if let p = cur.flatMap(axParent) {
                cur = p
                let newInProj = axSearch(p, key: "AXLinkSearchKey", text: "New Conversation in Project", limit: 5).first(where: {
                    axString($0, kAXRoleAttribute) == "AXLink" && axString($0, kAXDescriptionAttribute) == "New Conversation in Project"
                }) ?? findFirst(p, maxNodes: 80, maxDepth: 6) {
                    axString($0, kAXRoleAttribute) == "AXLink" && axString($0, kAXDescriptionAttribute) == "New Conversation in Project"
                }
                if let newInProj = newInProj, axPress(newInProj) {
                    openedViaSidebar = true
                    break
                }
            }
        }
    }

    if !openedViaSidebar {
        // Fallback: Cmd+Shift+K -> paste project name -> Enter
        sendKeyCombo(keyCode: 40, flags: [.maskCommand, .maskShift]) // K = 40
        usleep(350_000)
        pasteString(project)
        sendKeyCombo(keyCode: 36, flags: []) // Return
        usleep(450_000)
    }

    // 2. Wait up to 5.0s for new empty conversation in target project via Chromium AX search predicates
    var projectVerified = false
    var msgInput: AXUIElement?
    var modelBtn: AXUIElement?
    let waitStart = Date()
    let waitDeadline = waitStart.addingTimeInterval(5.0)
    while Date() < waitDeadline {
        let handles = getComposerHandles(axApp, wa)
        msgInput = handles.msgInput
        if handles.modelBtn != nil {
            modelBtn = handles.modelBtn
        }
        if msgInput == nil {
            wa = findMainWebArea(window) ?? wa
        } else {
            let emptyInput = collectText(msgInput!).isEmpty
            if emptyInput {
                let projectPickers = axSearch(wa, key: "AXControlSearchKey", text: "Select project", limit: 10).filter {
                    axString($0, kAXRoleAttribute) == "AXPopUpButton" && axString($0, kAXDescriptionAttribute).hasPrefix("Select project")
                }
                let matchingPicker = projectPickers.first {
                    axString($0, kAXDescriptionAttribute).localizedCaseInsensitiveContains(project)
                }
                let mismatchedPicker = (matchingPicker == nil) ? projectPickers.first : nil
                if matchingPicker != nil {
                    projectVerified = true
                    break
                } else if openedViaSidebar && mismatchedPicker == nil {
                    let curTitle = axString(wa, kAXTitleAttribute)
                    let mDesc = modelBtn.map { axString($0, kAXDescriptionAttribute) } ?? ""
                    let elapsed = Date().timeIntervalSince(waitStart)
                    if (curTitle != prevTitle && curTitle.localizedCaseInsensitiveContains(" - \(project) - ")) ||
                       (mDesc.hasPrefix("Select model") && elapsed >= 0.4) ||
                       elapsed >= 1.2 {
                        projectVerified = true
                        break
                    }
                }
            }
        }
        usleep(150_000)
    }

    // 3. Verify or select target model & thinking effort
    func extractCurrentModelLabel(_ desc: String) -> String {
        if let range = desc.range(of: "current: ", options: .caseInsensitive) {
            let tail = String(desc[range.upperBound...])
            return (tail.split(separator: ",").first.map(String.init) ?? tail).trimmingCharacters(in: .whitespacesAndNewlines)
        }
        if desc.hasPrefix("Model: ") {
            let tail = String(desc.dropFirst("Model: ".count))
            return (tail.split(separator: ",").first.map(String.init) ?? tail).trimmingCharacters(in: .whitespacesAndNewlines)
        }
        return desc.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func normalizeTargetEffort(_ raw: String) -> String {
        let low = raw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        switch low {
        case "", "default", "keep": return ""
        case "extra", "xhigh", "extra high", "extra_high", "extra-high": return "extra high"
        case "high": return "high"
        case "medium", "med": return "medium"
        case "low": return "low"
        default: return low
        }
    }

    func extractEffort(_ label: String) -> String {
        let low = label.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if low.contains("extra high") || low.contains("xhigh") || low.hasSuffix(" extra") || low == "extra" {
            return "extra high"
        }
        if low.contains("high") { return "high" }
        if low.contains("medium") { return "medium" }
        if low.contains("low") { return "low" }
        return ""
    }

    func modelMatches(_ desc: String, target: String) -> Bool {
        let t = target.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if t.isEmpty || t == "keep" { return true }
        let cur = extractCurrentModelLabel(desc).lowercased()
        if t.hasPrefix("orchestrator") {
            let words = t.split(whereSeparator: { $0.isWhitespace })
            return cur.hasPrefix("orchestrator") && words.allSatisfy { cur.contains($0) }
        }
        return !cur.hasPrefix("orchestrator") && cur.contains(t)
    }

    func modelAndEffortMatch(_ desc: String, targetModel: String, targetEffort: String) -> Bool {
        guard modelMatches(desc, target: targetModel) else { return false }
        let t = targetModel.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if t.isEmpty || t == "keep" { return true }
        let normEff = normalizeTargetEffort(targetEffort)
        if normEff.isEmpty { return true }
        return extractEffort(extractCurrentModelLabel(desc)) == normEff
    }

    func candidateLabel(_ el: AXUIElement) -> String {
        let t = axString(el, kAXTitleAttribute).trimmingCharacters(in: .whitespacesAndNewlines)
        if !t.isEmpty { return t }
        let d = axString(el, kAXDescriptionAttribute).trimmingCharacters(in: .whitespacesAndNewlines)
        if !d.isEmpty { return d }
        let texts = findAll(el, maxNodes: 20, maxDepth: 4) { axString($0, kAXRoleAttribute) == "AXStaticText" }
            .map {
                let v = axString($0, kAXValueAttribute).trimmingCharacters(in: .whitespacesAndNewlines)
                return v.isEmpty ? axString($0, kAXTitleAttribute).trimmingCharacters(in: .whitespacesAndNewlines) : v
            }
            .filter { !$0.isEmpty }
        return texts.joined(separator: " ")
    }

    let normEffort = normalizeTargetEffort(effort)
    let targetModelLow = modelKeyword.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
    if modelBtn == nil {
        modelBtn = getComposerHandles(axApp, wa).modelBtn
    }
    var modelDesc = modelBtn.map { axString($0, kAXDescriptionAttribute) } ?? ""
    if !modelAndEffortMatch(modelDesc, targetModel: modelKeyword, targetEffort: normEffort), let mBtn = modelBtn {
        _ = axPress(mBtn)
        usleep(400_000)
        // Search inside main webArea for target model menu item
        if let targetItem = findFirst(wa, maxNodes: 5000, maxDepth: 45, where: { el in
            if isModelSelector(el) { return false }
            let r = axString(el, kAXRoleAttribute)
            guard r == "AXMenuItem" || r == "AXButton" else { return false }
            let label = candidateLabel(el)
            guard !label.isEmpty else { return false }
            let labelLow = label.lowercased()
            if targetModelLow.hasPrefix("orchestrator") {
                let words = targetModelLow.split(whereSeparator: { $0.isWhitespace })
                return labelLow.hasPrefix("orchestrator") && words.allSatisfy { labelLow.contains($0) }
            }
            return !labelLow.hasPrefix("orchestrator") && labelLow.contains(targetModelLow)
        }) {
            if !normEffort.isEmpty && !targetModelLow.hasPrefix("orchestrator"), let pt = axCenter(targetItem) {
                CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: pt, mouseButton: .left)?.post(tap: .cghidEventTap)
                usleep(400_000)
                if let subMenu = findFirst(wa, maxNodes: 5000, maxDepth: 45, where: { axString($0, kAXRoleAttribute) == "AXMenu" }),
                   let effItem = findFirst(subMenu, maxNodes: 100, maxDepth: 6, where: { el in
                       guard axString(el, kAXRoleAttribute) == "AXMenuItem" else { return false }
                       let lbl = candidateLabel(el).trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
                       return lbl == normEffort || (normEffort == "extra high" && lbl == "extra")
                   }) {
                    _ = axPress(effItem)
                    usleep(350_000)
                } else {
                    _ = axPress(targetItem)
                    usleep(350_000)
                }
            } else {
                _ = axPress(targetItem)
                usleep(350_000)
            }
        }
        // Close menu if still open
        if findFirst(wa, maxNodes: 3000, maxDepth: 40, where: { axString($0, kAXRoleAttribute) == "AXMenu" }) != nil {
            sendKeyCombo(keyCode: 53, flags: []) // Escape
            usleep(150_000)
        }
        modelBtn = getComposerHandles(axApp, wa).modelBtn
        modelDesc = modelBtn.map { axString($0, kAXDescriptionAttribute) } ?? ""
    }

    // 4. Focus Message input
    var focused = false
    if let mi = msgInput {
        focused = axPress(mi)
        if !focused {
            focused = (AXUIElementSetAttributeValue(mi, kAXFocusedAttribute as CFString, kCFBooleanTrue) == .success)
        }
    }

    let modelOk = modelAndEffortMatch(modelDesc, targetModel: modelKeyword, targetEffort: normEffort)
    return ([
        "ok": projectVerified && modelOk,
        "opened_via_sidebar": openedViaSidebar,
        "project_verified": projectVerified,
        "model_desc": modelDesc,
        "focused": focused
    ], (projectVerified && modelOk) ? 0 : 1)
}

func runInspectComposer() -> ([String: Any], Int32) {
    guard AXIsProcessTrusted() else { return failDict("ax_not_trusted") }
    guard let app = findAntigravityApp() else { return failDict("antigravity_not_running") }
    let axApp = axAppElement(app)
    guard let pair = findMainWindowAndWebArea(axApp) else {
        return failDict("no_webarea")
    }
    return (getComposerState(app, axApp, pair.webArea), 0)
}

func runStartDictation() -> ([String: Any], Int32) {
    guard AXIsProcessTrusted() else { return failDict("ax_not_trusted") }
    guard let (app, axApp, _, wa) = activateAntigravity() else { return failDict("antigravity_not_running") }

    let initialHandles = getComposerHandles(axApp, wa)
    if let btn = initialHandles.idleBtn ?? axSearch(wa, key: "AXButtonSearchKey", text: "Record voice memo", limit: 5).first(where: {
        axString($0, kAXRoleAttribute) == "AXButton" && axString($0, kAXDescriptionAttribute) == "Record voice memo"
    }) {
        _ = axPress(btn)
    } else {
        sendKeyCombo(keyCode: 46, flags: .maskControl) // Ctrl+M
    }

    let startTs = Date()
    let deadline = startTs.addingTimeInterval(4.0)
    var retried = false
    var lastState: [String: Any] = [:]
    while Date() < deadline {
        lastState = getComposerState(app, axApp, wa)
        if (lastState["recording_active"] as? Bool) == true {
            return ([
                "ok": true,
                "recording_active": true,
                "dictation_button_desc": lastState["dictation_button_desc"] ?? ""
            ], 0)
        }
        if !retried && Date().timeIntervalSince(startTs) >= 1.6 {
            retried = true
            sendKeyCombo(keyCode: 46, flags: .maskControl) // Ctrl+M fallback retry
        }
        usleep(150_000)
    }
    return ([
        "ok": false,
        "recording_active": false,
        "dictation_button_desc": lastState["dictation_button_desc"] ?? "",
        "error": "dictation_start_timeout"
    ], 1)
}

func runStopDictation() -> ([String: Any], Int32) {
    guard AXIsProcessTrusted() else { return failDict("ax_not_trusted") }
    guard let app = findAntigravityApp() else { return failDict("antigravity_not_running") }
    let axApp = axAppElement(app)
    guard let pair = findMainWindowAndWebArea(axApp) else { return failDict("no_webarea") }
    let wa = pair.webArea

    let initialHandles = getComposerHandles(axApp, wa)
    if let activeBtn = initialHandles.activeBtn {
        if !axPress(activeBtn) {
            sendKeyCombo(keyCode: 46, flags: .maskControl) // Ctrl+M fallback if AXPress failed
        }
    }

    let deadline = Date().addingTimeInterval(3.0)
    while Date() < deadline {
        let state = getComposerState(app, axApp, wa)
        if (state["recording_active"] as? Bool) == false {
            return ([
                "ok": true,
                "recording_active": false,
                "text": state["text"] ?? ""
            ], 0)
        }
        usleep(150_000)
    }
    return (["ok": false, "recording_active": true, "error": "dictation_stop_timeout"], 1)
}

func runSubmit() -> ([String: Any], Int32) {
    guard AXIsProcessTrusted() else { return failDict("ax_not_trusted") }
    guard let (_, axApp, _, wa) = activateAntigravity() else { return failDict("antigravity_not_running") }

    let initialHandles = getComposerHandles(axApp, wa)
    if let sendBtn = initialHandles.sendBtn {
        _ = axPress(sendBtn)
    } else if let mi = initialHandles.msgInput {
        _ = AXUIElementSetAttributeValue(mi, kAXFocusedAttribute as CFString, kCFBooleanTrue)
        usleep(50_000)
        sendKeyCombo(keyCode: 36, flags: []) // Return
    } else {
        return failDict("composer_not_found")
    }

    let deadline = Date().addingTimeInterval(3.0)
    var remaining = ""
    while Date() < deadline {
        let h = getComposerHandles(axApp, wa)
        remaining = h.msgInput.map { collectText($0) } ?? ""
        if remaining.isEmpty || (h.sendBtn == nil && h.cancelBtn != nil) {
            return ([
                "ok": true,
                "submitted": true,
                "remaining_text": remaining
            ], 0)
        }
        usleep(150_000)
    }
    return ([
        "ok": false,
        "submitted": false,
        "remaining_text": remaining,
        "error": "submit_not_confirmed"
    ], 1)
}

func runDumpComposer() -> ([String: Any], Int32) {
    guard AXIsProcessTrusted() else { return failDict("ax_not_trusted") }
    guard let app = findAntigravityApp() else { return failDict("antigravity_not_running") }
    let axApp = axAppElement(app)
    guard let pair = findMainWindowAndWebArea(axApp) else { return failDict("no_webarea") }
    let wa = pair.webArea

    var items: [[String: String]] = []
    if let mi = findMessageInput(axApp, wa) {
        var ancestor: AXUIElement? = mi
        for _ in 1...4 {
            if let p = ancestor.flatMap(axParent) { ancestor = p }
        }
        if let root = ancestor {
            let els = findAll(root, maxNodes: 200, maxDepth: 10) { _ in true }
            for el in els {
                let r = axString(el, kAXRoleAttribute)
                let t = axString(el, kAXTitleAttribute)
                let d = axString(el, kAXDescriptionAttribute)
                let v = axString(el, kAXValueAttribute)
                let h = axString(el, kAXHelpAttribute)
                if !t.isEmpty || !d.isEmpty || !v.isEmpty || r == "AXButton" || r == "AXPopUpButton" || r == "AXTextArea" {
                    items.append(["role": r, "title": t, "desc": d, "value": v, "help": h])
                }
            }
        }
    }
    return (["ok": true, "composer_elements": items], 0)
}

// MARK: - Carbon Global Hotkey Support

let hotkeyKeyMap: [String: (code: UInt32, canonical: String)] = [
    "a": (UInt32(kVK_ANSI_A), "a"), "b": (UInt32(kVK_ANSI_B), "b"), "c": (UInt32(kVK_ANSI_C), "c"),
    "d": (UInt32(kVK_ANSI_D), "d"), "e": (UInt32(kVK_ANSI_E), "e"), "f": (UInt32(kVK_ANSI_F), "f"),
    "g": (UInt32(kVK_ANSI_G), "g"), "h": (UInt32(kVK_ANSI_H), "h"), "i": (UInt32(kVK_ANSI_I), "i"),
    "j": (UInt32(kVK_ANSI_J), "j"), "k": (UInt32(kVK_ANSI_K), "k"), "l": (UInt32(kVK_ANSI_L), "l"),
    "m": (UInt32(kVK_ANSI_M), "m"), "n": (UInt32(kVK_ANSI_N), "n"), "o": (UInt32(kVK_ANSI_O), "o"),
    "p": (UInt32(kVK_ANSI_P), "p"), "q": (UInt32(kVK_ANSI_Q), "q"), "r": (UInt32(kVK_ANSI_R), "r"),
    "s": (UInt32(kVK_ANSI_S), "s"), "t": (UInt32(kVK_ANSI_T), "t"), "u": (UInt32(kVK_ANSI_U), "u"),
    "v": (UInt32(kVK_ANSI_V), "v"), "w": (UInt32(kVK_ANSI_W), "w"), "x": (UInt32(kVK_ANSI_X), "x"),
    "y": (UInt32(kVK_ANSI_Y), "y"), "z": (UInt32(kVK_ANSI_Z), "z"),
    "0": (UInt32(kVK_ANSI_0), "0"), "1": (UInt32(kVK_ANSI_1), "1"), "2": (UInt32(kVK_ANSI_2), "2"),
    "3": (UInt32(kVK_ANSI_3), "3"), "4": (UInt32(kVK_ANSI_4), "4"), "5": (UInt32(kVK_ANSI_5), "5"),
    "6": (UInt32(kVK_ANSI_6), "6"), "7": (UInt32(kVK_ANSI_7), "7"), "8": (UInt32(kVK_ANSI_8), "8"),
    "9": (UInt32(kVK_ANSI_9), "9"),
    "space": (UInt32(kVK_Space), "space"),
    "return": (UInt32(kVK_Return), "return"),
    "enter": (UInt32(kVK_Return), "return"),
    "tab": (UInt32(kVK_Tab), "tab"),
    "f1": (UInt32(kVK_F1), "f1"), "f2": (UInt32(kVK_F2), "f2"), "f3": (UInt32(kVK_F3), "f3"),
    "f4": (UInt32(kVK_F4), "f4"), "f5": (UInt32(kVK_F5), "f5"), "f6": (UInt32(kVK_F6), "f6"),
    "f7": (UInt32(kVK_F7), "f7"), "f8": (UInt32(kVK_F8), "f8"), "f9": (UInt32(kVK_F9), "f9"),
    "f10": (UInt32(kVK_F10), "f10"), "f11": (UInt32(kVK_F11), "f11"), "f12": (UInt32(kVK_F12), "f12"),
    "`": (UInt32(kVK_ANSI_Grave), "`"), "backquote": (UInt32(kVK_ANSI_Grave), "`"), "grave": (UInt32(kVK_ANSI_Grave), "`"),
    "-": (UInt32(kVK_ANSI_Minus), "-"), "minus": (UInt32(kVK_ANSI_Minus), "-"),
    "=": (UInt32(kVK_ANSI_Equal), "="), "equal": (UInt32(kVK_ANSI_Equal), "="),
    "[": (UInt32(kVK_ANSI_LeftBracket), "["), "lbracket": (UInt32(kVK_ANSI_LeftBracket), "["),
    "]": (UInt32(kVK_ANSI_RightBracket), "]"), "rbracket": (UInt32(kVK_ANSI_RightBracket), "]"),
    "\\": (UInt32(kVK_ANSI_Backslash), "\\"), "backslash": (UInt32(kVK_ANSI_Backslash), "\\"),
    ";": (UInt32(kVK_ANSI_Semicolon), ";"), "semicolon": (UInt32(kVK_ANSI_Semicolon), ";"),
    "'": (UInt32(kVK_ANSI_Quote), "'"), "quote": (UInt32(kVK_ANSI_Quote), "'"),
    ",": (UInt32(kVK_ANSI_Comma), ","), "comma": (UInt32(kVK_ANSI_Comma), ","),
    ".": (UInt32(kVK_ANSI_Period), "."), "period": (UInt32(kVK_ANSI_Period), "."),
    "/": (UInt32(kVK_ANSI_Slash), "/"), "slash": (UInt32(kVK_ANSI_Slash), "/"),
]

func parseHotkey(_ spec: String) -> (keyCode: UInt32, modifiers: UInt32, normalized: String)? {
    let parts = spec.split(separator: "+").map { $0.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() }.filter { !$0.isEmpty }
    guard !parts.isEmpty else { return nil }
    var mods: UInt32 = 0
    var hasCtrl = false, hasOpt = false, hasShift = false, hasCmd = false
    var keyEntry: (code: UInt32, canonical: String)?

    for part in parts {
        switch part {
        case "ctrl", "control":
            mods |= UInt32(controlKey); hasCtrl = true
        case "opt", "option", "alt":
            mods |= UInt32(optionKey); hasOpt = true
        case "shift":
            mods |= UInt32(shiftKey); hasShift = true
        case "cmd", "command", "meta":
            mods |= UInt32(cmdKey); hasCmd = true
        default:
            guard keyEntry == nil, let mapped = hotkeyKeyMap[part] else { return nil }
            keyEntry = mapped
        }
    }
    guard let key = keyEntry else { return nil }
    let isFn = key.canonical.hasPrefix("f") && Int(key.canonical.dropFirst()) != nil
    if mods == 0 && !isFn { return nil }

    var normParts: [String] = []
    if hasCtrl { normParts.append("ctrl") }
    if hasOpt { normParts.append("option") }
    if hasShift { normParts.append("shift") }
    if hasCmd { normParts.append("cmd") }
    normParts.append(key.canonical)
    return (key.code, mods, normParts.joined(separator: "+"))
}

func emitLineJSON(_ dict: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: dict, options: [.sortedKeys]),
       let str = String(data: data, encoding: .utf8) {
        print(str)
        fflush(stdout)
    }
}

var activeHotKeyRef: EventHotKeyRef?
var activeNormalizedHotkey: String = ""

func runValidateHotkey(_ combo: String) -> ([String: Any], Int32) {
    guard let parsed = parseHotkey(combo) else {
        return failDict("invalid_hotkey", extra: ["hotkey": combo])
    }
    var ref: EventHotKeyRef?
    let hotKeyID = EventHotKeyID(signature: OSType(0x56574B59), id: 1)
    let status = RegisterEventHotKey(parsed.keyCode, parsed.modifiers, hotKeyID, GetApplicationEventTarget(), 0, &ref)
    if status == noErr, let ref = ref {
        UnregisterEventHotKey(ref)
        return (["ok": true, "hotkey": parsed.normalized], 0)
    }
    return failDict("register_failed", extra: ["hotkey": parsed.normalized, "status": Int(status)])
}

func cmdWatchHotkey(_ combo: String) -> Never {
    guard let parsed = parseHotkey(combo) else {
        failJSON("invalid_hotkey", extra: ["hotkey": combo])
    }
    activeNormalizedHotkey = parsed.normalized
    let app = NSApplication.shared
    app.setActivationPolicy(.prohibited)

    var eventSpec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
    var handlerRef: EventHandlerRef?
    let installStatus = InstallEventHandler(GetApplicationEventTarget(), { _, _, _ -> OSStatus in
        emitLineJSON(["event": "hotkey_pressed", "hotkey": activeNormalizedHotkey])
        return noErr
    }, 1, &eventSpec, nil, &handlerRef)
    if installStatus != noErr {
        failJSON("install_handler_failed", extra: ["status": Int(installStatus)])
    }

    let hotKeyID = EventHotKeyID(signature: OSType(0x56574B59), id: 1)
    let regStatus = RegisterEventHotKey(parsed.keyCode, parsed.modifiers, hotKeyID, GetApplicationEventTarget(), 0, &activeHotKeyRef)
    if regStatus != noErr || activeHotKeyRef == nil {
        failJSON("register_failed", extra: ["hotkey": parsed.normalized, "status": Int(regStatus)])
    }

    emitLineJSON(["ok": true, "event": "hotkey_registered", "hotkey": parsed.normalized])

    // Exit cleanly if parent closes stdin or sends SIGTERM/SIGINT
    DispatchQueue.global(qos: .utility).async {
        var byte: UInt8 = 0
        while read(STDIN_FILENO, &byte, 1) > 0 {}
        if let ref = activeHotKeyRef { UnregisterEventHotKey(ref) }
        exit(0)
    }

    signal(SIGTERM, SIG_IGN)
    signal(SIGINT, SIG_IGN)
    let termSrc = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
    termSrc.setEventHandler {
        if let ref = activeHotKeyRef { UnregisterEventHotKey(ref) }
        exit(0)
    }
    termSrc.resume()
    let intSrc = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
    intSrc.setEventHandler {
        if let ref = activeHotKeyRef { UnregisterEventHotKey(ref) }
        exit(0)
    }
    intSrc.resume()

    app.run()
    exit(0)
}

// MARK: - Persistent Stdio Worker & CLI Dispatch

func argValue(in argv: [String], _ flag: String, defaultVal: String) -> String {
    if let idx = argv.firstIndex(of: flag), idx + 1 < argv.count {
        return argv[idx + 1]
    }
    return defaultVal
}

func dispatchCommand(_ argv: [String]) -> ([String: Any], Int32) {
    guard let cmd = argv.first else {
        return failDict("missing_command")
    }
    switch cmd {
    case "builtin-mic":
        return runBuiltInMic()
    case "quick-check":
        return runQuickCheck()
    case "check":
        return runCheck()
    case "activate":
        return runActivate()
    case "prepare-code-orchestrator":
        return runPrepare(
            project: argValue(in: argv, "--project", defaultVal: "Code"),
            modelKeyword: argValue(in: argv, "--model", defaultVal: "Orchestrator"),
            effort: argValue(in: argv, "--effort", defaultVal: "")
        )
    case "inspect-composer":
        return runInspectComposer()
    case "start-dictation":
        return runStartDictation()
    case "stop-dictation":
        return runStopDictation()
    case "submit":
        return runSubmit()
    case "dump-composer":
        return runDumpComposer()
    case "validate-hotkey":
        return runValidateHotkey(argValue(in: argv, "--hotkey", defaultVal: ""))
    default:
        return failDict("unknown_command: \(cmd)")
    }
}

func cmdServe() -> Never {
    emitLineJSON(["ok": true, "event": "helper_ready"])
    while let line = readLine(strippingNewline: true) {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { continue }
        guard let data = trimmed.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let cmdArgs = obj["args"] as? [String], !cmdArgs.isEmpty else {
            emitLineJSON(["ok": false, "error": "invalid_request"])
            continue
        }
        if cmdArgs[0] == "quit" {
            emitLineJSON(["ok": true, "event": "helper_bye"])
            exit(0)
        }
        let (result, _) = autoreleasepool {
            dispatchCommand(cmdArgs)
        }
        emitLineJSON(result)
    }
    exit(0)
}

let args = CommandLine.arguments
guard args.count >= 2 else {
    failJSON("usage: agy-ax-helper <builtin-mic|quick-check|check|activate|prepare-code-orchestrator|inspect-composer|start-dictation|stop-dictation|submit|dump-composer|validate-hotkey|watch-hotkey|serve>")
}

let subArgs = Array(args.dropFirst())
switch args[1] {
case "serve":
    cmdServe()
case "watch-hotkey":
    cmdWatchHotkey(argValue(in: subArgs, "--hotkey", defaultVal: ""))
default:
    let (res, code) = dispatchCommand(subArgs)
    emitJSON(res, exitCode: code)
}
