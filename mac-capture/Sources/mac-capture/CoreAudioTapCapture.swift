import AppKit
import AudioToolbox
import CoreAudio
import Foundation

/// Thrown by `CoreAudioTapCapture.start()` (after teardown) when the tap's sample rate differs
/// from the requested rate. main.swift maps it to `FORMAT_MISMATCH ...` on stderr + exit status 3.
struct TapFormatMismatch: Error {
    let tapRate: Int
    let requestedRate: Int
}

@available(macOS 14.2, *)
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

    private func resolveBundleID() -> String {
        if let app = NSRunningApplication(processIdentifier: pid),
            let bundleID = app.bundleIdentifier, !bundleID.isEmpty
        {
            return bundleID
        }
        return "com.pioneerdj.rekordboxdj"
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

    private func queryDeviceUID(_ deviceID: AudioObjectID) -> String? {
        var uidAddress = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyDeviceUID,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var uid: CFString = "" as CFString
        var uidSize = UInt32(MemoryLayout<CFString>.size)
        let status = withUnsafeMutablePointer(to: &uid) { ptr in
            AudioObjectGetPropertyData(deviceID, &uidAddress, 0, nil, &uidSize, ptr)
        }
        guard status == noErr else { return nil }
        return uid as String
    }

    private func resolveActiveDeviceUID() -> (deviceID: AudioObjectID, uid: String)? {
        let processObj = resolveProcessObject()
        self.targetProcessObject = processObj

        if processObj != kAudioObjectUnknown {
            var devAddress = AudioObjectPropertyAddress(
                mSelector: kAudioProcessPropertyDevices,
                mScope: kAudioObjectPropertyScopeOutput,
                mElement: kAudioObjectPropertyElementMain
            )
            var devSize: UInt32 = 0
            let devStatus = AudioObjectGetPropertyDataSize(
                processObj, &devAddress, 0, nil, &devSize)
            if devStatus == noErr && devSize > 0 {
                let count = Int(devSize) / MemoryLayout<AudioObjectID>.size
                var devices = [AudioObjectID](repeating: 0, count: count)
                if AudioObjectGetPropertyData(
                    processObj, &devAddress, 0, nil, &devSize, &devices) == noErr,
                    let firstDev = devices.first
                {
                    if let uid = queryDeviceUID(firstDev) {
                        return (firstDev, uid)
                    }
                }
            }
        }

        // Fallback to system default output device
        var defaultDevAddress = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultOutputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var defaultDevID: AudioObjectID = 0
        var defaultSize = UInt32(MemoryLayout<AudioObjectID>.size)
        if AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject),
            &defaultDevAddress,
            0,
            nil,
            &defaultSize,
            &defaultDevID
        ) == noErr {
            if let uid = queryDeviceUID(defaultDevID) {
                return (defaultDevID, uid)
            }
        }

        return nil
    }

    func start() async throws {
        drainThread.start()

        let bundleID = resolveBundleID()
        logInfo("Configuring CoreAudio process tap for bundle: \(bundleID) (PID: \(pid))")

        let desc = CATapDescription()
        if #available(macOS 26.0, *) {
            desc.bundleIDs = [bundleID]
            desc.isProcessRestoreEnabled = true
        } else {
            let procObj = resolveProcessObject()
            guard procObj != kAudioObjectUnknown else {
                throw NSError(
                    domain: "CoreAudioTap",
                    code: 1,
                    userInfo: [
                        NSLocalizedDescriptionKey:
                            "Failed to resolve ProcessObject for PID \(pid)"
                    ]
                )
            }
            desc.processes = [procObj]
        }
        desc.isPrivate = true
        desc.muteBehavior = .unmuted
        desc.isMixdown = true

        if let active = resolveActiveDeviceUID() {
            desc.deviceUID = active.uid
            logInfo(
                "CoreAudio tap anchored to active device: \(active.uid) (ID: \(active.deviceID))"
            )
        } else {
            logInfo(
                "No active output device found for PID \(pid); defaulting tap to system output"
            )
        }

        var newTapID: AudioObjectID = kAudioObjectUnknown
        let tapStatus = AudioHardwareCreateProcessTap(desc, &newTapID)
        guard tapStatus == noErr else {
            drainThread.stop()
            throw NSError(
                domain: "CoreAudioTap",
                code: Int(tapStatus),
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "AudioHardwareCreateProcessTap failed: \(osStatusDescription(tapStatus))"
                ]
            )
        }
        self.tapID = newTapID

        let aggUID = UUID().uuidString
        let aggDesc: [String: Any] = [
            kAudioAggregateDeviceNameKey: "sessiontape-tap-\(aggUID)",
            kAudioAggregateDeviceUIDKey: aggUID,
            kAudioAggregateDeviceIsPrivateKey: 1 as NSNumber,
            kAudioAggregateDeviceIsStackedKey: 0 as NSNumber,
        ]
        var aggDevID: AudioObjectID = 0
        let aggStatus = AudioHardwareCreateAggregateDevice(
            aggDesc as CFDictionary, &aggDevID)
        guard aggStatus == noErr else {
            AudioHardwareDestroyProcessTap(newTapID)
            self.tapID = kAudioObjectUnknown
            drainThread.stop()
            throw NSError(
                domain: "CoreAudioTap",
                code: Int(aggStatus),
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "AudioHardwareCreateAggregateDevice failed: \(osStatusDescription(aggStatus))"
                ]
            )
        }
        self.aggregateDeviceID = aggDevID

        var formatAddress = AudioObjectPropertyAddress(
            mSelector: kAudioTapPropertyFormat,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var tapFormat = AudioStreamBasicDescription()
        var formatSize = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        let formatStatus = AudioObjectGetPropertyData(
            newTapID, &formatAddress, 0, nil, &formatSize, &tapFormat
        )
        guard formatStatus == noErr else {
            try? await stop()
            throw NSError(
                domain: "CoreAudioTap",
                code: Int(formatStatus),
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "Reading kAudioTapPropertyFormat failed: \(osStatusDescription(formatStatus))"
                ]
            )
        }
        logInfo(
            "CoreAudio tap format: \(Int(tapFormat.mSampleRate))Hz, \(tapFormat.mChannelsPerFrame) channels, flags=0x\(String(tapFormat.mFormatFlags, radix: 16))"
        )

        let tapRate = Int(tapFormat.mSampleRate.rounded())
        if tapRate != sampleRate {
            try? await stop()
            throw TapFormatMismatch(tapRate: tapRate, requestedRate: sampleRate)
        }
        let isFloat32PCM =
            tapFormat.mFormatID == kAudioFormatLinearPCM
            && (tapFormat.mFormatFlags & kAudioFormatFlagIsFloat) != 0
            && tapFormat.mBitsPerChannel == 32
        guard isFloat32PCM, tapFormat.mChannelsPerFrame == 1 || tapFormat.mChannelsPerFrame == 2
        else {
            try? await stop()
            throw NSError(
                domain: "CoreAudioTap",
                code: 2,
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "Unsupported tap format: formatID=\(tapFormat.mFormatID) flags=0x\(String(tapFormat.mFormatFlags, radix: 16)) bits=\(tapFormat.mBitsPerChannel) channels=\(tapFormat.mChannelsPerFrame) (need Float32 PCM, 1 or 2 channels)"
                ]
            )
        }
        tapChannels = Int(tapFormat.mChannelsPerFrame)
        tapInterleaved = (tapFormat.mFormatFlags & kAudioFormatFlagIsNonInterleaved) == 0

        var tapUIDAddress = AudioObjectPropertyAddress(
            mSelector: kAudioTapPropertyUID,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var tapUID: CFString = "" as CFString
        var tapUIDSize = UInt32(MemoryLayout<CFString>.size)
        let uidStatus = withUnsafeMutablePointer(to: &tapUID) { ptr in
            AudioObjectGetPropertyData(newTapID, &tapUIDAddress, 0, nil, &tapUIDSize, ptr)
        }
        guard uidStatus == noErr else {
            AudioHardwareDestroyAggregateDevice(aggDevID)
            AudioHardwareDestroyProcessTap(newTapID)
            self.aggregateDeviceID = kAudioObjectUnknown
            self.tapID = kAudioObjectUnknown
            drainThread.stop()
            throw NSError(
                domain: "CoreAudioTap",
                code: Int(uidStatus),
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "AudioObjectGetPropertyData (tap UID) failed: \(osStatusDescription(uidStatus))"
                ]
            )
        }

        var tapListAddress = AudioObjectPropertyAddress(
            mSelector: kAudioAggregateDevicePropertyTapList,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var tapArray = [tapUID] as CFArray
        let tapArraySize = UInt32(MemoryLayout<CFArray>.size)
        let setStatus = withUnsafePointer(to: &tapArray) { ptr in
            AudioObjectSetPropertyData(aggDevID, &tapListAddress, 0, nil, tapArraySize, ptr)
        }
        guard setStatus == noErr else {
            AudioHardwareDestroyAggregateDevice(aggDevID)
            AudioHardwareDestroyProcessTap(newTapID)
            self.aggregateDeviceID = kAudioObjectUnknown
            self.tapID = kAudioObjectUnknown
            drainThread.stop()
            throw NSError(
                domain: "CoreAudioTap",
                code: Int(setStatus),
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "AudioObjectSetPropertyData (tap list) failed: \(osStatusDescription(setStatus))"
                ]
            )
        }

        var procID: AudioDeviceIOProcID?
        let procStatus = AudioDeviceCreateIOProcID(
            aggDevID,
            { (dev, now, inputData, inputTime, outputData, outputTime, clientData) -> OSStatus in
                guard let clientData = clientData else { return noErr }
                let tapCapture = Unmanaged<CoreAudioTapCapture>.fromOpaque(clientData)
                    .takeUnretainedValue()
                return tapCapture.handleAudioIO(inputData: inputData)
            },
            Unmanaged.passUnretained(self).toOpaque(),
            &procID
        )
        guard procStatus == noErr, let validProcID = procID else {
            AudioHardwareDestroyAggregateDevice(aggDevID)
            AudioHardwareDestroyProcessTap(newTapID)
            self.aggregateDeviceID = kAudioObjectUnknown
            self.tapID = kAudioObjectUnknown
            drainThread.stop()
            throw NSError(
                domain: "CoreAudioTap",
                code: Int(procStatus),
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "AudioDeviceCreateIOProcID failed: \(osStatusDescription(procStatus))"
                ]
            )
        }
        self.ioProcID = validProcID

        let startStatus = AudioDeviceStart(aggDevID, validProcID)
        guard startStatus == noErr else {
            AudioDeviceDestroyIOProcID(aggDevID, validProcID)
            AudioHardwareDestroyAggregateDevice(aggDevID)
            AudioHardwareDestroyProcessTap(newTapID)
            self.ioProcID = nil
            self.aggregateDeviceID = kAudioObjectUnknown
            self.tapID = kAudioObjectUnknown
            drainThread.stop()
            throw NSError(
                domain: "CoreAudioTap",
                code: Int(startStatus),
                userInfo: [
                    NSLocalizedDescriptionKey:
                        "AudioDeviceStart failed: \(osStatusDescription(startStatus))"
                ]
            )
        }

        logInfo(
            "CoreAudio process tap started successfully (tap ID: \(newTapID), aggregate ID: \(aggDevID))"
        )
    }

    func handleAudioIO(inputData: UnsafePointer<AudioBufferList>) -> OSStatus {
        guard !drainThread.broken else { return noErr }
        let start = DispatchTime.now().uptimeNanoseconds

        let list = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inputData))
        fastConverter.enqueueTapFloat(
            list, channels: tapChannels, interleaved: tapInterleaved, queue: outboundQueue)

        if metricsEnabled {
            let elapsedNs = DispatchTime.now().uptimeNanoseconds &- start
            metrics.recordCallback(elapsedNs: elapsedNs, usedFallback: false)
        }
        return noErr
    }

    func stop() async throws {
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
            emit: { logInfo($0) },
            force: force
        )
    }
}
