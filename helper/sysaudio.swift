// Captures everything the Mac is playing (the other people in a meeting) and
// writes it to stdout as raw 16 kHz mono 16-bit PCM. Dictate reads the pipe.
//
// Build:  swiftc -O -swift-version 5 helper/sysaudio.swift -o helper/sysaudio
// Needs the Screen Recording permission (macOS asks on first run).
// Exits when stdin closes, so it can never outlive Dictate.

import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

let sampleRate = 16000

func log(_ message: String) {
    FileHandle.standardError.write(("[sysaudio] " + message + "\n").data(using: .utf8)!)
}

final class Capture: NSObject, SCStreamOutput, SCStreamDelegate {
    private var stream: SCStream?
    private let queue = DispatchQueue(label: "sysaudio.audio")
    private var announced = false

    func start() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(
            false, onScreenWindowsOnly: false)
        guard let display = content.displays.first else {
            log("no display found")
            exit(3)
        }
        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.sampleRate = sampleRate
        config.channelCount = 1
        config.excludesCurrentProcessAudio = true
        // Video is required by the API but unused: keep it as cheap as possible.
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)
        config.showsCursor = false

        let filter = SCContentFilter(display: display, excludingWindows: [])
        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        try stream.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
        try await stream.startCapture()
        self.stream = stream
        log("capturing")
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
                of type: SCStreamOutputType) {
        guard type == .audio, sampleBuffer.isValid,
              let description = sampleBuffer.formatDescription,
              let format = description.audioStreamBasicDescription else { return }
        if !announced {
            announced = true
            log("format rate=\(Int(format.mSampleRate)) channels=\(format.mChannelsPerFrame)")
        }
        try? sampleBuffer.withAudioBufferList { buffers, _ in
            // Float32, one buffer per channel: average the channels to mono.
            let channels = buffers.compactMap { buffer -> UnsafeBufferPointer<Float32>? in
                guard let data = buffer.mData else { return nil }
                return UnsafeBufferPointer(
                    start: data.assumingMemoryBound(to: Float32.self),
                    count: Int(buffer.mDataByteSize) / MemoryLayout<Float32>.size)
            }
            guard let frames = channels.map({ $0.count }).min(), frames > 0 else { return }
            var pcm = [Int16](repeating: 0, count: frames)
            let scale = 1.0 / Float32(channels.count)
            for i in 0..<frames {
                var sample: Float32 = 0
                for channel in channels { sample += channel[i] }
                pcm[i] = Int16(max(-1, min(1, sample * scale)) * 32767)
            }
            let written = pcm.withUnsafeBytes { fwrite($0.baseAddress, 1, $0.count, stdout) }
            fflush(stdout)
            if written == 0 { exit(0) }  // reader went away
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        log("stopped: \(error.localizedDescription)")
        exit(2)
    }
}

signal(SIGPIPE, SIG_IGN)

// Exit as soon as the parent closes our stdin (or dies).
Thread.detachNewThread {
    while FileHandle.standardInput.availableData.count > 0 {}
    exit(0)
}

let capture = Capture()
Task {
    do {
        try await capture.start()
    } catch {
        log("could not start: \(error.localizedDescription)")
        exit(1)
    }
}
RunLoop.main.run()
