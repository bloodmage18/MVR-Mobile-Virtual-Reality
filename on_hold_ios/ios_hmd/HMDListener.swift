// Phase 0 — HMD Listener
// A minimal iOS app that listens on TCP port 12345 over USB,
// displays received messages, and echoes them back to the PC.
//
// Xcode setup:
//   1. Create a new iOS App project (SwiftUI, minimum target iOS 15)
//   2. Replace the contents of ContentView.swift with this file
//      (or add this file and delete the default ContentView.swift)
//   3. In your @main App struct, point to ContentView as normal

import SwiftUI
import Network

// MARK: - Entry Point

@main
struct HMDApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}

// MARK: - UI

struct ContentView: View {
    @StateObject private var server = USBServer()

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()

            VStack(spacing: 24) {
                Text("HMD Listener")
                    .font(.largeTitle.bold())
                    .foregroundColor(.white)

                Circle()
                    .fill(server.isConnected ? Color.green : Color.orange)
                    .frame(width: 16, height: 16)

                Text(server.status)
                    .font(.system(.body, design: .monospaced))
                    .foregroundColor(.white)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 32)

                if !server.log.isEmpty {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 6) {
                            ForEach(server.log, id: \.self) { entry in
                                Text(entry)
                                    .font(.system(.caption, design: .monospaced))
                                    .foregroundColor(.green)
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding()
                    }
                    .background(Color.white.opacity(0.05))
                    .cornerRadius(10)
                    .padding(.horizontal, 16)
                    .frame(maxHeight: 300)
                }
            }
        }
    }
}

// MARK: - TCP Server

class USBServer: ObservableObject {
    @Published var status: String = "Starting..."
    @Published var isConnected: Bool = false
    @Published var log: [String] = []

    private var listener: NWListener?
    private let port: NWEndpoint.Port = 12345

    init() {
        start()
    }

    private func start() {
        do {
            listener = try NWListener(using: .tcp, on: port)
        } catch {
            update(status: "Failed to create listener: \(error)")
            return
        }

        listener?.stateUpdateHandler = { [weak self] state in
            DispatchQueue.main.async {
                switch state {
                case .ready:
                    self?.update(status: "Listening on port 12345\nWaiting for PC connection...")
                case .failed(let error):
                    self?.update(status: "Listener error: \(error)")
                case .cancelled:
                    self?.update(status: "Listener stopped.")
                default:
                    break
                }
            }
        }

        listener?.newConnectionHandler = { [weak self] connection in
            DispatchQueue.main.async {
                self?.isConnected = true
                self?.update(status: "PC connected.")
                self?.addLog("-- connection opened --")
            }
            connection.start(queue: .main)
            self?.receive(on: connection)
        }

        listener?.start(queue: .main)
    }

    private func receive(on connection: NWConnection) {
        connection.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, _, isComplete, error in
            if let data = data, !data.isEmpty {
                let message = String(data: data, encoding: .utf8) ?? "(binary \(data.count) bytes)"
                DispatchQueue.main.async {
                    self?.addLog("recv: \(message)")
                }
                // Echo the message back to the PC
                connection.send(content: data, completion: .idempotent)
            }

            if isComplete || error != nil {
                DispatchQueue.main.async {
                    self?.isConnected = false
                    self?.addLog("-- connection closed --")
                    self?.update(status: "Listening on port 12345\nWaiting for PC connection...")
                }
                connection.cancel()
            } else {
                self?.receive(on: connection)
            }
        }
    }

    private func update(status: String) {
        self.status = status
    }

    private func addLog(_ entry: String) {
        log.append(entry)
        if log.count > 50 {
            log.removeFirst()
        }
    }
}
