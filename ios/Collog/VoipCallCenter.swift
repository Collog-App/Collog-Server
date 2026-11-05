import AVFAudio
import CallKit
import Combine
import Foundation
import LiveKit
import PushKit
import UIKit

// 통화 화면이 보고 있는 상태. 발신/수신 양쪽이 같은 모델을 쓴다.
struct ActiveCall: Identifiable {
    enum Direction {
        case incoming
        case outgoing
    }

    enum Phase {
        case ringing      // 수신: 벨 울림 / 발신: 상대 응답 대기
        case connecting   // 서버·room 접속 중
        case active       // 양쪽 연결됨
    }

    let id: String
    let uuid: UUID
    let direction: Direction
    var peerName: String
    var phase: Phase
    var questions: [CollogAPI.Question] = []
    var notice: String?

    var statusText: String {
        switch (direction, phase) {
        case (.outgoing, .connecting): return "연결 중"
        case (.outgoing, .ringing): return "받을 때까지 기다리는 중"
        case (.incoming, .ringing): return "수신 전화"
        case (.incoming, .connecting): return "연결 중"
        case (_, .active): return "통화 중"
        }
    }
}

// PushKit VoIP push를 CallKit으로 연결하고, 수락/발신 시 backend와 LiveKit room을 잇는다.
@MainActor
final class VoipCallCenter: NSObject, ObservableObject {
    static let shared = VoipCallCenter()

    @Published private(set) var voipToken: String?
    @Published private(set) var apnsToken: String?
    @Published private(set) var activeCall: ActiveCall?
    @Published private(set) var events: [String] = []
    // 버튼은 요청값이 아닌 실제 출력 경로를 표시한다.
    @Published private(set) var isSpeakerEnabled = false
    @Published private(set) var audioOutputLabel = "연결 대기"

    private let registry = PKPushRegistry(queue: .main)
    private let callController = CXCallController()
    private let speaker = RingingQuestionSpeaker()
    private let room = Room()
    private let audioRouter = CallAudioRouter(session: SystemCallAudioSession())
    private var audioRouteObserver: AnyCancellable?
    private var audioVolumeObservation: NSKeyValueObservation?
    private lazy var provider: CXProvider = {
        let configuration = CXProviderConfiguration()
        configuration.supportsVideo = false
        configuration.maximumCallsPerCallGroup = 1
        configuration.maximumCallGroups = 1
        configuration.supportedHandleTypes = [.generic]
        return CXProvider(configuration: configuration)
    }()

    private var pendingOutgoing: (uuid: UUID, calleeId: String, name: String)?
    private var answeredCallIds: Set<String> = []
    private var pendingCapture: AudioCaptureOptions?
    private var analysisWriter: AnalysisPCMWriter?
    private var analysisTrack: LocalAudioTrack?
    private var rawCaptureRequired = false
    // CallKit의 didActivate는 /accept 응답보다 먼저 올 수 있다. 두 조건이 모두 갖춰진
    // 시점에 publish해야 하므로 어느 쪽이 늦든 같은 진입점을 다시 호출한다.
    private var isAudioSessionActive = false
    private var didPublishMicrophone = false

    func start() {
        // CallKit이 AVAudioSession의 소유자다. LiveKit 자동 설정을 끄고 엔진도 꺼둔 상태로
        // 시작해, 통화가 활성화되기 전에는 오디오 장치를 건드리지 않는다.
        AudioManager.shared.audioSession.isAutomaticConfigurationEnabled = false
        setEngine(.none)
        observeAudioSession()
        provider.setDelegate(self, queue: nil)
        room.add(delegate: self)
        registry.delegate = self
        registry.desiredPushTypes = [.voIP]
        UIApplication.shared.registerForRemoteNotifications()
        log("PushKit 등록 시작")
    }

    func setRemoteNotificationToken(_ deviceToken: Data) {
        apnsToken = deviceToken.hexString
        log("APNs 토큰 수신")
        registerDeviceIfPossible()
    }

    func registerDeviceIfPossible() {
        guard let apnsToken, let voipToken, CollogAPI.accessToken != nil else { return }
        Task {
            do {
                _ = try await CollogAPI.registerDevice(token: apnsToken, voipToken: voipToken)
                log("기기 등록 완료")
            } catch {
                log("기기 등록 실패: \(error.localizedDescription)")
            }
        }
    }

