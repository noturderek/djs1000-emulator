import Foundation

/// Developer self-test for audio-output.swift (never part of the app bundle). Build with
/// `swiftc -O -parse-as-library -framework AVFoundation -framework CoreAudio audio-selftest.swift audio-output.swift audio-sink.swift -o selftest`
/// then `selftest test` (virtual-time synthetic input), `selftest live` (real-time offline render, no device),
/// `selftest devices` (list output devices by UID).
@main
struct SelfTest {
    struct SimResult {
        var underrunsAfterWarmup: UInt64, overflows: UInt64, underruns: UInt64
        var adjustPPM: Double, integralPPM: Double, minDepth: Int, maxDepth: Int, target: Int
        var latencyP50: Double, latencyP95: Double, monotonic: Bool, produced: UInt64
    }

    /// Virtual-time simulation of producer (guest clock, 64-frame periods delivered in 2 ms polls with jitter) and
    /// consumer (device clock, fixed callback block) around the real JitterBuffer and DriftController.
    static func simulate(inRate: Double, inPPM: Double, outRate: Double, outPPM: Double, block: Int, seconds: Double,
                         jitterMs: Double = 1.0, stallFrom: Double = -1, stallTo: Double = -1) -> SimResult {
        let nominal = inRate / outRate
        let target = Int(ceil(Double(block) * nominal + 0.010 * inRate)) + 2
        let buffer = JitterBuffer(capacity: max(4096, target * 4 + 2048), targetFrames: target, rate: inRate,
                                  warmupFrames: UInt64(outRate * 5))
        var controller = DriftController(targetFrames: Double(target))
        var rng = SystemRandomNumberGenerator()
        let inPeriod = 64 / (inRate * (1 + inPPM * 1e-6)), outPeriod = Double(block) / (outRate * (1 + outPPM * 1e-6))
        var nextCallback = 0.0, producerFrame = 0.0, pending: [(Double, Double, Double)] = []
        var lastArrival = 0.0, now = 0.0, monotonic = true, lastValue: Float = -1, produced: UInt64 = 0
        var nextProduce = 0.0
        var out = [Float](repeating: 0, count: block), out2 = out
        while now < seconds {
            let nextArrival = pending.first?.0 ?? Double.infinity
            now = min(nextCallback, nextProduce, nextArrival)
            if now == nextProduce {
                let stalled = stallFrom >= 0 && now >= stallFrom && now < stallTo
                if !stalled {
                    let tick = ceil(now / 0.002) * 0.002
                    let arrival = max(lastArrival, tick + Double.random(in: 0...(jitterMs / 1000), using: &rng))
                    lastArrival = arrival
                    pending.append((arrival, producerFrame, now))
                }
                producerFrame += 64; nextProduce += inPeriod
            } else if now == nextArrival {
                let chunk = pending.removeFirst()
                var ramp = (0..<64).map { Float(chunk.1 + Double($0)) / 1e7 }
                ramp.withUnsafeMutableBufferPointer { p in
                    buffer.push(p.baseAddress, p.baseAddress, count: 64, writeNs: UInt64(chunk.2 * 1e9)) }
                produced += 64
            } else {
                let r = buffer.read(&out, &out2, count: block, ratio: nominal * (1 + controller.adjust), nowNs: UInt64(now * 1e9))
                for v in out where v > 0 { if v < lastValue - 1e-6 { monotonic = false }; lastValue = v }
                if r.playing && !r.underran && !r.prefilledNow {
                    _ = controller.update(depth: r.depthBefore, dt: Double(block) / outRate, inputRate: inRate)
                } else { controller.reset() }
                nextCallback += outPeriod
            }
        }
        let c = buffer.counters()
        return SimResult(underrunsAfterWarmup: c.underrunsAfterWarmup, overflows: c.overflows, underruns: c.underruns,
                         adjustPPM: controller.adjustPPM, integralPPM: controller.integralPPM, minDepth: c.depthMin,
                         maxDepth: c.depthMax, target: c.target, latencyP50: c.latency["p50"] ?? 0,
                         latencyP95: c.latency["p95"] ?? 0, monotonic: monotonic, produced: produced)
    }

    static var failures = 0
    static func check(_ name: String, _ ok: Bool, _ detail: String) {
        print((ok ? "PASS " : "FAIL ") + name + ": " + detail)
        if !ok { failures += 1 }
    }

