// Painel de Controle — app de barra de menu (Fase 4).
//
// Mostra o hashrate total na barra de menu do Mac e, ao clicar, os totais, os alertas ativos e as
// máquinas, com o controle remoto (perfil, dormir/acordar, reiniciar) quando a máquina permite.
// Só conversa com a API do painel (/api/status e /api/miners/{id}/control); não guarda dados.
//
// Compilar e instalar: macos/build.sh

import AppKit
import ServiceManagement
import SwiftUI

// MARK: - API do painel (só os campos usados aqui)

struct Snapshot: Decodable {
    let generated_at: Double
    let miners: [Miner]
    let totals: Totals
    let alerts: AlertSummary?
}

struct Totals: Decodable {
    let hashrate_ths: Double
    let online: Int
    let total: Int
    let finance: FinanceTotals?
}

struct FinanceTotals: Decodable {
    let profit_eur_day: Double?
}

struct Miner: Decodable, Identifiable {
    let id: String
    let name: String
    let type: String
    let status: String
    let reasons: [String]
    let hidden: Bool?
    let control: Bool?
    let local: Local?
    let pool_worker: PoolWorker?
}

struct Local: Decodable {
    let online: Bool
    let hashrate_ths: Double?
    let profile: String?
}

struct PoolWorker: Decodable {
    let hashrate_ths: Double
    let online: Bool
}

struct AlertSummary: Decodable {
    let active: [ActiveAlert]
}

struct ActiveAlert: Decodable, Identifiable {
    let key: String
    let kind: String
    let title: String
    let detail: String
    var id: String { key }
}

struct ControlOptions: Decodable {
    let type: String
    let actions: [String]
    let profiles: [Profile]?
    let current_profile: String?
    let sleeping: Bool?
}

struct Profile: Decodable, Hashable {
    let name: String
    let watts: Double?
    let ths: Double?
}

struct APIError: Decodable { let detail: String }

// MARK: - Formatação (igual ao painel web)

enum Fmt {
    static func number(_ v: Double, _ digits: Int) -> String {
        let f = NumberFormatter()
        f.locale = Locale(identifier: "pt_BR")
        f.numberStyle = .decimal
        f.minimumFractionDigits = digits
        f.maximumFractionDigits = digits
        return f.string(from: NSNumber(value: v)) ?? "\(v)"
    }

    /// TH/s -> "26,1 TH/s", "1,47 TH/s", "52,0 kH/s"
    static func hashrate(_ ths: Double?) -> String {
        guard let ths, ths > 0 else { return "—" }
        let units: [(Double, String)] = [(1e6, "EH/s"), (1e3, "PH/s"), (1, "TH/s"), (1e-3, "GH/s"), (1e-6, "MH/s"), (1e-9, "kH/s")]
        var scale: Double = 1e-12
        var unit: String = "H/s"
        for (s, u) in units where ths >= s {
            scale = s
            unit = u
            break
        }
        let v: Double = ths / scale
        let digits: Int
        if v >= 100 { digits = 0 } else if v >= 10 { digits = 1 } else { digits = 2 }
        return number(v, digits) + " " + unit
    }

    static func euro(_ v: Double) -> String {
        let sign: String
        if v > 0 { sign = "+" } else if v < 0 { sign = "−" } else { sign = "" }
        return sign + "€ " + number(abs(v), 2)
    }

    static func ago(_ ts: Date?) -> String {
        guard let ts else { return "" }
        let s = Int(Date().timeIntervalSince(ts))
        if s < 60 { return "há \(max(s, 0))s" }
        return "há \(s / 60) min"
    }
}

let statusLabel = ["online": "Funcionando", "warning": "Atenção", "offline": "Offline",
                   "sleeping": "Dormindo", "idle": "Sem aluguel", "unknown": "Aguardando dados"]

func statusColor(_ s: String) -> Color {
    switch s {
    case "online": return .green
    case "warning": return .yellow
    case "offline": return .red
    case "sleeping": return .blue
    default: return .gray
    }
}

