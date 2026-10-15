import SwiftUI

@main struct CodexHarborApp: App {
    @NSApplicationDelegateAdaptor(HarborAppDelegate.self) private var appDelegate
    @StateObject private var model = LauncherModel()
    var body: some Scene {
        WindowGroup(Distribution.current.appName) {
            Dashboard(model: model).frame(minWidth: 1120, minHeight: 720)
                .task {
                    await model.refresh()
                    model.quotas.refreshDue()
                    while !Task.isCancelled {
                        try? await Task.sleep(for: .seconds(4))
                        if !model.busy {
                            await model.refresh()
                            if NSApp.isActive { model.quotas.refreshDue() }
                        }
                    }
                }
        }.defaultSize(width: 1320, height: 850)
    }
}

struct HarborCard<Content: View>: View {
    let title: String
    var subtitle: String = ""
    @ViewBuilder var content: () -> Content
    var body: some View {
        VStack(alignment: .leading, spacing: 15) {
            VStack(alignment: .leading, spacing: 5) {
                Text(title).font(.headline)
                if !subtitle.isEmpty { Text(subtitle).font(.callout).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true) }
            }
            content()
        }.padding(20).frame(maxWidth: .infinity, alignment: .leading)
            .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 14))
            .overlay(RoundedRectangle(cornerRadius: 14).stroke(Color.primary.opacity(0.09), lineWidth: 1))
    }
}

struct AddSourceButton: View {
    let title: String
    let detail: String
    var action: () -> Void
    var body: some View {
        Button(action: action) {
            HStack(spacing: 12) {
                Image(systemName: "plus").font(.title2.weight(.medium))
                    .frame(width: 34, height: 34).background(Color.accentColor.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
                VStack(alignment: .leading, spacing: 4) {
                    Text(title).font(.headline)
                    Text(detail).font(.caption).foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
            }.padding(16).frame(maxWidth: .infinity, minHeight: 92, alignment: .leading)
                .background(Color.accentColor.opacity(0.07), in: RoundedRectangle(cornerRadius: 13))
                .overlay(RoundedRectangle(cornerRadius: 13).stroke(Color.accentColor.opacity(0.28)))
        }.buttonStyle(.plain).foregroundStyle(Color.accentColor)
    }
}

struct SelectionCard<Content: View, Actions: View>: View {
    let selected: Bool
    let accessibilityLabel: String
    var action: (() -> Void)?
    @ViewBuilder var content: () -> Content
    @ViewBuilder var actions: () -> Actions
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            content().allowsHitTesting(false).accessibilityHidden(action != nil)
            actions()
        }.padding(13)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background {
                if let action {
                    // Standalone cards select via a background button. Sidebar rows
                    // use the native table for selection and drag initiation.
                    Button(action: action) {
                        RoundedRectangle(cornerRadius: 11)
                            .fill(Color(nsColor: .controlBackgroundColor))
                            .contentShape(RoundedRectangle(cornerRadius: 11))
                    }.buttonStyle(.plain)
                        .accessibilityLabel(accessibilityLabel)
                        .accessibilityAddTraits(selected ? .isSelected : [])
                        .acceptSelectionOnActivation()
                } else {
                    RoundedRectangle(cornerRadius: 11)
                        .fill(Color(nsColor: .controlBackgroundColor))
                        .accessibilityLabel(accessibilityLabel)
                }
            }
            .overlay(RoundedRectangle(cornerRadius: 11)
                .strokeBorder(selected ? Color.accentColor : Color.primary.opacity(0.09), lineWidth: selected ? 1.5 : 1)
                .allowsHitTesting(false))
    }
}

extension View {
    @ViewBuilder func acceptSelectionOnActivation() -> some View {
        if #available(macOS 15.0, *) {
            // Only harmless selection accepts the activation click. Do not apply
            // this to login, launch, probe, removal, or other side-effect actions.
            self.allowsWindowActivationEvents()
        } else {
            self
        }
    }
}
