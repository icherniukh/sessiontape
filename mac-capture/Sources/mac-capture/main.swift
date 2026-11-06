import AVFoundation
import AudioToolbox
import CoreAudio
import Foundation
import ScreenCaptureKit
import Synchronization

extension FileHandle: @retroactive TextOutputStream {
    public func write(_ string: String) {
        let data = Data(string.utf8)
        self.write(data)
    }
}

protocol AudioCaptureBackend: AnyObject, Sendable {
    func start() async throws
    func stop() async throws
    func tickMetrics(force: Bool)
    var isBroken: Bool { get }
}

extension AudioCaptureBackend {
    func tickMetrics() {
        tickMetrics(force: false)
    }
    var isBroken: Bool {
        false
    }
}

enum CaptureBackendMode: String {
    case auto
    case tap
    case sck
}

private struct CaptureRuntimeOptions {
    var diag = false
    var metrics = true
    var logPath: String?
    var backend: CaptureBackendMode = .auto

    static func parse(arguments: [String]) -> (pid: Int32, sampleRate: Int, options: Self)? {
        var args = Array(arguments.dropFirst())
        guard args.count >= 2,
            let pid = Int32(args.removeFirst()),
            let sampleRate = Int(args.removeFirst())
        else {
            return nil
        }

        var options = Self()
        var index = 0
        while index < args.count {
            let flag = args[index]
            switch flag {
            case "--diag":
                options.diag = true
                index += 1
            case "--no-metrics":
                options.metrics = false
                index += 1
            case "--metrics":
                options.metrics = true
                index += 1
            case "--backend":
                index += 1
                guard index < args.count else { return nil }
                if let mode = CaptureBackendMode(rawValue: args[index].lowercased()) {
                    options.backend = mode
                } else {
                    return nil
                }
                index += 1
            case "--tap":
                options.backend = .tap
                index += 1
            case "--sck":
                options.backend = .sck
                index += 1
            case "--log":
                index += 1
                guard index < args.count else { return nil }
                options.logPath = args[index]
                index += 1
            default:
                return nil
            }
        }

        options.applyEnvironmentFallback()
        return (pid, sampleRate, options)
    }

    mutating func applyEnvironmentFallback() {
        let env = ProcessInfo.processInfo.environment
        if env["RB_MAC_CAPTURE_DIAG"] == "1" {
            diag = true
        }
        if env["RB_MAC_CAPTURE_METRICS"] == "0" {
            metrics = false
        }
        if let modeStr = env["RB_MAC_CAPTURE_BACKEND"]?.lowercased(),
            let mode = CaptureBackendMode(rawValue: modeStr)
        {
            backend = mode
        }
        if let path = env["RB_MAC_CAPTURE_LOG"], !path.isEmpty {
            logPath = path
        }
    }
}

nonisolated(unsafe) private var captureOptions = CaptureRuntimeOptions()

private func writeLogLine(_ line: String, to handle: FileHandle) {
    handle.write(Data(line.utf8))
}

final class DiagnosticLogger {
    private let lock = NSLock()
    private let diagnosticHandle: FileHandle?
    private var stderrHandle = FileHandle.standardError

    init(logPath: String?) {
        guard let path = logPath, !path.isEmpty else {
            self.diagnosticHandle = nil
            return
        }

        FileManager.default.createFile(atPath: path, contents: nil)
        let handle = FileHandle(forWritingAtPath: path)
        handle?.seekToEndOfFile()
        self.diagnosticHandle = handle

        if handle == nil {
            writeLogLine("ERROR: Failed to open diagnostic log at \(path)\n", to: stderrHandle)
        }
    }

    func log(level: String, message: String) {
        let line = "\(level): \(message)\n"
        lock.lock()
        defer { lock.unlock() }
        writeLogLine(line, to: stderrHandle)
        diagnosticHandle?.write(Data(line.utf8))
    }
}

nonisolated(unsafe) private var diagnosticLogger = DiagnosticLogger(logPath: nil)

func logError(_ message: String) {
    diagnosticLogger.log(level: "ERROR", message: message)
}

func logInfo(_ message: String) {
    diagnosticLogger.log(level: "INFO", message: message)
}

func logDebug(_ message: String) {
    guard captureOptions.diag else { return }
    diagnosticLogger.log(level: "DEBUG", message: message)
}