    static func unitTests() {
        var r = simulate(inRate: 44100, inPPM: 0, outRate: 44100, outPPM: 0, block: 128, seconds: 120)
        check("equal-rates", r.underrunsAfterWarmup == 0 && r.overflows == 0 && abs(r.adjustPPM) < 300 && r.monotonic, "\(r)")
        r = simulate(inRate: 44100, inPPM: 0, outRate: 44100, outPPM: -120, block: 128, seconds: 180)
        check("drift-learned", r.underrunsAfterWarmup == 0 && r.overflows == 0 && abs(r.integralPPM - 120) < 25 && r.monotonic,
              "integral \(r.integralPPM) ppm (expect about +120: a slow device clock needs a larger input-per-output ratio) \(r)")
        r = simulate(inRate: 44100, inPPM: 200, outRate: 48000, outPPM: 0, block: 512, seconds: 180)
        check("48k-512", r.underrunsAfterWarmup == 0 && r.overflows == 0 && abs(r.integralPPM - 200) < 40 && r.monotonic
              && r.maxDepth < r.target * 2, "integral \(r.integralPPM) ppm \(r)")
        r = simulate(inRate: 44100, inPPM: 50, outRate: 48000, outPPM: 0, block: 256, seconds: 120, jitterMs: 4)
        check("jitter-4ms", r.underrunsAfterWarmup == 0 && r.overflows == 0, "\(r)")
        r = simulate(inRate: 44100, inPPM: 0, outRate: 48000, outPPM: 0, block: 128, seconds: 40, stallFrom: 20, stallTo: 20.15)
        check("stall-recovers", r.underruns >= 1 && r.underruns <= 2 && r.overflows == 0 && r.monotonic, "\(r)")
        // overflow policy: no consumer drops the oldest frames, keeps the newest, and counts
        let b = JitterBuffer(capacity: 4096, targetFrames: 300, rate: 44100)
        for k in 0..<700 {
            var ramp = (0..<64).map { Float(k * 64 + $0) / 1e7 }
            ramp.withUnsafeMutableBufferPointer { p in b.push(p.baseAddress, p.baseAddress, count: 64, writeNs: UInt64(k) * 1_000_000) }
        }
        var l = [Float](repeating: 0, count: 8), rr = l
        let res = b.read(&l, &rr, count: 8, ratio: 1, nowNs: 1_000_000_000)
        let c = b.counters()
        let newest = Float(700 * 64) / 1e7
        check("overflow-drop-oldest", c.overflows > 0 && c.depth <= 4096 && res.playing && l[0] > newest - 4200.0 / 1e7 && l[0] > 0.0005,
              "overflows \(c.overflows) dropped frames \(c.overflowFrames) depth \(c.depth) first sample \(l[0]) newest \(newest)")
        // prefill: nothing plays until the target is reached
        let p = JitterBuffer(capacity: 4096, targetFrames: 300, rate: 44100)
        var z = [Float](repeating: 0.5, count: 64), z2 = z
        var one = [Float](repeating: 1, count: 200)
        one.withUnsafeMutableBufferPointer { q in p.push(q.baseAddress, q.baseAddress, count: 200, writeNs: 0) }
        _ = p.read(&z, &z2, count: 64, ratio: 1, nowNs: 0)
        let silent = z.allSatisfy { $0 == 0 }
        one.withUnsafeMutableBufferPointer { q in p.push(q.baseAddress, q.baseAddress, count: 200, writeNs: 0) }
        _ = p.read(&z, &z2, count: 64, ratio: 1, nowNs: 0)
        check("prefill", silent && z.allSatisfy { $0 == 1 }, "silent before target, signal after")
        // unit ratio is an identity
        let q = JitterBuffer(capacity: 4096, targetFrames: 10, rate: 44100)
        var ramp = (0..<500).map { Float($0) }
        ramp.withUnsafeMutableBufferPointer { s in q.push(s.baseAddress, s.baseAddress, count: 500, writeNs: 0) }
        var o = [Float](repeating: 0, count: 100), o2 = o
        _ = q.read(&o, &o2, count: 100, ratio: 1, nowNs: 0)
        check("identity-ratio", (0..<100).allSatisfy { o[$0] == Float($0) }, "first \(o[0]) last \(o[99])")
        // flush drops stale audio
        q.flush()
        _ = q.read(&o, &o2, count: 100, ratio: 1, nowNs: 0)
        check("flush", o.allSatisfy { $0 == 0 } && q.depthFrames == 0, "silent after flush")
        var d = DriftController(targetFrames: 300)
        for _ in 0..<100_000 { _ = d.update(depth: 5000, dt: 0.003, inputRate: 44100) }
        check("controller-bounded", abs(d.adjustPPM) <= 5000.0001, "adjust \(d.adjustPPM)")
        // unavailable device is reported honestly and drops audio
        let unavailable = AudioOutput(deviceUID: "sp16-test-no-such-device-uid", offline: nil)
        unavailable.start(); Thread.sleep(forTimeInterval: 0.5)
        var sample: [Float] = [0.1, 0.2]
        sample.withUnsafeMutableBufferPointer { s in unavailable.push(left: s.baseAddress, right: s.baseAddress, count: 2, rate: 44100, writeNs: 0) }
        let snap = unavailable.snapshot()
        check("device-unavailable", (snap["state"] as? String) == "unavailable" && (snap["dropped_while_unavailable"] as? UInt64) == 2,
              "\(snap["state"] ?? "?") \(snap["last_error"] ?? "")")
        unavailable.stop()
    }

