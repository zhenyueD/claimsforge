"""
凭据加密存储（Windows DPAPI / Unix Fernet 兜底）
- Windows: 使用 CryptProtectData，绑定当前用户，离开本机或换用户解密失败
- 非 Windows: 使用 cryptography.Fernet + machine-id 派生密钥
"""
import os
import sys
import base64
import json
from pathlib import Path

CRED_DIR = Path(__file__).parent.parent / "data" / ".credentials"
CRED_DIR.mkdir(parents=True, exist_ok=True)


def _windows_protect(plaintext: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    def _blob(data: bytes) -> DATA_BLOB:
        buf = ctypes.create_string_buffer(data, len(data))
        return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))

    in_blob = _blob(plaintext)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob), "easyclaw-cs-team", None, None, None, 0, ctypes.byref(out_blob)
    ):
        raise RuntimeError("CryptProtectData failed")
    try:
        cipher = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)
    return cipher


def _windows_unprotect(cipher: bytes) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    def _blob(data: bytes) -> DATA_BLOB:
        buf = ctypes.create_string_buffer(data, len(data))
        return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))

    in_blob = _blob(cipher)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob)
    ):
        raise RuntimeError("CryptUnprotectData failed (wrong user / machine?)")
    try:
        plain = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)
    return plain


def _fernet_key() -> bytes:
    """非 Windows：从 machine-id + user 派生稳定密钥。"""
    import hashlib
    seed_parts = [
        os.environ.get("USER", "u"),
        os.environ.get("HOME", "h"),
    ]
    for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            seed_parts.append(Path(p).read_text().strip())
        except Exception:
            pass
    raw = "|".join(seed_parts).encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_to_file(name: str, plaintext: str) -> Path:
    path = CRED_DIR / f"{name}.bin"
    data = plaintext.encode("utf-8")
    if sys.platform == "win32":
        cipher = _windows_protect(data)
        meta = {"scheme": "dpapi", "version": 1}
    else:
        from cryptography.fernet import Fernet  # type: ignore
        cipher = Fernet(_fernet_key()).encrypt(data)
        meta = {"scheme": "fernet", "version": 1}
    payload = {
        "meta": meta,
        "cipher_b64": base64.b64encode(cipher).decode("ascii"),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    return path


def decrypt_from_file(name: str) -> str | None:
    path = CRED_DIR / f"{name}.bin"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        cipher = base64.b64decode(payload["cipher_b64"])
        scheme = payload.get("meta", {}).get("scheme")
        if scheme == "dpapi":
            return _windows_unprotect(cipher).decode("utf-8")
        elif scheme == "fernet":
            from cryptography.fernet import Fernet  # type: ignore
            return Fernet(_fernet_key()).decrypt(cipher).decode("utf-8")
        else:
            return None
    except Exception as e:
        print(f"[secure_store] decrypt failed: {e}", file=sys.stderr)
        return None


def has(name: str) -> bool:
    return (CRED_DIR / f"{name}.bin").exists()


if __name__ == "__main__":
    # CLI: python secure_store.py set <name>   # 从 stdin 读 plaintext
    #      python secure_store.py get <name>
    import sys as _s
    if len(_s.argv) < 3:
        print("usage: secure_store.py set|get <name>")
        _s.exit(2)
    op, name = _s.argv[1], _s.argv[2]
    if op == "set":
        text = _s.stdin.read().strip()
        if not text:
            print("empty plaintext", file=_s.stderr); _s.exit(2)
        p = encrypt_to_file(name, text)
        print(f"OK -> {p}")
    elif op == "get":
        v = decrypt_from_file(name)
        print(v or "")
    else:
        print("unknown op"); _s.exit(2)
