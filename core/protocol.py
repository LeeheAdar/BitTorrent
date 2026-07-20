import asyncio
import logging
import struct
from asyncio import Queue, StreamReader, StreamWriter
from concurrent.futures import CancelledError
from typing import Optional, Tuple, Union, override

import bitstring

# Default request size as specified in BEP3 — 2^14 (16 KiB)
REQUEST_SIZE = 2 ** 14
MAX_READ_ATTEMPTS = 10
CHUNK_SIZE = 10 * 1024

# Length header: 4-byte unsigned big-endian integer
HEADER_FORMAT = ">I"
HEADER_LENGTH = struct.calcsize(HEADER_FORMAT)

# Message type: 1-byte signed big-endian integer
TYPE_FORMAT = ">b"
TYPE_LENGTH = struct.calcsize(TYPE_FORMAT)

OUTPUT_PATH = "../downloads/"

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s"
)


class States:
    choked = "choked"
    interested = "interested"
    pending_request = "pending request"
    stopped = "stopped"
    # States for the *remote* peer's view of us (used when seeding)
    peer_choked = "peer_choked"  # we are choking the remote peer
    peer_interested = "peer_interested"  # remote peer is interested in us


def is_valid_peer(ip: str, port: int) -> bool:
    """Return False for IPv6 addresses or out-of-range ports."""
    if ":" in ip:  # IPv6 — skip for now
        return False
    if port <= 0 or port > 65535:
        return False
    return True