func osStatusDescription(_ status: OSStatus) -> String {
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

private struct AudioBufferListView {
    let pointer: UnsafeMutablePointer<AudioBufferList>
    let buffers: UnsafeMutableAudioBufferListPointer

    static func from(sampleBuffer: CMSampleBuffer) -> AudioBufferListView? {
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
        guard bufferListSize > 0 else { return nil }

        let rawListPtr = UnsafeMutableRawPointer.allocate(
            byteCount: bufferListSize,
            alignment: MemoryLayout<AudioBufferList>.alignment
        )

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
            rawListPtr.deallocate()
            logError("CMSampleBufferGetAudioBufferList failed: \(osStatusDescription(status))")
            return nil
        }

        let pointer = rawListPtr.assumingMemoryBound(to: AudioBufferList.self)
        return AudioBufferListView(
            pointer: pointer,
            buffers: UnsafeMutableAudioBufferListPointer(pointer)
        )
    }

    func release() {
        pointer.deallocate()
    }
}

final class FastPCMConverter {
    private let sampleRate: Int
    // Scratch is reallocated only by `reserve(maxFrames:)`, which must run before the IO proc
    // starts; the audio thread only reads these two fields.
    private var scratchCapacity: Int
    private var scratchSamples: UnsafeMutablePointer<Int16>
    private var diagnosticBuffersLogged = 0
    /// Callback buffers that did not fit in the scratch and were cut short (lost audio).
    let truncatedBuffers = Atomic<Int>(0)

    init(sampleRate: Int, maxFrames: Int = 16384) {
        self.sampleRate = sampleRate
        self.scratchCapacity = maxFrames * 2
        self.scratchSamples = .allocate(capacity: self.scratchCapacity)
    }

    deinit {
        scratchSamples.deallocate()
    }

    /// Grows the scratch to hold `maxFrames` stereo frames. Not RT-safe: call before audio starts.
    func reserve(maxFrames: Int) {
        let needed = maxFrames * 2
        guard needed > scratchCapacity else { return }
        scratchSamples.deallocate()
        scratchSamples = .allocate(capacity: needed)
        scratchCapacity = needed
    }

    func convertAndEnqueue(sampleBuffer: CMSampleBuffer, queue: PCMOutboundQueue) -> Bool {
        let numSamples = CMSampleBufferGetNumSamples(sampleBuffer)
        guard numSamples > 0 else { return true }

        guard let formatDescription = CMSampleBufferGetFormatDescription(sampleBuffer) else {
            return false
        }
        let inputFormat = AVAudioFormat(cmAudioFormatDescription: formatDescription)
        let desc = InputAudioDescription(inputFormat)

        if diagnosticBuffersLogged < 5 {
            logDebug("audio buffer #\(diagnosticBuffersLogged + 1): \(desc.summary)")
            diagnosticBuffersLogged += 1
        }

        guard desc.channels == 2 else { return false }
        guard Int(desc.sampleRate.rounded()) == sampleRate else { return false }

        guard let bufferView = AudioBufferListView.from(sampleBuffer: sampleBuffer) else {
            return true
        }
        defer { bufferView.release() }

        let audioBuffers = bufferView.buffers
        switch (desc.commonFormat, desc.isInterleaved, audioBuffers.count) {
        case (.pcmFormatFloat32, false, 2):
            enqueueFloatDeinterleavedStereo(audioBuffers, frames: numSamples, queue: queue)
            return true
        case (.pcmFormatFloat32, true, 1):
            enqueueFloatInterleavedStereo(audioBuffers[0], frames: numSamples, queue: queue)
            return true
        case (.pcmFormatInt16, true, 1):
            let buffer = audioBuffers[0]
            guard let data = buffer.mData else { return true }
            queue.enqueue(bytes: data, byteCount: Int(buffer.mDataByteSize))
            return true
        default:
            logDebug(
                "Falling back to AVAudioConverter for unsupported format: \(desc.summary) buffers=\(audioBuffers.count)"
            )
            return false
        }
    }

    private func clampToInt16(_ sample: Float) -> Int16 {
        let scaled = sample * 32767.0
        if scaled > 32767.0 { return 32767 }
        if scaled < -32768.0 { return -32768 }
        return Int16(scaled.rounded())
    }

    func enqueueFloatDeinterleavedStereo(
        _ audioBuffers: UnsafeMutableAudioBufferListPointer,
        frames: Int,
        queue: PCMOutboundQueue
    ) {
        guard
            let leftData = audioBuffers[0].mData,
            let rightData = audioBuffers[1].mData
        else { return }

        let left = leftData.assumingMemoryBound(to: Float.self)
        let right = rightData.assumingMemoryBound(to: Float.self)
        let sampleCount = stereoFramesThatFit(frames) * 2

        for frame in 0..<(sampleCount / 2) {
            let base = frame * 2
            scratchSamples[base] = clampToInt16(left[frame])
            scratchSamples[base + 1] = clampToInt16(right[frame])
        }

        queue.enqueue(
            bytes: scratchSamples,
            byteCount: sampleCount * MemoryLayout<Int16>.size
        )
    }

