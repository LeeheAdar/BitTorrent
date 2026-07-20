"""A minimal bencoding encoder & decoder for .torrent_files.
Follows BEP3 and supports 4 data types (Integer, String, List, Dictionary).
BEP3 Specifications: https://www.bittorrent.org/beps/bep_0003.html
"""
from typing import Optional, Any

TOKEN_LENGTH = 1


class Tokens:
    _int = 'i'
    _list = 'l'
    _dict = 'd'
    end = 'e'
    str_sep = ':'


class EncodedTokens:
    _int = b'i'
    _list = b'l'
    _dict = b'd'
    end = b'e'
    str_sep = b':'


class Decoder:
    """Decodes a bencoded sequence of bytes."""

    def __init__(self, data: bytes):
        if not isinstance(data, bytes):
            raise TypeError("'data' must be of type bytes")
        self.data = data
        self._index = 0

    def bdecode(self):
        """Recursively bdecode data."""
        return self._decode_next()

    def _decode_next(self):
        c = self._peek()

        if c is None:
            raise EOFError("Unexpected end of data")

        elif c == EncodedTokens._int:
            self._index += TOKEN_LENGTH
            end = self.data.index(EncodedTokens.end, self._index)
            val = self.data[self._index:end]
            self._index = end + TOKEN_LENGTH
            return int(val)

        elif c == EncodedTokens._list:
            self._index += TOKEN_LENGTH
            lst = []
            while self.data[self._index:self._index + 1] != EncodedTokens.end:
                lst.append(self._decode_next())
            self._index += TOKEN_LENGTH
            return lst

        elif c == EncodedTokens._dict:
            self._index += TOKEN_LENGTH
            d = {}
            while self.data[self._index:self._index + 1] != EncodedTokens.end:
                key, val = self._decode_next(), self._decode_next()
                d[key] = val
            self._index += TOKEN_LENGTH
            return d

        else:
            sep = self.data.index(EncodedTokens.str_sep, self._index)
            length = int(self.data[self._index:sep])
            start = sep + 1
            end = start + length
            s = self.data[start:end]
            self._index = end
            return s

    def _peek(self) -> Optional[bytes]:
        """Returns the next character from the bencoded data."""
        if self._index >= len(self.data):
            return None
        return self.data[self._index:self._index + 1]

    def decode_bytes(self, obj):
        """Recursively decode bytes to strings."""
        if isinstance(obj, dict):
            return {self.decode_bytes(k): self.decode_bytes(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self.decode_bytes(i) for i in obj]
        elif isinstance(obj, bytes):
            try:
                return obj.decode()
            except UnicodeDecodeError:
                return obj
        else:
            return obj


class Encoder:
    """Bencode a sequence of bytes."""

    def __init__(self, data: Any):
        self.data = data

    def bencode(self) -> Optional[bytes]:
        """Recursively bencode data."""

        def encode_next(data) -> Optional[bytes]:
            if isinstance(data, int):
                return EncodedTokens._int + str(data).encode() + EncodedTokens.end
            elif isinstance(data, list):
                return EncodedTokens._list + b"".join(encode_next(i) for i in data) + EncodedTokens.end
            elif isinstance(data, dict):
                # BEP 3 requirement: keys must be sorted raw bytes
                out = bytearray(EncodedTokens._dict)
                for k in sorted(data.keys(), key=lambda x:
                x if isinstance(x, bytes) else str(x).encode()):
                    v = data[k]
                    out += encode_next(k) + encode_next(v)
                out += EncodedTokens.end
                return out
            elif isinstance(data, str):
                return str(len(data)).encode() + EncodedTokens.str_sep + data.encode()
            elif isinstance(data, bytes):
                result = bytearray()
                return result + str.encode(str(len(data))) + EncodedTokens.str_sep + data
            else:
                return None

        return encode_next(self.data)