// MARK: - Estado

@MainActor
final class Store: ObservableObject {
    @Published var snap: Snapshot?
    @Published var error: String?
    @Published var updated: Date?
    @Published var message: (text: String, ok: Bool)?
    @Published var options: [String: ControlOptions] = [:]
    @Published var busy = false
    @AppStorage("panelURL") var panelURL = "http://127.0.0.1:8080"

    private var timer: Timer?

    init() {
        Task { await refresh() }
        timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
            Task { await self?.refresh() }
        }
    }

    var base: String { panelURL.trimmingCharacters(in: .whitespaces).trimmingCharacters(in: CharacterSet(charactersIn: "/")) }

    var activeAlerts: [ActiveAlert] { (snap?.alerts?.active ?? []).filter { $0.kind == "alert" } }

    /// Texto da barra de menu. O ₿ é o caractere da fonte do sistema: um SF Symbol dentro do
    /// texto do MenuBarExtra não aparece (só o texto aparecia).
    var barText: String {
        guard let snap, error == nil else { return "₿ —" }
        return "₿ " + Fmt.hashrate(snap.totals.hashrate_ths)
    }

    var barTitle: String { activeAlerts.isEmpty ? barText : barText + " ⚠︎" }

    private func request(_ path: String, method: String = "GET", body: [String: Any]? = nil) async throws -> Data {
        guard let url = URL(string: base + path) else { throw URLError(.badURL) }
        var req = URLRequest(url: url, timeoutInterval: 20)
        req.httpMethod = method
        req.cachePolicy = .reloadIgnoringLocalCacheData
        if let body {
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.setValue("1", forHTTPHeaderField: "X-Minerador")   // o painel exige para ações
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, resp) = try await URLSession.shared.data(for: req)
        if let http = resp as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            let detail = (try? JSONDecoder().decode(APIError.self, from: data))?.detail
            throw NSError(domain: "painel", code: http.statusCode,
                          userInfo: [NSLocalizedDescriptionKey: detail ?? "erro \(http.statusCode)"])
        }
        return data
    }

    func refresh() async {
        do {
            snap = try JSONDecoder().decode(Snapshot.self, from: try await request("/api/status"))
            error = nil
            updated = Date()
        } catch {
            self.error = "Painel fora do ar (\(base))"
        }
    }

    /// Opções de controle (perfis, dormindo) das máquinas que permitem controle.
    func loadOptions() async {
        for m in snap?.miners ?? [] where m.control == true {
            if let o = try? JSONDecoder().decode(ControlOptions.self,
                                                 from: try await request("/api/miners/\(m.id)/control")) {
                options[m.id] = o
            }
        }
    }

    func run(_ miner: Miner, _ action: String, value: Any? = nil) async {
        busy = true
        defer { busy = false }
        var body: [String: Any] = ["action": action]
        if let value { body["value"] = value }
        do {
            let data = try await request("/api/miners/\(miner.id)/control", method: "POST", body: body)
            let msg = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["message"] as? String
            message = (msg ?? "Comando enviado.", true)
        } catch {
            message = (error.localizedDescription, false)
        }
        await loadOptions()
        await refresh()
    }
}

// MARK: - Confirmação nativa

@MainActor
func confirm(_ question: String, _ info: String, destructive: Bool = false) -> Bool {
    let a = NSAlert()
    a.messageText = question
    a.informativeText = info
    a.alertStyle = destructive ? .critical : .warning
    a.addButton(withTitle: "Confirmar")
    a.addButton(withTitle: "Cancelar")
    NSApp.activate(ignoringOtherApps: true)
    return a.runModal() == .alertFirstButtonReturn
}

// MARK: - Visual "Matrix"

enum Theme {
    static let green = Color(red: 0.25, green: 1.0, blue: 0.45)
    static let greenDim = Color(red: 0.45, green: 0.85, blue: 0.55)
    static func label(_ s: String) -> some View {
        Text(s.uppercased()).font(.system(size: 9.5, weight: .semibold, design: .monospaced))
            .tracking(1).foregroundStyle(greenDim).shadow(color: .black, radius: 2).shadow(color: .black, radius: 2)
    }
}

