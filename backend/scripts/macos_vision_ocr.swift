import Foundation
import Vision
import AppKit

guard CommandLine.arguments.count == 2 else {
    fputs("usage: macos_vision_ocr.swift IMAGE\n", stderr)
    exit(2)
}

let path = CommandLine.arguments[1]
guard let image = NSImage(contentsOfFile: path),
      let data = image.tiffRepresentation,
      let representation = NSBitmapImageRep(data: data),
      let cgImage = representation.cgImage else {
    fputs("unable to decode image\n", stderr)
    exit(3)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["zh-Hans", "en-US"]
request.usesLanguageCorrection = true

let handler = VNImageRequestHandler(cgImage: cgImage, orientation: .up)
try handler.perform([request])

let payload: [[String: Any]] = (request.results ?? []).compactMap { observation in
    guard let candidate = observation.topCandidates(1).first else { return nil }
    return ["confidence": Double(candidate.confidence), "text": candidate.string]
}
let json = try JSONSerialization.data(withJSONObject: payload, options: [])
FileHandle.standardOutput.write(json)
