import Darwin
import Foundation
import Security

// This service is deliberately unrelated to Codex, ChatGPT, and private Harbor.
private let service = "org.codexharbor.community.api-keys"
private let maximumSecretBytes = 16 * 1024
private let maximumRequestBytes = 64 * 1024

private enum Failure: String, Error {
    case invalidRequest = "invalid_request"
    case notFound = "not_found"
    case accessDenied = "access_denied"
    case locked
    case unavailable
    case failure
}

private func check(_ status: OSStatus) throws {
    switch status {
    case errSecSuccess: return
    case errSecItemNotFound: throw Failure.notFound
    case errSecInteractionNotAllowed, errSecInteractionRequired: throw Failure.locked
    case errSecAuthFailed, errSecUserCanceled: throw Failure.accessDenied
    case errSecNotAvailable, errSecNoSuchKeychain, errSecInvalidKeychain: throw Failure.unavailable
    default: throw Failure.failure
    }
}

private func validSecret(_ value: String) -> Bool {
    !value.isEmpty && value.utf8.count <= maximumSecretBytes &&
        !value.unicodeScalars.contains { $0.value < 32 || $0.value == 127 }
}

private func validReference(_ value: String) -> Bool {
    value.utf8.count == 69 && value.range(of: "\\Achc_[0-9a-f]{32}_[0-9a-f]{32}\\z",
                                         options: .regularExpression) != nil
}

private func fileInfo(_ path: String) throws -> stat {
    var info = stat()
    guard lstat(path, &info) == 0 else { throw Failure.invalidRequest }
    return info
}

private func canonicalPath(_ path: String) throws -> String {
    guard let resolved = realpath(path, nil) else { throw Failure.invalidRequest }
    defer { free(resolved) }
    return String(cString: resolved)
}

private func testKeychainPath(_ raw: String, creating: Bool) throws -> String {
    let url = URL(fileURLWithPath: raw)
    guard raw.hasPrefix("/"), raw.utf8.count < PATH_MAX,
          !raw.unicodeScalars.contains(where: { $0.value < 32 || $0.value == 127 }),
          url.lastPathComponent == "test.keychain-db" else { throw Failure.invalidRequest }
    let parent = url.deletingLastPathComponent()
    guard raw == parent.path + "/test.keychain-db",
          try canonicalPath(parent.path) == parent.path else { throw Failure.invalidRequest }
    let root = parent.deletingLastPathComponent().path
    var buffer = [CChar](repeating: 0, count: Int(PATH_MAX))
    guard confstr(_CS_DARWIN_USER_TEMP_DIR, &buffer, buffer.count) > 0 else { throw Failure.invalidRequest }
    let userTemporary = try canonicalPath(String(cString: buffer))
    guard [userTemporary, "/private/tmp"].contains(root),
          parent.lastPathComponent.hasPrefix("codex-harbor-keychain-test-") else { throw Failure.invalidRequest }
    let parentInfo = try fileInfo(parent.path)
    guard parentInfo.st_uid == getuid(), parentInfo.st_mode & S_IFMT == S_IFDIR,
          parentInfo.st_mode & 0o777 == 0o700 else { throw Failure.invalidRequest }
    if creating {
        var info = stat()
        guard lstat(raw, &info) != 0, errno == ENOENT else { throw Failure.invalidRequest }
    } else {
        let info = try fileInfo(raw)
        guard info.st_uid == getuid(), info.st_mode & S_IFMT == S_IFREG,
              info.st_mode & 0o077 == 0, info.st_nlink == 1 else { throw Failure.invalidRequest }
    }
    return raw
}

private func readRequest() throws -> [String: Any] {
    var input = Data()
    while let chunk = try FileHandle.standardInput.read(upToCount: 4096), !chunk.isEmpty {
        guard input.count + chunk.count <= maximumRequestBytes else { throw Failure.invalidRequest }
        input.append(chunk)
    }
    guard let request = try JSONSerialization.jsonObject(with: input) as? [String: Any] else {
        throw Failure.invalidRequest
    }
    return request
}