class PeerConnection:
    """Manages one TCP connection to a remote BitTorrent peer.

    Supports both outbound connections (pulled from *peers_queue*) and inbound
    connections (passed via the *inbound* kwarg as a (reader, writer) tuple).
    When inbound is provided the queue is not consumed and we act as a seeder
    from the start.

    After handshake the connection:
    - Sends our current bitfield.
    - Sends Interested so the remote peer may unchoke us.
    - Optionally unchokes the remote peer when they express interest (seeding).
    - Handles all standard BEP 3 messages in a single async loop.
    """

    max_read_attempts = MAX_READ_ATTEMPTS

    def __init__(self, peers_queue: Queue, info_hash, peer_id,
                 piece_manager, block_callback=None,
                 inbound: Optional[Tuple[StreamReader, StreamWriter]] = None):
        self.my_state: set = set()
        self.peer_state: set = set()

        self.peers_queue = peers_queue
        self.info_hash = info_hash
        self.peer_id = peer_id
        self.piece_manager = piece_manager
        self.block_callback = block_callback

        self.remote_id = None
        self.writer: Optional[StreamWriter] = None
        self.reader: Optional[StreamReader] = None

        # If an (reader, writer) pair is given we skip the queue entirely
        self._inbound = inbound

        self.future = asyncio.ensure_future(self._start())

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def _start(self):
        """Main coroutine — either opens an outbound connection or handles
        an already-open inbound one, then drives the message loop."""
        if self._inbound:
            await self._run_connection(*self._inbound, inbound=True)
            return

        while States.stopped not in self.my_state:
            ip, port = await self.peers_queue.get()
            if not is_valid_peer(ip, port):
                continue
            logging.info(f"Got peer {ip}:{port} from queue")
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(ip, port), timeout=5
                )
            except (OSError, asyncio.TimeoutError,
                    ConnectionRefusedError, ConnectionResetError) as e:
                logging.warning(f"Skipping {ip}:{port} ({e})")
                continue

            logging.info(f"Connected to {ip}:{port}")
            try:
                await self._run_connection(reader, writer, inbound=False)
            finally:
                try:
                    self.peers_queue.task_done()
                except ValueError:
                    pass

    async def _run_connection(self, reader: StreamReader,
                              writer: StreamWriter, *, inbound: bool):
        """Drive the full lifecycle of one peer connection (both directions)."""
        self.reader = reader
        self.writer = writer

        try:
            if inbound:
                buffer = await self._handshake_inbound()
            else:
                buffer = await self._handshake()

            if buffer is None:
                return

            # We start choked by the remote peer; also choke them until interested
            self.my_state.add(States.choked)
            self.my_state.add(States.peer_choked)

            await self._send_bitfield()

            if not inbound:
                # Outbound: express interest immediately
                await self._send_interested()
                self.my_state.add(States.interested)

            async for message in StreamIterator(self.reader, buffer):
                if States.stopped in self.my_state:
                    break
                await self._dispatch(message)

                # If we are unchoked and not waiting on a reply, request a block
                if (
                        self.remote_id in self.piece_manager.peers
                        and States.choked not in self.my_state
                        and States.pending_request not in self.my_state
                        and not self.piece_manager.complete
                ):
                    await self._request_piece()

        except asyncio.TimeoutError:
            logging.warning("Peer timed out")
        except (ConnectionRefusedError, TimeoutError):
            logging.warning("Unable to connect to peer")
        except (ConnectionResetError, CancelledError):
            logging.warning("Connection closed")
        except Exception as e:
            logging.exception(f"An error occurred: {e}")
            raise
        finally:
            await self.cancel()

    # ------------------------------------------------------------------
    # Message dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, message):
        """Route a decoded message to the appropriate handler."""
        if isinstance(message, Choke):
            self.my_state.add(States.choked)

        elif isinstance(message, Unchoke):
            self.my_state.discard(States.choked)
            logging.debug("Peer unchoked us — requesting pieces")

        elif isinstance(message, Interested):
            # Remote peer wants data from us — unchoke them so we can serve
            self.peer_state.add(States.peer_interested)
            if States.peer_choked in self.my_state:
                self.my_state.discard(States.peer_choked)
                await self._send_unchoke()

        elif isinstance(message, NotInterested):
            self.peer_state.discard(States.peer_interested)

        elif isinstance(message, Have):
            self.piece_manager.update_peer(self.remote_id, message.piece_index)
            if States.interested not in self.my_state:
                await self._send_interested()
                self.my_state.add(States.interested)

        elif isinstance(message, BitField):
            self.piece_manager.add_peer(self.remote_id, message.bitfield)
            # Express interest if the peer has anything we need
            if States.interested not in self.my_state:
                await self._send_interested()
                self.my_state.add(States.interested)

        elif isinstance(message, Request):
            await self._handle_request(message)

        elif isinstance(message, Piece):
            self.my_state.discard(States.pending_request)
            if self.block_callback:
                self.block_callback(
                    peer_id=self.remote_id,
                    piece_index=message.index,
                    block_offset=message.begin,
                    data=message.block,
                )

        elif isinstance(message, Cancel):
            # Cancel is best-effort; we log and ignore for now
            logging.debug(
                f"Cancel received for piece {message.index} "
                f"offset {message.begin} len {message.length}"
            )

        elif isinstance(message, KeepAlive):
            pass  # Nothing to do

    # ------------------------------------------------------------------
    # Outbound helpers
    # ------------------------------------------------------------------

    async def _send_bitfield(self):
        """Send our current bitfield so the remote peer knows what we have.

        Uses PieceManager.get_bitfield() to build an accurate byte array
        rather than always sending an empty one.
        """
        bitfield_bytes = self.piece_manager.get_bitfield()
        msg = (
                struct.pack(">Ib", 1 + len(bitfield_bytes), Message.BitField)
                + bytes(bitfield_bytes)
        )
        self.writer.write(msg)
        await self.writer.drain()

    async def _send_interested(self):
        """Send an Interested message to the remote peer."""
        logging.info(f"Sending Interested to {self.remote_id}")
        self.writer.write(Interested().encode())
        await self.writer.drain()

    async def _send_unchoke(self):
        """Send an Unchoke message, allowing the remote peer to request blocks."""
        logging.info(f"Unchoking peer {self.remote_id}")
        self.writer.write(Unchoke().encode())
        await self.writer.drain()

    async def _request_piece(self):
        """Ask the remote peer for the next needed block (rarest-first)."""
        block = self.piece_manager.next_request(self.remote_id)
        if block:
            message = Request(block.piece, block.offset, block.length).encode()
            logging.debug(
                f"Requesting block offset={block.offset} piece={block.piece} "
                f"len={block.length} from {self.remote_id}"
            )
            self.writer.write(message)
            await self.writer.drain()
            self.my_state.add(States.pending_request)

    async def _handle_request(self, message: "Request"):
        """Serve a piece block to the remote peer (seeding path).

        Reads the requested block from disk via PieceManager.read_block()
        and sends a Piece message back. Silently ignores requests for pieces
        we do not yet have.
        """
        if States.peer_choked in self.my_state:
            logging.debug("Ignoring Request from a peer we're choking")
            return

        if not self.piece_manager.have_piece(message.index):
            logging.debug(
                f"Peer requested piece {message.index} which we don't have — ignoring"
            )
            return

        data = self.piece_manager.read_block(
            message.index, message.begin, message.length
        )
        if data is None:
            logging.warning(
                f"Failed to read block for piece {message.index} "
                f"offset {message.begin}"
            )
            return

        response = Piece(message.index, message.begin, data).encode()
        logging.debug(
            f"Serving piece {message.index} offset {message.begin} "
            f"({len(data)} bytes) to {self.remote_id}"
        )
        self.writer.write(response)
        await self.writer.drain()

    # ------------------------------------------------------------------
    # Handshake
    # ------------------------------------------------------------------

    async def _handshake(self) -> Optional[bytes]:
        """Outbound handshake — we send first, then read the response."""
        self.writer.write(Handshake(self.info_hash, self.peer_id).encode())
        await self.writer.drain()

        buffer = b""
        attempts = 1
        while len(buffer) < Handshake.length and attempts < self.max_read_attempts:
            attempts += 1
            buffer += await self.reader.read(StreamIterator.chunk_size)

        response = Handshake.decode(buffer[:Handshake.length])
        if not response:
            logging.error("Could not parse handshake response")
            return None
        if response.info_hash != self.info_hash:
            raise ValueError("Handshake info_hash mismatch")

        self.remote_id = response.peer_id
        logging.info(f"Handshake OK with {self.remote_id}")
        return buffer[Handshake.length:]

    async def _handshake_inbound(self) -> Optional[bytes]:
        """Inbound handshake — we read the remote's handshake first, then reply."""
        buffer = b""
        attempts = 0
        while len(buffer) < Handshake.length and attempts < self.max_read_attempts:
            attempts += 1
            buffer += await self.reader.read(StreamIterator.chunk_size)

        response = Handshake.decode(buffer[:Handshake.length])
        if not response:
            logging.error("Could not parse inbound handshake")
            return None
        if response.info_hash != self.info_hash:
            logging.warning("Inbound handshake info_hash mismatch — dropping")
            return None

        self.remote_id = response.peer_id
        # Reply with our own handshake
        self.writer.write(Handshake(self.info_hash, self.peer_id).encode())
        await self.writer.drain()
        logging.info(f"Inbound handshake OK with {self.remote_id}")
        return buffer[Handshake.length:]

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    async def cancel(self):
        """Remove peer from PieceManager and close the TCP connection."""
        logging.info(f"Closing connection to {self.remote_id}")
        if not self.future.done():
            self.future.cancel()
        if self.writer is not None:
            try:
                if self.remote_id:
                    self.piece_manager.remove_peer(self.remote_id)
                self.writer.close()
                await self.writer.wait_closed()
            except Exception as e:
                logging.warning(f"Error closing writer: {e}")

    def stop(self):
        """Signal this connection to stop (called externally by Peer.stop())."""
        self.my_state.add(States.stopped)
        if not self.future.done():
            self.future.cancel()


