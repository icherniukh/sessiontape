import Foundation
import XCTest

@testable import mac_capture

/// Mutable state shared between a test and a helper thread; access is ordered by the
/// expectation/join the test waits on.
private final class Box<T>: @unchecked Sendable {
    var value: T
    init(_ value: T) { self.value = value }
}

final class PCMOutboundQueueTests: XCTestCase {
    private func bytes(_ range: Range<Int>) -> [UInt8] { range.map { UInt8(truncatingIfNeeded: $0) } }

    private func enqueue(_ queue: PCMOutboundQueue, _ array: [UInt8]) -> Bool {
        array.withUnsafeBytes { queue.enqueue(bytes: $0.baseAddress!, byteCount: array.count) }
    }

    private func dequeue(_ queue: PCMOutboundQueue, max: Int = 65536) -> [UInt8] {
        var dest = [UInt8](repeating: 0, count: max)
        let n = dest.withUnsafeMutableBytes { queue.dequeueBlocking(into: $0.baseAddress!, maxBytes: max) }
        return Array(dest[0..<n])
    }

    func testCapacityIsBoundedInMilliseconds() {
        let q = PCMOutboundQueue(sampleRate: 48_000, channels: 2, bytesPerSample: 2, bufferMs: 2000)
        // 48000 * 4 bytes * 2 s = 384000 bytes -> next power of two
        XCTAssertEqual(q.capacity, 524_288)
        XCTAssertEqual(PCMOutboundQueue(capacityBytes: 3000).capacity, 4096)
    }

    func testWrapAroundPreservesBytes() {
        let q = PCMOutboundQueue(capacityBytes: 1024)
        XCTAssertEqual(q.capacity, 1024)
        // Advance head/tail close to the end of the ring, then write across the boundary.
        XCTAssertTrue(enqueue(q, bytes(0..<1000)))
        XCTAssertEqual(dequeue(q).count, 1000)
        let payload = bytes(0..<200)
        XCTAssertTrue(enqueue(q, payload))
        XCTAssertEqual(dequeue(q), payload)
        XCTAssertEqual(q.depth, 0)
    }

    func testFullQueueDropsNewestAndCounts() {
        let q = PCMOutboundQueue(capacityBytes: 1024)
        XCTAssertTrue(enqueue(q, bytes(0..<1024)))
        XCTAssertFalse(enqueue(q, bytes(0..<100)))
        XCTAssertFalse(enqueue(q, bytes(0..<50)))
        XCTAssertEqual(q.droppedChunks, 2)
        XCTAssertEqual(q.droppedBytes, 150)
        XCTAssertEqual(q.enqueuedBytes, 1024)
        // Oldest data is retained intact (drop-newest).
        XCTAssertEqual(dequeue(q), bytes(0..<1024))
        // Space is available again after draining.
        XCTAssertTrue(enqueue(q, bytes(0..<100)))
    }

    func testCloseThenDrainReturnsRemainingBytesThenZero() {
        let q = PCMOutboundQueue(capacityBytes: 1024)
        let payload = bytes(0..<300)
        XCTAssertTrue(enqueue(q, payload))
        q.close()
        XCTAssertEqual(dequeue(q), payload)
        let start = Date()
        XCTAssertEqual(dequeue(q).count, 0)
        XCTAssertEqual(dequeue(q).count, 0)
        XCTAssertLessThan(Date().timeIntervalSince(start), 1.0)
        XCTAssertFalse(enqueue(q, payload), "enqueue after close must be rejected")
    }

    func testBlockedConsumerWakesOnClose() {
        let q = PCMOutboundQueue(capacityBytes: 1024)
        let done = expectation(description: "consumer returned")
        let result = Box(-1)
        DispatchQueue.global().async {
            var dest = [UInt8](repeating: 0, count: 64)
            result.value = dest.withUnsafeMutableBytes { q.dequeueBlocking(into: $0.baseAddress!, maxBytes: 64) }
            done.fulfill()
        }
        Thread.sleep(forTimeInterval: 0.2)
        q.close()
        wait(for: [done], timeout: 2.0)
        XCTAssertEqual(result.value, 0)
    }

