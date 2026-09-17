// Local-only screenshot OCR. Compile: swiftc -O screen_reader.swift -o screen-reader
// Apple Vision request documentation: https://developer.apple.com/documentation/vision/vnrecognizetextrequest
import Foundation
import Vision
import CoreGraphics
import ImageIO

struct ReaderFailure: Error { let message: String }

func readFrame(_ path: String) throws -> [String: Any] {
    guard let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        throw ReaderFailure(message: "Cannot decode screenshot")
    }
    let width = image.width, height = image.height
    guard width > 0, height > 0, width <= 1920, height <= 1280 else {
        throw ReaderFailure(message: "Screenshot dimensions are outside supported bounds")
    }
    let scale = width <= 480 ? 4 : 1
    guard let enlarged = CGContext(data: nil, width: width * scale, height: height * scale,
        bitsPerComponent: 8, bytesPerRow: width * scale * 4,
        space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
        throw ReaderFailure(message: "Cannot allocate OCR image")
    }
    enlarged.interpolationQuality = .none
    enlarged.draw(image, in: CGRect(x: 0, y: 0, width: width * scale, height: height * scale))
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["en-US"]
    request.usesLanguageCorrection = false
    request.minimumTextHeight = 0.018
    guard let scaledImage = enlarged.makeImage() else { throw ReaderFailure(message: "Cannot scale screenshot") }
    try VNImageRequestHandler(cgImage: scaledImage, options: [:]).perform([request])
    var lines: [[String: Any]] = []
    for observation in request.results ?? [] {
        guard let candidate = observation.topCandidates(1).first else { continue }
        let box = observation.boundingBox
        let x = box.minX * Double(width)
        let y = (1 - box.maxY) * Double(height)
        lines.append(["text": candidate.string, "confidence": Double(candidate.confidence),
            "box": ["x": x, "y": y, "width": box.width * Double(width), "height": box.height * Double(height)]])
    }
    lines.sort {
        let a = $0["box"] as! [String: Double], b = $1["box"] as! [String: Double]
        return abs(a["y"]! - b["y"]!) > 4 ? a["y"]! < b["y"]! : a["x"]! < b["x"]!
    }
    var indicator: [String: Any] = ["visible": false,
        "absence_is_conclusive": false, "method": "exact 9,7,5,3,1 red-pixel triangle in a white bottom dialogue box"]
    // This test is deliberately restricted to original-resolution GBA frames.
    // The observed game's indicator is a 9x5 downward red triangle (25 pixels).
    // A red rectangle, text glyph, or red object elsewhere cannot match this test.
    if width == 240 && height == 160,
       let raw = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8,
           bytesPerRow: width * 4, space: CGColorSpaceCreateDeviceRGB(),
           bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) {
        raw.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
        let pixels = raw.data!.assumingMemoryBound(to: UInt8.self)
        func red(_ x: Int, _ y: Int) -> Bool {
            guard x >= 0, y >= 0, x < width, y < height else { return false }
            let i = (y * width + x) * 4
            let r = Int(pixels[i]), g = Int(pixels[i+1]), b = Int(pixels[i+2])
            return r > 150 && r > g * 2 && r > b * 2
        }
        func white(_ x: Int, _ y: Int) -> Bool {
            let i = (y * width + x) * 4
            return pixels[i] > 230 && pixels[i+1] > 230 && pixels[i+2] > 230
        }
        var whites = 0
        for y in 117..<154 { for x in 5..<235 { if white(x, y) { whites += 1 } } }
        let whiteRatio = Double(whites) / Double(37 * 230)
        if whiteRatio > 0.65 {
            search: for y in 117...148 { for x in 6...225 {
                guard red(x, y) else { continue }
                var exact = true
                for dy in -1...5 { for dx in -1...9 {
                    let expected = dy >= 0 && dy < 5 && dx >= dy && dx <= 8 - dy
                    if red(x+dx, y+dy) != expected { exact = false }
                } }
                if exact {
                    // Require OCR evidence of a nearby line in the same dialog box.
                    let adjacentText = lines.contains { line in
                        let box = line["box"] as! [String: Double]
                        return box["x"]! < Double(x) && box["y"]! < Double(y+6)
                            && box["y"]! + box["height"]! > Double(y-8)
                            && box["x"]! + box["width"]! <= Double(x+18)
                    }
                    if adjacentText {
                        indicator = ["visible": true, "absence_is_conclusive": false,
                            "method": "exact red-pixel triangle plus adjacent OCR text and white dialogue region",
                            "box": ["x": x, "y": y, "width": 9, "height": 5],
                            "red_pixels": 25, "row_widths": [9, 7, 5, 3, 1],
                            "dialogue_white_fraction": whiteRatio]
                        break search
                    }
                }
            } }
        }
    }
    return ["valid": true, "width": width, "height": height, "upscale": scale,
        "text": lines.compactMap { $0["text"] as? String }.joined(separator: "\n"),
        "lines": lines, "continue_indicator": indicator,
        "warning": "OCR may misread names, symbols, or partial text. It is not authoritative game state."]
}

func respond(_ path: String) {
    let result: [String: Any]
    do { result = try autoreleasepool { try readFrame(path) } }
    catch { result = ["valid": false, "text": "", "lines": [], "error": String(describing: error)] }
    if let data = try? JSONSerialization.data(withJSONObject: result, options: [.sortedKeys]),
       var output = String(data: data, encoding: .utf8) {
        output += "\n"
        FileHandle.standardOutput.write(output.data(using: .utf8)!)
    }
}

if CommandLine.arguments.dropFirst().first == "--stdio" {
    while let line = readLine() {
        if let data = line.data(using: .utf8),
           let input = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
           let path = input["path"] as? String { respond(path) }
        else { FileHandle.standardOutput.write(Data("{\"valid\":false,\"error\":\"Expected a JSON path\"}\n".utf8)) }
    }
} else if CommandLine.arguments.count == 2 {
    respond(CommandLine.arguments[1])
} else {
    FileHandle.standardError.write(Data("Usage: screen-reader IMAGE.png | --stdio\n".utf8))
    exit(2)
}
