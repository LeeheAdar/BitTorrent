import logging
from collections import namedtuple
from hashlib import sha1
from typing import List

from .bencoding import *
from .protocol import OUTPUT_PATH

SHA1_LENGTH = 20

# Represents files within the torrent, i.e. the files to write to disk
TorrentFile = namedtuple("TorrentFile", ("name", "length"))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s"
)


class Torrent:
    """Represents .torrent file's meta-data, used as a wrapper for bencoding utilities."""

    def __init__(self, filename: str):
        self.filename = filename
        self.files: List[TorrentFile] = []

        with open(self.filename, "rb") as f:
            self.meta_data = Decoder(f.read()).bdecode()

        self.info_hash = sha1(
            Encoder(self.meta_data[b"info"]).bencode()
        ).digest()

        self._identify_files()

    def _identify_files(self):
        """Identify files included in the relevant .torrent file"""
        if self.multi_file:
            # TODO add support for multi-file torrents
            raise NotImplementedError("Multi-file torrents are not supported")
        self.files.append(
            TorrentFile(
                self.meta_data[b"info"][b"name"].decode(),
                self.meta_data[b"info"][b"length"]
            )
        )

    @property
    def multi_file(self) -> bool:
        """Returns True if multiple .torrent files are available"""
        return b"files" in self.meta_data[b"info"]

    @property
    def announce(self):
        """Returns the announce URL to the tracker."""
        if b"announce" in self.meta_data:
            return self.meta_data[b"announce"].decode()

        if b"announce-list" in self.meta_data:
            # announce-list is a list of lists
            return self.meta_data[b"announce-list"][0][0].decode()

        return None

    @property
    def piece_length(self) -> int:
        """Returns the length of each piece from the .torrent file."""
        return self.meta_data[b"info"][b"piece length"]

    @property
    def total_size(self) -> int:
        """Returns the total size of all the files in the .torrent file."""
        return sum(f.length for f in self.files)

    @property
    def pieces(self):
        """Returns a list of all pieces hashes (each 20-byte long).
        The meta_data[b"info"][b"pieces"] is a string containing all pieces SHA1 hashes.
        """
        data = self.meta_data[b"info"][b"pieces"]
        pieces = []
        offset = 0
        length = len(data)

        while offset < length:
            pieces.append(data[offset:offset + SHA1_LENGTH])
            offset += SHA1_LENGTH
        return pieces

    @property
    def output_file(self):
        """Returns the full path for the output file"""
        return OUTPUT_PATH + self.meta_data[b"info"][b"name"].decode()

    def __str__(self):
        name = self.meta_data[b"info"][b"name"].decode() if isinstance(self.meta_data[b"info"][b"name"],
                                                                       bytes) else str(self.meta_data[b"info"][b"name"])
        length = self.meta_data[b"info"].get(b"length", None)
        announce = self.meta_data[b"announce"].decode() if isinstance(self.meta_data[b"announce"], bytes) else str(
            self.meta_data[b"announce"])
        return (
            f"Filename: {name}\n"
            f"File length: {length}\n"
            f"Announce URL: {announce}\n"
            f"Hash: {self.info_hash.hex()}"
        )