# ---------------------------------------------------------------------------
# Stream parser
# ---------------------------------------------------------------------------

class StreamIterator:
    """Async iterator that reads raw bytes from a StreamReader and emits
    decoded BitTorrent Message objects one at a time.

    Also serves as the read path for blocks we need to serve back to peers:
    all incoming data — whether it is a request from a leecher or a piece
    from a seeder — flows through here and is dispatched by _dispatch().
    """

    _header_length = HEADER_LENGTH
    _header_format = HEADER_FORMAT
    chunk_size = CHUNK_SIZE

    def __init__(self, reader: StreamReader, initial: bytes = b""):
        self.reader = reader
        self.buffer = initial

    def __aiter__(self):
        return self

    async def __anext__(self):
        """Read from the stream until a complete message can be parsed."""
        while True:
            try:
                data = await self.reader.read(self.chunk_size)
                if data:
                    self.buffer += data
                    message = self.parse()
                    if message:
                        return message
                else:
                    self.buffer = b""
                    raise StopAsyncIteration()
            except ConnectionResetError:
                logging.debug("Connection reset by peer")
                raise StopAsyncIteration()
            except CancelledError:
                logging.debug("Connection cancelled")
                raise StopAsyncIteration()
            except StopAsyncIteration:
                raise
            except Exception as e:
                logging.exception(f"Stream error: {e}")
                raise StopAsyncIteration()

    def parse(self) -> Optional["Message"]:
        """Attempt to extract one complete message from the internal buffer.

        Format: <4-byte length><1-byte id><payload>
        Returns None if there is not yet enough data.
        """
        if len(self.buffer) < self._header_length:
            return None

        message_length = struct.unpack(
            self._header_format, self.buffer[:self._header_length]
        )[0]

        if message_length == 0:
            self._consume(message_length)
            return KeepAlive()

        if len(self.buffer) < HEADER_LENGTH + message_length:
            return None

        message_id = struct.unpack(
            TYPE_FORMAT,
            self.buffer[HEADER_LENGTH:HEADER_LENGTH + TYPE_LENGTH]
        )[0]

        if message_id == Message.Choke:
            self._consume(message_length)
            return Choke()
        elif message_id == Message.Unchoke:
            self._consume(message_length)
            return Unchoke()
        elif message_id == Message.Interested:
            self._consume(message_length)
            return Interested()
        elif message_id == Message.NotInterested:
            self._consume(message_length)
            return NotInterested()
        elif message_id == Message.Have:
            data = self._data(message_length)
            self._consume(message_length)
            return Have.decode(data)
        elif message_id == Message.BitField:
            data = self._data(message_length)
            self._consume(message_length)
            return BitField.decode(data)
        elif message_id == Message.Request:
            data = self._data(message_length)
            self._consume(message_length)
            return Request.decode(data)
        elif message_id == Message.Piece:
            data = self._data(message_length)
            self._consume(message_length)
            return Piece.decode(data)
        elif message_id == Message.Cancel:
            data = self._data(message_length)
            self._consume(message_length)
            return Cancel.decode(data)
        else:
            logging.info(f"Unsupported message id: {message_id}")
            # Consume the unknown message so we don't stall
            self._consume(message_length)
            return None

    def _consume(self, message_length: int):
        """Advance the buffer past the current message."""
        self.buffer = self.buffer[HEADER_LENGTH + message_length:]

    def _data(self, message_length: int) -> bytes:
        """Return the raw bytes of the current message (length prefix included)."""
        return self.buffer[:HEADER_LENGTH + message_length]


