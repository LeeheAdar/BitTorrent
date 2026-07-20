import secrets
import socket

from Crypto.Cipher import PKCS1_OAEP
from Crypto.Hash import SHA256
from Crypto.Protocol.KDF import HKDF
from Crypto.PublicKey import RSA
from Crypto.Random import get_random_bytes
from Crypto.Util import number

from auth.message_codes import *


def send_data(sock: socket.socket, data: bytes):
    sock.sendall(len(data).to_bytes(4, 'big') + data)


def recv_data(sock: socket.socket) -> bytes:
    length = int.from_bytes(sock.recv(4), 'big')
    data = b''
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise ConnectionError("Socket connection broken")
        data += chunk
    return data


def perform_rsa(sock: socket.socket, is_client: bool) -> bytes:
    if is_client:
        send_data(sock, KeyExchangeMode.RSAPublicKey)
        public_key = RSA.import_key(recv_data(sock))
        cipher = PKCS1_OAEP.new(public_key)
        secret = get_random_bytes(16)
        send_data(sock, cipher.encrypt(secret))
        return secret
    else:
        request = recv_data(sock)
        if request != KeyExchangeMode.RSAPublicKey:
            raise ValueError("Expected RSA public key request")
        key = RSA.generate(2048)
        cipher = PKCS1_OAEP.new(key)
        send_data(sock, key.publickey().export_key())
        encrypted = recv_data(sock)
        secret = cipher.decrypt(encrypted)
        return secret


def perform_dh(sock: socket.socket, is_client: bool) -> bytes:
    if is_client:
        data = recv_data(sock).decode()
        p, g, A = map(int, data.split(','))
        b = secrets.randbelow(p)
        B = pow(g, b, p)
        send_data(sock, str(B).encode())
        shared = pow(A, b, p)
    else:
        p = number.getPrime(512)
        g = 2
        a = secrets.randbelow(p)
        A = pow(g, a, p)
        send_data(sock, f"{p},{g},{A}".encode())
        B = int(recv_data(sock).decode())
        shared = pow(B, a, p)

    shared_bytes = shared.to_bytes((shared.bit_length() + 7) // 8, 'big')
    return HKDF(shared_bytes, 16, b'', SHA256)