    func log(_ message: String) {
        events.insert(message, at: 0)
        events = Array(events.prefix(30))
        print("[Collog] \(message)")
    }

    // MARK: - 발신

    func startOutgoingCall(calleeId: String, name: String) {
        guard activeCall == nil else { return }
        let uuid = UUID()
        pendingOutgoing = (uuid, calleeId, name)
        let action = CXStartCallAction(call: uuid, handle: CXHandle(type: .generic, value: name))
        action.contactIdentifier = name
        callController.request(CXTransaction(action: action)) { error in
            guard let error else { return }
            Task { @MainActor [weak self] in
                self?.pendingOutgoing = nil
                self?.log("발신 실패: \(error.localizedDescription)")
            }
        }
    }

    // 화면의 종료 버튼도 CallKit을 거쳐야 시스템 통화 상태와 어긋나지 않는다.
    func endActiveCall() {
        guard let uuid = activeCall?.uuid else { return }
        callController.request(CXTransaction(action: CXEndCallAction(call: uuid))) { error in
            guard let error else { return }
            Task { @MainActor [weak self] in
                self?.log("종료 실패: \(error.localizedDescription)")
            }
        }
    }

    // MARK: - 미디어

    // room에는 먼저 접속하되 마이크는 publish하지 않는다. CallKit이 세션을 활성화한 뒤
    // didActivate에서 publish해야 오디오 라우팅이 어긋나지 않는다.
    private func connectMedia(
        url: String,
        token: String,
        roomName: String,
        constraints: CollogAPI.AudioConstraints
    ) async throws {
        // 기침과 발화 특성을 보존해야 하므로 서버가 AGC/NS를 꺼둔 값을 그대로 따른다.
        if constraints.autoGainControl || constraints.noiseSuppression {
            log("경고: 서버가 AGC/NS를 켜서 보냈다. 음향 분석 신뢰도가 떨어진다")
        }
        pendingCapture = AudioCaptureOptions(
            echoCancellation: constraints.echoCancellation,
            autoGainControl: constraints.autoGainControl,
            noiseSuppression: constraints.noiseSuppression,
            highpassFilter: false,
            typingNoiseDetection: false
        )
        try await room.connect(url: url, token: token)
        log("LiveKit 접속: room=\(roomName)")
        publishMicrophoneIfReady()
    }

    private func publishMicrophoneIfReady() {
        guard !didPublishMicrophone else { return }
        guard isAudioSessionActive else {
            log("마이크 publish 대기: 오디오 세션 미활성")
            return
        }
        guard let options = pendingCapture else {
            log("마이크 publish 대기: room 접속 전")
            return
        }
        didPublishMicrophone = true
        Task {
            do {
                let publication = try await room.localParticipant.setMicrophone(
                    enabled: true,
                    captureOptions: options
                )
                log("마이크 publish 완료")
                do {
                    try audioRouter.microphoneDidStart()
                } catch {
                    log("마이크 시작 후 출력 경로 복구 실패: \(error.localizedDescription)")
                }
                refreshAudioRoute(event: "microphone-published")
                attachAnalysisWriter(to: publication)
            } catch {
                didPublishMicrophone = false
                log("마이크 publish 실패: \(error.localizedDescription)")
            }
        }
    }

    // 서버가 요구할 때만 분석용 PCM을 남긴다. 같은 캡처 스트림을 관찰하므로 별도의
    // AVAudioEngine tap을 만들지 않는다.
    private func attachAnalysisWriter(to publication: LocalTrackPublication?) {
        guard analysisWriter == nil else { return }
        guard rawCaptureRequired else {
            log("분석 PCM 생략: 서버가 원본 캡처를 요구하지 않음")
            return
        }
        let resolved = publication?.track
            ?? room.localParticipant.audioTracks.first?.track
        guard let track = resolved as? LocalAudioTrack else {
            log("분석 PCM 실패: local audio track을 찾지 못했다")
            return
        }
        let writer = AnalysisPCMWriter()
        do {
            try writer.start()
        } catch {
            log("분석 PCM 시작 실패: \(error.localizedDescription)")
            return
        }
        track.add(audioRenderer: writer)
        analysisWriter = writer
        analysisTrack = track
        log("분석 PCM 기록 시작")
    }

