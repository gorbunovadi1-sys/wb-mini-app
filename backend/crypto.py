import logging
import os

from cryptography.fernet import Fernet

log = logging.getLogger("crypto")

ENV_FILE = os.path.join(os.path.dirname(__file__), "..", ".env")


def _get_or_create_key() -> bytes:
    key = os.environ.get("ENCRYPTION_KEY")
    if key:
        return key.encode()

    # Dev convenience only: generate once and persist to the local .env so restarts
    # keep working. On Railway this must be set as a real persistent env var —
    # writing to a local .env file has no effect there, and a key that isn't
    # persisted means every redeploy makes existing encrypted API keys unreadable.
    new_key = Fernet.generate_key()
    with open(ENV_FILE, "a", encoding="utf-8") as f:
        f.write(f"\nENCRYPTION_KEY={new_key.decode()}\n")
    os.environ["ENCRYPTION_KEY"] = new_key.decode()
    log.warning(
        "ENCRYPTION_KEY was missing — generated a new one and saved to .env for local "
        "dev. On Railway, set ENCRYPTION_KEY manually to a persistent value or you'll "
        "lose access to stored API keys on the next redeploy."
    )
    return new_key


_fernet = Fernet(_get_or_create_key())


def encrypt(plaintext: str) -> str:
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _fernet.decrypt(ciphertext.encode()).decode()
