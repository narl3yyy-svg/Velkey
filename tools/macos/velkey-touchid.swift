// Touch ID helper for Velkey on macOS. Built by runmac.sh / installmac.sh with
// `xcrun swiftc` (Apple's command-line tools, which Homebrew already needs).
//
//   velkey-touchid available                 exit 0 if Touch ID can be used
//   velkey-touchid store  ACCOUNT            password on stdin -> Keychain
//   velkey-touchid get    ACCOUNT REASON     fingerprint, then password -> stdout
//   velkey-touchid delete ACCOUNT
//
// Exit codes: 0 ok, 2 cancelled / fingerprint not accepted, 3 nothing stored,
// 4 Touch ID not available, 1 other error. Secrets only travel through
// stdin/stdout pipes, never on the command line.
import Foundation
import LocalAuthentication
import Security

let service = "io.github.velkey.touchid"
let args = CommandLine.arguments

func fail(_ code: Int32, _ msg: String) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(code)
}

func query(_ account: String) -> [String: Any] {
    return [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account]
}

func canUseTouchID() -> Bool {
    var err: NSError?
    return LAContext().canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &err)
}

guard args.count >= 2 else { fail(1, "usage: velkey-touchid available|store|get|delete ACCOUNT [REASON]") }

switch args[1] {
case "available":
    exit(canUseTouchID() ? 0 : 4)

case "store":
    guard args.count >= 3 else { fail(1, "store needs an account") }
    let secret = FileHandle.standardInput.readDataToEndOfFile()
    SecItemDelete(query(args[2]) as CFDictionary)
    var item = query(args[2])
    item[kSecValueData as String] = secret
    item[kSecAttrLabel as String] = "Velkey Touch ID"
    item[kSecAttrAccessible as String] = kSecAttrAccessibleWhenUnlockedThisDeviceOnly
    let status = SecItemAdd(item as CFDictionary, nil)
    exit(status == errSecSuccess ? 0 : 1)

case "get":
    guard args.count >= 4 else { fail(1, "get needs an account and a reason") }
    guard canUseTouchID() else { exit(4) }
    let ctx = LAContext()
    ctx.localizedFallbackTitle = ""   // no "enter password" button: Velkey's own field is the fallback
    let done = DispatchSemaphore(value: 0)
    var ok = false
    ctx.evaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, localizedReason: args[3]) { success, _ in
        ok = success
        done.signal()
    }
    done.wait()
    guard ok else { exit(2) }
    var q = query(args[2])
    q[kSecReturnData as String] = true
    q[kSecMatchLimit as String] = kSecMatchLimitOne
    var out: AnyObject?
    let status = SecItemCopyMatching(q as CFDictionary, &out)
    guard status == errSecSuccess, let data = out as? Data else { exit(3) }
    FileHandle.standardOutput.write(data)
    exit(0)

case "delete":
    guard args.count >= 3 else { fail(1, "delete needs an account") }
    let status = SecItemDelete(query(args[2]) as CFDictionary)
    exit(status == errSecSuccess || status == errSecItemNotFound ? 0 : 1)

default:
    fail(1, "unknown command \(args[1])")
}