    // renderer를 먼저 떼고 파일을 닫은 뒤 업로드한다. 업로드가 끝나면 로컬 파일을 지운다.
    private func finishAnalysisRecording(callId: String) {
        guard let writer = analysisWriter else {
            log("분석 PCM 업로드 생략: 기록된 writer 없음")
            return
        }
        analysisWriter = nil
        analysisTrack?.remove(audioRenderer: writer)
        analysisTrack = nil
        let duration = writer.durationSeconds
        let levels = writer.levelSummary
        // 서버 게이트는 50ms 프레임 RMS의 분위수를 활성 레벨로 보고 임계값과 비교한다.
        log(
            String(
                format: "분석 PCM peak %.1f / rms %.1f / p75 %.1f / p90 %.1f dBFS (%d프레임)",
                levels.peak,
                levels.rms,
                levels.p75,
                levels.p90,
                levels.frames
            )
        )
        guard let fileURL = writer.finish(), duration > 0 else {
            writer.discard()
            log("분석 PCM 없음")
            return
        }
        Task {
            do {
                let upload = try await CollogAPI.rawAudioUploadUrl(
                    callId: callId,
                    durationSec: duration,
                    sampleRate: Int(AnalysisPCMWriter.sampleRate)
                )
                try await CollogAPI.uploadRawAudio(to: upload.uploadUrl, fileURL: fileURL)
                try await CollogAPI.rawAudioComplete(callId: callId, assetId: upload.assetId)
                log("분석 PCM 업로드 완료 (\(Int(duration))초)")
            } catch {
                log("분석 PCM 업로드 실패: \(error.localizedDescription)")
            }
            // 완료든 실패든 기기에 원본을 남기지 않는다.
            try? FileManager.default.removeItem(at: fileURL)
        }
    }

    private func teardown() {
        speaker.stop()
        audioRouter.deactivate()
        isAudioSessionActive = false
        isSpeakerEnabled = false
        audioOutputLabel = "연결 대기"
        if let callId = activeCall?.id {
            finishAnalysisRecording(callId: callId)
        } else {
            analysisTrack = nil
            analysisWriter?.discard()
            analysisWriter = nil
        }
        rawCaptureRequired = false
        didPublishMicrophone = false
        pendingCapture = nil
        pendingOutgoing = nil
        activeCall = nil
        Task {
            await room.disconnect()
            setEngine(.none)
            log("LiveKit 연결 해제")
        }
    }

    private func setEngine(_ availability: AudioEngineAvailability) {
        do {
            try AudioManager.shared.setEngineAvailability(availability)
        } catch {
            log("오디오 엔진 설정 실패: \(error.localizedDescription)")
        }
    }

    // 통화 중 스피커폰 전환. 부모가 손을 못 쓰거나 소리가 작을 때 쓴다.
    func toggleSpeaker() {
        guard isAudioSessionActive else { return }
        do {
            try audioRouter.toggleSpeaker()
        } catch {
            log("오디오 출력 전환 실패: \(error.localizedDescription)")
        }
        refreshAudioRoute(event: "speaker-button")
    }

    private func observeAudioSession() {
        guard audioRouteObserver == nil else { return }
        audioRouteObserver = NotificationCenter.default
            .publisher(for: AVAudioSession.routeChangeNotification)
            .receive(on: DispatchQueue.main)
            .sink { [weak self] notification in
                let reason = notification.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt
                MainActor.assumeIsolated {
                    self?.audioRouteChanged(reason: reason)
                }
            }
        audioVolumeObservation = AVAudioSession.sharedInstance()
            .observe(\.outputVolume, options: [.new]) { [weak self] _, _ in
                Task { @MainActor [weak self] in
                    guard let self, self.isAudioSessionActive else { return }
                    self.refreshAudioRoute(event: "volume-change")
                }
            }
    }

    private func audioRouteChanged(reason rawReason: UInt?) {
        guard isAudioSessionActive else { return }
        let reason = rawReason.flatMap(AVAudioSession.RouteChangeReason.init(rawValue:))
        // 자신의 setCategory/override 알림에서 재설정하면 순환할 수 있으므로,
        // 실제 기기의 연결·해제에서만 경로를 다시 선택한다.
        if reason == .newDeviceAvailable || reason == .oldDeviceUnavailable {
            do {
                try audioRouter.externalDeviceChanged()
            } catch {
                log("외부 오디오 경로 전환 실패: \(error.localizedDescription)")
            }
        }
        refreshAudioRoute(event: "route-change-\(rawReason ?? 0)")
    }

