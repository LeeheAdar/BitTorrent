import asyncio
import logging
import math
import os
import time
from asyncio import Queue
from dataclasses import dataclass
from hashlib import sha1
from typing import List, Optional

from .protocol import PeerConnection, REQUEST_SIZE, Have
from .torrent import Torrent
from .tracker import Tracker

MAX_PEER_CONNECTIONS = 15
MAX_PENDING_TIME = 5 * 60 * 1000
DEFAULT_INTERVAL = 30 * 60

# Address we listen on for incoming peer connections (seeding)
LISTEN_IP = "0.0.0.0"
LISTEN_PORT = 6881


@dataclass
class PendingRequest:
    block: "Block"
    added: float


logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s"
)


class Peer:
    """A BitTorrent Peer class representing the local peer that manages all connections.

    On start it announces to the tracker, fills the peer queue consumed by a pool of
    PeerConnection workers, and additionally opens a TCP listen server so remote peers
    can initiate connections to us (required for seeding / acting as a full peer).
    """

    _interval = DEFAULT_INTERVAL

    def __init__(self, torrent: Torrent):
        self.tracker = Tracker(torrent)
        self.available_peers = Queue()
        self.peers: List[PeerConnection] = []
        self.piece_manager = PieceManager(torrent)
        self.abort = False
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self):
        """Start downloading (and seeding) the relevant .torrent file.

        Spins up:
        - A pool of outbound PeerConnection workers.
        - An asyncio TCP server that accepts inbound peer connections.
        - The main announce/download loop.
        """
        self.peers = [
            PeerConnection(
                self.available_peers,
                self.tracker.torrent.info_hash,
                self.tracker.peer_id,
                self.piece_manager,
                self._block_retrieved
            ) for _ in range(MAX_PEER_CONNECTIONS)
        ]

        # Initialize piece manager's connections
        self.piece_manager.connections = self.peers

        # Start listening for inbound connections (seeding)
        self._server = await asyncio.start_server(
            self._accept_inbound, LISTEN_IP, LISTEN_PORT
        )
        logging.info(f"Listening for inbound peers on port {LISTEN_PORT}")

        previous = None

        while True:
            if self.piece_manager.complete:
                logging.info("Download complete — continuing to seed")
                # Keep running as a seeder; only break on abort
            if self.abort:
                logging.info("Aborting")
                break

            current = time.time()
            if (not previous) or (previous + self._interval < current):
                tracker_response = await self.tracker.connect(
                    first_announce=previous if previous else False,
                    uploaded=self.piece_manager.bytes_uploaded,
                    downloaded=self.piece_manager.bytes_downloaded,
                )

                if tracker_response.response != {}:
                    previous = current
                    self._interval = tracker_response.interval
                    self._empty_queue()
                    for peer in tracker_response.peers:
                        self.available_peers.put_nowait(peer)
            else:
                await asyncio.sleep(5)

        await self.stop()

    async def _accept_inbound(self, reader: asyncio.StreamReader,
                              writer: asyncio.StreamWriter):
        """Handle a raw inbound TCP connection from a remote peer.

        Creates a PeerConnection in *inbound* mode — it skips the queue and
        uses the already-open reader/writer pair instead.
        """
        addr = writer.get_extra_info("peername")
        logging.info(f"Inbound connection from {addr}")
        conn = PeerConnection(
            self.available_peers,
            self.tracker.torrent.info_hash,
            self.tracker.peer_id,
            self.piece_manager,
            self._block_retrieved,
            inbound=(reader, writer),
        )
        self.peers.append(conn)
        self.piece_manager.connections.append(conn)

    def _empty_queue(self):
        """Drain the available_peers queue."""
        while not self.available_peers.empty():
            self.available_peers.get_nowait()

    async def stop(self):
        """Stops downloading/seeding, closes listen server and tracker session."""
        self.abort = True
        for peer in self.peers:
            peer.stop()
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        self.piece_manager.close()
        await self.tracker.close()

    def _block_retrieved(self, peer_id, piece_index, block_offset, data):
        """Callback invoked by PeerConnection when a new block arrives."""
        self.piece_manager.block_received(
            peer_id=peer_id,
            piece_index=piece_index,
            block_offset=block_offset,
            data=data
        )


