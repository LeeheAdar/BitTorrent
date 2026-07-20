import socket
import struct
from typing import Optional, Tuple, Union, List

from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes
from Crypto.Util.Padding import pad, unpad

from auth.message_codes import *

LENGTH_FIELD_FORMAT = "I"  # Unsigned integer
LENGTH_FIELD_SIZE = struct.calcsize(LENGTH_FIELD_FORMAT)
IV_SIZE = 16

DIVIDER = b'\x02'
CHUNK_SIZE = 1024 * 100


class EncryptedSocket:
    _socket: socket.socket
    _symmetric_key: bytes

    def __init__(self, sock: socket.socket, key: bytes):
        self._socket = sock
        self._symmetric_key = key

    def send_message(self, code: Union[CommandCodes, ResponseCodes],
                     entries: Optional[List[str]] = None):
        if entries:
            data = code + DIVIDER + DIVIDER.join(entry.encode() for entry in entries)
        else:
            data = code

        encrypted_message = self._encrypt(data)
        self._socket.sendall(struct.pack(LENGTH_FIELD_FORMAT, len(encrypted_message)) + encrypted_message)

    def read_message(self) -> bytes:
        encoded_message_length = self._socket.recv(LENGTH_FIELD_SIZE)
        while len(encoded_message_length) < LENGTH_FIELD_SIZE:
            size_part = self._socket.recv(LENGTH_FIELD_SIZE - len(encoded_message_length))
            if not size_part:
                return b''
            encoded_message_length += size_part

        data_len = struct.unpack(LENGTH_FIELD_FORMAT, encoded_message_length)[0]

        data = b''
        while len(data) < data_len:
            chunk = self._socket.recv(min(CHUNK_SIZE, data_len - len(data)))
            if not chunk:
                return b''
            data += chunk

        return self._decrypt(data)

    def _encrypt(self, data: bytes) -> bytes:
        # Generating iv
        iv = get_random_bytes(IV_SIZE)

        # encrypted the data using AES (CBC)
        cipher = AES.new(self._symmetric_key, AES.MODE_CBC, iv)
        padded_data = pad(data, AES.block_size)

        # Adding the iv at the start of the encrypted data
        encoded_data = iv + cipher.encrypt(padded_data)
        return encoded_data

    def _decrypt(self, data: bytes) -> bytes:
        # Extracting iv and encrypted data
        iv = data[:IV_SIZE]
        encoded_data = data[IV_SIZE:]

        # Decrypting data using iv
        decipher = AES.new(self._symmetric_key, AES.MODE_CBC, iv)
        decrypted_data = unpad(decipher.decrypt(encoded_data), AES.block_size)
        return decrypted_data

    def parse_message(self, message: bytes) -> Tuple[bytes, List[str]]:
        command = message.split(DIVIDER)[0]
        data = [entry.decode() for entry in message.split(DIVIDER)[1:]]
        return command, data