# ---------------------------------------------------------------------------
# Message classes
# ---------------------------------------------------------------------------

class Message:
    """Base class and ID registry for all BEP 3 messages."""

    Choke = 0
    Unchoke = 1
    Interested = 2
    NotInterested = 3
    Have = 4
    BitField = 5
    Request = 6
    Piece = 7
    Cancel = 8

    Handshake = None
    KeepAlive = None

    _header_format = ">Ib"
    _header_size = struct.calcsize(_header_format)

    _length_format = ">I"
    _length_field_size = struct.calcsize(_length_format)

    _id_format = ">b"
    _id_field_size = struct.calcsize(_id_format)

    def encode(self) -> bytes:
        raise NotImplementedError("'encode' not implemented")

    @classmethod
    def decode(cls, data: bytes):
        raise NotImplementedError("'decode' not implemented")


class Choke(Message):
    """Choke message — <len=0001><id=0>."""

    def __str__(self):
        return "Choke"


class Unchoke(Message):
    """Unchoke message — <len=0001><id=1>."""

    @override
    def encode(self) -> bytes:
        return struct.pack(">Ib", 1, Message.Unchoke)

    def __str__(self):
        return "Unchoke"


class Interested(Message):
    """Interested message — <len=0001><id=2>."""

    @override
    def encode(self) -> bytes:
        return struct.pack(
            Interested._header_format,
            Interested._header_size,
            Message.Interested,
        )

    def __str__(self):
        return "Interested"


