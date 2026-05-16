import AVFoundation
import Foundation
import ScreenCaptureKit

var standardError = FileHandle.standardError

extension FileHandle: @retroactive TextOutputStream {
    public func write(_ string: String) {
        let data = Data(string.utf8)
        self.write(data)
    }
}

private let diagnosticsEnabled = ProcessInfo.processInfo.environment["RB_MAC_CAPTURE_DIAG"] == "1"

func logError(_ message: String) {
    print("ERROR: \(message)", to: &standardError)
}

func logInfo(_ message: String) {
    print("INFO: \(message)", to: &standardError)
}

func logDebug(_ message: String) {
    guard diagnosticsEnabled else { return }
    print("DEBUG: \(message)", to: &standardError)
}

private func osStatusDescription(_ status: OSStatus) -> String {
    let n = UInt32(bitPattern: status)
    let bytes = [
        Character(UnicodeScalar((n >> 24) & 0xff)!),
        Character(UnicodeScalar((n >> 16) & 0xff)!),
        Character(UnicodeScalar((n >> 8) & 0xff)!),
        Character(UnicodeScalar(n & 0xff)!),
    ]
    let fourCC = String(bytes)
    if fourCC.allSatisfy({ $0.isASCII && !$0.isWhitespace }) {
        return "\(status) ('\(fourCC)')"
    }
    return "\(status)"
}

private func captureErrorDescription(_ error: Error) -> String {
    let nsError = error as NSError
    var message = "\(nsError.domain) code=\(nsError.code)"
    if !nsError.localizedDescription.isEmpty {
        message += " \(nsError.localizedDescription)"
    }
    if let underlying = nsError.userInfo[NSUnderlyingErrorKey] {
        message += " underlying=\(underlying)"
    }
    return message
}

private struct InputAudioDescription: Equatable {
    let sampleRate: Double
    let channels: Int
    let commonFormat: AVAudioCommonFormat
    let isInterleaved: Bool

    init(_ format: AVAudioFormat) {
        self.sampleRate = format.sampleRate
        self.channels = Int(format.channelCount)
        self.commonFormat = format.commonFormat
        self.isInterleaved = format.isInterleaved
    }

    var summary: String {
        "rate=\(Int(sampleRate)) channels=\(channels) format=\(commonFormat.rawValue) interleaved=\(isInterleaved)"
    }
}

final class OutputWriter {
    private let fd = STDOUT_FILENO
    private(set) var droppedWrites = 0
    private(set) var broken = false

    init() {
        let currentFlags = fcntl(fd, F_GETFL)
        if currentFlags >= 0 {
            _ = fcntl(fd, F_SETFL, currentFlags | O_NONBLOCK)
        }
    }

    func writeAll(_ data: UnsafeRawPointer, byteCount: Int) {
        guard !broken else { return }

        var totalWritten = 0
        while totalWritten < byteCount {
            let remaining = byteCount - totalWritten
            let bytesWritten = write(fd, data.advanced(by: totalWritten), remaining)
            if bytesWritten > 0 {
                totalWritten += bytesWritten
                continue
            }

            if bytesWritten < 0 && errno == EINTR {
                continue
            }

            if bytesWritten < 0 && (errno == EAGAIN || errno == EWOULDBLOCK) {
                droppedWrites += 1
                if droppedWrites <= 5 || droppedWrites % 100 == 0 {
                    logError("stdout backpressure: dropped audio packet count=\(droppedWrites)")
                }
                return
            }

            broken = true
            logError("Failed to write \(remaining) bytes to stdout errno=\(errno)")
            exit(1)
        }
    }
}

final class FastPCMConverter {
    private let sampleRate: Int
    private var scratchSamples = [Int16]()
    private var diagnosticBuffersLogged = 0

    init(sampleRate: Int) {
        self.sampleRate = sampleRate
    }

    func convertAndWrite(sampleBuffer: CMSampleBuffer, writer: OutputWriter) -> Bool {
        let numSamples = CMSampleBufferGetNumSamples(sampleBuffer)
        guard numSamples > 0 else { return true }

        guard let formatDescription = CMSampleBufferGetFormatDescription(sampleBuffer) else { return false }
        let inputFormat = AVAudioFormat(cmAudioFormatDescription: formatDescription)
        let desc = InputAudioDescription(inputFormat)

        if diagnosticBuffersLogged < 5 {
            logDebug("audio buffer #\(diagnosticBuffersLogged + 1): \(desc.summary)")
            diagnosticBuffersLogged += 1
        }

        guard desc.channels == 2 else { return false }
        guard Int(desc.sampleRate.rounded()) == sampleRate else { return false }

        var bufferListSize = 0
        CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: &bufferListSize,
            bufferListOut: nil,
            bufferListSize: 0,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: 0,
            blockBufferOut: nil
        )