    /// Real-time pass through the whole AVAudioEngine graph with a software device clock: a 120 BPM click train pushed
    /// at the guest rate, rendered to a WAV. No hardware device is opened.
    static func live(seconds: Double, rate: Double, ppm: Double, path: String) {
        let output = AudioOutput(deviceUID: nil, offline: OfflineSpec("offline:\(path):\(Int(rate)):\(ppm):256"))
        output.start()
        Thread.sleep(forTimeInterval: 0.3)
        let guestRate = 44100.0
        var frame: UInt64 = 0
        let start = AudioSink.nowNs()
        var burst = [Float](repeating: 0, count: 128)
        while Double(AudioSink.nowNs() - start) / 1e9 < seconds {
            let due = Double(frame) / guestRate
            let now = Double(AudioSink.nowNs() - start) / 1e9
            if due > now { Thread.sleep(forTimeInterval: min(0.002, due - now)); continue }
            for i in 0..<128 {
                let f = frame + UInt64(i), inBeat = f % 22050
                burst[i] = inBeat < 2400 ? 0.4 * Float(sin(Double(inBeat) * 2 * .pi * 880 / guestRate)) * Float(1 - Double(inBeat) / 2400) : 0
            }
            burst.withUnsafeBufferPointer { p in output.push(left: p.baseAddress, right: p.baseAddress, count: 128, rate: guestRate, writeNs: AudioSink.nowNs()) }
            frame += 128
        }
        let snap = output.snapshot()
        output.stop()
        if let data = try? JSONSerialization.data(withJSONObject: snap, options: [.sortedKeys]), let text = String(data: data, encoding: .utf8) { print(text) }
    }

    static func main() {
        let args = CommandLine.arguments
        switch args.count > 1 ? args[1] : "test" {
        case "devices":
            for d in AudioOutput.outputDevices() { print("\(d.uid)\t\(d.name)") }
        case "graph":
            // Real-device graph, IO never started: no sound is possible.
            for uid in [nil] + AudioOutput.outputDevices().map({ Optional($0.uid) }) {
                let o = AudioOutput(deviceUID: uid, offline: nil); o.dryRun = true; o.start(); Thread.sleep(forTimeInterval: 1.0)
                let snap = o.snapshot()
                print("\(uid ?? "default")\t\(snap["state"] ?? "?")\t\(snap["device_name"] ?? "")\t\(snap["device_rate"] ?? 0)\tblock \(snap["block_frames"] ?? 0)\t\(snap["last_error"] ?? "")")
                o.stop()
            }
        case "live":
            live(seconds: Double(args.count > 2 ? args[2] : "20") ?? 20, rate: Double(args.count > 3 ? args[3] : "48000") ?? 48000,
                 ppm: Double(args.count > 4 ? args[4] : "0") ?? 0, path: args.count > 5 ? args[5] : "/tmp/sp16-audio-live.wav")
        default:
            unitTests()
            print(failures == 0 ? "ALL PASS" : "\(failures) FAILED")
            exit(failures == 0 ? 0 : 1)
        }
    }
}