class NotInterested(Message):
    """Not Interested message — <len=0001><id=3>."""

    @override
    def encode(self) -> bytes:
        return struct.pack(
            NotInterested._header_format,
            NotInterested._header_size,
            Message.NotInterested,
        )

    def __str__(self):
        return "Not Interested"


class Have(Message):
    """Have message — <len=0005><id=4><piece index>.

    Sent after a piece is fully downloaded and verified, to notify all
    connected peers that we now have it.
    """

    _format = ">bI"  # id + piece_index (without length prefix)
    _length = struct.calcsize(_format)

    def __init__(self, piece_index: int):
        self.piece_index = piece_index

    @override
    def encode(self) -> bytes:
        return (
                struct.pack(self._length_format, self._length)
                + struct.pack(self._format, Message.Have, self.piece_index)
        )

    @classmethod
    @override
    def decode(cls, data: bytes) -> "Have":
        _, piece_index = struct.unpack(
            Have._format,
            data[Message._length_field_size:Message._length_field_size + Have._length]
        )
        return Have(piece_index)

    def __str__(self):
        return f"Have({self.piece_index})"


class BitField(Message):
    """BitField message — <len=0001+X><id=5><bitfield>.

    Bit i (MSB first within each byte) is 1 if we have piece i, else 0.
    """

    def __init__(self, data: bytes):
        self.bitfield = bitstring.BitArray(bytes=data)

    @override
    def encode(self) -> bytes:
        bitfield_bytes = self.bitfield.tobytes()
        return (
                struct.pack(">Ib", 1 + len(bitfield_bytes), Message.BitField)
                + bitfield_bytes
        )

    @classmethod
    @override
    def decode(cls, data: bytes) -> "BitField":
        message_length = struct.unpack(
            Message._length_format, data[:Message._length_field_size]
        )[0]
        logging.debug(f"Decoding BitField of length {message_length}")
        bitfield = data[Message._header_size:Message._header_size + message_length - 1]
        return BitField(bitfield)

    def __str__(self):
        return "BitField"


class Request(Message):
    """Request message — <len=0013><id=6><index><begin><length>.

    Asks the remote peer to send us a block of *length* bytes starting at
    *begin* within piece *index*. Default block size is 2^14 bytes.
    """

    _format = ">bIII"  # id + index + begin + length
    _length = struct.calcsize(_format)

    def __init__(self, index: int, begin: int, length: int = REQUEST_SIZE):
        self.index = index
        self.begin = begin
        self.length = length

    @override
    def encode(self) -> bytes:
        return (
                struct.pack(">I", self._length)
                + struct.pack(self._format, Message.Request,
                              self.index, self.begin, self.length)
        )

    @classmethod
    @override
    def decode(cls, data: bytes) -> "Request":
        _, index, begin, length = struct.unpack(
            Request._format,
            data[Message._length_field_size:Message._length_field_size + Request._length]
        )
        return Request(index, begin, length)

    def __str__(self):
        return f"Request(piece={self.index} begin={self.begin} len={self.length})"