        let rawListPtr = UnsafeMutableRawPointer.allocate(
            byteCount: bufferListSize,
            alignment: MemoryLayout<AudioBufferList>.alignment
        )
        defer { rawListPtr.deallocate() }

        var retainedBlockBuffer: CMBlockBuffer?
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: rawListPtr.assumingMemoryBound(to: AudioBufferList.self),
            bufferListSize: bufferListSize,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: 0,
            blockBufferOut: &retainedBlockBuffer
        )
        guard status == noErr else {
            logError("CMSampleBufferGetAudioBufferList failed: \(osStatusDescription(status))")
            return true
        }

        let audioBuffers = UnsafeMutableAudioBufferListPointer(
            rawListPtr.assumingMemoryBound(to: AudioBufferList.self)
        )

        switch (desc.commonFormat, desc.isInterleaved, audioBuffers.count) {
        case (.pcmFormatFloat32, false, 2):
            return convertFloatDeinterleavedStereo(audioBuffers, frames: numSamples, writer: writer)
        case (.pcmFormatFloat32, true, 1):
            return convertFloatInterleavedStereo(audioBuffers[0], frames: numSamples, writer: writer)
        case (.pcmFormatInt16, true, 1):
            let buffer = audioBuffers[0]
            guard let data = buffer.mData else { return true }
            writer.writeAll(data, byteCount: Int(buffer.mDataByteSize))
            return true
        default:
            logDebug("Falling back to AVAudioConverter for unsupported format: \(desc.summary) buffers=\(audioBuffers.count)")
            return false
        }
    }

    private func ensureScratchCapacity(_ sampleCount: Int) {
        if scratchSamples.count < sampleCount {
            scratchSamples = Array(repeating: 0, count: sampleCount)
        }
    }

    private func clampToInt16(_ sample: Float) -> Int16 {
        let scaled = sample * 32767.0
        if scaled > 32767.0 { return 32767 }
        if scaled < -32768.0 { return -32768 }
        return Int16(scaled.rounded())
    }

    private func convertFloatDeinterleavedStereo(
        _ audioBuffers: UnsafeMutableAudioBufferListPointer,
        frames: Int,
        writer: OutputWriter
    ) -> Bool {
        guard
            let leftData = audioBuffers[0].mData,
            let rightData = audioBuffers[1].mData
        else { return true }

        let left = leftData.assumingMemoryBound(to: Float.self)
        let right = rightData.assumingMemoryBound(to: Float.self)
        let sampleCount = frames * 2
        ensureScratchCapacity(sampleCount)

        for frame in 0..<frames {
            let base = frame * 2
            scratchSamples[base] = clampToInt16(left[frame])
            scratchSamples[base + 1] = clampToInt16(right[frame])
        }

        scratchSamples.withUnsafeBytes { bytes in
            writer.writeAll(bytes.baseAddress!, byteCount: sampleCount * MemoryLayout<Int16>.size)
        }
        return true
    }

    private func convertFloatInterleavedStereo(
        _ buffer: AudioBuffer,
        frames: Int,
        writer: OutputWriter
    ) -> Bool {
        guard let data = buffer.mData else { return true }
        let input = data.assumingMemoryBound(to: Float.self)
        let sampleCount = frames * 2
        ensureScratchCapacity(sampleCount)

        for idx in 0..<sampleCount {
            scratchSamples[idx] = clampToInt16(input[idx])
        }

        scratchSamples.withUnsafeBytes { bytes in
            writer.writeAll(bytes.baseAddress!, byteCount: sampleCount * MemoryLayout<Int16>.size)
        }
        return true
    }
}

final class ConverterFallback {
    private let sampleRate: Int
    private let outputFormat: AVAudioFormat
    private var converter: AVAudioConverter?