    /// Frames of stereo output the scratch buffer can hold; counts a truncation when `frames` doesn't fit.
    private func stereoFramesThatFit(_ frames: Int) -> Int {
        let fit = min(frames, scratchCapacity / 2)
        if fit < frames { truncatedBuffers.wrappingAdd(1, ordering: .relaxed) }
        return fit
    }

    func enqueueFloatInterleavedStereo(
        _ buffer: AudioBuffer,
        frames: Int,
        queue: PCMOutboundQueue
    ) {
        guard let data = buffer.mData else { return }
        let input = data.assumingMemoryBound(to: Float.self)
        let sampleCount = stereoFramesThatFit(frames) * 2

        for idx in 0..<sampleCount {
            scratchSamples[idx] = clampToInt16(input[idx])
        }

        queue.enqueue(
            bytes: scratchSamples,
            byteCount: sampleCount * MemoryLayout<Int16>.size
        )
    }

    /// Mono Float32 -> duplicated into both stereo channels. RT-safe (preallocated scratch).
    func enqueueFloatMonoAsStereo(
        _ buffer: AudioBuffer,
        frames: Int,
        queue: PCMOutboundQueue
    ) {
        guard let data = buffer.mData else { return }
        let input = data.assumingMemoryBound(to: Float.self)
        let frameCount = stereoFramesThatFit(frames)

        for frame in 0..<frameCount {
            let s = clampToInt16(input[frame])
            scratchSamples[frame * 2] = s
            scratchSamples[frame * 2 + 1] = s
        }

        queue.enqueue(
            bytes: scratchSamples,
            byteCount: frameCount * 2 * MemoryLayout<Int16>.size
        )
    }