/// Chuva de caracteres verdes. Só anima com a janela à vista (aberta e não encoberta);
/// parada com "Reduzir movimento". Os caracteres são símbolos resolvidos uma vez só:
/// desenhar Text novo a cada quadro custava ~90% da thread principal, mesmo com a janela fechada.
struct MatrixRain: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var onScreen = false
    private static let glyphs = Array("ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ0123456789₿").map { String($0) }
    private static let headColor = Color(red: 0.85, green: 1.0, blue: 0.9)

    private static func rnd(_ i: Int) -> Double {
        let x = sin(Double(i) * 12.9898) * 43758.5453
        return x - floor(x)
    }

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 12.0, paused: reduceMotion || !onScreen)) { timeline in
            Canvas { ctx, size in
                let t: Double = timeline.date.timeIntervalSinceReferenceDate
                let cell: CGFloat = 14
                let cols = Int(size.width / cell) + 1
                let rows = Int(size.height / cell) + 1
                let n = MatrixRain.glyphs.count
                for c in 0..<cols {
                    let speed: Double = 2.5 + MatrixRain.rnd(c) * 6.0          // linhas por segundo
                    let length: Int = 6 + Int(MatrixRain.rnd(c + 97) * 16.0)
                    let span = Double(rows + length)
                    let head = Int((t * speed + MatrixRain.rnd(c + 31) * span).truncatingRemainder(dividingBy: span))
                    let tick = Int(t * 3.0)
                    for k in 0..<length {
                        let row = head - k
                        if row < 0 || row >= rows { continue }
                        let fade: Double = k == 0 ? 1.0 : (1.0 - Double(k) / Double(length)) * 0.9
                        let flicker = (k == 0 || (c + row) % 5 == 0) ? tick : 0
                        let gi = abs(c * 31 + row * 17 + flicker * 7) % n
                        let id: Int = k == 0 ? gi + n : gi                    // n… = cabeça clara
                        guard let symbol = ctx.resolveSymbol(id: id) else { continue }
                        ctx.opacity = fade
                        ctx.draw(symbol, at: CGPoint(x: CGFloat(c) * cell + cell / 2, y: CGFloat(row) * cell + cell / 2))
                    }
                }
            } symbols: {
                ForEach(0..<MatrixRain.glyphs.count, id: \.self) { i in
                    MatrixRain.glyph(i, color: Theme.green).tag(i)
                }
                ForEach(0..<MatrixRain.glyphs.count, id: \.self) { i in
                    MatrixRain.glyph(i, color: MatrixRain.headColor).tag(i + MatrixRain.glyphs.count)
                }
            }
        }
        .background(WindowVisibility(visible: $onScreen))
    }

    private static func glyph(_ i: Int, color: Color) -> some View {
        Text(glyphs[i]).font(.system(size: 12, weight: .medium, design: .monospaced)).foregroundColor(color)
    }
}

/// Avisa se a janela que contém a view está à vista: aberta e não encoberta.
/// A janela do MenuBarExtra continua viva depois de fechada, então onAppear/onDisappear não bastam.
struct WindowVisibility: NSViewRepresentable {
    @Binding var visible: Bool

    func makeNSView(context: Context) -> Probe {
        let probe = Probe()
        probe.onChange = { v in if visible != v { visible = v } }
        return probe
    }

    func updateNSView(_ nsView: Probe, context: Context) {}

    final class Probe: NSView {
        var onChange: ((Bool) -> Void)?
        private var tokens: [NSObjectProtocol] = []

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            for tk in tokens { NotificationCenter.default.removeObserver(tk) }
            tokens = []
            guard let w = window else { report(false); return }
            let names: [Notification.Name] = [NSWindow.didChangeOcclusionStateNotification,
                                              NSWindow.didBecomeKeyNotification, NSWindow.didResignKeyNotification,
                                              NSWindow.willCloseNotification]
            for name in names {
                let tk = NotificationCenter.default.addObserver(forName: name, object: w, queue: .main) { [weak self] _ in
                    self?.check()
                }
                tokens.append(tk)
            }
            check()
        }

