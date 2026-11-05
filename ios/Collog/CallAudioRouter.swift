// Routing decisions are independent of CallKit/LiveKit so accessory changes and
// failures can be tested without relying on simulator audio hardware.
enum CallAudioOutput: Equatable {
    case speaker
    case receiver
    case external(String)
    case unavailable

    var isExternal: Bool {
        if case .external = self { return true }
        return false
    }

    var label: String {
        switch self {
        case .speaker: "스피커폰"
        case .receiver: "수화부"
        case .external(let name): name
        case .unavailable: "연결 대기"
        }
    }
}

@MainActor
protocol CallAudioSession: AnyObject {
    var output: CallAudioOutput { get }
    var externalInputUID: String? { get }
    func configure(defaultToSpeaker: Bool) throws
    func preferInput(uid: String?) throws
    func overrideSpeaker(_ enabled: Bool) throws
}

@MainActor
final class CallAudioRouter {
    private enum Preference { case automatic, speaker, receiver }
    private let session: any CallAudioSession
    private var preference = Preference.automatic
    private var isActive = false

    var output: CallAudioOutput { session.output }

    init(session: any CallAudioSession) {
        self.session = session
    }

    // Called only inside CallKit's didActivate window. Automatic means external
    // audio when available, otherwise a speaker default that survives route resets.
    func activate() throws {
        preference = .automatic
        isActive = true
        // CallKit activation remains valid even if an accessory disappears while
        // selecting it. A later route event, button or mic start can retry.
        try applyPreference()
    }

    func deactivate() {
        isActive = false
        preference = .automatic
    }

    func toggleSpeaker() throws {
        guard isActive else { return }
        let previous = preference
        if output == .speaker {
            preference = session.externalInputUID == nil ? .receiver : .automatic
        } else {
            preference = .speaker
        }
        do {
            try applyPreference()
        } catch {
            preference = previous
            throw error
        }
    }

    // Only physical accessory add/remove events call this. Configuration/override
    // notifications merely refresh the UI, preventing a route-change feedback loop.
    func externalDeviceChanged() throws {
        guard isActive else { return }
        preference = .automatic
        try applyPreference()
    }

    func microphoneDidStart() throws {
        guard isActive, !output.isExternal else { return }
        if preference == .receiver, output == .receiver { return }
        if preference != .receiver, output == .speaker { return }
        try applyPreference()
    }

    private func applyPreference() throws {
        // availableInputs depends on the configured category. Discover accessories
        // only after enabling playAndRecord/HFP. defaultToSpeaker is a fallback
        // and, unlike an explicit override, respects connected accessories.
        try session.configure(defaultToSpeaker: preference != .receiver)
        let input = preference == .automatic ? session.externalInputUID : nil
        try session.overrideSpeaker(false)
        try session.preferInput(uid: input)
        // .none releases an override; it does not force the receiver. The session's
        // default and preferred input determine the route in automatic mode.
        if preference == .speaker { try session.overrideSpeaker(true) }
    }
}

#if os(iOS)
import AVFAudio

@MainActor
final class SystemCallAudioSession: CallAudioSession {
    private let session = AVAudioSession.sharedInstance()

    var output: CallAudioOutput {
        let outputs = session.currentRoute.outputs
        // A mixed route is not reported as "speaker" while an accessory is in use.
        if let external = outputs.first(where: {
            $0.portType != .builtInSpeaker && $0.portType != .builtInReceiver
        }) {
            switch external.portType {
            case .bluetoothHFP, .bluetoothA2DP, .bluetoothLE: return .external("Bluetooth")
            case .headphones, .headsetMic: return .external("유선 이어폰")
            case .usbAudio: return .external("USB 오디오")
            default: return .external("외부 오디오")
            }
        }
        if outputs.contains(where: { $0.portType == .builtInSpeaker }) { return .speaker }
        if outputs.contains(where: { $0.portType == .builtInReceiver }) { return .receiver }
        return .unavailable
    }

    var externalInputUID: String? {
        let available = session.availableInputs ?? []
        let external = available.filter {
            [.headsetMic, .bluetoothHFP, .usbAudio, .lineIn].contains($0.portType)
        }
        // Preserve a selected accessory rather than hopping between paired devices.
        if let current = session.currentRoute.inputs.first,
           external.contains(where: { $0.uid == current.uid }) {
            return current.uid
        }
        return external.first?.uid
    }

    func configure(defaultToSpeaker: Bool) throws {
        var options: AVAudioSession.CategoryOptions = [.mixWithOthers, .allowBluetoothHFP]
        if defaultToSpeaker { options.insert(.defaultToSpeaker) }
        if session.category != .playAndRecord || session.mode != .voiceChat
            || session.categoryOptions != options {
            try session.setCategory(.playAndRecord, mode: .voiceChat, options: options)
        }
    }

    func preferInput(uid: String?) throws {
        let input = uid.flatMap { id in session.availableInputs?.first { $0.uid == id } }
        if uid != nil, input == nil { throw RoutingError.accessoryUnavailable }
        if session.preferredInput?.uid != input?.uid {
            try session.setPreferredInput(input)
        }
    }

    func overrideSpeaker(_ enabled: Bool) throws {
        try session.overrideOutputAudioPort(enabled ? .speaker : .none)
    }

    private enum RoutingError: Error { case accessoryUnavailable }
}
#endif
