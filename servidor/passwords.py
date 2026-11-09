"""Hash de contraseñas compatible con el formato scrypt de Werkzeug 3.

Evita depender de paquetes externos para este ejercicio de sockets.
"""
import hashlib
import hmac
import secrets


def generate_password_hash(password: str) -> str:
    method = 'scrypt:32768:8:1'
    salt = secrets.token_hex(8)
    digest = hashlib.scrypt(password.encode('utf-8'), salt=salt.encode('utf-8'), n=32768, r=8, p=1, maxmem=64 * 1024 * 1024)
    return f'{method}${salt}${digest.hex()}'


def check_password_hash(stored: str, password: str) -> bool:
    try:
        method, salt, expected = stored.split('$')
        parts = method.split(':')
        if len(parts) == 4 and parts[0] == 'scrypt':
            n, r, p = map(int, parts[1:])
            if (n, r, p) != (32768, 8, 1):
                return False
            digest = hashlib.scrypt(password.encode('utf-8'), salt=salt.encode('utf-8'), n=n, r=r, p=p, maxmem=64 * 1024 * 1024)
        elif len(parts) == 3 and parts[0] == 'pbkdf2' and parts[1] == 'sha256':
            iterations = int(parts[2])
            if not 1 <= iterations <= 2_000_000:
                return False
            digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt.encode('utf-8'), iterations)
        else:
            return False
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError, OverflowError):
        return False
