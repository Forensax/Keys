from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session
from starlette.requests import Request

from .models import AppSetting


SESSION_KEY_AUTHENTICATED = "authenticated"
SESSION_KEY_VAULT_ID = "vault_id"
SESSION_KEY_PASSWORD_PROOF = "password_proof"
SETTING_PASSWORD_HASH = "password_hash"
SETTING_ENCRYPTION_SALT = "encryption_salt"
SETTING_PASSWORD_PROOF_SALT = "password_proof_salt"

_ph = PasswordHasher()
_vault_keys: dict[str, bytes] = {}


@dataclass(frozen=True)
class AuthState:
    initialized: bool
    authenticated: bool


def get_setting(db: Session, key: str) -> str | None:
    setting = db.get(AppSetting, key)
    return setting.value if setting else None


def set_setting(db: Session, key: str, value: str) -> None:
    setting = db.get(AppSetting, key)
    if setting is None:
        setting = AppSetting(key=key, value=value)
        db.add(setting)
    else:
        setting.value = value


def is_initialized(db: Session) -> bool:
    return get_setting(db, SETTING_PASSWORD_HASH) is not None


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def needs_rehash(password_hash: str) -> bool:
    return _ph.check_needs_rehash(password_hash)


def random_salt() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def _decode_salt(salt: str) -> bytes:
    return base64.urlsafe_b64decode(salt.encode("ascii"))


def derive_fernet_key(password: str, salt: str) -> bytes:
    raw_key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), _decode_salt(salt), 390_000, dklen=32)
    return base64.urlsafe_b64encode(raw_key)


def get_fernet(password: str, salt: str) -> Fernet:
    return Fernet(derive_fernet_key(password, salt))


def get_fernet_from_key(key: bytes) -> Fernet:
    return Fernet(key)


def encrypt_api_key(api_key: str, password: str, salt: str) -> str:
    return get_fernet(password, salt).encrypt(api_key.encode("utf-8")).decode("ascii")


def encrypt_api_key_with_fernet(api_key: str, fernet: Fernet) -> str:
    return fernet.encrypt(api_key.encode("utf-8")).decode("ascii")


def decrypt_api_key(encrypted_api_key: str, password: str, salt: str) -> str:
    try:
        return get_fernet(password, salt).decrypt(encrypted_api_key.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("无法解密 API Key，请使用当前密码重新登录。") from exc


def decrypt_api_key_with_fernet(encrypted_api_key: str, fernet: Fernet) -> str:
    try:
        return fernet.decrypt(encrypted_api_key.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("无法解密 API Key，请使用当前密码重新登录。") from exc


def encrypt_secret_with_fernet(value: str, fernet: Fernet) -> str:
    if not value:
        return ""
    return fernet.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret_with_fernet(value: str, fernet: Fernet) -> str:
    if not value:
        return ""
    try:
        return fernet.decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("无法解密代理认证信息，请使用当前密码重新登录。") from exc


def make_password_proof(password: str, salt: str) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), _decode_salt(salt), 120_000, dklen=32)
    return base64.urlsafe_b64encode(digest).decode("ascii")


def constant_time_equal(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return False
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def setup_application(db: Session, password: str) -> None:
    encryption_salt = random_salt()
    proof_salt = random_salt()
    set_setting(db, SETTING_PASSWORD_HASH, hash_password(password))
    set_setting(db, SETTING_ENCRYPTION_SALT, encryption_salt)
    set_setting(db, SETTING_PASSWORD_PROOF_SALT, proof_salt)


def authenticate(db: Session, password: str) -> bool:
    password_hash = get_setting(db, SETTING_PASSWORD_HASH)
    if not password_hash:
        return False
    valid = verify_password(password_hash, password)
    if valid and needs_rehash(password_hash):
        set_setting(db, SETTING_PASSWORD_HASH, hash_password(password))
    return valid


def login_session(request: Request, db: Session, password: str) -> None:
    proof_salt = get_setting(db, SETTING_PASSWORD_PROOF_SALT)
    encryption_salt = get_setting(db, SETTING_ENCRYPTION_SALT)
    if not proof_salt or not encryption_salt:
        raise RuntimeError("应用尚未初始化。")
    vault_id = secrets.token_urlsafe(32)
    _vault_keys[vault_id] = derive_fernet_key(password, encryption_salt)
    request.session[SESSION_KEY_AUTHENTICATED] = True
    request.session[SESSION_KEY_VAULT_ID] = vault_id
    request.session[SESSION_KEY_PASSWORD_PROOF] = make_password_proof(password, proof_salt)


def logout_session(request: Request) -> None:
    vault_id = request.session.get(SESSION_KEY_VAULT_ID)
    if vault_id:
        _vault_keys.pop(vault_id, None)
    request.session.clear()


def get_session_password(request: Request, db: Session, submitted_password: str | None = None) -> str | None:
    if submitted_password:
        return submitted_password

    if not request.session.get(SESSION_KEY_AUTHENTICATED):
        return None

    # The app intentionally never stores the plaintext password in the session.
    return None


def get_session_fernet(request: Request) -> Fernet | None:
    vault_id = request.session.get(SESSION_KEY_VAULT_ID)
    if not vault_id:
        return None
    key = _vault_keys.get(vault_id)
    if not key:
        return None
    return get_fernet_from_key(key)


def require_session_fernet(request: Request) -> Fernet:
    fernet = get_session_fernet(request)
    if fernet is None:
        raise ValueError("密钥库已锁定，请重新登录。")
    return fernet


def check_session_password_proof(request: Request, db: Session, password: str) -> bool:
    proof_salt = get_setting(db, SETTING_PASSWORD_PROOF_SALT)
    if not proof_salt:
        return False
    expected = request.session.get(SESSION_KEY_PASSWORD_PROOF)
    actual = make_password_proof(password, proof_salt)
    return constant_time_equal(expected, actual)


def is_authenticated(request: Request) -> bool:
    return bool(request.session.get(SESSION_KEY_AUTHENTICATED)) and get_session_fernet(request) is not None


def key_hint(api_key: str) -> str:
    api_key = api_key.strip()
    if not api_key:
        return ""
    if len(api_key) <= 8:
        return "..." + api_key[-4:]
    return f"{api_key[:3]}...{api_key[-4:]}"