    init(sampleRate: Int) {
        self.sampleRate = sampleRate
        self.outputFormat = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: Double(sampleRate),
            channels: 2,
            interleaved: true
        )!
    }

    func convertAndWrite(sampleBuffer: CMSampleBuffer, writer: OutputWriter) {
        let numSamples = CMSampleBufferGetNumSamples(sampleBuffer)
        guard numSamples > 0 else { return }

        guard let formatDescription = CMSampleBufferGetFormatDescription(sampleBuffer) else { return }
        let inputFormat = AVAudioFormat(cmAudioFormatDescription: formatDescription)

        if converter == nil || converter?.inputFormat != inputFormat {
            converter = AVAudioConverter(from: inputFormat, to: outputFormat)
            logDebug("Created AVAudioConverter fallback input=\(InputAudioDescription(inputFormat).summary)")
        }
        guard let converter else { return }

        guard let inputBuffer = AVAudioPCMBuffer(
            pcmFormat: inputFormat,
            frameCapacity: AVAudioFrameCount(numSamples)
        ) else { return }
        inputBuffer.frameLength = AVAudioFrameCount(numSamples)

        var bufferListSize = 0
        CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: &bufferListSize,
            bufferListOut: nil,
            bufferListSize: 0,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: 0,
            blockBufferOut: nil
        )

        let rawListPtr = UnsafeMutableRawPointer.allocate(
            byteCount: bufferListSize,
            alignment: MemoryLayout<AudioBufferList>.alignment
        )
        defer { rawListPtr.deallocate() }

        var retainedBlockBuffer: CMBlockBuffer?
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: rawListPtr.assumingMemoryBound(to: AudioBufferList.self),
            bufferListSize: bufferListSize,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: 0,
            blockBufferOut: &retainedBlockBuffer
        )
        guard status == noErr else {
            logError("CMSampleBufferGetAudioBufferList fallback failed: \(osStatusDescription(status))")
            return
        }

        let sourceListPointer = UnsafeMutableAudioBufferListPointer(
            rawListPtr.assumingMemoryBound(to: AudioBufferList.self)
        )
        let destListPointer = UnsafeMutableAudioBufferListPointer(inputBuffer.mutableAudioBufferList)
        for i in 0..<min(sourceListPointer.count, destListPointer.count) {
            let srcBuf = sourceListPointer[i]
            let dstBuf = destListPointer[i]
            if let srcData = srcBuf.mData, let dstData = dstBuf.mData {
                memcpy(dstData, srcData, Int(srcBuf.mDataByteSize))
            }
        }

        let frameRatio = outputFormat.sampleRate / inputFormat.sampleRate
        let outFrameCapacity = AVAudioFrameCount(Double(numSamples) * frameRatio) + 1024
        guard let outputBuffer = AVAudioPCMBuffer(
            pcmFormat: outputFormat,
            frameCapacity: outFrameCapacity
        ) else { return }

        var error: NSError?
        var didProvideInput = false
        let inputBlock: AVAudioConverterInputBlock = { _, outStatus in
            if didProvideInput {
                outStatus.pointee = .noDataNow
                return nil
            }
            didProvideInput = true
            outStatus.pointee = .haveData
            return inputBuffer
        }

        let result = converter.convert(to: outputBuffer, error: &error, withInputFrom: inputBlock)
        if let error {
            logError("Conversion error: \(error)")
            return
        }
        if result == .error {
            logError("Conversion failed with status .error")
            return
        }

        let outListPointer = UnsafeMutableAudioBufferListPointer(outputBuffer.mutableAudioBufferList)
        guard let data = outListPointer.first?.mData else { return }
        writer.writeAll(data, byteCount: Int(outListPointer[0].mDataByteSize))
    }
}

final class AppAudioCapture: NSObject, SCStreamOutput, SCStreamDelegate {
    private var stream: SCStream?
    private let pid: pid_t
    private let sampleRate: Int
    private let writer = OutputWriter()
    private let fastConverter: FastPCMConverter
    private let fallbackConverter: ConverterFallback

    private var callbackCount = 0
    private var fallbackCount = 0
    private var slowCallbackCount = 0

    init(pid: pid_t, sampleRate: Int) {
        self.pid = pid
        self.sampleRate = sampleRate
        self.fastConverter = FastPCMConverter(sampleRate: sampleRate)
        self.fallbackConverter = ConverterFallback(sampleRate: sampleRate)
        super.init()
    }

