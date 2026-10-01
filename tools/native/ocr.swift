// Local OCR for screens with no accessibility tree: macOS Vision, on-device, zh-Hans + en.
//   ocr <image.png>  ->  JSON lines {"text": ..., "x": .., "y": .., "w": .., "h": .., "conf": ..}  (0-1, top-left origin)
//
// Vision reads a row of tabs ("全部 文档 图片 视频 音频 其他") as one line, and its per-character boxes are spread
// evenly over the line, so they show no gaps; clicked at the line's centre, every tab was missed. Each line is
// therefore split on the pixels: columns of the line that are all background, in a run wider than 0.6 of the line's
// height, separate segments, and each segment is read again on its own.
import Foundation
import Vision
import AppKit

let path = CommandLine.arguments[1]
guard let img = NSImage(contentsOfFile: path),
      let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("cannot read \(path)\n".data(using: .utf8)!); exit(1)
}
let W = cg.width, H = cg.height

// Grey pixels of the whole image, once.
var grey = [UInt8](repeating: 0, count: W * H)
let ctx = CGContext(data: &grey, width: W, height: H, bitsPerComponent: 8, bytesPerRow: W,
                    space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGImageAlphaInfo.none.rawValue)!
ctx.draw(cg, in: CGRect(x: 0, y: 0, width: W, height: H))
func px(_ x: Int, _ y: Int) -> Int { Int(grey[y * W + x]) }       // y from the top (bitmap rows are top-down)

func recognize(_ image: CGImage) throws -> [VNRecognizedTextObservation] {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.recognitionLanguages = ["zh-Hans", "en-US"]
    req.usesLanguageCorrection = true
    try VNImageRequestHandler(cgImage: image, options: [:]).perform([req])
    return req.results ?? []
}

func emit(_ text: String, x: Int, y: Int, w: Int, h: Int, conf: Float) throws {
    let t = text.trimmingCharacters(in: .whitespaces)
    if t.isEmpty { return }
    let row: [String: Any] = ["text": t, "x": Double(x) / Double(W), "y": Double(y) / Double(H),
                              "w": Double(w) / Double(W), "h": Double(h) / Double(H), "conf": conf]
    print(String(data: try JSONSerialization.data(withJSONObject: row), encoding: .utf8)!)
}

for obs in try recognize(cg) {
    guard let top = obs.topCandidates(1).first else { continue }
    let b = obs.boundingBox
    let x0 = max(0, Int(b.minX * Double(W))), x1 = min(W - 1, Int(b.maxX * Double(W)))
    let y0 = max(0, Int((1 - b.maxY) * Double(H))), y1 = min(H - 1, Int((1 - b.minY) * Double(H)))
    let lh = y1 - y0
    guard lh > 4, x1 - x0 > lh * 2 else {
        try emit(top.string, x: x0, y: y0, w: x1 - x0, h: lh, conf: top.confidence); continue
    }
    // Background: the most common grey on the line's border rows.
    var hist = [Int](repeating: 0, count: 256)
    for x in x0...x1 { hist[px(x, y0)] += 1; hist[px(x, y1)] += 1 }
    let bg = hist.indices.max(by: { hist[$0] < hist[$1] })!
    var ink = [Bool](repeating: false, count: x1 - x0 + 1)
    for x in x0...x1 { for y in y0...y1 where abs(px(x, y) - bg) > 40 { ink[x - x0] = true; break } }
    // Segments of ink separated by wide background runs.
    var segs: [(Int, Int)] = []
    var start: Int? = nil, gap = 0
    let minGap = max(4, Int(0.6 * Double(lh)))
    for (i, on) in ink.enumerated() {
        if on {
            if start == nil { start = i } else if gap >= minGap { segs.append((start!, i - gap - 1)); start = i }
            gap = 0
        } else if start != nil { gap += 1 }
    }
    if let s = start { segs.append((s, ink.count - 1 - gap)) }
    if segs.count < 2 {
        try emit(top.string, x: x0, y: y0, w: x1 - x0, h: lh, conf: top.confidence); continue
    }
    for (a, z) in segs {
        let pad = lh / 3
        let r = CGRect(x: max(0, x0 + a - pad), y: max(0, y0 - pad), width: min(W - 1, x0 + z + pad) - max(0, x0 + a - pad),
                       height: min(H - 1, y1 + pad) - max(0, y0 - pad))
        guard let crop = cg.cropping(to: r) else { continue }
        var text = (try recognize(crop)).compactMap { $0.topCandidates(1).first?.string }.joined(separator: " ")
        if text.trimmingCharacters(in: .whitespaces).isEmpty {
            // Too small to be read alone: the line's own characters whose (spread-out) boxes centre in this segment.
            let lo = Double(x0 + a) / Double(W), hi = Double(x0 + z) / Double(W)
            var i = top.string.startIndex
            while i < top.string.endIndex {
                let j = top.string.index(after: i)
                if let cb = try? top.boundingBox(for: i..<j)?.boundingBox, cb.midX >= lo, cb.midX <= hi {
                    text += String(top.string[i..<j])
                }
                i = j
            }
        }
        try emit(text, x: x0 + a, y: y0, w: z - a + 1, h: lh, conf: top.confidence)
    }
}