    func testConcurrentProducerConsumerIntegrity() {
        let q = PCMOutboundQueue(capacityBytes: 4096)
        let totalBytes = 8 * 1024 * 1024
        let chunkSizes = [1, 7, 64, 191, 512, 1000]
        let done = expectation(description: "consumer done")
        let received = Box(0)
        let mismatches = Box(0)

        DispatchQueue.global().async {
            var dest = [UInt8](repeating: 0, count: 777)
            while true {
                let n = dest.withUnsafeMutableBytes { q.dequeueBlocking(into: $0.baseAddress!, maxBytes: 777) }
                if n == 0 { break }
                for i in 0..<n where dest[i] != UInt8(truncatingIfNeeded: received.value &+ i) {
                    mismatches.value += 1
                }
                received.value += n
            }
            done.fulfill()
        }

        var sent = 0
        var iteration = 0
        var chunk = [UInt8](repeating: 0, count: 1000)
        while sent < totalBytes {
            let size = min(chunkSizes[iteration % chunkSizes.count], totalBytes - sent)
            iteration += 1
            for i in 0..<size { chunk[i] = UInt8(truncatingIfNeeded: sent &+ i) }
            // Retry until accepted so the stream stays contiguous; the queue itself drops on full.
            while !chunk.withUnsafeBytes({ q.enqueue(bytes: $0.baseAddress!, byteCount: size) }) {
                Thread.sleep(forTimeInterval: 0.0001)
            }
            sent += size
        }
        q.close()
        wait(for: [done], timeout: 60)
        XCTAssertEqual(received.value, totalBytes)
        XCTAssertEqual(mismatches.value, 0)
    }

    func testDrainThreadWritesEveryByteToPipe() throws {
        var fds: [Int32] = [0, 0]
        XCTAssertEqual(pipe(&fds), 0)
        let readFD = fds[0], writeFD = fds[1]

        let q = PCMOutboundQueue(capacityBytes: 64 * 1024)
        let drain = PCMOutboundDrainThread(queue: q, fd: writeFD)
        drain.start()

        let total = 4 * 1024 * 1024
        let collected = Box(Data())
        let readDone = expectation(description: "reader EOF")
        DispatchQueue.global().async {
            var buf = [UInt8](repeating: 0, count: 32768)
            while true {
                let n = buf.withUnsafeMutableBytes { read(readFD, $0.baseAddress!, 32768) }
                if n <= 0 { break }
                collected.value.append(contentsOf: buf[0..<n])
            }
            readDone.fulfill()
        }

        var sent = 0
        var chunk = [UInt8](repeating: 0, count: 3840)
        while sent < total {
            for i in 0..<chunk.count { chunk[i] = UInt8(truncatingIfNeeded: sent &+ i) }
            while !chunk.withUnsafeBytes({ q.enqueue(bytes: $0.baseAddress!, byteCount: chunk.count) }) {
                Thread.sleep(forTimeInterval: 0.0001)
            }
            sent += chunk.count
        }

        // stop() must join the drain thread, i.e. everything is written when it returns.
        XCTAssertTrue(drain.stop())
        XCTAssertEqual(drain.bytesWritten, sent)
        close(writeFD)
        wait(for: [readDone], timeout: 30)
        close(readFD)

        XCTAssertEqual(collected.value.count, sent)
        var ok = true
        collected.value.withUnsafeBytes { raw in
            for i in 0..<raw.count where raw[i] != UInt8(truncatingIfNeeded: i) {
                ok = false
                break
            }
        }
        XCTAssertTrue(ok, "byte pattern mismatch")
        XCTAssertTrue(drain.stop(), "stop must be idempotent")
    }

    func testStopWithoutStartIsNoOp() {
        let drain = PCMOutboundDrainThread(queue: PCMOutboundQueue(capacityBytes: 1024), fd: -1)
        XCTAssertTrue(drain.stop())
    }
}

final class CaptureMetricsTests: XCTestCase {
    func testP99IsClampedToMax() {
        // All callbacks fall into the 5ms+ bucket whose ceiling is 10ms, but the real max is 6.2ms.
        let counts = [0, 0, 0, 0, 0, 0, 0, 100]
        XCTAssertEqual(
            CaptureMetrics.p99Ms(bucketCounts: counts, totalCallbacks: 100, maxMs: 6.2), 6.2, accuracy: 1e-9)
    }

    func testP99UsesBucketCeilingWhenBelowMax() {
        let counts = [90, 9, 0, 0, 0, 0, 0, 1]
        XCTAssertEqual(
            CaptureMetrics.p99Ms(bucketCounts: counts, totalCallbacks: 100, maxMs: 7.0), 0.10, accuracy: 1e-9)
    }

    func testP99ZeroWhenNoCallbacks() {
        XCTAssertEqual(
            CaptureMetrics.p99Ms(bucketCounts: [0, 0, 0, 0, 0, 0, 0, 0], totalCallbacks: 0, maxMs: 0), 0)
    }
}
