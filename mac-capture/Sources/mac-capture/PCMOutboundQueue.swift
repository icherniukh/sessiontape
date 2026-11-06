import Foundation
import Synchronization

/// Mach semaphore wrapper. `semaphore_signal` is a plain Mach trap with no allocation and no
/// userspace lock, so it is safe to call from the real-time CoreAudio IO proc (unlike
/// `DispatchSemaphore`, whose slow path can touch the allocator and priority-inversion machinery).
final class MachSemaphore: @unchecked Sendable {
    private var sem: semaphore_t = 0

    init() {
        let result = semaphore_create(mach_task_self_, &sem, SYNC_POLICY_FIFO, 0)
        precondition(result == KERN_SUCCESS, "semaphore_create failed: \(result)")
    }

    deinit {
        semaphore_destroy(mach_task_self_, sem)
    }

    func signal() {
        semaphore_signal(sem)
    }

    /// Returns true when signalled, false on timeout (or any other wait failure).
    func wait(timeoutMs: Int) -> Bool {
        let ms = max(0, timeoutMs)
        let ts = mach_timespec_t(
            tv_sec: UInt32(ms / 1000), tv_nsec: clock_res_t((ms % 1000) * 1_000_000))
        return semaphore_timedwait(sem, ts) == KERN_SUCCESS
    }
}

/// Lock-free Single-Producer Single-Consumer (SPSC) circular byte ring buffer.
/// Audio callback thread acts as the sole producer (zero locks, zero heap allocations).
/// Stdout drain thread acts as the sole consumer.
///
/// Overflow policy: drop-newest. When the ring cannot hold a whole incoming chunk, that chunk is
/// discarded and counted. Dropping the oldest data instead would require the producer to advance
/// `tail`, but in an SPSC ring only the consumer may write `tail`; doing otherwise would need a
/// lock or CAS loop on the real-time thread. The ring is bounded in milliseconds (see `init`) so
/// that a stalled reader loses fresh audio rather than accumulating many seconds of stale audio.
final class PCMOutboundQueue: @unchecked Sendable {
    /// How long the consumer sleeps before re-checking the ring, so a lost wake-up can never hang it.
    static let waitTimeoutMs = 50

    let capacity: Int
    private let mask: Int
    private let buffer: UnsafeMutableRawPointer

    private let head = Atomic<Int>(0)
    private let tail = Atomic<Int>(0)
    private let droppedBytesAtomic = Atomic<Int>(0)
    private let droppedChunksAtomic = Atomic<Int>(0)
    private let enqueuedBytesAtomic = Atomic<Int>(0)
    private let isClosedAtomic = Atomic<Bool>(false)
    /// Set by the consumer just before it sleeps so the producer only signals when needed.
    private let consumerWaiting = Atomic<Bool>(false)
    private let ready = MachSemaphore()

    /// - Parameter bufferMs: ring size in milliseconds of audio; the byte size is rounded up to a
    ///   power of two (so the effective bound is between `bufferMs` and `2 * bufferMs`).
    init(sampleRate: Int = 48_000, channels: Int = 2, bytesPerSample: Int = 2, bufferMs: Int = 2000) {
        let bytesPerSecond = max(1, sampleRate) * max(1, channels) * max(1, bytesPerSample)
        let wanted = (bytesPerSecond * max(1, bufferMs) + 999) / 1000
        self.capacity = Self.roundedCapacity(wanted)
        self.mask = capacity - 1
        self.buffer = .allocate(byteCount: capacity, alignment: 64)
    }

    /// Byte-sized initializer, mainly for tests.
    init(capacityBytes: Int) {
        self.capacity = Self.roundedCapacity(capacityBytes)
        self.mask = capacity - 1
        self.buffer = .allocate(byteCount: capacity, alignment: 64)
    }

    private static func roundedCapacity(_ bytes: Int) -> Int {
        var cap = 1024
        while cap < bytes { cap <<= 1 }
        return cap
    }

    deinit {
        buffer.deallocate()
    }

    var droppedChunks: Int { droppedChunksAtomic.load(ordering: .relaxed) }
    var droppedBytes: Int { droppedBytesAtomic.load(ordering: .relaxed) }
    var enqueuedBytes: Int { enqueuedBytesAtomic.load(ordering: .relaxed) }
    var depth: Int {
        let h = head.load(ordering: .relaxed)
        let t = tail.load(ordering: .relaxed)
        return max(0, h - t)
    }

    @discardableResult
    func enqueue(bytes: UnsafeRawPointer, byteCount: Int) -> Bool {
        guard !isClosedAtomic.load(ordering: .relaxed), byteCount > 0 else { return false }

        let h = head.load(ordering: .relaxed)
        let t = tail.load(ordering: .acquiring)
        let freeSpace = capacity - (h - t)

        guard freeSpace >= byteCount else {
            droppedBytesAtomic.wrappingAdd(byteCount, ordering: .relaxed)
            droppedChunksAtomic.wrappingAdd(1, ordering: .relaxed)
            return false
        }

        let writeOffset = h & mask
        let firstChunk = min(byteCount, capacity - writeOffset)
        memcpy(buffer.advanced(by: writeOffset), bytes, firstChunk)
        if firstChunk < byteCount {
            memcpy(buffer, bytes.advanced(by: firstChunk), byteCount - firstChunk)
        }

        // Sequentially consistent store + load of `consumerWaiting` (Dekker-style with the
        // consumer) guarantees that either the consumer sees this data before sleeping or we see
        // the flag and signal. The consumer's timed wait covers any remaining gap.
        head.store(h + byteCount, ordering: .sequentiallyConsistent)
        enqueuedBytesAtomic.wrappingAdd(byteCount, ordering: .relaxed)
        if consumerWaiting.load(ordering: .sequentiallyConsistent) {
            ready.signal()
        }
        return true
    }

