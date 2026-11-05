import Foundation
import Synchronization

/// Lock-free Single-Producer Single-Consumer (SPSC) circular byte ring buffer.
/// Audio callback thread acts as the sole producer (zero locks, zero heap allocations).
/// Stdout drain thread acts as the sole consumer.
final class PCMOutboundQueue: @unchecked Sendable {
    let capacity: Int
    private let mask: Int
    private let buffer: UnsafeMutableRawPointer

    private let head = Atomic<Int>(0)
    private let tail = Atomic<Int>(0)
    private let droppedBytesAtomic = Atomic<Int>(0)
    private let droppedChunksAtomic = Atomic<Int>(0)
    private let enqueuedBytesAtomic = Atomic<Int>(0)
    private let isClosedAtomic = Atomic<Bool>(false)
    private let ready = DispatchSemaphore(value: 0)

    init(capacityBytes: Int = 2 * 1024 * 1024) {
        var cap = 1024
        while cap < capacityBytes { cap <<= 1 }
        self.capacity = cap
        self.mask = cap - 1
        self.buffer = .allocate(byteCount: cap, alignment: 64)
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

        head.store(h + byteCount, ordering: .releasing)
        enqueuedBytesAtomic.wrappingAdd(byteCount, ordering: .relaxed)
        ready.signal()
        return true
    }

    func enqueue(_ data: Data) {
        data.withUnsafeBytes { raw in
            guard let base = raw.baseAddress else { return }
            enqueue(bytes: base, byteCount: data.count)
        }
    }

    func dequeueBlocking(into destination: UnsafeMutableRawPointer, maxBytes: Int) -> Int {
        while true {
            ready.wait()
            let t = tail.load(ordering: .relaxed)
            let h = head.load(ordering: .acquiring)
            let available = h - t
            if available == 0 {
                if isClosedAtomic.load(ordering: .relaxed) {
                    return 0
                }
                continue
            }

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
    }

    func close() {
        isClosedAtomic.store(true, ordering: .releasing)
        ready.signal()
    }
}

/// Blocking stdout writer used only on the drain thread.
final class StdoutDrain: @unchecked Sendable {
    private let fd = STDOUT_FILENO
    private let bytesWrittenAtomic = Atomic<Int>(0)
    private let brokenAtomic = Atomic<Bool>(false)

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
    private let writer = StdoutDrain()
    private var thread: Thread?

    init(queue: PCMOutboundQueue) {
        self.queue = queue
    }

    var bytesWritten: Int { writer.bytesWritten }
    var droppedChunks: Int { queue.droppedChunks }
    var broken: Bool { writer.broken }

    func start() {
        let drainThread = Thread { [weak self, queue, writer] in
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
        thread = drainThread
    }

    func stop() {
        queue.close()
    }
}
