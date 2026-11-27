import AppKit
import AudioToolbox
import CoreAudio
import Foundation
import Synchronization

/// Exit status used when the tap format changes mid-session, the HAL reports abnormal IO stop,
/// `coreaudiod` restarts, or the tap callback stalls. The Python supervisor restarts capture.
let tapFormatChangedExitStatus: Int32 = 4

/// Awake-uptime threshold without an IO callback before `checkHealth()` exits for a restart.
let tapStallTimeoutNs: UInt64 = 5_000_000_000
/// Awake-uptime threshold of exact-zero samples after start before logging a TCC permission hint.
private let silentStartupWarningNs: UInt64 = 60_000_000_000

/// Counters bumped by HAL listener blocks. Lock-free: the overload listener is invoked
/// synchronously from the IO thread.
private final class HALCounters: @unchecked Sendable {
    let overloads = Atomic<Int>(0)
    let ioStopped = Atomic<Int>(0)
}

final class CoreAudioTapCapture: AudioCaptureBackend, @unchecked Sendable {
    private let pid: pid_t
    private let sampleRate: Int
    private let metricsEnabled: Bool
    private let outboundQueue: PCMOutboundQueue
    private let drainThread: PCMOutboundDrainThread
    private let fastConverter: FastPCMConverter
    private let metrics = CaptureMetrics()

    private var tapID: AudioObjectID = kAudioObjectUnknown
    private var aggregateDeviceID: AudioObjectID = kAudioObjectUnknown
    private var ioProcID: AudioDeviceIOProcID?
    private var targetProcessObject: AudioObjectID = kAudioObjectUnknown
    // Validated tap layout; written before AudioDeviceStart, read-only in the IO proc.
    private var tapChannels = 2
    private var tapInterleaved = true
    private var validatedFormat = AudioStreamBasicDescription()

    private let teardownLock = NSLock()
    private var didTeardown = false
    private let stopping = Atomic<Bool>(false)
    private let restartExitFired = Atomic<Bool>(false)
    private let lastCallbackUptimeNs = Atomic<UInt64>(0)
    private var startedUptimeNs: UInt64 = 0
    private var silentWarningLogged = false
    private let counters = HALCounters()

    /// Queue for IO-thread notifications. The HAL dispatches `kAudioDeviceProcessorOverload`
    /// synchronously from the IO thread, so only atomic increments may run here. Nothing that
    /// teardown waits on may share this queue.
    private let halQueue = DispatchQueue(
        label: "dev.icherniukh.sessiontape.tap.hal", qos: .userInteractive)
    /// Queue for everything else (format and device changes). Handlers never call teardown
    /// directly; they hop to a global queue so `teardown()` can drain this one without deadlock.
    private let controlQueue = DispatchQueue(
        label: "dev.icherniukh.sessiontape.tap.control", qos: .utility)

    private struct Listener {
        let objectID: AudioObjectID
        var address: AudioObjectPropertyAddress
        let queue: DispatchQueue
        let block: AudioObjectPropertyListenerBlock
    }
    private var listeners: [Listener] = []

    var isBroken: Bool { drainThread.broken }

    init(pid: pid_t, sampleRate: Int, metricsEnabled: Bool) {
        self.pid = pid
        self.sampleRate = sampleRate
        self.metricsEnabled = metricsEnabled
        let queue = PCMOutboundQueue()
        self.outboundQueue = queue
        self.drainThread = PCMOutboundDrainThread(queue: queue)
        self.fastConverter = FastPCMConverter(sampleRate: sampleRate)
    }

    /// How the target PID maps to a tap: by bundle ID (macOS 26), by process object, or not at all.
    private enum TargetProcess {
        case bundle(String)
        case noBundle
        case notFound
    }

    private func lookupTargetProcess() -> TargetProcess {
        if let app = NSRunningApplication(processIdentifier: pid),
            let bundleID = app.bundleIdentifier, !bundleID.isEmpty
        {
            return .bundle(bundleID)
        }
        // No bundle ID (CLI tools such as afplay): the process may still exist. EPERM means it
        // exists but belongs to another user.
        if kill(pid, 0) == 0 || errno == EPERM { return .noBundle }
        return .notFound
    }

