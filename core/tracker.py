import asyncio
import logging
import random
import socket
import struct
from typing import List
from urllib.parse import urlencode

import aiohttp

from .bencoding import *

# 2-byte big-endian unsigned short
PORT_FORMAT = ">H"
IDENTIFIER_PREFIX = "-PY0001-"

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s"
)


class TrackerKeys:
    failure = b"failure reason"
    interval = b"interval"
    complete = b"complete"
    incomplete = b"incomplete"
    peers = b"peers"


class TrackerResponses:
    ok = 200
    failure = "failure"


class TrackerResponse:
    """Response from tracker after successfully connecting to the tracker announce URL."""

    def __init__(self, response: dict):
        self.response = response

    @property
    def failure(self) -> Optional[str]:
        """Returns failure reason if tracker failed, else None."""
        if TrackerKeys.failure in self.response:
            return self.response[TrackerKeys.failure].decode()
        return None

    @property
    def interval(self) -> int:
        """Returns the number of seconds the downloader should wait between regular requests."""
        return self.response.get(TrackerKeys.interval, 0)

    @property
    def complete(self):
        """Returns number of peers with the entire file (seeders)."""
        return self.response.get(TrackerKeys.complete, 0)

    @property
    def incomplete(self):
        """Returns number of peers without the entire file (leechers)."""
        return self.response.get(TrackerKeys.incomplete, 0)

    @property
    def peers(self) -> List[tuple[str, int]]:
        """Returns a list of peers structured by the following format: (ip, port)."""
        peers = self.response.get(TrackerKeys.peers, [])
        logging.debug(f"peers: {peers}")
        if isinstance(peers, list):
            # Non-compact mode: peers is a list of dictionaries
            result = []
            for peer in peers:
                ip, port = peer.get(b"ip"), peer.get(b"port")
                if isinstance(ip, bytes):
                    ip = ip.decode()
                result.append((ip, port))
            return result
        else:
            # Compact mode (binary): peer([4-byte ipv4], [2-byte port])
            if len(peers) % 6 != 0:
                logging.warning(f"Compact peers length not divisible by 6: {len(peers)}")
            return [
                (socket.inet_ntoa(peers[i:i + 4]), struct.unpack(">H", peers[i + 4:i + 6])[0])
                for i in range(0, len(peers), 6)
            ]
        return []

    def __str__(self):
        return "incomplete: {incomplete}\n" \
               "complete: {complete}\n" \
               "interval: {interval}\n" \
               "peers: {peers}\n".format(
            incomplete=self.incomplete,
            complete=self.complete,
            interval=self.interval,
            peers=", ".join([i for (i, _) in self.peers]))


class Tracker:
    """Represent connection to a tracker for a given .torrent (download/seeding state)."""

    def __init__(self, torrent):
        self.torrent = torrent
        self.peer_id = _calculate_peer_id()
        self.http_client = None

    async def connect(self,
                      first_announce: bool = None,
                      uploaded: int = 0,
                      downloaded: int = 0) -> TrackerResponse:
        """Makes announce call to tracker to update relevant statistics."""
        if self.http_client is None:
            self.http_client = aiohttp.ClientSession()

        params = self._construct_tracker_params(uploaded, downloaded)
        if first_announce:
            params["event"] = "started"

        announce = self.torrent.announce
        if not announce:
            raise NotImplementedError("Torrent has no tracker (DHT-only not supported yet)")

        url = announce + "?" + urlencode(params)
        logging.info(f"Connecting to tracker: {url}")

        try:
            async with self.http_client.get(url) as response:
                if response.status != TrackerResponses.ok:
                    raise ConnectionError(
                        f"Unable to connect to tracker: status code {response.status}"
                    )

                data = await response.read()
                _validate(data)
                logging.debug(f"Received: {data}")

                return TrackerResponse(Decoder(data).bdecode())

        except aiohttp.ClientError as e:
            logging.warning(f"HTTP tracker connection failed: {e}")
            return TrackerResponse({})
        except asyncio.TimeoutError:
            logging.warning("HTTP tracker timed out")
            return TrackerResponse({})

    async def close(self):
        if self.http_client:
            await self.http_client.close()

    def _construct_tracker_params(self, uploaded: int, downloaded: int) -> dict:
        """Constructs the URL parameters used while issuing announce call to tracker."""
        return {
            "info_hash": self.torrent.info_hash,
            "peer_id": self.peer_id,
            "port": 54321,
            "uploaded": uploaded,
            "downloaded": downloaded,
            "left": self.torrent.total_size - downloaded,
            "compact": 1,
        }


def _decode_port(port) -> int:
    """Converts packed binary port number to int."""
    return struct.unpack(PORT_FORMAT, port)[0]


def _calculate_peer_id():
    """Returns a 20-byte long identifier ("-PY0001-<random-integers>")."""
    return IDENTIFIER_PREFIX + "".join([str(random.randint(0, 9)) for _ in range(12)])


def _validate(tracker_response: bytes) -> None:
    """Detect errors in tracker response (including when status code 200)."""
    try:
        message = tracker_response.decode("utf-8")
        if TrackerResponses.failure in message:
            raise ConnectionError(f"Unable to connect to tracker: {message}")
    except UnicodeDecodeError:
        pass
