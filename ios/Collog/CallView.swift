import SwiftUI

// 발신과 수신이 같은 화면을 쓴다. 오늘의 질문은 연결 대기 중 낭독되고 통화 중에는
// 참고용으로 계속 보인다.
struct CallView: View {
    let initialCall: ActiveCall
    @ObservedObject private var callCenter = VoipCallCenter.shared

    // fullScreenCover의 item은 동일 id 안에서 값이 바뀌어도 최초 snapshot일 수 있다.
    // 현재 callCenter 상태를 다시 읽어 connecting/ringing/active 전환이 화면에 반영되게 한다.
    private var call: ActiveCall {
        guard let current = callCenter.activeCall, current.id == initialCall.id else {
            return initialCall
        }
        return current
    }

    var body: some View {
        VStack(spacing: 24) {
            VStack(spacing: 8) {
                Text(call.peerName)
                    .font(.largeTitle.bold())
                Text(call.statusText)
                    .foregroundStyle(.secondary)
            }
            .padding(.top, 48)

            if let notice = call.notice {
                Text(notice)
                    .font(.footnote)
                    .multilineTextAlignment(.center)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal)
            }

            if !call.questions.isEmpty {
                VStack(alignment: .leading, spacing: 12) {
                    HStack {
                        Text("오늘의 질문")
                            .font(.headline)
                        Spacer()
                        if let first = call.questions.first {
                            Label(
                                first.usesRemoteTTS ? "ElevenLabs 음성" : "iOS 폴백 음성",
                                systemImage: first.usesRemoteTTS ? "waveform.badge.mic" : "iphone"
                            )
                            .font(.caption.bold())
                            .foregroundStyle(first.usesRemoteTTS ? .green : .orange)
                        }
                    }
                    ForEach(call.questions) { question in
                        HStack(alignment: .top, spacing: 10) {
                            Image(systemName: "quote.opening")
                                .foregroundStyle(.tint)
                            Text(question.text)
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding()
                .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 16))
                .padding(.horizontal)
            }

            Spacer()

            Text("오디오 출력: \(callCenter.audioOutputLabel)")
                .font(.footnote)
                .foregroundStyle(.secondary)

            Button {
                callCenter.toggleSpeaker()
            } label: {
                Label(
                    callCenter.isSpeakerEnabled ? "스피커폰 끄기" : "스피커폰 켜기",
                    systemImage: callCenter.isSpeakerEnabled
                        ? "speaker.wave.3.fill" : "speaker.wave.1"
                )
                .font(.title3.bold())
                .frame(maxWidth: .infinity)
                .padding(.vertical, 14)
            }
            .buttonStyle(.bordered)
            .tint(callCenter.isSpeakerEnabled ? .accentColor : .secondary)
            .padding(.horizontal)

            Button {
                callCenter.endActiveCall()
            } label: {
                Label("통화 종료", systemImage: "phone.down.fill")
                    .font(.title3.bold())
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 14)
            }
            .buttonStyle(.borderedProminent)
            .tint(.red)
            .padding(.horizontal)
            .padding(.bottom, 32)
        }
    }
}
