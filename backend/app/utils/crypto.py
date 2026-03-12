from cryptography.fernet import Fernet
from ..config import settings
import base64
import hashlib


def _get_key() -> bytes:
    """从 SECRET_KEY 生成 Fernet 密钥"""
    # 使用 SHA256 生成 32 字节的密钥
    key = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    return base64.urlsafe_b64encode(key)


def encrypt_credentials(plaintext: str) -> str:
    """加密凭证"""
    f = Fernet(_get_key())
    return f.encrypt(plaintext.encode()).decode()


def decrypt_credentials(ciphertext: str) -> str:
    """解密凭证"""
    f = Fernet(_get_key())
    return f.decrypt(ciphertext.encode()).decode()