    private func refreshAudioRoute(event: String) {
        guard isAudioSessionActive else { return }
        let output = audioRouter.output
        isSpeakerEnabled = output == .speaker
        audioOutputLabel = output.label

        let session = AVAudioSession.sharedInstance()
        // UID/장치 이름 대신 포트 종류만 기록한다. AirPods 이름에 개인정보가 들어갈 수 있다.
        let inputs = session.currentRoute.inputs.map { $0.portType.rawValue }.joined(separator: ",")
        let outputs = session.currentRoute.outputs.map { $0.portType.rawValue }.joined(separator: ",")
        let processing = AudioManager.shared.audioProcessingState
        let platform = AudioManager.shared.platformVoiceProcessingState
        log(
            "오디오[\(event)] in=\(inputs) out=\(outputs) "
                + "volume=\(String(format: "%.2f", session.outputVolume)) "
                + "category=\(session.category.rawValue) mode=\(session.mode.rawValue) "
                + "options=\(session.categoryOptions.rawValue) sampleRate=\(Int(session.sampleRate)) "
                + "AEC=\(processing.echoCancellation.effective) "
                + "AGC=\(processing.autoGainControl.effective) NS=\(processing.noiseSuppression.effective) "
                + "VPIO=\(platform.voiceProcessingEnabled.isActive) "
                + "platformAGC=\(platform.voiceProcessingAGCEnabled.isActive)"
        )
    }

    // 초기 데모에서는 첫 질문만 읽고 나머지는 화면의 참고 질문으로 둔다. 재생 실패가
    // 통화 연결을 막아서는 안 된다.
    private func speakFirstQuestion(_ questions: [CollogAPI.Question]) {
        guard let first = questions.first else { return }
        // 라우팅은 CallKit 활성화와 기기 변경에서만 설정한다. 질문 재생이 Bluetooth
        // 선택을 덮어쓰거나 통화 세션을 다시 구성해서는 안 된다.
        let contract = first.usesRemoteTTS ? "ElevenLabs REMOTE_ASSET" : "iOS LOCAL fallback"
        log("질문 음성 계약: \(contract)")
        speaker.speak(first) { [weak self] message in
            self?.log(message)
        }
    }
}

// MARK: - PushKit

extension VoipCallCenter: PKPushRegistryDelegate {
    nonisolated func pushRegistry(
        _ registry: PKPushRegistry,
        didUpdate credentials: PKPushCredentials,
        for type: PKPushType
    ) {
        MainActor.assumeIsolated {
            voipToken = credentials.token.hexString
            log("VoIP 토큰 수신")
            registerDeviceIfPossible()
        }
    }

    nonisolated func pushRegistry(
        _ registry: PKPushRegistry,
        didInvalidatePushTokenFor type: PKPushType
    ) {
        MainActor.assumeIsolated {
            voipToken = nil
            log("VoIP 토큰 무효화")
        }
    }

