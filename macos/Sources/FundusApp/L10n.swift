import Foundation

/// German or English, following the system's first preferred language.
/// Two languages, like the web interface; anything else gets English.
enum L10n {
    static let german: Bool = {
        (Locale.preferredLanguages.first ?? "en").lowercased().hasPrefix("de")
    }()
}

/// `L("Beenden", "Quit")` — the German string first, matching the codebase.
func L(_ de: String, _ en: String) -> String { L10n.german ? de : en }
