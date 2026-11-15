// Captures the microphone and writes raw 16 kHz mono 16-bit PCM to stdout.
//
// Why a separate process: closing a microphone inside the app can deadlock
// in the audio system (seen with PortAudio on macOS 26: the mic stayed on
// and could never be opened again until Dictate was restarted). Dictate
// stops recording by killing this process, which always works and always
// releases the microphone.
//
// Build:  swiftc -O -swift-version 5 helper/micaudio.swift -o helper/micaudio
// Exits when stdin closes, so it can never outlive Dictate.

import AudioToolbox
import Foundation

func log(_ message: String) {
    FileHandle.standardError.write(("[micaudio] " + message + "\n").data(using: .utf8)!)
}

signal(SIGPIPE, SIG_IGN)

var format = AudioStreamBasicDescription(
    mSampleRate: 16000,
    mFormatID: kAudioFormatLinearPCM,
    mFormatFlags: kLinearPCMFormatFlagIsSignedInteger | kLinearPCMFormatFlagIsPacked,
    mBytesPerPacket: 2, mFramesPerPacket: 1, mBytesPerFrame: 2,
    mChannelsPerFrame: 1, mBitsPerChannel: 16, mReserved: 0)

let onAudio: AudioQueueInputCallback = { _, queue, buffer, _, _, _ in
    let count = Int(buffer.pointee.mAudioDataByteSize)
    if count > 0 {
        let written = fwrite(buffer.pointee.mAudioData, 1, count, stdout)
        fflush(stdout)
        if written == 0 { exit(0) }  // reader went away
    }
    AudioQueueEnqueueBuffer(queue, buffer, 0, nil)
}

var created: AudioQueueRef?
var status = AudioQueueNewInput(&format, onAudio, nil, nil, nil, 0, &created)
guard status == noErr, let queue = created else {
    log("could not open the microphone (status \(status))")
    exit(1)
}
for _ in 0..<4 {  // 50 ms buffers
    var buffer: AudioQueueBufferRef?
    if AudioQueueAllocateBuffer(queue, 1600, &buffer) == noErr, let buffer = buffer {
        AudioQueueEnqueueBuffer(queue, buffer, 0, nil)
    }
}
// Everything slow is done; the microphone itself is still off. Dictate keeps
// one helper waiting here ("armed") so that recording starts instantly, and
// sends one byte when the user taps the hotkey.
log("armed")
if FileHandle.standardInput.availableData.count == 0 { exit(0) }  // parent gone

status = AudioQueueStart(queue, nil)
guard status == noErr else {
    log("could not start the microphone (status \(status))")
    exit(1)
}
log("capturing")

// Exit as soon as the parent closes our stdin (or dies).
Thread.detachNewThread {
    while FileHandle.standardInput.availableData.count > 0 {}
    exit(0)
}
RunLoop.main.run()