    func enqueue(_ data: Data) {
        data.withUnsafeBytes { raw in
            guard let base = raw.baseAddress else { return }
            enqueue(bytes: base, byteCount: data.count)
        }
    }

    /// Blocks until data is available and copies up to `maxBytes` into `destination`.
    /// Returns 0 only after `close()` once every remaining byte has been drained.
    func dequeueBlocking(into destination: UnsafeMutableRawPointer, maxBytes: Int) -> Int {
        while true {
            // Load `closed` before `head`: if closed was observed, any enqueue that completed
            // before close() is already visible in `head`.
            let closed = isClosedAtomic.load(ordering: .acquiring)
            let t = tail.load(ordering: .relaxed)
            let h = head.load(ordering: .acquiring)
            let available = h - t

            if available > 0 {
                let toRead = min(available, maxBytes)
                let readOffset = t & mask
                let firstChunk = min(toRead, capacity - readOffset)
                memcpy(destination, buffer.advanced(by: readOffset), firstChunk)
                if firstChunk < toRead {
                    memcpy(destination.advanced(by: firstChunk), buffer, toRead - firstChunk)
                }
                tail.store(t + toRead, ordering: .releasing)
                return toRead
            }

            if closed { return 0 }

            consumerWaiting.store(true, ordering: .sequentiallyConsistent)
            // Re-check after publishing the flag so a racing enqueue/close is not missed.
            if head.load(ordering: .sequentiallyConsistent) == t
                && !isClosedAtomic.load(ordering: .sequentiallyConsistent)
            {
                _ = ready.wait(timeoutMs: Self.waitTimeoutMs)
            }
            consumerWaiting.store(false, ordering: .relaxed)
        }
    }

    func close() {
        isClosedAtomic.store(true, ordering: .sequentiallyConsistent)
        ready.signal()
    }
}

/// Blocking fd writer used only on the drain thread (stdout in production).
final class StdoutDrain: @unchecked Sendable {
    private let fd: Int32
    private let bytesWrittenAtomic = Atomic<Int>(0)
    private let brokenAtomic = Atomic<Bool>(false)

    init(fd: Int32 = STDOUT_FILENO) {
        self.fd = fd
    }

    var bytesWritten: Int { bytesWrittenAtomic.load(ordering: .relaxed) }
    var broken: Bool { brokenAtomic.load(ordering: .relaxed) }

    func writeAll(_ data: Data) {
        guard !broken, !data.isEmpty else { return }
        data.withUnsafeBytes { rawBuffer in
            guard let base = rawBuffer.baseAddress else { return }
            writeAll(base, byteCount: data.count)
        }
    }

    func writeAll(_ data: UnsafeRawPointer, byteCount: Int) {
        guard !broken, byteCount > 0 else { return }

        var totalWritten = 0
        while totalWritten < byteCount {
            let remaining = byteCount - totalWritten
            let wrote = write(fd, data.advanced(by: totalWritten), remaining)
            if wrote > 0 {
                totalWritten += wrote
                bytesWrittenAtomic.wrappingAdd(wrote, ordering: .relaxed)
                continue
            }
            if wrote < 0 && errno == EINTR {
                continue
            }
            brokenAtomic.store(true, ordering: .releasing)
            logError("Failed to write \(remaining) bytes to stdout errno=\(errno)")
            return
        }
    }
}

final class PCMOutboundDrainThread: @unchecked Sendable {
    private let queue: PCMOutboundQueue
    private let writer: StdoutDrain
    private let finished = DispatchGroup()
    private let startLock = NSLock()
    private var started = false

    init(queue: PCMOutboundQueue, fd: Int32 = STDOUT_FILENO) {
        self.queue = queue
        self.writer = StdoutDrain(fd: fd)
    }

    var bytesWritten: Int { writer.bytesWritten }
    var droppedChunks: Int { queue.droppedChunks }
    var broken: Bool { writer.broken }

    func start() {
        startLock.lock()
        defer { startLock.unlock() }
        guard !started else { return }
        started = true

        finished.enter()
        let drainThread = Thread { [weak self, queue, writer, finished] in
            defer { finished.leave() }
            let bufferCapacity = 65536
            let dest = UnsafeMutableRawPointer.allocate(byteCount: bufferCapacity, alignment: 16)
            defer { dest.deallocate() }

            while true {
                if let self, self.broken { break }
                let bytesRead = queue.dequeueBlocking(into: dest, maxBytes: bufferCapacity)
                guard bytesRead > 0 else { break }
                writer.writeAll(dest, byteCount: bytesRead)
            }
        }
        drainThread.name = "dev.icherniukh.sessiontape.stdoutDrain"
        drainThread.qualityOfService = .userInitiated
        drainThread.start()
    }

    /// Closes the queue, then waits for the drain thread to flush every remaining byte and exit.
    /// The wait is bounded so a reader that stopped consuming stdout cannot hang shutdown.
    /// Safe to call repeatedly and before `start()`. Returns false if the join timed out.
    @discardableResult
    func stop(joinTimeout: TimeInterval = 2.0) -> Bool {
        queue.close()
        startLock.lock()
        let wasStarted = started
        startLock.unlock()
        guard wasStarted else { return true }
        let joined = finished.wait(timeout: .now() + joinTimeout) == .success
        if !joined {
            logError("Drain thread did not finish within \(joinTimeout)s; \(queue.depth) bytes unflushed")
        }
        return joined
    }
}