    nonisolated func pushRegistry(
        _ registry: PKPushRegistry,
        didReceiveIncomingPushWith payload: PKPushPayload,
        for type: PKPushType,
        completion: @escaping () -> Void
    ) {
        MainActor.assumeIsolated {
            // iOS는 VoIP push를 받은 실행 안에서 반드시 reportNewIncomingCall을 호출하도록
            // 강제한다. 네트워크 요청은 사용자가 수락한 뒤에 한다.
            let call = payload.dictionaryPayload["call"] as? [String: Any] ?? [:]
            let uuid = (call["callUUID"] as? String).flatMap(UUID.init) ?? UUID()
            let callerName = call["callerName"] as? String ?? "콜록"

            guard let callId = call["callId"] as? String else {
                log("callId 없는 push 무시")
                reportAndImmediatelyEnd(uuid: uuid, completion: completion)
                return
            }
            if let expiresAt = call["expiresAt"] as? String,
               let expiry = Date.fromCollogISO8601(expiresAt),
               expiry < Date() {
                log("만료된 push 무시: \(callId)")
                reportAndImmediatelyEnd(uuid: uuid, completion: completion)
                return
            }

            activeCall = ActiveCall(
                id: callId,
                uuid: uuid,
                direction: .incoming,
                peerName: callerName,
                phase: .ringing
            )

            let update = CXCallUpdate()
            update.localizedCallerName = callerName
            update.remoteHandle = CXHandle(
                type: .generic,
                value: call["callerId"] as? String ?? "collog"
            )
            update.hasVideo = false
            update.supportsHolding = false
            update.supportsGrouping = false
            update.supportsUngrouping = false

            provider.reportNewIncomingCall(with: uuid, update: update) { [weak self] error in
                MainActor.assumeIsolated {
                    if let error {
                        self?.log("CallKit 보고 실패: \(error.localizedDescription)")
                        self?.activeCall = nil
                    } else {
                        self?.log("수신 통화 표시: \(callerName)")
                    }
                    completion()
                }
            }
        }
    }

    // CallKit에 보고하지 않고 completion만 호출하면 앱이 종료되므로, 무시할 push도 한 번
    // 보고한 뒤 즉시 종료한다.
    private func reportAndImmediatelyEnd(uuid: UUID, completion: @escaping () -> Void) {
        let update = CXCallUpdate()
        update.localizedCallerName = "콜록"
        provider.reportNewIncomingCall(with: uuid, update: update) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.provider.reportCall(with: uuid, endedAt: nil, reason: .unanswered)
                completion()
            }
        }
    }
}

// MARK: - CallKit

extension VoipCallCenter: CXProviderDelegate {
    nonisolated func providerDidReset(_ provider: CXProvider) {
        MainActor.assumeIsolated {
            teardown()
        }
    }

    nonisolated func provider(_ provider: CXProvider, perform action: CXStartCallAction) {
        MainActor.assumeIsolated {
            guard let pending = pendingOutgoing, pending.uuid == action.callUUID else {
                action.fail()
                return
            }
            // 수락과 같은 이유로 먼저 fulfill한다. CallKit action 안에서 네트워크를 기다리면
            // timeout으로 통화가 정리된다.
            action.fulfill()
            provider.reportOutgoingCall(with: pending.uuid, startedConnectingAt: nil)
            activeCall = ActiveCall(
                id: "",
                uuid: pending.uuid,
                direction: .outgoing,
                peerName: pending.name,
                phase: .connecting
            )
            Task {
                do {
                    let created = try await CollogAPI.createCall(calleeId: pending.calleeId)
                    log("발신 통화 생성: \(created.callId)")
                    activeCall = ActiveCall(
                        id: created.callId,
                        uuid: pending.uuid,
                        direction: .outgoing,
                        peerName: pending.name,
                        phase: .connecting,
                        questions: created.questions,
                        notice: created.recordingEnabled ? nil : created.recordingDisabledMessage
                    )
                    try await connectMedia(
                        url: created.livekitUrl,
                        token: created.accessToken,
                        roomName: created.roomName,
                        constraints: created.audioConstraints
                    )
                    activeCall?.phase = .ringing
                    speakFirstQuestion(created.questions)
                } catch {
                    log("발신 실패: \(error.localizedDescription)")
                    endActiveCall()
                }
            }
        }
    }

    nonisolated func provider(_ provider: CXProvider, perform action: CXAnswerCallAction) {
        MainActor.assumeIsolated {
            guard let call = activeCall, call.direction == .incoming else {
                action.fail()
                return
            }
            activeCall?.phase = .connecting
            answeredCallIds.insert(call.id)
            // CallKit action은 몇 초 안에 끝나야 한다. /accept 왕복과 LiveKit 접속을 기다린
            // 뒤에 fulfill하면 CallKit이 timeout으로 통화를 정리해 오디오 세션까지 내린다.
            // 수락 자체는 즉시 보고하고, 실제 연결은 뒤에서 진행한다.
            action.fulfill()
            Task {
                do {
                    let accepted = try await CollogAPI.accept(callId: call.id)
                    rawCaptureRequired = accepted.rawCaptureRequired
                    try await connectMedia(
                        url: accepted.livekitUrl,
                        token: accepted.accessToken,
                        roomName: accepted.roomName,
                        constraints: accepted.audioConstraints
                    )
                    activeCall?.phase = .active
                    log("수락 완료: \(call.id)")
                } catch {
                    // 이미 fulfill했으므로 실패는 통화 종료로 알린다.
                    log("수락 실패: \(error.localizedDescription)")
                    endActiveCall()
                }
            }
        }
    }

