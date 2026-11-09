// Плашка GV справа сверху поверх всех окон: последние сообщения сессии Claude пузырями — моё справа синим, ответ слева серым.
// overlay <файл сессии .jsonl> — запускает voice.py, когда узнал сессию; плашка выходит сама, когда сервис пропал.
// Клики проходят сквозь плашку; активна одна кнопка над ней — свернуть или развернуть, состояние запоминается.
import AppKit

let path = CommandLine.arguments[1]
let service = getppid()

final class Flipped: NSView { override var isFlipped: Bool { true } }

final class Overlay: NSObject, NSApplicationDelegate {
  var panel: NSPanel!
  var toggle: NSPanel!
  var button: NSButton!
  var box = Flipped()
  var items: [String: (mine: Bool, text: String)] = [:]  // показываемые сообщения по uuid строки
  var parent: [String: String] = [:]  // uuid строки → uuid предыдущей: откат начинает новую ветку, старая остаётся в файле
  var leaf = ""  // последняя строка — конец живой ветки
  var offset: UInt64 = 0
  var rest = Data()
  var hidden = UserDefaults.standard.bool(forKey: "gvOverlayHidden")
  let keep = 4, maxChars = 320, width: CGFloat = 400, pad: CGFloat = 10, gap: CGFloat = 6, bp: CGFloat = 9
  let edge: CGFloat = 10, btn: CGFloat = 24

  func floating(clicks: Bool) -> NSPanel {
    let p = NSPanel(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
    p.level = .statusBar
    p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary]
    p.isOpaque = false
    p.backgroundColor = .clear
    p.hasShadow = true
    p.ignoresMouseEvents = !clicks
    p.appearance = NSAppearance(named: .darkAqua)
    let fx = NSVisualEffectView()
    fx.material = .hudWindow
    fx.state = .active
    fx.blendingMode = .behindWindow
    fx.wantsLayer = true
    fx.layer?.cornerRadius = clicks ? btn / 2 : 14
    fx.layer?.masksToBounds = true
    p.contentView = fx
    return p
  }

  func applicationDidFinishLaunching(_ n: Notification) {
    panel = floating(clicks: false)
    panel.contentView!.addSubview(box)
    toggle = floating(clicks: true)
    button = NSButton(frame: NSRect(x: 0, y: 0, width: btn, height: btn))
    button.isBordered = false
    button.target = self
    button.action = #selector(flip)
    toggle.contentView!.addSubview(button)
    place()
    Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
      if getppid() != service { exit(0) }  // сервис GV вышел — уходим и мы
      self?.poll()
    }
    poll()
  }

  @objc func flip() {
    hidden.toggle()
    UserDefaults.standard.set(hidden, forKey: "gvOverlayHidden")
    place()
  }

  // Кнопка — в углу экрана, плашка — под ней; свёрнуто — видна одна кнопка.
  func place() {
    guard let scr = NSScreen.main?.visibleFrame else { return }
    button.image = NSImage(systemSymbolName: hidden ? "text.bubble" : "chevron.up", accessibilityDescription: nil)
    button.contentTintColor = NSColor(white: 1, alpha: 0.85)
    toggle.setFrame(NSRect(x: scr.maxX - edge - btn, y: scr.maxY - edge - btn, width: btn, height: btn), display: true)
    toggle.orderFrontRegardless()
    if hidden || box.subviews.isEmpty { panel.orderOut(nil); return }
    let h = box.frame.height
    panel.setFrame(NSRect(x: scr.maxX - edge - width, y: scr.maxY - edge - btn - 6 - h, width: width, height: h), display: true)
    panel.orderFrontRegardless()
  }

  func poll() {
    guard let fh = FileHandle(forReadingAtPath: path) else { return }
    defer { try? fh.close() }
    let size = (try? fh.seekToEnd()) ?? 0
    guard size > offset else { return }
    try? fh.seek(toOffset: offset)
    rest += fh.readDataToEndOfFile()
    offset = size
    var lines = rest.split(separator: UInt8(ascii: "\n"), omittingEmptySubsequences: false)
    rest = Data(lines.removeLast())  // хвост без перевода строки — дочитаем в следующий раз
    for l in lines { apply(Data(l)) }
    render()
  }

  // Своё — текст запроса строкой (не команды и не служебное); ответ — текст финального сообщения хода.
  func apply(_ line: Data) {
    guard let d = try? JSONSerialization.jsonObject(with: line) as? [String: Any], let uuid = d["uuid"] as? String else { return }
    parent[uuid] = d["parentUuid"] as? String ?? ""
    leaf = uuid
    guard let m = d["message"] as? [String: Any] else { return }
    switch d["type"] as? String {
    case "user":
      guard d["isMeta"] as? Bool != true, let t = m["content"] as? String, !t.hasPrefix("<") else { return }
      items[uuid] = (true, t)
    case "assistant":
      guard m["stop_reason"] as? String == "end_turn" else { return }
      let t = (m["content"] as? [[String: Any]] ?? []).filter { $0["type"] as? String == "text" }
        .compactMap { $0["text"] as? String }.joined(separator: "\n")
      if !t.isEmpty { items[uuid] = (false, t) }
    default: return
    }
  }

  // Последние сообщения живой ветки: от конца назад по ссылкам на предыдущую строку.
  func visible() -> [(mine: Bool, text: String)] {
    var out: [(mine: Bool, text: String)] = []
    var id = leaf
    while !id.isEmpty, out.count < keep {
      if let i = items[id] { out.append(i) }
      id = parent[id] ?? ""
    }
    return out.reversed()
  }

  func clean(_ s: String) -> String {
    var t = s.replacingOccurrences(of: "🎙", with: "")
    for mark in ["**", "`"] { t = t.replacingOccurrences(of: mark, with: "") }
    t = t.trimmingCharacters(in: .whitespacesAndNewlines)
    return t.count > maxChars ? String(t.prefix(maxChars)) + "…" : t
  }

  func render() {
    let shown = visible()
    guard !shown.isEmpty else { return }
    box.subviews.forEach { $0.removeFromSuperview() }
    let maxText = width - 2 * pad - 44 - 2 * bp
    var y = pad
    for i in shown {
      let label = NSTextField(wrappingLabelWithString: clean(i.text))
      label.font = .systemFont(ofSize: 13)
      label.textColor = .white
      let size = label.cell!.cellSize(forBounds: NSRect(x: 0, y: 0, width: maxText, height: 10_000))
      let w = ceil(size.width) + 2 * bp, h = ceil(size.height) + 2 * bp
      let bubble = NSView(frame: NSRect(x: i.mine ? width - pad - w : pad, y: y, width: w, height: h))
      bubble.wantsLayer = true
      bubble.layer?.cornerRadius = 12
      bubble.layer?.backgroundColor = (i.mine ? NSColor.systemBlue.withAlphaComponent(0.9) : NSColor(white: 1, alpha: 0.13)).cgColor
      label.frame = NSRect(x: bp, y: bp, width: ceil(size.width), height: ceil(size.height))
      bubble.addSubview(label)
      box.addSubview(bubble)
      y += h + gap
    }
    box.frame = NSRect(x: 0, y: 0, width: width, height: y - gap + pad)
    place()
  }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = Overlay()
app.delegate = delegate
app.run()
