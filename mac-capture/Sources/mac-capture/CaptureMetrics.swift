import Foundation
import Synchronization

/// Real-time safe rolling window metrics for audio callback monitoring.
/// Strictly lock-free and zero heap allocations on the audio thread.
final class CaptureMetrics: @unchecked Sendable {
    private let windowNs: UInt64
    private let slowThresholdNs: UInt64

    private let windowStartNs = Atomic<UInt64>(0)
    private let callbackCount = Atomic<Int>(0)
    private let slowCount = Atomic<Int>(0)
    private let fallbackCount = Atomic<Int>(0)
    private let maxLatencyNs = Atomic<UInt64>(0)
    private let lastDroppedWrites = Atomic<Int>(0)

    // Latency histogram buckets (lock-free percentile tracking):
    // 0: < 50us, 1: < 100us, 2: < 250us, 3: < 500us, 4: < 1ms, 5: < 2ms, 6: < 5ms, 7: >= 5ms
    private let b0 = Atomic<Int>(0)
    private let b1 = Atomic<Int>(0)
    private let b2 = Atomic<Int>(0)
    private let b3 = Atomic<Int>(0)
    private let b4 = Atomic<Int>(0)
    private let b5 = Atomic<Int>(0)
    private let b6 = Atomic<Int>(0)
    private let b7 = Atomic<Int>(0)
    private static let bucketUpperLimitsMs: [Double] = [0.05, 0.10, 0.25, 0.50, 1.00, 2.00, 5.00, 10.00]

    init(windowSeconds: Double = 1.0, slowThresholdMs: Double = 5.0) {
        self.windowNs = UInt64(windowSeconds * 1_000_000_000.0)
        self.slowThresholdNs = UInt64(slowThresholdMs * 1_000_000.0)
        self.windowStartNs.store(DispatchTime.now().uptimeNanoseconds, ordering: .relaxed)
    }

    func recordCallback(elapsedNs: UInt64, usedFallback: Bool) {
        callbackCount.wrappingAdd(1, ordering: .relaxed)
        if usedFallback {
            fallbackCount.wrappingAdd(1, ordering: .relaxed)
        }
        if elapsedNs > slowThresholdNs {
            slowCount.wrappingAdd(1, ordering: .relaxed)
        }

        var currentMax = maxLatencyNs.load(ordering: .relaxed)
        while elapsedNs > currentMax {
            let (exchanged, original) = maxLatencyNs.compareExchange(
                expected: currentMax,
                desired: elapsedNs,
                successOrdering: .relaxed,
                failureOrdering: .relaxed
            )
            if exchanged { break }
            currentMax = original
        }

        if elapsedNs < 50_000 {
            b0.wrappingAdd(1, ordering: .relaxed)
        } else if elapsedNs < 100_000 {
            b1.wrappingAdd(1, ordering: .relaxed)
        } else if elapsedNs < 250_000 {
            b2.wrappingAdd(1, ordering: .relaxed)
        } else if elapsedNs < 500_000 {
            b3.wrappingAdd(1, ordering: .relaxed)
        } else if elapsedNs < 1_000_000 {
            b4.wrappingAdd(1, ordering: .relaxed)
        } else if elapsedNs < 2_000_000 {
            b5.wrappingAdd(1, ordering: .relaxed)
        } else if elapsedNs < 5_000_000 {
            b6.wrappingAdd(1, ordering: .relaxed)
        } else {
            b7.wrappingAdd(1, ordering: .relaxed)
        }
    }

    func recordCallback(elapsedMs: Double, usedFallback: Bool) {
        let ns = UInt64(max(0.0, elapsedMs * 1_000_000.0))
        recordCallback(elapsedNs: ns, usedFallback: usedFallback)
    }

    func emitIfDue(
        bytesWritten: Int,
        droppedWrites: Int,
        emit: (String) -> Void,
        force: Bool = false
    ) {
        let now = DispatchTime.now().uptimeNanoseconds
        let start = windowStartNs.load(ordering: .relaxed)
        let elapsed = now &- start
        guard force || elapsed >= windowNs else { return }

        windowStartNs.store(now, ordering: .relaxed)

        let totalCallbacks = callbackCount.exchange(0, ordering: .relaxed)
        let slow = slowCount.exchange(0, ordering: .relaxed)
        let fallback = fallbackCount.exchange(0, ordering: .relaxed)
        let maxNs = maxLatencyNs.exchange(0, ordering: .relaxed)

        let lastDrops = lastDroppedWrites.load(ordering: .relaxed)
        let drops = max(droppedWrites - lastDrops, 0)
        lastDroppedWrites.store(droppedWrites, ordering: .relaxed)

        let counts = [
            b0.exchange(0, ordering: .relaxed),
            b1.exchange(0, ordering: .relaxed),
            b2.exchange(0, ordering: .relaxed),
            b3.exchange(0, ordering: .relaxed),
            b4.exchange(0, ordering: .relaxed),
            b5.exchange(0, ordering: .relaxed),
            b6.exchange(0, ordering: .relaxed),
            b7.exchange(0, ordering: .relaxed),
        ]

        var p99Ms = 0.0
        if totalCallbacks > 0 {
            let targetCount = Int(Double(totalCallbacks) * 0.99)
            var accumulated = 0
            for idx in 0..<counts.count {
                accumulated += counts[idx]
                if accumulated >= targetCount {
                    p99Ms = Self.bucketUpperLimitsMs[idx]
                    break
                }
            }
        }

        let maxMs = Double(maxNs) / 1_000_000.0

        let line = String(
            format: "CAPTURE drops=%d cb_max=%.2f cb_p99=%.2f slow=%d fb=%d cb=%d bytes=%d",
            drops,
            maxMs,
            p99Ms,
            slow,
            fallback,
            totalCallbacks,
            bytesWritten
        )
        emit(line)
    }
}