class Block:
    """Represents a partial piece — the unit actually transmitted between peers.

    Default size is 2^14 bytes (16 KiB) per BEP 3; the final block of the last
    piece may be smaller.
    """

    Missing = 0
    Pending = 1
    Retrieved = 2

    def __init__(self, piece: int, offset: int, length: int = REQUEST_SIZE):
        self.piece = piece
        self.offset = offset
        self.length = length
        self.status = Block.Missing
        self.data = None


class Piece:
    """Represents one piece of the torrent content.

    Each piece is subdivided into Block objects for wire transfer. The piece
    verifies its integrity via SHA-1 before being written to disk.
    """

    def __init__(self, index: int, blocks: List[Block], piece_hash):
        self.index = index
        self.blocks = blocks
        self.piece_hash = piece_hash

    def reset(self):
        """Resets all blocks to Missing (e.g. after a hash mismatch)."""
        for block in self.blocks:
            block.status = Block.Missing

    def next_request(self) -> Optional[Block]:
        """Returns the next Missing block, marking it Pending."""
        missing = [block for block in self.blocks if block.status == Block.Missing]
        if missing:
            missing[0].status = Block.Pending
            return missing[0]
        return None

    def block_received(self, offset: int, data: bytes):
        """Marks the block at *offset* as Retrieved and stores its data."""
        matches = [block for block in self.blocks if block.offset == offset]
        block = matches[0] if matches else None
        if block:
            block.status = Block.Retrieved
            block.data = data
        else:
            logging.warning(f"Trying to retrieve a non-existing block {offset}")

    def is_complete(self) -> bool:
        """Returns True when every block has been Retrieved."""
        return all(block.status == Block.Retrieved for block in self.blocks)

    def is_hash_matching(self) -> bool:
        """Verifies the assembled piece against the expected SHA-1 hash."""
        if any(block.data is None for block in self.blocks):
            return False
        return sha1(self.data).digest() == self.piece_hash

    @property
    def data(self) -> bytes:
        """Assembles and returns the full piece data from its sorted blocks."""
        retrieved = sorted(self.blocks, key=lambda block: block.offset)
        return b"".join([block.data for block in retrieved])


async def _safe_drain(writer):
    try:
        await writer.drain()
    except Exception:
        pass