    /// Walks a tap AudioBufferList whose layout was validated at start (Float32, 1 or 2 channels).
    /// RT-safe: no allocation; buffers that do not match the validated layout are dropped.
    func enqueueTapFloat(
        _ list: UnsafeMutableAudioBufferListPointer,
        channels: Int,
        interleaved: Bool,
        queue: PCMOutboundQueue
    ) {
        let floatSize = MemoryLayout<Float>.size
        switch (channels, interleaved) {
        case (2, true):
            guard list.count >= 1, list[0].mNumberChannels == 2 else { return }
            let buffer = list[0]
            guard buffer.mData != nil, buffer.mDataByteSize > 0 else { return }
            enqueueFloatInterleavedStereo(
                buffer, frames: Int(buffer.mDataByteSize) / (floatSize * 2), queue: queue)
        case (2, false):
            guard list.count >= 2 else { return }
            let bytes = min(list[0].mDataByteSize, list[1].mDataByteSize)
            guard bytes > 0 else { return }
            enqueueFloatDeinterleavedStereo(list, frames: Int(bytes) / floatSize, queue: queue)
        case (1, _):
            guard list.count >= 1 else { return }
            let buffer = list[0]
            guard buffer.mData != nil, buffer.mDataByteSize > 0 else { return }
            enqueueFloatMonoAsStereo(
                buffer, frames: Int(buffer.mDataByteSize) / floatSize, queue: queue)
        default:
            return
        }
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

    func convertAndEnqueue(sampleBuffer: CMSampleBuffer, queue: PCMOutboundQueue) {
        let numSamples = CMSampleBufferGetNumSamples(sampleBuffer)
        guard numSamples > 0 else { return }

        guard let formatDescription = CMSampleBufferGetFormatDescription(sampleBuffer) else {
            return
        }
        let inputFormat = AVAudioFormat(cmAudioFormatDescription: formatDescription)

        if converter == nil || converter?.inputFormat != inputFormat {
            converter = AVAudioConverter(from: inputFormat, to: outputFormat)
            logDebug(
                "Created AVAudioConverter fallback input=\(InputAudioDescription(inputFormat).summary)"
            )
        }
        guard let converter else { return }

        guard
            let inputBuffer = AVAudioPCMBuffer(
                pcmFormat: inputFormat,
                frameCapacity: AVAudioFrameCount(numSamples)
            )
        else { return }
        inputBuffer.frameLength = AVAudioFrameCount(numSamples)

        guard let bufferView = AudioBufferListView.from(sampleBuffer: sampleBuffer) else {
            return
        }
        defer { bufferView.release() }

        let sourceListPointer = bufferView.buffers
        let destListPointer = UnsafeMutableAudioBufferListPointer(
            inputBuffer.mutableAudioBufferList)
        for i in 0..<min(sourceListPointer.count, destListPointer.count) {
            let srcBuf = sourceListPointer[i]
            let dstBuf = destListPointer[i]
            if let srcData = srcBuf.mData, let dstData = dstBuf.mData {
                memcpy(dstData, srcData, Int(srcBuf.mDataByteSize))
            }
        }

        let frameRatio = outputFormat.sampleRate / inputFormat.sampleRate
        let outFrameCapacity = AVAudioFrameCount(Double(numSamples) * frameRatio) + 1024
        guard
            let outputBuffer = AVAudioPCMBuffer(
                pcmFormat: outputFormat,
                frameCapacity: outFrameCapacity
            )
        else { return }

        var error: NSError?
        // convert(to:error:withInputFrom:) calls the block synchronously on this thread,
        // so the unchecked-Sendable box is safe. Some SDKs mark the block @Sendable.
        let input = OneShotConverterInput(inputBuffer)
        let inputBlock: AVAudioConverterInputBlock = { _, outStatus in
            input.next(outStatus)
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

        let outListPointer = UnsafeMutableAudioBufferListPointer(
            outputBuffer.mutableAudioBufferList)
        guard let data = outListPointer.first?.mData else { return }
        queue.enqueue(bytes: data, byteCount: Int(outListPointer[0].mDataByteSize))
    }
}

final class AppAudioCapture: NSObject, SCStreamOutput, SCStreamDelegate, AudioCaptureBackend, @unchecked Sendable {
    private var stream: SCStream?
    private let pid: pid_t
    private let sampleRate: Int
    private let outboundQueue: PCMOutboundQueue
    private let drainThread: PCMOutboundDrainThread
    private let fastConverter: FastPCMConverter
    private let fallbackConverter: ConverterFallback
    private let metrics = CaptureMetrics()
    private let metricsEnabled: Bool

    var isBroken: Bool { drainThread.broken }

    private var callbackCount = 0
    private var fallbackCount = 0
    private var slowCallbackCount = 0
    private var queueDropLogCount = 0

    init(pid: pid_t, sampleRate: Int, metricsEnabled: Bool) {
        self.pid = pid
        self.sampleRate = sampleRate
        self.metricsEnabled = metricsEnabled
        let queue = PCMOutboundQueue()
        self.outboundQueue = queue
        self.drainThread = PCMOutboundDrainThread(queue: queue)
        self.fastConverter = FastPCMConverter(sampleRate: sampleRate)
        self.fallbackConverter = ConverterFallback(sampleRate: sampleRate)
        super.init()
    }

    func start() async throws {
        drainThread.start()

        let content = try await SCShareableContent.excludingDesktopWindows(
            false, onScreenWindowsOnly: false)
        logInfo(
            "SCK shareable content: displays=\(content.displays.count) apps=\(content.applications.count) windows=\(content.windows.count)"
        )

        guard let targetApp = content.applications.first(where: { $0.processID == self.pid }) else {
            throw NSError(
                domain: "MacCapture",
                code: 1,
                userInfo: [NSLocalizedDescriptionKey: "Could not find application with PID \(pid)"]
            )
        }

        logInfo(
            "Found target app: \(targetApp.applicationName) (PID: \(pid)) bundle=\(targetApp.bundleIdentifier)"
        )

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
        config.queueDepth = 8
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1)

        logDebug("Creating SCStream with display-scoped app filter")
        let newStream = SCStream(filter: filter, configuration: config, delegate: self)
        let audioQueue = DispatchQueue(
            label: "dev.icherniukh.sessiontape.audioQueue",
            qos: .userInteractive)
        try newStream.addStreamOutput(
            self, type: SCStreamOutputType.audio, sampleHandlerQueue: audioQueue)

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
        drainThread.stop()
        tickMetrics(force: true)
    }

    func tickMetrics(force: Bool = false) {
        guard metricsEnabled else { return }
        metrics.emitIfDue(
            bytesWritten: drainThread.bytesWritten,
            droppedWrites: outboundQueue.droppedChunks,
            emit: { logInfo($0) },
            force: force
        )
    }

    func stream(
        _ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        of type: SCStreamOutputType
    ) {
        guard type == .audio, !drainThread.broken else { return }

        let start = DispatchTime.now().uptimeNanoseconds
        callbackCount += 1
        let dropsBefore = outboundQueue.droppedChunks

        let convertedFast = fastConverter.convertAndEnqueue(
            sampleBuffer: sampleBuffer, queue: outboundQueue)
        if !convertedFast {
            fallbackCount += 1
            fallbackConverter.convertAndEnqueue(sampleBuffer: sampleBuffer, queue: outboundQueue)
        }

        if outboundQueue.droppedChunks > dropsBefore {
            queueDropLogCount += 1
            if queueDropLogCount <= 5 || queueDropLogCount % 100 == 0 {
                logError(
                    "PCM queue overflow: dropped chunk count=\(outboundQueue.droppedChunks) depth=\(outboundQueue.depth)"
                )
            }
        }

        let elapsedMs = Double(DispatchTime.now().uptimeNanoseconds - start) / 1_000_000.0
        metrics.recordCallback(elapsedMs: elapsedMs, usedFallback: !convertedFast)
        if elapsedMs > 5.0 {
            slowCallbackCount += 1
            if slowCallbackCount <= 10 || slowCallbackCount % 50 == 0 {
                logDebug(
                    "slow audio callback count=\(slowCallbackCount) elapsed_ms=\(String(format: "%.3f", elapsedMs)) fallback_count=\(fallbackCount) queue_drops=\(outboundQueue.droppedChunks)"
                )
            }
        } else if captureOptions.diag && callbackCount <= 5 {
            logDebug(
                "audio callback #\(callbackCount) elapsed_ms=\(String(format: "%.3f", elapsedMs)) fallback_count=\(fallbackCount)"
            )
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        logError("Stream stopped with error: \(captureErrorDescription(error))")
        exit(1)
    }
}

func main() async {
    // A closed stdout pipe must surface as EPIPE (drain thread sets `broken`), not kill the process.
    signal(SIGPIPE, SIG_IGN)

    guard let parsed = CaptureRuntimeOptions.parse(arguments: ProcessInfo.processInfo.arguments)
    else {
        fputs(
            "Usage: mac-capture <PID> <SAMPLE_RATE> [--backend tap|sck|auto] [--tap|--sck] [--diag] [--metrics|--no-metrics] [--log PATH]\n",
            stderr
        )
        exit(1)
    }

    captureOptions = parsed.options
    diagnosticLogger = DiagnosticLogger(logPath: captureOptions.logPath)

    let pid = parsed.pid
    let sampleRate = parsed.sampleRate

    setbuf(stdout, nil)
    logInfo("Starting mac-capture for PID \(pid) at \(sampleRate)Hz (backend: \(captureOptions.backend.rawValue))...")

    func makeBackend(_ mode: CaptureBackendMode) -> AudioCaptureBackend {
        // The deployment target (macOS 15) is above the 14.2 process-tap floor, so no availability check.
        mode == .sck
            ? AppAudioCapture(pid: pid, sampleRate: sampleRate, metricsEnabled: captureOptions.metrics)
            : CoreAudioTapCapture(pid: pid, sampleRate: sampleRate, metricsEnabled: captureOptions.metrics)
    }
    var capture = makeBackend(captureOptions.backend)

    do {
        do {
            try await capture.start()
        } catch let mismatch as TapFormatMismatch {
            // Contract with the reader: exact line on stderr, exit status 3.
            fputs(
                "FORMAT_MISMATCH tap=\(mismatch.tapRate) requested=\(mismatch.requestedRate)\n",
                stderr)
            exit(3)
        } catch {
            if captureOptions.backend == .auto && !(capture is AppAudioCapture) {
                logError(
                    "CoreAudio tap failed to start: \(captureErrorDescription(error)). Falling back to ScreenCaptureKit..."
                )
                let fallback = makeBackend(.sck)
                try await fallback.start()
                capture = fallback
            } else {
                throw error
            }
        }

        let activeCapture = capture
        let sigintSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
        let sigtermSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)

        let shutdown = {
            logInfo("Received shutdown signal...")
            Task {
                try? await activeCapture.stop()
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
            if activeCapture.isBroken {
                logError("Capture output stream broke (stdout pipe closed). Exiting cleanly...")
                try? await activeCapture.stop()
                exit(0)
            }
            activeCapture.tickMetrics()
        }
    } catch {
        logError("Failed to start capture: \(captureErrorDescription(error))")
        exit(1)
    }
}

await main()

/// Hands one buffer to an AVAudioConverter input block, then reports no data.
private final class OneShotConverterInput: @unchecked Sendable {
    private var buffer: AVAudioPCMBuffer?

    init(_ buffer: AVAudioPCMBuffer) { self.buffer = buffer }

    func next(_ outStatus: UnsafeMutablePointer<AVAudioConverterInputStatus>) -> AVAudioBuffer? {
        guard let buffer else {
            outStatus.pointee = .noDataNow
            return nil
        }
        self.buffer = nil
        outStatus.pointee = .haveData
        return buffer
    }
}
