import Foundation

private struct BenchmarkResult: Encodable {
    let component: String
    let iterations: Int
    let framesPerBuffer: Int
    let elapsedS: Double
    let buffersPerS: Double
    let convertMs: PercentileStats
}

private struct PercentileStats: Encodable {
    let count: Int
    let p50: Double
    let p95: Double
    let p99: Double
    let max: Double
}

private func clampToInt16(_ sample: Float) -> Int16 {
    let scaled = sample * 32767.0
    if scaled > 32767.0 { return 32767 }
    if scaled < -32768.0 { return -32768 }
    return Int16(scaled.rounded())
}

private func convertFloatDeinterleavedStereo(
    left: UnsafePointer<Float>,
    right: UnsafePointer<Float>,
    frames: Int,
    scratch: inout [Int16]
) -> Int {
    let sampleCount = frames * 2
    if scratch.count < sampleCount {
        scratch = Array(repeating: 0, count: sampleCount)
    }

    for frame in 0..<frames {
        let base = frame * 2
        scratch[base] = clampToInt16(left[frame])
        scratch[base + 1] = clampToInt16(right[frame])
    }
    return sampleCount * MemoryLayout<Int16>.size
}

private func percentile(_ sorted: [Double], _ percentile: Double) -> Double {
    guard !sorted.isEmpty else { return 0.0 }
    let index = Int((percentile / 100.0) * Double(sorted.count - 1))
    return sorted[index]
}

@main
struct MacCaptureBenchmark {
    static func main() {
        let framesPerBuffer = 480
        let iterations = 100_000
        let left = (0..<framesPerBuffer).map { Float(sin(Double($0) / 48.0)) }
        let right = (0..<framesPerBuffer).map { Float(cos(Double($0) / 48.0)) }
        var scratch = [Int16](repeating: 0, count: framesPerBuffer * 2)
        var latenciesMs = [Double]()
        latenciesMs.reserveCapacity(iterations)

        left.withUnsafeBufferPointer { leftBuffer in
            right.withUnsafeBufferPointer { rightBuffer in
                guard let leftPtr = leftBuffer.baseAddress, let rightPtr = rightBuffer.baseAddress else {
                    fputs("benchmark setup failed\n", stderr)
                    exit(1)
                }

                let warmupStart = DispatchTime.now().uptimeNanoseconds
                for _ in 0..<1_000 {
                    _ = convertFloatDeinterleavedStereo(
                        left: leftPtr,
                        right: rightPtr,
                        frames: framesPerBuffer,
                        scratch: &scratch
                    )
                }
                _ = warmupStart

                let start = DispatchTime.now().uptimeNanoseconds
                for _ in 0..<iterations {
                    let t0 = DispatchTime.now().uptimeNanoseconds
                    _ = convertFloatDeinterleavedStereo(
                        left: leftPtr,
                        right: rightPtr,
                        frames: framesPerBuffer,
                        scratch: &scratch
                    )
                    let elapsedMs = Double(DispatchTime.now().uptimeNanoseconds - t0) / 1_000_000.0
                    latenciesMs.append(elapsedMs)
                }
                let elapsedS = Double(DispatchTime.now().uptimeNanoseconds - start) / 1_000_000_000.0
                let sorted = latenciesMs.sorted()
                let result = BenchmarkResult(
                    component: "mac_capture.fast_pcm_converter",
                    iterations: iterations,
                    framesPerBuffer: framesPerBuffer,
                    elapsedS: round(elapsedS * 1_000_000.0) / 1_000_000.0,
                    buffersPerS: round((Double(iterations) / elapsedS) * 100.0) / 100.0,
                    convertMs: PercentileStats(
                        count: sorted.count,
                        p50: round(percentile(sorted, 50) * 1000.0) / 1000.0,
                        p95: round(percentile(sorted, 95) * 1000.0) / 1000.0,
                        p99: round(percentile(sorted, 99) * 1000.0) / 1000.0,
                        max: round(sorted.last! * 1000.0) / 1000.0
                    )
                )

                let encoder = JSONEncoder()
                encoder.outputFormatting = [.sortedKeys]
                let data = try! encoder.encode(result)
                FileHandle.standardOutput.write(data)
                FileHandle.standardOutput.write(Data("\n".utf8))
            }
        }
    }
}