class PieceManager:
    """Tracks all pieces and coordinates disk I/O for single-file torrents.

    Responsibilities:
    - Maintaining missing / ongoing / have piece lists.
    - Selecting the next block to request (rarest-first strategy).
    - Writing completed pieces to disk and reading blocks back for seeding.
    - Broadcasting Have messages to connected peers after a piece completes.
    - Exposing a real bitfield so peers know what we have.
    """

    request_size = REQUEST_SIZE

    def __init__(self, torrent: Torrent):
        self.torrent = torrent

        # peer_id -> bitstring.BitArray representing that peer's available pieces
        self.peers = {}

        self.pending_blocks: List[PendingRequest] = []
        self.ongoing_pieces: List[Piece] = []
        # Pieces fully downloaded and verified
        self.have_pieces: List[Piece] = []
        self.missing_pieces: List[Piece] = self._initialize_pieces()

        self.total_pieces = len(torrent.pieces)
        self.max_pending_time = MAX_PENDING_TIME
        self._bytes_uploaded = 0

        # Active PeerConnection objects — populated by Peer so we can push Have msgs
        self.connections: List[PeerConnection] = []

        os.makedirs(os.path.dirname(torrent.output_file), exist_ok=True)

        binary_flag = getattr(os, "O_BINARY", 0)  # os.O_BINARY only exists on Windows
        self.file_descriptor = os.open(
            torrent.output_file,
            os.O_RDWR | os.O_CREAT | binary_flag
        )

        self._bytes_downloaded = 0

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def _initialize_pieces(self) -> List[Piece]:
        """Build the full list of Piece/Block objects from torrent metadata."""
        pieces = []
        total_pieces = len(self.torrent.pieces)
        standard_blocks = math.ceil(self.torrent.piece_length / self.request_size)

        for index, info_hash in enumerate(self.torrent.pieces):
            if index < (total_pieces - 1):
                blocks = [
                    Block(index, offset * self.request_size)
                    for offset in range(standard_blocks)
                ]
            else:
                # Last piece — may be shorter than piece_length
                last_length = (
                        self.torrent.total_size
                        - self.torrent.piece_length * (total_pieces - 1)
                )
                num_blocks = math.ceil(last_length / self.request_size)
                blocks = []
                for i in range(num_blocks):
                    length = self.request_size
                    if i == num_blocks - 1:
                        length = last_length - self.request_size * (num_blocks - 1)
                    blocks.append(Block(index, i * self.request_size, length))

            pieces.append(Piece(index, blocks, info_hash))
        return pieces

    # ------------------------------------------------------------------
    # Peer registry
    # ------------------------------------------------------------------

    def add_peer(self, peer_id, bitfield):
        """Register a peer and its bitfield."""
        self.peers[peer_id] = bitfield

    def update_peer(self, peer_id, index: int):
        """Update a peer's bitfield after it sends a Have message."""
        if peer_id in self.peers:
            self.peers[peer_id][index] = 1

    def remove_peer(self, peer_id):
        """Remove a disconnected peer."""
        if peer_id in self.peers:
            del self.peers[peer_id]

    # ------------------------------------------------------------------
    # Piece selection (download side)
    # ------------------------------------------------------------------

    def next_request(self, peer_id) -> Optional[Block]:
        """Return the next Block to request from *peer_id*, or None."""
        if peer_id not in self.peers:
            return None

        block = self._expired_requests(peer_id)
        if not block:
            block = self._next_ongoing(peer_id)
            if not block:
                rarest = self._get_rarest_piece(peer_id)
                if rarest:
                    block = rarest.next_request()
        return block

    def block_received(self, peer_id, piece_index: int,
                       block_offset: int, data: bytes):
        """Called when a block arrives; writes piece to disk when complete."""
        logging.debug(
            f"Block {block_offset} of piece {piece_index} from {peer_id}"
        )

        # Remove from pending
        for idx, req in enumerate(self.pending_blocks):
            if req.block.piece == piece_index and req.block.offset == block_offset:
                del self.pending_blocks[idx]
                break

        piece = next(
            (p for p in self.ongoing_pieces if p.index == piece_index), None
        )

        if piece:
            piece.block_received(block_offset, data)
            if piece.is_complete():
                if piece.is_hash_matching():
                    self._write(piece)

                    self._bytes_downloaded += sum(len(b.data) for b in piece.blocks)
                    for b in piece.blocks:
                        b.data = None  # release memory; piece is on disk now

                    self.ongoing_pieces.remove(piece)
                    self.have_pieces.append(piece)
                    # Announce to all connected peers that we now have this piece
                    self._broadcast_have(piece.index)
                    completed = (
                            self.total_pieces
                            - len(self.missing_pieces)
                            - len(self.ongoing_pieces)
                    )
                    logging.info(
                        f"Downloaded {completed} / {self.total_pieces} pieces"
                    )
                else:
                    logging.warning(
                        f"Hash mismatch — discarding piece {piece.index}"
                    )
                    piece.reset()
                    if piece not in self.missing_pieces:
                        self.missing_pieces.append(piece)
                    if piece in self.ongoing_pieces:
                        self.ongoing_pieces.remove(piece)
        else:
            logging.warning("Received block for unknown ongoing piece")

    # ------------------------------------------------------------------
    # Seeding — reading data back from disk
    # ------------------------------------------------------------------

    def read_block(self, piece_index: int, offset: int, length: int) -> Optional[bytes]:
        """Read *length* bytes at *offset* within piece *piece_index* from disk.

        Returns None if the piece is not available or the read fails.
        Used to serve Request messages from remote peers.
        """
        if not self.have_piece(piece_index):
            return None
        try:
            piece_start = piece_index * self.torrent.piece_length
            os.lseek(self.file_descriptor, piece_start + offset, os.SEEK_SET)
            data = os.read(self.file_descriptor, length)
            self._bytes_uploaded += len(data)
            return data
        except OSError as e:
            logging.error(f"Failed to read block from disk: {e}")
            return None

    # ------------------------------------------------------------------
    # Bitfield helpers
    # ------------------------------------------------------------------

    def get_bitfield(self) -> bytearray:
        """Return a bytearray bitfield representing pieces we have.

        Each bit corresponds to one piece index (MSB first), as per BEP 3.
        """
        bitfield = bytearray((self.total_pieces + 7) // 8)
        for piece in self.have_pieces:
            byte_index = piece.index // 8
            bit_index = 7 - (piece.index % 8)
            bitfield[byte_index] |= (1 << bit_index)
        return bitfield

    def _broadcast_have(self, piece_index: int):
        """Send a Have message to every connected peer after we acquire a piece."""
        msg = Have(piece_index).encode()
        for conn in self.connections:
            if conn.writer and not conn.writer.is_closing():
                try:
                    conn.writer.write(msg)
                    # fire-and-forget drain; errors handled in the connection coro
                    asyncio.ensure_future(_safe_drain(conn.writer))
                except Exception as e:
                    logging.warning(f"Failed to send Have to peer: {e}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _expired_requests(self, peer_id) -> Optional[Block]:
        """Re-issue a pending block request that has exceeded MAX_PENDING_TIME."""
        current_time = int(round(time.time() * 1000))
        for req in self.pending_blocks:
            block = req.block
            if (peer_id in self.peers and self.peers[peer_id][block.piece]
                    and req.added + self.max_pending_time < current_time):
                logging.info(
                    f"Re-requesting block {block.offset} of piece {block.piece}"
                )
                req.added = current_time
                return block
        return None

    def _next_ongoing(self, peer_id) -> Optional[Block]:
        """Find the next missing block in an already-started piece the peer has."""
        for piece in self.ongoing_pieces:
            if peer_id in self.peers and self.peers[peer_id][piece.index]:
                block = piece.next_request()
                if block:
                    added = int(round(time.time() * 1000))
                    self.pending_blocks.append(PendingRequest(block, added))
                    return block
        return None

    def _get_rarest_piece(self, peer_id) -> Optional[Piece]:
        """Select the rarest missing piece available from *peer_id* (rarest-first)."""
        piece_count = {}
        for piece in self.missing_pieces:
            if not (peer_id in self.peers and self.peers[peer_id][piece.index]):
                continue
            piece_count[piece.index] = sum(
                1 for peer in self.peers.values() if peer[piece.index]
            )

        if not piece_count:
            logging.debug("No pieces available from this peer")
            return None

        rarest_index = min(piece_count, key=piece_count.get)
        rarest_piece = next(
            (p for p in self.missing_pieces if p.index == rarest_index), None
        )

        if rarest_piece:
            self.missing_pieces.remove(rarest_piece)
            self.ongoing_pieces.append(rarest_piece)
        return rarest_piece

    def _write(self, piece: Piece):
        """Write a verified piece to the correct offset in the output file."""
        # TODO: extend to support multi-file torrents
        piece_start = piece.index * self.torrent.piece_length
        os.lseek(self.file_descriptor, piece_start, os.SEEK_SET)
        os.write(self.file_descriptor, piece.data)

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def have_piece(self, piece_index: int) -> bool:
        """Return True if we have fully downloaded and verified *piece_index*."""
        return any(p.index == piece_index for p in self.have_pieces)

    def get_piece(self, piece_index: int) -> Optional[Piece]:
        """Return the Piece object for *piece_index* if we have it, else None."""
        return next(
            (p for p in self.have_pieces if p.index == piece_index), None
        )

    def close(self):
        """Close the underlying file descriptor."""
        try:
            if self.file_descriptor:
                os.close(self.file_descriptor)
        except OSError:
            pass

    @property
    def complete(self) -> bool:
        """True when every piece has been downloaded and verified."""
        return len(self.have_pieces) == self.total_pieces

    @property
    def bytes_downloaded(self) -> int:
        return self._bytes_downloaded

    @property
    def bytes_uploaded(self) -> int:
        return self._bytes_uploaded