    func start() async throws {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
        logInfo("SCK shareable content: displays=\(content.displays.count) apps=\(content.applications.count) windows=\(content.windows.count)")

        guard let targetApp = content.applications.first(where: { $0.processID == self.pid }) else {
            throw NSError(
                domain: "MacCapture",
                code: 1,
                userInfo: [NSLocalizedDescriptionKey: "Could not find application with PID \(pid)"]
            )
        }

        logInfo("Found target app: \(targetApp.applicationName) (PID: \(pid)) bundle=\(targetApp.bundleIdentifier)")

        guard let display = content.displays.first else {
            throw NSError(
                domain: "MacCapture",
                code: 2,
                userInfo: [NSLocalizedDescriptionKey: "No display found for SCContentFilter"]
            )
        }
        logInfo("Using display ID \(display.displayID) for SCContentFilter")

        let filter = SCContentFilter(display: display, including: [targetApp], exceptingWindows: [])
        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.excludesCurrentProcessAudio = false
        config.showsCursor = false
        config.sampleRate = sampleRate
        config.channelCount = 2

        logDebug("Creating SCStream with display-scoped app filter")
        let newStream = SCStream(filter: filter, configuration: config, delegate: self)
        let audioQueue = DispatchQueue(label: "com.rb-recorder.audioQueue", qos: .userInitiated)
        try newStream.addStreamOutput(self, type: SCStreamOutputType.audio, sampleHandlerQueue: audioQueue)

        self.stream = newStream
        do {
            try await newStream.startCapture()
            logInfo("ScreenCaptureKit audio stream started successfully")
        } catch {
            logError("SCStream.startCapture failed: \(captureErrorDescription(error))")
            throw error
        }
    }

    func stop() async throws {
        if let stream {
            try await stream.stopCapture()
            self.stream = nil
            logInfo("ScreenCaptureKit audio stream stopped")
        }
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, !writer.broken else { return }

        let start = DispatchTime.now().uptimeNanoseconds
        callbackCount += 1

        let convertedFast = fastConverter.convertAndWrite(sampleBuffer: sampleBuffer, writer: writer)
        if !convertedFast {
            fallbackCount += 1
            fallbackConverter.convertAndWrite(sampleBuffer: sampleBuffer, writer: writer)
        }

        let elapsedMs = Double(DispatchTime.now().uptimeNanoseconds - start) / 1_000_000.0
        if elapsedMs > 5.0 {
            slowCallbackCount += 1
            if slowCallbackCount <= 10 || slowCallbackCount % 50 == 0 {
                logDebug("slow audio callback count=\(slowCallbackCount) elapsed_ms=\(String(format: "%.3f", elapsedMs)) fallback_count=\(fallbackCount) dropped_writes=\(writer.droppedWrites)")
            }
        } else if diagnosticsEnabled && callbackCount <= 5 {
            logDebug("audio callback #\(callbackCount) elapsed_ms=\(String(format: "%.3f", elapsedMs)) fallback_count=\(fallbackCount)")
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        logError("Stream stopped with error: \(captureErrorDescription(error))")
        exit(1)
    }
}

func main() async {
    let args = ProcessInfo.processInfo.arguments
    guard args.count == 3, let pid = Int32(args[1]), let sampleRate = Int(args[2]) else {
        logError("Usage: mac-capture <PID> <SAMPLE_RATE>")
        exit(1)
    }

    setbuf(stdout, nil)
    logInfo("Starting mac-capture for PID \(pid) at \(sampleRate)Hz...")

    let capture = AppAudioCapture(pid: pid, sampleRate: sampleRate)

    do {
        try await capture.start()

        let sigintSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
        let sigtermSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)

        let shutdown = {
            logInfo("Received shutdown signal...")
            Task {
                try? await capture.stop()
                exit(0)
            }
        }

        sigintSource.setEventHandler(handler: shutdown)
        sigtermSource.setEventHandler(handler: shutdown)

        signal(SIGINT, SIG_IGN)
        signal(SIGTERM, SIG_IGN)

        sigintSource.resume()
        sigtermSource.resume()

        while true {
            try? await Task.sleep(nanoseconds: 1_000_000_000)
        }
    } catch {
        logError("Failed to start capture: \(captureErrorDescription(error))")
        exit(1)
    }
}

await main()