private func run() throws -> [String: Any] {
    let args = Array(CommandLine.arguments.dropFirst())
    guard args.isEmpty || args == ["--test-keychain"] else { throw Failure.invalidRequest }
    let testing = !args.isEmpty
    let request: [String: Any]
    do { request = try readRequest() } catch { throw Failure.invalidRequest }
    guard let operation = request["op"] as? String else { throw Failure.invalidRequest }
    var fields: Set<String> = ["op"]
    var allowInteraction = false
    if let value = request["allow_interaction"] {
        guard CFGetTypeID(value as CFTypeRef) == CFBooleanGetTypeID(),
              let boolean = value as? Bool, !(testing && boolean) else { throw Failure.invalidRequest }
        allowInteraction = boolean
        fields.insert("allow_interaction")
    }
    if testing { fields.insert("test_keychain") }
    if ["get", "put", "delete"].contains(operation) {
        fields.insert("ref")
        guard let ref = request["ref"] as? String, validReference(ref) else { throw Failure.invalidRequest }
    } else if !testing || !["test_create", "test_destroy"].contains(operation) {
        throw Failure.invalidRequest
    }
    if ["put", "test_create"].contains(operation) {
        fields.insert("secret")
        guard let secret = request["secret"] as? String, validSecret(secret) else { throw Failure.invalidRequest }
    }
    guard Set(request.keys) == fields else { throw Failure.invalidRequest }
    // Background operations fail promptly; only an explicit user action may
    // request OS authorization. The adapter and alarm still bound the wait.
    try check(SecKeychainSetUserInteractionAllowed(allowInteraction))
    var selected: SecKeychain?
    if testing {
        guard let raw = request["test_keychain"] as? String else { throw Failure.invalidRequest }
        let path = try testKeychainPath(raw, creating: operation == "test_create")
        if operation == "test_create" {
            let password = Data((request["secret"] as! String).utf8)
            let status = password.withUnsafeBytes {
                SecKeychainCreate(path, UInt32(password.count), $0.baseAddress, false, nil, &selected)
            }
            try check(status)
            let descriptor = open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
            guard descriptor >= 0 else { throw Failure.failure }
            defer { close(descriptor) }
            var info = stat()
            guard fstat(descriptor, &info) == 0, info.st_uid == getuid(),
                  info.st_mode & S_IFMT == S_IFREG, info.st_nlink == 1,
                  fchmod(descriptor, 0o600) == 0 else { throw Failure.failure }
            return ["ok": true]
        }
        try check(SecKeychainOpen(path, &selected))
    } else {
        try check(SecKeychainCopyDefault(&selected))
    }
    guard let keychain = selected else { throw Failure.unavailable }
    if operation == "test_destroy" {
        try check(SecKeychainDelete(keychain))
        return ["ok": true]
    }
    let ref = request["ref"] as! String
    // Every read/update/delete carries both namespace and exact opaque account.
    let identity: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: service,
        kSecAttrAccount as String: ref,
    ]
    var query = identity
    query[kSecMatchSearchList as String] = [keychain]
    switch operation {
    case "get":
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var value: CFTypeRef?
        try check(SecItemCopyMatching(query as CFDictionary, &value))
        guard let data = value as? Data, data.count <= maximumSecretBytes,
              let secret = String(data: data, encoding: .utf8), validSecret(secret) else { throw Failure.failure }
        return ["ok": true, "secret": secret]
    case "put":
        let data = Data((request["secret"] as! String).utf8)
        var item = identity
        item[kSecUseKeychain as String] = keychain
        item[kSecValueData as String] = data
        item[kSecAttrLabel as String] = "Codex Harbor Community API Key"
        // Default access trusts this helper, never an allow-all application ACL.
        let status = SecItemAdd(item as CFDictionary, nil)
        if status == errSecDuplicateItem {
            try check(SecItemUpdate(query as CFDictionary, [kSecValueData as String: data] as CFDictionary))
        } else {
            try check(status)
        }
    case "delete":
        let status = SecItemDelete(query as CFDictionary)
        if status != errSecItemNotFound { try check(status) }
    default:
        throw Failure.invalidRequest
    }
    return ["ok": true]
}

private func emit(_ response: [String: Any]) {
    // Error messages never contain request values, paths, OS text, or credentials.
    guard let data = try? JSONSerialization.data(withJSONObject: response), data.count <= maximumRequestBytes else {
        exit(1)
    }
    do {
        try FileHandle.standardOutput.write(contentsOf: data)
        try FileHandle.standardOutput.write(contentsOf: Data([10]))
    } catch {
        exit(1)
    }
}

// The adapter has a shorter deadline; this also bounds direct CLI invocations.
alarm(35)
do {
    emit(try run())
} catch let error as Failure {
    emit(["ok": false, "error": error.rawValue])
} catch {
    emit(["ok": false, "error": Failure.failure.rawValue])
}