    nonisolated func provider(_ provider: CXProvider, perform action: CXEndCallAction) {
        MainActor.assumeIsolated {
            let call = activeCall
            action.fulfill()
            teardown()
            // 발신은 POST /calls 응답이 오기 전까지 통화 ID가 없다. 그 사이 종료하면
            // 서버에 보고할 대상이 없으므로 로컬 정리만 한다.
            guard let call, !call.id.isEmpty else { return }
            let answered = call.direction == .outgoing || answeredCallIds.contains(call.id)
            answeredCallIds.remove(call.id)
            Task {
                do {
                    // 수락 전 종료는 거절, 수락 뒤 또는 발신은 정상 종료다.
                    if answered {
                        try await CollogAPI.end(callId: call.id)
                    } else {
                        try await CollogAPI.decline(callId: call.id)
                    }
                } catch {
                    log("종료 보고 실패: \(error.localizedDescription)")
                }
            }
        }
    }

    nonisolated func provider(_ provider: CXProvider, didActivate session: AVAudioSession) {
        MainActor.assumeIsolated {
            isAudioSessionActive = true
            do {
                try audioRouter.activate()
            } catch {
                // 라우팅 실패와 CallKit 활성 상태는 별개다. 경로 변경/버튼/마이크
                // 시작에서 재시도할 수 있도록 엔진과 통화 진행은 유지한다.
                log("오디오 경로 초기화 실패: \(error.localizedDescription)")
            }
            setEngine(.default)
            refreshAudioRoute(event: "callkit-activated")
            log("오디오 세션 활성화")
            publishMicrophoneIfReady()
        }
    }

    nonisolated func provider(_ provider: CXProvider, didDeactivate session: AVAudioSession) {
        MainActor.assumeIsolated {
            audioRouter.deactivate()
            setEngine(.none)
            isAudioSessionActive = false
            isSpeakerEnabled = false
            audioOutputLabel = "연결 대기"
            log("오디오 세션 비활성화")
        }
    }
}

// MARK: - LiveKit

extension VoipCallCenter: RoomDelegate {
    nonisolated func room(
        _ room: Room,
        participant: RemoteParticipant,
        didSubscribeTrack publication: RemoteTrackPublication
    ) {
        guard let audio = publication.track as? RemoteAudioTrack else { return }
        let gain = audio.volume
        Task { @MainActor in
            guard self.activeCall != nil else { return }
            self.log("원격 음성 수신: playbackGain=\(gain)")
            self.refreshAudioRoute(event: "remote-audio-subscribed")
        }
    }

    nonisolated func room(_ room: Room, participantDidConnect participant: RemoteParticipant) {
        // RoomDelegate는 main thread를 보장하지 않는다.
        Task { @MainActor in
            guard let call = activeCall else { return }
            // 상대가 room에 들어왔다. 질문 낭독을 문장 중간이어도 즉시 멈춘다.
            speaker.stop(reason: "상대 연결로 질문 음성 즉시 중단")
            activeCall?.phase = .active
            if call.direction == .outgoing {
                provider.reportOutgoingCall(with: call.uuid, connectedAt: nil)
            }
            log("상대 참가: \(participant.identity?.stringValue ?? "-")")
        }
    }

    nonisolated func room(_ room: Room, participantDidDisconnect participant: RemoteParticipant) {
        Task { @MainActor in
            log("상대 퇴장")
            endActiveCall()
        }
    }
}

private extension Data {
    var hexString: String {
        map { String(format: "%02x", $0) }.joined()
    }
}

private extension Date {
    // 서버는 microsecond 정밀도 ISO-8601을 보낸다. 소수점 유무 양쪽을 모두 시도한다.
    static func fromCollogISO8601(_ value: String) -> Date? {
        let withFraction = ISO8601DateFormatter()
        withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = withFraction.date(from: value) { return date }
        return ISO8601DateFormatter().date(from: value)
    }
}