class Piece(Message):
    """Piece message — <len=0009+X><id=7><index><begin><block>.

    Despite the name (per BEP 3 spec) this actually carries a *block* — a
    sub-slice of a piece. Used both when we download (remote → us) and when
    we seed (us → remote).
    """

    _format = ">bII"  # id + index + begin
    _length = struct.calcsize(_format)

    def __init__(self, index: int, begin: int, block: bytes):
        self.index = index
        self.begin = begin
        self.block = block

    @override
    def encode(self) -> bytes:
        message_length = Piece._length + len(self.block)
        return (
                struct.pack(Message._length_format, message_length)
                + struct.pack(
            f">bII{len(self.block)}s",
            Message.Piece,
            self.index,
            self.begin,
            self.block,
        )
        )

    @classmethod
    @override
    def decode(cls, data: bytes) -> "Piece":
        logging.debug(f"Decoding Piece of length {len(data)}")
        message_length = struct.unpack(
            Message._length_format, data[:Message._length_field_size]
        )[0]
        _, index, begin, block = struct.unpack(
            f"{Piece._format}{message_length - Piece._length}s",
            data[Message._length_field_size:Message._length_field_size + message_length]
        )
        return Piece(index, begin, block)

    def __str__(self):
        return f"Piece(index={self.index} begin={self.begin})"


class Cancel(Message):
    """Cancel message — <len=0013><id=8><index><begin><length>.

    Cancels a previously sent Request (used in end-game mode).
    """

    _format = ">bIII"
    _length = struct.calcsize(_format)

    def __init__(self, index: int, begin: int, length: int = REQUEST_SIZE):
        self.index = index
        self.begin = begin
        self.length = length

    @override
    def encode(self) -> bytes:
        return (
                struct.pack(Message._length_format, self._length)
                + struct.pack(self._format, Message.Cancel,
                              self.index, self.begin, self.length)
        )

    @classmethod
    @override
    def decode(cls, data: bytes) -> "Cancel":
        _, index, begin, length = struct.unpack(
            Cancel._format,
            data[Message._length_field_size:Message._length_field_size + Cancel._length]
        )
        return Cancel(index, begin, length)

    def __str__(self):
        return f"Cancel(piece={self.index} begin={self.begin})"


class Handshake(Message):
    """BitTorrent handshake — <pstrlen><pstr><reserved><info_hash><peer_id>.

    Fields:
        pstrlen  : 1 byte  — always 19
        pstr     : 19 bytes — "BitTorrent protocol"
        reserved : 8 bytes  — all zero (extension bits; we set none)
        info_hash: 20 bytes — SHA-1 of the bencoded info dict
        peer_id  : 20 bytes — our local identifier
    """

    _pstr = b"BitTorrent protocol"
    _pstrlen = len(_pstr)
    _format = ">B19s8s20s20s"
    length = 68

    def __init__(self, info_hash: Union[bytes, str], peer_id: Union[bytes, str]):
        if isinstance(info_hash, str):
            info_hash = info_hash.encode()
        if isinstance(peer_id, str):
            peer_id = peer_id.encode()
        self.info_hash = info_hash
        self.peer_id = peer_id

    @override
    def encode(self) -> bytes:
        return struct.pack(
            Handshake._format,
            Handshake._pstrlen,
            Handshake._pstr,
            b"\x00" * 8,
            self.info_hash,
            self.peer_id,
        )

    @classmethod
    @override
    def decode(cls, data: bytes) -> Optional["Handshake"]:
        if len(data) < cls.length:
            return None
        pstrlen, pstr, reserved, info_hash, peer_id = struct.unpack(
            cls._format, data[:cls.length]
        )
        if pstr != cls._pstr:
            return None
        return Handshake(info_hash, peer_id)

    def __str__(self):
        return "Handshake"


class KeepAlive(Message):
    """Keep-Alive message — <len=0000> (no id or payload)."""

    @override
    def encode(self) -> bytes:
        return struct.pack(">I", 0)

    def __str__(self):
        return "KeepAlive"