    private func resolveProcessObject() -> AudioObjectID {
        var procAddress = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyTranslatePIDToProcessObject,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var processObj: AudioObjectID = kAudioObjectUnknown
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        var targetPid = self.pid

        let status = AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject),
            &procAddress,
            UInt32(MemoryLayout<pid_t>.size),
            &targetPid,
            &size,
            &processObj
        )
        if status == noErr && processObj != kAudioObjectUnknown {
            return processObj
        }
        return kAudioObjectUnknown
    }

    /// Reads a CFString property (device or tap UID) from a HAL object.
    private func stringProperty(
        _ object: AudioObjectID, _ selector: AudioObjectPropertySelector
    ) -> (value: String?, status: OSStatus) {
        var address = AudioObjectPropertyAddress(
            mSelector: selector,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var value: CFString = "" as CFString
        var size = UInt32(MemoryLayout<CFString>.size)
        let status = withUnsafeMutablePointer(to: &value) { ptr in
            AudioObjectGetPropertyData(object, &address, 0, nil, &size, ptr)
        }
        return (status == noErr ? value as String : nil, status)
    }

    private func queryDeviceUID(_ deviceID: AudioObjectID) -> String? {
        stringProperty(deviceID, kAudioDevicePropertyDeviceUID).value
    }

    /// UIDs of the output devices the process currently plays to (informational only: the tap
    /// itself is not pinned to any of them).
    private func processOutputDeviceUIDs(_ processObj: AudioObjectID) -> [String] {
        var devAddress = AudioObjectPropertyAddress(
            mSelector: kAudioProcessPropertyDevices,
            mScope: kAudioObjectPropertyScopeOutput,
            mElement: kAudioObjectPropertyElementMain
        )
        var devSize: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(processObj, &devAddress, 0, nil, &devSize) == noErr,
            devSize > 0
        else { return [] }
        var devices = [AudioObjectID](
            repeating: 0, count: Int(devSize) / MemoryLayout<AudioObjectID>.size)
        guard AudioObjectGetPropertyData(processObj, &devAddress, 0, nil, &devSize, &devices)
            == noErr
        else { return [] }
        return devices.map { queryDeviceUID($0) ?? "device#\($0)" }
    }

    private func readTapFormat(_ tap: AudioObjectID) -> AudioStreamBasicDescription? {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioTapPropertyFormat,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var format = AudioStreamBasicDescription()
        var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        guard AudioObjectGetPropertyData(tap, &address, 0, nil, &size, &format) == noErr else {
            return nil
        }
        return format
    }

    private static func coreAudioError(_ code: Int, _ message: String) -> NSError {
        NSError(
            domain: "CoreAudioTap", code: code,
            userInfo: [NSLocalizedDescriptionKey: message])
    }

    // MARK: - Listeners

    private func addListener(
        _ objectID: AudioObjectID, selector: AudioObjectPropertySelector,
        scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal,
        queue: DispatchQueue, name: String, block: @escaping AudioObjectPropertyListenerBlock
    ) {
        var address = AudioObjectPropertyAddress(
            mSelector: selector,
            mScope: scope,
            mElement: kAudioObjectPropertyElementMain
        )
        let status = AudioObjectAddPropertyListenerBlock(objectID, &address, queue, block)
        if status == noErr {
            listeners.append(
                Listener(objectID: objectID, address: address, queue: queue, block: block))
        } else {
            logError("Failed to add \(name) listener: \(osStatusDescription(status))")
        }
    }

    private func removeListeners() {
        for var listener in listeners {
            AudioObjectRemovePropertyListenerBlock(
                listener.objectID, &listener.address, listener.queue, listener.block)
        }
        listeners.removeAll()
    }

    /// Process device list changed. Informational: the unpinned tap follows the process.
    private func handleProcessDevicesChange(_ processObj: AudioObjectID) {
        guard !stopping.load(ordering: .relaxed) else { return }
        let uids = processOutputDeviceUIDs(processObj)
        logInfo(
            "Process output devices changed: \(uids.isEmpty ? "none" : uids.joined(separator: ", "))"
        )
    }

    /// The tap format moved. Anything that differs from what the IO proc was validated against
    /// (rate, channels, layout) invalidates the capture: exit with the restartable status.
    private func handleTapFormatChange(_ tap: AudioObjectID) {
        guard !stopping.load(ordering: .relaxed) else { return }
        guard let now = readTapFormat(tap) else {
            logDebug("Tap format notification, but the format could not be read; ignoring")
            return
        }
        let old = validatedFormat
        let same =
            now.mSampleRate == old.mSampleRate && now.mFormatID == old.mFormatID
            && now.mFormatFlags == old.mFormatFlags
            && now.mChannelsPerFrame == old.mChannelsPerFrame
            && now.mBitsPerChannel == old.mBitsPerChannel
        guard !same else {
            logDebug("Tap format notification with unchanged format; ignoring")
            return
        }
        guard !restartExitFired.exchange(true, ordering: .sequentiallyConsistent) else { return }

        // Contract with the reader: exact line on stderr, exit status 4 (restartable).
        fputs(
            "FORMAT_CHANGED tap=\(Int(now.mSampleRate.rounded())) requested=\(sampleRate)\n",
            stderr)
        logError(
            "Tap format changed mid-session: \(Int(old.mSampleRate))Hz/\(old.mChannelsPerFrame)ch -> \(Int(now.mSampleRate))Hz/\(now.mChannelsPerFrame)ch flags=0x\(String(now.mFormatFlags, radix: 16)); exiting for restart"
        )
        DispatchQueue.global().async { [self] in
            teardown()
            exit(tapFormatChangedExitStatus)
        }
    }

    // MARK: - Start

    func start() async throws {
        do {
            try startTap()
        } catch {
            // Every failure leaves nothing behind.
            teardown()
            throw error
        }
    }

    private func startTap() throws {
        let desc = CATapDescription()
        // bundleIDs/isProcessRestoreEnabled exist only in the macOS 26 SDK (Swift 6.2 / Xcode 26).
        // Older SDKs build the PID-based path only.
        var tapsByBundleID = false
        switch lookupTargetProcess() {
        case .notFound:
            throw Self.coreAudioError(
                2, "No process with PID \(pid); refusing to tap anything else")
        case .bundle(let bundleID):
            logInfo("Configuring CoreAudio process tap for bundle: \(bundleID) (PID: \(pid))")
            #if compiler(>=6.2)
            if #available(macOS 26.0, *) {
                desc.bundleIDs = [bundleID]
                desc.isProcessRestoreEnabled = true
                tapsByBundleID = true
            }
            #endif
        case .noBundle:
            logInfo("PID \(pid) has no bundle ID; configuring CoreAudio process tap by PID")
        }
        let processObj = resolveProcessObject()
        self.targetProcessObject = processObj
        if !tapsByBundleID {
            guard processObj != kAudioObjectUnknown else {
                throw Self.coreAudioError(
                    1,
                    "Failed to resolve audio ProcessObject for PID \(pid) (process is not an audio client)"
                )
            }
            desc.processes = [processObj]
        }
        desc.isPrivate = true
        desc.muteBehavior = .unmuted
        desc.isMixdown = true
        // deviceUID stays unset on purpose: per CATapDescription.h it is set only when a tap
        // "only taps a specific hardware device". Unset, the tap mixes the process output
        // regardless of which device the process plays to, so it follows a device switch.
        // Optional A/B overrides via environment:
        //   SESSIONTAPE_TAP_PIN=1             pin tap to process's current output device
        //   SESSIONTAPE_TAP_DRIFT=0|1         sub-tap drift compensation (default 0: single source clock)
        //   SESSIONTAPE_TAP_CLOCK=0|clockdev|subdev  aggregate clock layout (default 0: tap-driven)
        //   SESSIONTAPE_TAP_BUFFER_FRAMES=N   aggregate IO buffer frames (default 4096; 0 keeps HAL default)
        let experimentEnv = ProcessInfo.processInfo.environment
        let clockMode: String = {
            guard let raw = experimentEnv["SESSIONTAPE_TAP_CLOCK"], !raw.isEmpty, raw != "0" else {
                return "0"
            }
            return raw == "1" ? "clockdev" : raw
        }()
        var primaryOutputUID: String?
        if processObj != kAudioObjectUnknown {
            let uids = processOutputDeviceUIDs(processObj)
            primaryOutputUID = uids.first
            if experimentEnv["SESSIONTAPE_TAP_PIN"] == "1", let first = uids.first {
                desc.deviceUID = first
                logInfo("CoreAudio tap pinned to device \(first)")
            } else {
                logInfo(
                    "CoreAudio tap is not pinned to a device; process output devices now: \(uids.isEmpty ? "none" : uids.joined(separator: ", "))"
                )
            }
        }

        var newTapID: AudioObjectID = kAudioObjectUnknown
        let tapStatus = AudioHardwareCreateProcessTap(desc, &newTapID)
        guard tapStatus == noErr else {
            throw Self.coreAudioError(
                Int(tapStatus),
                "AudioHardwareCreateProcessTap failed: \(osStatusDescription(tapStatus))")
        }
        self.tapID = newTapID

        guard let tapFormat = readTapFormat(newTapID) else {
            throw Self.coreAudioError(0, "Reading kAudioTapPropertyFormat failed")
        }
        logInfo(
            "CoreAudio tap format: \(Int(tapFormat.mSampleRate))Hz, \(tapFormat.mChannelsPerFrame) channels, flags=0x\(String(tapFormat.mFormatFlags, radix: 16))"
        )

        let tapRate = Int(tapFormat.mSampleRate.rounded())
        guard tapRate >= 8_000 && tapRate <= 384_000 else {
            throw Self.coreAudioError(2, "Invalid tap sample rate: \(tapRate) Hz")
        }
        let isFloat32PCM =
            tapFormat.mFormatID == kAudioFormatLinearPCM
            && (tapFormat.mFormatFlags & kAudioFormatFlagIsFloat) != 0
            && tapFormat.mBitsPerChannel == 32
        guard isFloat32PCM, tapFormat.mChannelsPerFrame == 1 || tapFormat.mChannelsPerFrame == 2
        else {
            throw Self.coreAudioError(
                2,
                "Unsupported tap format: formatID=\(tapFormat.mFormatID) flags=0x\(String(tapFormat.mFormatFlags, radix: 16)) bits=\(tapFormat.mBitsPerChannel) channels=\(tapFormat.mChannelsPerFrame) (need Float32 PCM, 1 or 2 channels)"
            )
        }
        if tapRate != sampleRate {
            logInfo(
                "Tap sample rate (\(tapRate)Hz) differs from requested \(sampleRate)Hz; adopting native tap rate \(tapRate)Hz"
            )
        }
        validatedFormat = tapFormat
        tapChannels = Int(tapFormat.mChannelsPerFrame)
        tapInterleaved = (tapFormat.mFormatFlags & kAudioFormatFlagIsNonInterleaved) == 0

        outboundQueue.configureCapacity(sampleRate: tapRate)
        drainThread.start()

        // Mid-session format changes (e.g. the output device switched to another sample rate)
        // and coreaudiod restarts (which invalidate all existing HAL object IDs).
        addListener(
            AudioObjectID(kAudioObjectSystemObject),
            selector: kAudioHardwarePropertyServiceRestarted,
            queue: controlQueue,
            name: "audio service restarted"
        ) { [weak self] _, _ in
            self?.handleAudioServiceRestarted()
        }
        let tapForListener = newTapID
        addListener(
            newTapID, selector: kAudioTapPropertyFormat, queue: controlQueue, name: "tap format"
        ) { [weak self] _, _ in
            self?.handleTapFormatChange(tapForListener)
        }
        if processObj != kAudioObjectUnknown {
            addListener(
                processObj, selector: kAudioProcessPropertyDevices,
                scope: kAudioObjectPropertyScopeOutput, queue: controlQueue,
                name: "process devices"
            ) { [weak self] _, _ in
                self?.handleProcessDevicesChange(processObj)
            }
        }

        let (tapUIDValue, uidStatus) = stringProperty(newTapID, kAudioTapPropertyUID)
        guard uidStatus == noErr, let tapUID = tapUIDValue else {
            throw Self.coreAudioError(
                Int(uidStatus),
                "AudioObjectGetPropertyData (tap UID) failed: \(osStatusDescription(uidStatus))")
        }

        // The tap list goes in the creation dictionary, so the aggregate never exists without its
        // tap. Private: invisible to other processes. kAudioAggregateDeviceTapAutoStartKey is
        // deliberately not set: it makes AudioDeviceStart wait for the first tapped audio, but the
        // reader needs a continuous (silent) stream to detect the DJ stopping.
        // Default drift compensation is off (0): this private aggregate has no hardware input or
        // output sub-devices of its own, so there is no second crystal clock to resample against,
        // and enabling `[ahal_dsp]` drift resampling adds ~35-40% steady-state coreaudiod CPU.
        let driftEnabled = experimentEnv["SESSIONTAPE_TAP_DRIFT"] == "1" ? 1 : 0
        let defaultBufferFrames = 4096
        let requestedBufferFrames: Int = {
            if let raw = experimentEnv["SESSIONTAPE_TAP_BUFFER_FRAMES"], let parsed = Int(raw) {
                return max(0, parsed)
            }
            return defaultBufferFrames
        }()

        let aggUID = UUID().uuidString
        var aggDesc: [String: Any] = [
            kAudioAggregateDeviceNameKey: "sessiontape-tap-\(aggUID)",
            kAudioAggregateDeviceUIDKey: aggUID,
            kAudioAggregateDeviceIsPrivateKey: 1 as NSNumber,
            kAudioAggregateDeviceIsStackedKey: 0 as NSNumber,
            kAudioAggregateDeviceTapListKey: [
                [
                    kAudioSubTapUIDKey: tapUID,
                    kAudioSubTapDriftCompensationKey: driftEnabled as NSNumber,
                ]
            ],
        ]
        if let clockUID = primaryOutputUID {
            switch clockMode {
            case "clockdev":
                aggDesc[kAudioAggregateDeviceClockDeviceKey] = clockUID
                logInfo("Aggregate clock device set to \(clockUID) (zero output streams)")
            case "subdev":
                aggDesc[kAudioAggregateDeviceMainSubDeviceKey] = clockUID
                aggDesc[kAudioAggregateDeviceSubDeviceListKey] = [[kAudioSubDeviceUIDKey: clockUID]]
                logInfo("Aggregate clocked by main sub-device \(clockUID)")
            default:
                break
            }
        }
        logInfo(
            "Tap config: pin=\(experimentEnv["SESSIONTAPE_TAP_PIN"] ?? "0") drift=\(driftEnabled) clock=\(clockMode) req_buf=\(requestedBufferFrames)"
        )
        var aggDevID: AudioObjectID = 0
        let aggStatus = AudioHardwareCreateAggregateDevice(aggDesc as CFDictionary, &aggDevID)
        guard aggStatus == noErr else {
            throw Self.coreAudioError(
                Int(aggStatus),
                "AudioHardwareCreateAggregateDevice failed: \(osStatusDescription(aggStatus))")
        }
        self.aggregateDeviceID = aggDevID

        let maxFrames = queryMaxBufferFrames(for: aggDevID)
        if requestedBufferFrames > 0 {
            configureAggregateBufferSize(
                for: aggDevID, requestedFrames: requestedBufferFrames, maxRangeFrames: maxFrames)
        }
        reserveScratch(maxRangeFrames: maxFrames)
        logBufferFrameSizes(aggregate: aggDevID, process: processObj)

        // Overload is dispatched synchronously from the IO thread: atomic increment only.
        let halCounters = counters
        addListener(
            aggDevID, selector: kAudioDeviceProcessorOverload, queue: halQueue,
            name: "processor overload"
        ) { _, _ in
            halCounters.overloads.wrappingAdd(1, ordering: .relaxed)
        }
        addListener(
            aggDevID, selector: kAudioDevicePropertyIOStoppedAbnormally, queue: controlQueue,
            name: "IO stopped abnormally"
        ) { [weak self] _, _ in
            halCounters.ioStopped.wrappingAdd(1, ordering: .relaxed)
            self?.handleIOStoppedAbnormally()
        }

        var procID: AudioDeviceIOProcID?
        let procStatus = AudioDeviceCreateIOProcID(
            aggDevID,
            { (dev, now, inputData, inputTime, outputData, outputTime, clientData) -> OSStatus in
                guard let clientData = clientData else { return noErr }
                return Unmanaged<CoreAudioTapCapture>.fromOpaque(clientData)
                    ._withUnsafeGuaranteedRef { $0.handleAudioIO(inputData: inputData) }
            },
            Unmanaged.passUnretained(self).toOpaque(),
            &procID
        )
        guard procStatus == noErr, let validProcID = procID else {
            throw Self.coreAudioError(
                Int(procStatus),
                "AudioDeviceCreateIOProcID failed: \(osStatusDescription(procStatus))")
        }
        self.ioProcID = validProcID

        let startStatus = AudioDeviceStart(aggDevID, validProcID)
        guard startStatus == noErr else {
            throw Self.coreAudioError(
                Int(startStatus),
                "AudioDeviceStart failed: \(osStatusDescription(startStatus))")
        }

        let nowNs = clock_gettime_nsec_np(CLOCK_UPTIME_RAW)
        startedUptimeNs = nowNs
        lastCallbackUptimeNs.store(nowNs, ordering: .relaxed)

        // Emit TAP_FORMAT only after AudioDeviceStart returns: macOS TCC can block AudioDeviceStart
        // while prompting the user for System Audio Recording permission.
        fputs("TAP_FORMAT rate=\(tapRate) channels=\(tapFormat.mChannelsPerFrame)\n", stderr)
        fflush(stderr)

        logInfo(
            "CoreAudio process tap started successfully (tap ID: \(newTapID), aggregate ID: \(aggDevID))"
        )
    }

    private func bufferFrameSize(_ device: AudioObjectID) -> UInt32? {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyBufferFrameSize,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var frames: UInt32 = 0
        var size = UInt32(MemoryLayout<UInt32>.size)
        return AudioObjectGetPropertyData(device, &address, 0, nil, &size, &frames) == noErr
            ? frames : nil
    }

    /// Logs the IO buffer size of the aggregate and of each device the target process plays to.
    private func logBufferFrameSizes(aggregate: AudioObjectID, process: AudioObjectID) {
        var parts = ["aggregate=\(bufferFrameSize(aggregate).map(String.init) ?? "?")"]
        if process != kAudioObjectUnknown {
            var devAddress = AudioObjectPropertyAddress(
                mSelector: kAudioProcessPropertyDevices,
                mScope: kAudioObjectPropertyScopeOutput,
                mElement: kAudioObjectPropertyElementMain
            )
            var devSize: UInt32 = 0
            if AudioObjectGetPropertyDataSize(process, &devAddress, 0, nil, &devSize) == noErr,
                devSize > 0
            {
                var devices = [AudioObjectID](
                    repeating: 0, count: Int(devSize) / MemoryLayout<AudioObjectID>.size)
                if AudioObjectGetPropertyData(process, &devAddress, 0, nil, &devSize, &devices)
                    == noErr
                {
                    for dev in devices {
                        let uid = queryDeviceUID(dev) ?? "device#\(dev)"
                        parts.append("\(uid)=\(bufferFrameSize(dev).map(String.init) ?? "?")")
                    }
                }
            }
        }
        logInfo("IO buffer frame sizes: \(parts.joined(separator: " "))")
    }

    private func handleIOStoppedAbnormally() {
        guard !stopping.load(ordering: .relaxed) else { return }
        logError("HAL reported IO stopped abnormally on the capture aggregate device; exiting for restart")
        guard !restartExitFired.exchange(true, ordering: .sequentiallyConsistent) else { return }
        fputs("IO_STOPPED_ABNORMALLY\n", stderr)
        DispatchQueue.global().async { [self] in
            teardown()
            exit(tapFormatChangedExitStatus)
        }
    }

    private func handleAudioServiceRestarted() {
        guard !stopping.load(ordering: .relaxed) else { return }
        logError("coreaudiod restarted; HAL tap and aggregate handles are stale; exiting for restart")
        guard !restartExitFired.exchange(true, ordering: .sequentiallyConsistent) else { return }
        fputs("AUDIO_SERVICE_RESTARTED\n", stderr)
        DispatchQueue.global().async { [self] in
            teardown()
            exit(tapFormatChangedExitStatus)
        }
    }

    static func isCallbackStalled(lastCallbackNs: UInt64, nowNs: UInt64, timeoutNs: UInt64 = tapStallTimeoutNs) -> Bool {
        lastCallbackNs > 0 && nowNs > lastCallbackNs && (nowNs - lastCallbackNs) > timeoutNs
    }

    func checkHealth() {
        guard !stopping.load(ordering: .relaxed) else { return }
        let nowNs = clock_gettime_nsec_np(CLOCK_UPTIME_RAW)
        let lastNs = lastCallbackUptimeNs.load(ordering: .relaxed)
        if Self.isCallbackStalled(lastCallbackNs: lastNs, nowNs: nowNs) {
            let stalledSec = Double(nowNs - lastNs) / 1_000_000_000.0
            guard !restartExitFired.exchange(true, ordering: .sequentiallyConsistent) else { return }
            fputs(String(format: "TAP_STALLED idle_s=%.1f\n", stalledSec), stderr)
            logError(
                String(
                    format: "CoreAudio tap IO callback stalled for %.1fs; exiting for restart",
                    stalledSec)
            )
            DispatchQueue.global().async { [self] in
                teardown()
                exit(tapFormatChangedExitStatus)
            }
            return
        }

        if !silentWarningLogged && startedUptimeNs > 0
            && !fastConverter.seenNonZeroAudio.load(ordering: .relaxed)
            && nowNs > startedUptimeNs && (nowNs - startedUptimeNs) >= silentStartupWarningNs
        {
            silentWarningLogged = true
            logWarning(
                "No non-zero audio samples received after 60s from PID \(pid). If audio is playing, grant System Audio Recording permission in System Settings > Privacy & Security > Audio & Video Recording."
            )
        }
    }

    private func bufferFrameSizeRange(_ device: AudioObjectID) -> (min: Int, max: Int)? {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyBufferFrameSizeRange,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var range = AudioValueRange()
        var size = UInt32(MemoryLayout<AudioValueRange>.size)
        let status = AudioObjectGetPropertyData(device, &address, 0, nil, &size, &range)
        guard status == noErr, range.mMaximum.isFinite, range.mMaximum > 0 else {
            logInfo("Aggregate IO buffer frame size range unavailable (\(osStatusDescription(status)))")
            return nil
        }
        let minFrames = range.mMinimum.isFinite && range.mMinimum > 0 ? Int(range.mMinimum) : 1
        let maxFrames = Int(range.mMaximum)
        logInfo("Aggregate IO buffer frame size range: \(minFrames)..\(maxFrames) frames")
        return (minFrames, maxFrames)
    }

    private func queryMaxBufferFrames(for device: AudioObjectID) -> Int? {
        bufferFrameSizeRange(device)?.max
    }

    /// Raises the private capture aggregate's IO buffer (default 4096 frames ≈ 85 ms @ 48 kHz) so
    /// coreaudiod and mac-capture wake ~8x less often without changing the target app's output buffer.
    private func configureAggregateBufferSize(
        for device: AudioObjectID, requestedFrames: Int, maxRangeFrames: Int?
    ) {
        let clamped = maxRangeFrames.map { min(requestedFrames, $0) } ?? requestedFrames
        guard clamped > 0 else { return }
        var frames = UInt32(clamped)
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyBufferFrameSize,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        let status = AudioObjectSetPropertyData(
            device, &address, 0, nil, UInt32(MemoryLayout<UInt32>.size), &frames)
        if status != noErr {
            logError(
                "Setting aggregate IO buffer frame size to \(frames) failed: \(osStatusDescription(status))"
            )
        }
    }

    /// Sizes the converter scratch from the aggregate's largest IO buffer so the IO proc never
    /// has to truncate. Runs before AudioDeviceStart (allocation is not allowed on the IO thread).
    private func reserveScratch(maxRangeFrames: Int?) {
        let floorFrames = 16384
        let ceilingFrames = 1 << 20
        var frames = floorFrames
        if let maxRangeFrames, maxRangeFrames > 0 {
            frames = max(floorFrames, min(maxRangeFrames, ceilingFrames))
        }
        fastConverter.reserve(maxFrames: frames)
    }

    func handleAudioIO(inputData: UnsafePointer<AudioBufferList>) -> OSStatus {
        guard !drainThread.broken else { return noErr }
        let start = clock_gettime_nsec_np(CLOCK_UPTIME_RAW)
        lastCallbackUptimeNs.store(start, ordering: .relaxed)

        let list = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inputData))
        fastConverter.enqueueTapFloat(
            list, channels: tapChannels, interleaved: tapInterleaved, queue: outboundQueue)

        if metricsEnabled {
            let elapsedNs = clock_gettime_nsec_np(CLOCK_UPTIME_RAW) &- start
            metrics.recordCallback(elapsedNs: elapsedNs, usedFallback: false)
        }
        return noErr
    }

    func stop() async throws {
        teardown()
    }

    /// Idempotent; safe to call from any thread and more than once. Listeners are removed first
    /// and the control queue drained, so no handler runs against destroyed objects.
    private func teardown() {
        stopping.store(true, ordering: .sequentiallyConsistent)
        teardownLock.lock()
        defer { teardownLock.unlock() }
        guard !didTeardown else { return }
        didTeardown = true

        removeListeners()
        controlQueue.sync {}

        if let procID = ioProcID, aggregateDeviceID != kAudioObjectUnknown {
            AudioDeviceStop(aggregateDeviceID, procID)
            AudioDeviceDestroyIOProcID(aggregateDeviceID, procID)
            self.ioProcID = nil
        }
        if aggregateDeviceID != kAudioObjectUnknown {
            AudioHardwareDestroyAggregateDevice(aggregateDeviceID)
            self.aggregateDeviceID = kAudioObjectUnknown
        }
        if tapID != kAudioObjectUnknown {
            AudioHardwareDestroyProcessTap(tapID)
            self.tapID = kAudioObjectUnknown
        }
        drainThread.stop()
        tickMetrics(force: true)
        logInfo("CoreAudio process tap stopped")
    }

    func tickMetrics(force: Bool = false) {
        guard metricsEnabled else { return }
        metrics.emitIfDue(
            bytesWritten: drainThread.bytesWritten,
            droppedWrites: outboundQueue.droppedChunks,
            emit: { [self] line in
                // Per-window deltas, like the other fields.
                let overloads = counters.overloads.exchange(0, ordering: .relaxed)
                let stopped = counters.ioStopped.exchange(0, ordering: .relaxed)
                let truncated = fastConverter.truncatedBuffers.exchange(0, ordering: .relaxed)
                logInfo(
                    "\(line) hal_overload=\(overloads) io_stopped=\(stopped) trunc=\(truncated)")
            },
            force: force
        )
    }
}