        private func check() {
            guard let w = window else { report(false); return }
            report(w.isVisible && w.occlusionState.contains(.visible))
        }

        private func report(_ v: Bool) {
            DispatchQueue.main.async { [weak self] in self?.onChange?(v) }
        }

        deinit { for tk in tokens { NotificationCenter.default.removeObserver(tk) } }
    }
}

struct MatrixBackground: View {
    var body: some View {
        ZStack {
            Color.black
            MatrixRain().opacity(0.62)
            // escurece o meio para os números ficarem legíveis
            LinearGradient(colors: [.black.opacity(0.12), .black.opacity(0.32), .black.opacity(0.48)],
                           startPoint: .top, endPoint: .bottom)
            // linhas de varredura bem leves, como num monitor antigo
            Canvas { ctx, size in
                var y: CGFloat = 0
                while y < size.height {
                    ctx.fill(Path(CGRect(x: 0, y: y, width: size.width, height: 1)), with: .color(.black.opacity(0.18)))
                    y += 3
                }
            }
        }
    }
}

// MARK: - Telas

struct MinerRow: View {
    @EnvironmentObject var store: Store
    let miner: Miner

    var hashrate: Double? {
        if let l = miner.local, l.online { return l.hashrate_ths }
        if let p = miner.pool_worker, p.online { return p.hashrate_ths }
        return nil
    }

    var subtitle: String {
        let label = statusLabel[miner.status] ?? miner.status
        if miner.status == "warning" || miner.status == "offline", let r = miner.reasons.first { return "\(label): \(r)" }
        if let p = miner.local?.profile { return "\(label) · perfil \(p)" }
        return label
    }

    var body: some View {
        HStack(alignment: .center, spacing: 8) {
            Circle().fill(statusColor(miner.status)).frame(width: 9, height: 9)
                .shadow(color: statusColor(miner.status), radius: 4)
            VStack(alignment: .leading, spacing: 1) {
                Text(miner.name).font(.system(size: 13, weight: .semibold)).foregroundStyle(.white)
                    .shadow(color: .black, radius: 2).shadow(color: .black, radius: 3)
                Text(subtitle).font(.system(size: 11, design: .monospaced)).foregroundStyle(Theme.greenDim).lineLimit(2)
                    .shadow(color: .black, radius: 2).shadow(color: .black, radius: 3)
            }
            Spacer(minLength: 6)
            Text(Fmt.hashrate(hashrate)).font(.system(size: 13, weight: .semibold, design: .monospaced))
                .foregroundStyle(Theme.green).shadow(color: .black, radius: 2).shadow(color: .black, radius: 3)
            if miner.control == true { controlMenu }
        }
        .padding(.vertical, 3)
    }

    @ViewBuilder var controlMenu: some View {
        let o = store.options[miner.id]
        Menu {
            if let o, o.type == "luxos" {
                Menu("Perfil de potência") {
                    ForEach(o.profiles ?? [], id: \.self) { p in
                        Button(profileLabel(p, current: p.name == o.current_profile)) { ask("profile", p) }
                            .disabled(p.name == o.current_profile)
                    }
                }
                if o.sleeping == true {
                    Button("Acordar") { ask("wakeup") }
                } else {
                    Button("Dormir") { ask("sleep") }
                }
                Divider()
                Button("Reiniciar mineração") { ask("restart") }
                Button("Reiniciar máquina") { ask("reboot") }
            } else if let o, o.type == "axeos" {
                Button("Reiniciar Bitaxe") { ask("restart") }
                Button("Ajustar frequência e voltagem no painel…") { openPanel(store.base) }
            } else {
                Text("Consultando a máquina…")
            }
        } label: {
            Image(systemName: "slider.horizontal.3")
        }
        .menuStyle(.borderlessButton)
        .menuIndicator(.hidden)
        .fixedSize()
        .disabled(store.busy)
        .help("Controle remoto")
    }

    func profileLabel(_ p: Profile, current: Bool) -> String {
        var s = p.name
        if let w = p.watts { s += " · \(Fmt.number(w, 0)) W" }
        if let t = p.ths { s += " · \(Fmt.number(t, 0)) TH/s" }
        return current ? s + " (atual)" : s
    }

    func ask(_ action: String, _ profile: Profile? = nil) {
        let isLux = miner.type == "luxos"
        let (q, info): (String, String) = {
            switch action {
            case "profile": return ("Trocar \(miner.name) para o perfil \(profile?.name ?? "")?",
                                    "A máquina reajusta frequência e voltagem; o hashrate leva alguns minutos para estabilizar.")
            case "sleep": return ("Pôr \(miner.name) para dormir?",
                                  "Ela para de minerar e passa a consumir pouco. Só volta com “Acordar”, mesmo se reiniciar.")
            case "wakeup": return ("Acordar \(miner.name)?", "Ela volta a minerar no perfil atual; leva alguns minutos até chegar ao hashrate.")
            case "reboot": return ("Reiniciar \(miner.name) inteira?", "A máquina desliga e liga de novo. Fica sem minerar por uns 5 minutos.")
            default: return ("Reiniciar \(isLux ? "a mineração da " : "")\(miner.name)?",
                             isLux ? "Só o programa de mineração reinicia. Volta em 1 a 2 minutos." : "Ela volta em cerca de 1 minuto.")
            }
        }()
        guard confirm(q, info, destructive: action == "reboot" || action == "sleep") else { return }
        Task { await store.run(miner, action, value: profile?.name) }
    }
}

func openPanel(_ base: String) {
    if let url = URL(string: base) { NSWorkspace.shared.open(url) }
}

struct ContentView: View {
    @EnvironmentObject var store: Store
    @State private var showSettings = false
    @State private var urlDraft = ""
    @State private var launchAtLogin = SMAppService.mainApp.status == .enabled

    var visibleMiners: [Miner] {
        (store.snap?.miners ?? []).filter { $0.hidden != true && $0.status != "idle" }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header
            if let err = store.error {
                Label(err, systemImage: "bolt.horizontal.circle").foregroundStyle(.red).font(.system(size: 12))
            }
            if let snap = store.snap {
                totals(snap)
                if !store.activeAlerts.isEmpty { alerts }
                Rectangle().fill(Theme.green.opacity(0.3)).frame(height: 1)
                if visibleMiners.isEmpty {
                    Text("Nenhuma máquina ativa").foregroundStyle(.secondary).font(.system(size: 12))
                }
                ForEach(visibleMiners) { MinerRow(miner: $0) }
            }
            if let m = store.message {
                Text(m.text).font(.system(size: 12)).foregroundStyle(m.ok ? .green : .red)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Rectangle().fill(Theme.green.opacity(0.3)).frame(height: 1)
            if showSettings { settings }
            footer
        }
        .padding(14)
        .frame(width: 360)
        .background(MatrixBackground())
        .overlay(RoundedRectangle(cornerRadius: 1).stroke(Theme.green.opacity(0.25), lineWidth: 1))
        .environment(\.colorScheme, .dark)
        .tint(Theme.green)
        .task {
            await store.refresh()
            await store.loadOptions()
        }
    }

    var header: some View {
        HStack {
            Text("₿ PAINEL DE CONTROLE").font(.system(size: 13, weight: .bold, design: .monospaced))
                .tracking(1.5).foregroundStyle(Theme.green).shadow(color: Theme.green.opacity(0.8), radius: 6)
            Spacer()
            if store.busy { ProgressView().controlSize(.small) }
            Text(Fmt.ago(store.updated)).font(.system(size: 10, design: .monospaced)).foregroundStyle(Theme.greenDim)
        }
    }

    func totals(_ snap: Snapshot) -> some View {
        HStack(alignment: .top) {
            stat("Hashrate total", Fmt.hashrate(snap.totals.hashrate_ths))
            Spacer()
            stat("Máquinas ativas", "\(snap.totals.online) de \(snap.totals.total)")
            Spacer()
            if let p = snap.totals.finance?.profit_eur_day {
                stat("Resultado/dia", Fmt.euro(p), color: p < 0 ? Color(red: 1, green: 0.42, blue: 0.4) : Theme.green)
            }
        }
    }

    func stat(_ k: String, _ v: String, color: Color = .white) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Theme.label(k)
            Text(v).font(.system(size: 17, weight: .bold, design: .monospaced)).foregroundStyle(color)
                .shadow(color: .black, radius: 2).shadow(color: .black, radius: 3).shadow(color: color.opacity(0.45), radius: 6)
        }
    }

    var alerts: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(store.activeAlerts.prefix(4)) { a in
                VStack(alignment: .leading, spacing: 0) {
                    Text(a.title).font(.system(size: 12, weight: .semibold))
                    Text(a.detail).font(.system(size: 11)).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.red.opacity(0.18)))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.red.opacity(0.5), lineWidth: 1))
    }

    var settings: some View {
        VStack(alignment: .leading, spacing: 6) {
            Theme.label("Endereço do painel")
            HStack {
                TextField("http://127.0.0.1:8080", text: $urlDraft).textFieldStyle(.roundedBorder)
                    .onSubmit(saveURL)
                Button("Salvar", action: saveURL)
            }
            Toggle("Abrir ao iniciar o Mac", isOn: $launchAtLogin)
                .onChange(of: launchAtLogin) { on in
                    do {
                        if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
                    } catch {
                        store.message = ("Não deu para mudar: \(error.localizedDescription)", false)
                        launchAtLogin = SMAppService.mainApp.status == .enabled
                    }
                }
            Rectangle().fill(Theme.green.opacity(0.3)).frame(height: 1)
        }
        .onAppear { urlDraft = store.panelURL }
    }

    func saveURL() {
        store.panelURL = urlDraft
        store.options = [:]
        Task { await store.refresh(); await store.loadOptions() }
    }

    var footer: some View {
        HStack {
            Button("Abrir painel") { openPanel(store.base) }
            Spacer()
            Button { showSettings.toggle() } label: { Image(systemName: "gearshape") }.help("Ajustes")
            Button { Task { await store.refresh(); await store.loadOptions() } } label: {
                Image(systemName: "arrow.clockwise")
            }.help("Atualizar agora")
            Button("Sair") { NSApp.terminate(nil) }
        }
    }
}

/// O macOS pinta o texto da barra de menu de uma cor só; para ficar verde, o rótulo vira uma
/// imagem colorida (isTemplate = false). Verde médio: legível com a barra clara e com a escura.
@MainActor
func barImage(_ text: String, alert: Bool) -> NSImage {
    let green = Color(red: 0.13, green: 0.78, blue: 0.36)
    var label = Text(text).foregroundColor(green)
    if alert { label = label + Text(" ⚠︎").foregroundColor(.orange) }
    let renderer = ImageRenderer(content: label.font(.system(size: 13.5, weight: .bold)).monospacedDigit())
    renderer.scale = NSScreen.main?.backingScaleFactor ?? 2
    let image = renderer.nsImage ?? NSImage()
    image.isTemplate = false
    return image
}

@main
struct PainelBarraApp: App {
    @StateObject private var store = Store()

    var body: some Scene {
        MenuBarExtra {
            ContentView().environmentObject(store)
        } label: {
            Image(nsImage: barImage(store.barText, alert: !store.activeAlerts.isEmpty))
                .accessibilityLabel(store.barTitle)
        }
        .menuBarExtraStyle(.window)
    }
}
