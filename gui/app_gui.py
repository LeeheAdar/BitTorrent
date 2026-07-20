from __future__ import annotations

import asyncio
import os
import random
import socket
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox
from typing import Callable, Optional

import customtkinter as ctk

try:
    from auth.encrypted_socket import EncryptedSocket
    import auth.key_exchanging as key_exchanging
    from auth.key_exchanging import KeyExchangeMode, perform_rsa, perform_dh
    from auth.message_codes import CommandCodes, ResponseCodes

    _REAL_AUTH = True
except ImportError:
    print("Encountered ImportError, entering DEMO-mode (authentication)")
    _REAL_AUTH = False

try:
    from core.torrent import Torrent
    from core.client import Peer

    _REAL_TORRENT = True
except ImportError:
    print("Encountered ImportError, entering DEMO-mode (torrent)")
    _REAL_TORRENT = False

# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

C = {
    'bg0': '#0f1117',
    'bg1': '#181b23',
    'bg2': '#222635',
    'card': '#1c2030',
    'accent': '#3b82f6',
    'acc_h': '#2563eb',
    'acc_dim': '#1e3a5f',
    'ok': '#22c55e',
    'ok_dim': '#14532d',
    'warn': '#f59e0b',
    'err': '#ef4444',
    'err_dim': '#7f1d1d',
    'admin': '#a855f7',
    'adm_dim': '#3b0764',
    'fg0': '#f1f5f9',
    'fg1': '#94a3b8',
    'fg2': '#475569',
    'border': '#2d3448',
    'dot_on': '#22c55e',
    'dot_off': '#2d3448',
}

APP_TITLE = "PyTorrent"
APP_VER = "1.0"
ADMIN_USER = "admin"
ADMIN_PASS = "password1"
SERVER_IP = "127.0.0.1"
SERVER_PORT = 12345

# Set to False to start with an empty user table, empty admin connection
# list, and an empty torrent list (no sample/mock entries). This only
# affects seed *data* — it does NOT affect _REAL_AUTH/_REAL_TORRENT, which
# control whether the app talks to a real backend or runs its built-in
# simulation when those modules aren't importable.
DEMO_DATA = False

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# ---------------------------------------------------------------------------
# Auth stubs (demo mode when real modules are missing)
# ---------------------------------------------------------------------------

if not _REAL_AUTH:
    class _RC:
        SignInSuccess = b'11111'
        SignUpSuccess = b'11110'
        TakenUsername = b'11101'
        SignInFailed = b'11100'
        GeneralError = b'11011'
        CodeSent = b'11010'
        VerificationSuccess = b'11001'
        VerificationFailed = b'11000'
        ResetPasswordSuccess = b'10111'
        WrongUsername = b'10110'
        AdminLoginSuccess = b'10101'
        ServerStatus = b'10100'

    class _CC:
        SignIn = b'00001'
        SignUp = b'00010'
        SendCode = b'00011'
        VerifyCode = b'00100'
        ResetPassword = b'00101'
        GetServerStatus = b'00111'

    ResponseCodes = _RC()
    CommandCodes = _CC()

if DEMO_DATA:
    _DEMO_USERS: dict = {
        ADMIN_USER: {"password": ADMIN_PASS, "email": "admin@local"},
        "alice": {"password": "alice123", "email": "alice@example.com"},
    }

    _DEMO_CONNECTIONS: list = [
        {"ip": "10.0.0.12", "user": "alice", "status": "downloading", "at": "09:14:22"},
        {"ip": "10.0.0.55", "user": "bob",   "status": "idle",        "at": "09:22:05"},
    ]

    # Torrent list shared across screens. Each entry is a plain dict so all
    # screens can reference the same object and see live mutations.
    MOCK_TORRENTS: list = [
        {"id": 1, "name": "debian-12.5-amd64-netinst.iso", "size": "628 MB",
         "size_bytes": 658505728, "tracker": "http://bttracker.debian.org:6969/announce",
         "info_hash": "a3e4b9f1c2d8e5a7b0c3d6e9f2a5b8c1d4e7f0a3",
         "pieces": 300, "piece_size": "2 MB", "status": "downloading",
         "progress": 0.63, "speed_dl": 1.24, "speed_ul": 0.31,
         "peers": 47, "seeds": 112, "eta": "04:12", "added": "2025-09-10 14:22",
         "ctrl": None},
        {"id": 2, "name": "ubuntu-24.04-desktop-amd64.iso", "size": "5.7 GB",
         "size_bytes": 6121308160, "tracker": "https://torrent.ubuntu.com/announce",
         "info_hash": "c7d2e5f8a1b4c7d0e3f6a9b2c5d8e1f4a7b0c3d6",
         "pieces": 2750, "piece_size": "2 MB", "status": "paused",
         "progress": 0.28, "speed_dl": 0.0, "speed_ul": 0.0,
         "peers": 0, "seeds": 0, "eta": "—", "added": "2025-09-11 09:05",
         "ctrl": None},
        {"id": 3, "name": "archlinux-2025.09.01-x86_64.iso", "size": "1.1 GB",
         "size_bytes": 1181116006, "tracker": "http://tracker.archlinux.org:6969/announce",
         "info_hash": "b5e8f1a4c7d0e3f6a9b2c5d8e1f4a7b0c3d6e9f2",
         "pieces": 530, "piece_size": "2 MB", "status": "complete",
         "progress": 1.0, "speed_dl": 0.0, "speed_ul": 0.08,
         "peers": 3, "seeds": 0, "eta": "—", "added": "2025-09-08 18:44",
         "ctrl": None},
    ]
else:
    _DEMO_USERS: dict = {
        ADMIN_USER: {"password": ADMIN_PASS, "email": "admin@local"},
    }
    _DEMO_CONNECTIONS: list = []
    MOCK_TORRENTS: list = []


# ---------------------------------------------------------------------------
# CtrlAdapter — bridges asyncio Peer to tkinter via threading + callbacks
# ---------------------------------------------------------------------------

class CtrlAdapter:
    """Runs core/client.py Peer on a background thread and pushes state
    updates back to the UI via plain callbacks scheduled with .after().

    When _REAL_TORRENT is False it runs a short demo simulation instead so
    the UI is still exercisable without the core libraries.

    The adapter writes live stats into a *torrent_dict* supplied at start()
    so the UI tick loops can simply read from the same dict they already use.
    """

    def __init__(self,
                 on_status: Callable[[str], None],
                 on_finish: Callable[[], None],
                 on_error: Callable[[str], None]):
        self._on_status = on_status
        self._on_finish = on_finish
        self._on_error = on_error
        self._peer: Optional[Peer] = None
        self._task = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._torrent_dict: Optional[dict] = None
        # Snapshot values for speed calculation
        self._last_bytes_dl = 0
        self._last_bytes_ul = 0
        self._last_sample_time = 0.0

    def start(self, torrent_path: str, torrent_dict: dict):
        """Start the download/seed process for *torrent_path*.

        *torrent_dict* is the shared dict in MOCK_TORRENTS so the adapter
        can write live stats (progress, speed, peers, status) directly into it.
        """
        self._torrent_dict = torrent_dict
        self._running = True
        self._thread = threading.Thread(
            target=self._run, args=(torrent_path,), daemon=True
        )
        self._thread.start()

    def stop(self):
        """Request graceful shutdown."""
        self._running = False
        if self._peer and _REAL_TORRENT:
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(self._peer.stop(), self._loop)
        if self._task:
            self._task.cancel()

    def _run(self, torrent_path: str):
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            if _REAL_TORRENT:
                torrent = Torrent(torrent_path)
                self._peer = Peer(torrent)
                # Populate basic metadata into the shared dict
                if self._torrent_dict is not None:
                    self._torrent_dict.update({
                        "name": os.path.basename(torrent_path),
                        "size": _fmt_size(torrent.total_size),
                        "size_bytes": torrent.total_size,
                        "tracker": torrent.announce or "—",
                        "info_hash": torrent.info_hash.hex(),
                        "pieces": len(torrent.pieces),
                        "piece_size": _fmt_size(torrent.piece_length),
                    })
                # Schedule a periodic stats poller alongside the download coro
                self._task = self._loop.create_task(self._drive())
                self._loop.run_until_complete(
                    asyncio.gather(self._task, self._poll_stats())
                )
            else:
                # Demo simulation — not real progress, just UI exercise
                for msg in ["Connecting to peers…", "Handshaking…", "Downloading…"]:
                    if not self._running:
                        return
                    self._on_status(msg)
                    time.sleep(2)
                self._on_finish()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self._on_error(str(e))

    async def _drive(self):
        """Await the Peer download/seed loop."""
        self._on_status("Connecting to peers…")
        await self._peer.start()
        if self._torrent_dict is not None:
            self._torrent_dict["status"] = "complete"
            self._torrent_dict["progress"] = 1.0
            self._torrent_dict["speed_dl"] = 0.0
            self._torrent_dict["eta"] = "—"
        self._on_status("Seeding…")
        self._on_finish()

    async def _poll_stats(self):
        """Periodically sample PieceManager and write live stats into *torrent_dict*.

        Runs concurrently with _drive() on the same event loop. Updates every
        second so the tkinter tick loop always has fresh values to display.
        """
        while self._running:
            await asyncio.sleep(1)
            if self._peer is None or self._torrent_dict is None:
                continue
            pm = self._peer.piece_manager
            now = time.time()
            dl = pm.bytes_downloaded
            ul = pm.bytes_uploaded
            dt = now - self._last_sample_time if self._last_sample_time else 1.0
            speed_dl = max(0.0, (dl - self._last_bytes_dl) / dt / 1_048_576)
            speed_ul = max(0.0, (ul - self._last_bytes_ul) / dt / 1_048_576)
            self._last_bytes_dl = dl
            self._last_bytes_ul = ul
            self._last_sample_time = now

            progress = dl / pm.torrent.total_size if pm.torrent.total_size else 0.0
            peer_count = len(pm.peers)

            # ETA in hh:mm:ss, or "—" when speed is zero / complete
            if speed_dl > 0 and progress < 1.0:
                remaining = pm.torrent.total_size - dl
                secs = int(remaining / (speed_dl * 1_048_576))
                eta = f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}:{secs % 60:02d}"
            else:
                eta = "—"

            self._torrent_dict.update({
                "progress": min(progress, 1.0),
                "speed_dl": round(speed_dl, 2),
                "speed_ul": round(speed_ul, 2),
                "peers": peer_count,
                "eta": eta,
                "status": (
                    "complete" if pm.complete else
                    "seeding" if pm.complete else
                    "downloading"
                ),
            })


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _fmt_size(n: int) -> str:
    """Format a byte count as a human-readable string (KB / MB / GB)."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _card(parent, title: str | None = None) -> ctk.CTkFrame:
    """Create a standard card frame with optional section title."""
    f = ctk.CTkFrame(parent, fg_color=C['card'], corner_radius=10,
                     border_width=1, border_color=C['border'])
    if title:
        ctk.CTkLabel(f, text=title,
                     font=ctk.CTkFont(size=13, weight='bold'),
                     text_color=C['fg1']).pack(anchor='w', padx=16, pady=(12, 4))
    return f


def _div(parent, pady=(6, 6)):
    """Horizontal divider line."""
    ctk.CTkFrame(parent, height=1, fg_color=C['border']).pack(
        fill='x', padx=16, pady=pady)


def _badge(parent, status: str) -> ctk.CTkFrame:
    """Return a coloured status badge frame."""
    cfg = {
        'downloading': (C['accent'],  C['acc_dim'],  '↓ Downloading'),
        'paused':      (C['warn'],    '#3d2e00',     '⏸ Paused'),
        'complete':    (C['ok'],      C['ok_dim'],   '✓ Complete'),
        'seeding':     (C['ok'],      C['ok_dim'],   '↑ Seeding'),
        'connecting':  (C['fg2'],     C['bg2'],      '⟳ Connecting'),
        'active':      (C['ok'],      C['ok_dim'],   '● Active'),
        'idle':        (C['fg2'],     C['bg2'],      '◌ Idle'),
        'auth':        (C['admin'],   C['adm_dim'],  '🔑 Auth'),
    }
    col, bg, lbl = cfg.get(status, (C['fg2'], C['bg2'], status))
    f = ctk.CTkFrame(parent, fg_color=bg, corner_radius=6)
    ctk.CTkLabel(f, text=lbl, font=ctk.CTkFont(size=11, weight='bold'),
                 text_color=col).pack(padx=8, pady=3)
    return f


def _entry(parent, placeholder='', show='', height=36) -> ctk.CTkEntry:
    """Standard styled entry widget."""
    return ctk.CTkEntry(parent,
                        fg_color=C['bg2'], border_color=C['border'],
                        text_color=C['fg0'], placeholder_text=placeholder,
                        show=show, height=height)


def _btn(parent, text, cmd, /, *,
         color=None, hover=None, tc=None,
         w=0, h=34, bold=False, **kw) -> ctk.CTkButton:
    """Standard styled button."""
    return ctk.CTkButton(
        parent, text=text, command=cmd,
        fg_color=color or C['accent'],
        hover_color=hover or C['acc_h'],
        text_color=tc or C['fg0'],
        width=w, height=h,
        font=ctk.CTkFont(size=12, weight='bold' if bold else 'normal'),
        **kw)


# ---------------------------------------------------------------------------
# Base screen
# ---------------------------------------------------------------------------

class Screen(ctk.CTkFrame):
    def __init__(self, parent, app: 'App', **kw):
        super().__init__(parent, fg_color=C['bg0'], corner_radius=0, **kw)
        self.app = app

    def on_show(self, **kw):
        pass


# ---------------------------------------------------------------------------
# Connection screen
# ---------------------------------------------------------------------------

class ConnectScreen(Screen):
    def _lazy_build(self):
        if hasattr(self, '_built'):
            return
        self._built = True

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        card = ctk.CTkFrame(self, fg_color=C['card'], corner_radius=16,
                            border_width=1, border_color=C['border'])
        card.grid(row=0, column=0, padx=20, pady=60, sticky='n')
        card.configure(width=420, height=460)
        card.grid_propagate(False)

        logo = ctk.CTkFrame(card, fg_color=C['acc_dim'], corner_radius=12)
        logo.pack(pady=(32, 0), padx=36, fill='x')
        ctk.CTkLabel(logo, text="⬡  PyTorrent",
                     font=ctk.CTkFont(size=26, weight='bold'),
                     text_color=C['accent']).pack(pady=16)

        ctk.CTkLabel(card, text="Server connection",
                     font=ctk.CTkFont(size=13), text_color=C['fg1']).pack(pady=(12, 4))
        _div(card, pady=(0, 10))

        body = ctk.CTkFrame(card, fg_color='transparent')
        body.pack(fill='x', padx=32)

        ipf = ctk.CTkFrame(body, fg_color='transparent')
        ipf.pack(fill='x', pady=(0, 10))
        ctk.CTkLabel(ipf, text="Server IP", font=ctk.CTkFont(size=11),
                     text_color=C['fg1'], anchor='w').pack(fill='x', pady=(0, 3))
        self._ip = _entry(ipf, placeholder=SERVER_IP)
        self._ip.pack(fill='x')
        self._ip.insert(0, SERVER_IP)

        pf = ctk.CTkFrame(body, fg_color='transparent')
        pf.pack(fill='x', pady=(0, 10))
        ctk.CTkLabel(pf, text="Port", font=ctk.CTkFont(size=11),
                     text_color=C['fg1'], anchor='w').pack(fill='x', pady=(0, 3))
        self._port = _entry(pf, placeholder=str(SERVER_PORT))
        self._port.pack(fill='x')
        self._port.insert(0, str(SERVER_PORT))

        ctk.CTkLabel(body, text="Key exchange method",
                     font=ctk.CTkFont(size=11), text_color=C['fg1'],
                     anchor='w').pack(fill='x', pady=(0, 3))
        self._km = ctk.StringVar(value="RSA")
        kmr = ctk.CTkFrame(body, fg_color='transparent')
        kmr.pack(fill='x', pady=(0, 14))
        for m in ("RSA", "DH"):
            ctk.CTkRadioButton(kmr, text=m, variable=self._km, value=m,
                               fg_color=C['accent'], hover_color=C['acc_h'],
                               text_color=C['fg0']).pack(side='left', padx=(0, 20))

        self._err = ctk.CTkLabel(body, text="",
                                 font=ctk.CTkFont(size=11), text_color=C['err'])
        self._err.pack(pady=(0, 4))

        self._cbtn = _btn(body, "Connect →", self._connect,
                          color=C['accent'], h=38, bold=True)
        self._cbtn.pack(fill='x')

        _div(card, pady=(14, 6))
        ctk.CTkLabel(card, text="No server? App runs in demo mode.",
                     font=ctk.CTkFont(size=10), text_color=C['fg2']).pack()

    def on_show(self, **kw):
        self._lazy_build()
        if hasattr(self, '_err'):
            self._err.configure(text="")

    def _connect(self):
        self._cbtn.configure(state='disabled', text="Connecting…")
        self._err.configure(text="")
        ip = self._ip.get().strip() or SERVER_IP
        port = int(self._port.get().strip() or SERVER_PORT)

        def _do():
            self.app.auth.key_method = self._km.get()
            ok, msg = self.app.auth.connect(ip, port)
            self.after(0, lambda: self._done(ok, msg))

        threading.Thread(target=_do, daemon=True).start()

    def _done(self, ok, msg):
        self._cbtn.configure(state='normal', text="Connect →")
        if ok:
            self.app.show('login')
        else:
            self._err.configure(text=f"⚠  {msg}")


# ---------------------------------------------------------------------------
# Login screen
# ---------------------------------------------------------------------------

class LoginScreen(Screen):
    def _lazy_build(self):
        if hasattr(self, '_built'):
            return
        self._built = True

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        card = ctk.CTkFrame(self, fg_color=C['card'], corner_radius=16,
                            border_width=1, border_color=C['border'])
        card.grid(row=0, column=0, padx=20, pady=50, sticky='n')
        card.configure(width=420, height=430)
        card.grid_propagate(False)

        logo = ctk.CTkFrame(card, fg_color=C['acc_dim'], corner_radius=12)
        logo.pack(pady=(32, 0), padx=36, fill='x')
        ctk.CTkLabel(logo, text="⬡  PyTorrent",
                     font=ctk.CTkFont(size=26, weight='bold'),
                     text_color=C['accent']).pack(pady=16)

        ctk.CTkLabel(card, text="Sign in to your account",
                     font=ctk.CTkFont(size=13), text_color=C['fg1']).pack(pady=(12, 4))
        _div(card, pady=(0, 10))

        body = ctk.CTkFrame(card, fg_color='transparent')
        body.pack(fill='x', padx=32)

        ctk.CTkLabel(body, text="Username", font=ctk.CTkFont(size=11),
                     text_color=C['fg1'], anchor='w').pack(fill='x', pady=(0, 3))
        self._u = _entry(body, placeholder="Enter username")
        self._u.pack(fill='x', pady=(0, 10))
        self._u.bind('<Return>', lambda e: self._p.focus())

        ctk.CTkLabel(body, text="Password", font=ctk.CTkFont(size=11),
                     text_color=C['fg1'], anchor='w').pack(fill='x', pady=(0, 3))
        self._p = _entry(body, placeholder="Enter password", show='●')
        self._p.pack(fill='x', pady=(0, 4))
        self._p.bind('<Return>', lambda e: self._login())

        self._err = ctk.CTkLabel(body, text="",
                                 font=ctk.CTkFont(size=11), text_color=C['err'])
        self._err.pack(pady=(2, 2))

        self._lbtn = _btn(body, "Sign In", self._login, h=38, bold=True)
        self._lbtn.pack(fill='x')

        _div(card, pady=(12, 6))

        lr = ctk.CTkFrame(card, fg_color='transparent')
        lr.pack(fill='x', padx=32)
        _btn(lr, "Create account →",
             lambda: self.app.show('register'),
             color='transparent', hover=C['bg2'], tc=C['accent'], h=28).pack(side='left')
        _btn(lr, "Forgot password?",
             lambda: self.app.show('forgot'),
             color='transparent', hover=C['bg2'], tc=C['fg1'], h=28).pack(side='right')

    def on_show(self, **kw):
        self._lazy_build()
        self._err.configure(text="")
        self._p.delete(0, 'end')

    def _login(self):
        u, p = self._u.get().strip(), self._p.get()
        if not u or not p:
            self._err.configure(text="Please fill in all fields.")
            return
        self._lbtn.configure(state='disabled', text="Signing in…")
        self._err.configure(text="")

        def _do():
            ok, msg = self.app.auth.sign_in(u, p)
            self.after(0, lambda: self._done(ok, msg, u))

        threading.Thread(target=_do, daemon=True).start()

    def _done(self, ok, msg, username):
        self._lbtn.configure(state='normal', text="Sign In")
        if ok:
            self.app.current_user = username
            self.app.show('admin' if (msg == "admin" or username == ADMIN_USER) else 'main')
        else:
            self._err.configure(text=f"⚠  {msg}")


# ---------------------------------------------------------------------------
# Register screen
# ---------------------------------------------------------------------------

class RegisterScreen(Screen):
    def _lazy_build(self):
        if hasattr(self, '_built'):
            return
        self._built = True

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        card = ctk.CTkFrame(self, fg_color=C['card'], corner_radius=16,
                            border_width=1, border_color=C['border'])
        card.grid(row=0, column=0, padx=20, pady=40, sticky='n')
        card.configure(width=420, height=510)
        card.grid_propagate(False)

        logo = ctk.CTkFrame(card, fg_color=C['acc_dim'], corner_radius=12)
        logo.pack(pady=(28, 0), padx=36, fill='x')
        ctk.CTkLabel(logo, text="⬡  PyTorrent",
                     font=ctk.CTkFont(size=26, weight='bold'),
                     text_color=C['accent']).pack(pady=14)

        ctk.CTkLabel(card, text="Create a new account",
                     font=ctk.CTkFont(size=13), text_color=C['fg1']).pack(pady=(10, 4))
        _div(card, pady=(0, 8))

        body = ctk.CTkFrame(card, fg_color='transparent')
        body.pack(fill='x', padx=32)

        def lf(t):
            ctk.CTkLabel(body, text=t, font=ctk.CTkFont(size=11),
                         text_color=C['fg1'], anchor='w').pack(fill='x', pady=(0, 3))

        lf("Username")
        self._u = _entry(body, placeholder="Choose a username")
        self._u.pack(fill='x', pady=(0, 8))

        lf("Email")
        self._em = _entry(body, placeholder="your@email.com")
        self._em.pack(fill='x', pady=(0, 8))

        lf("Password")
        self._p = _entry(body, placeholder="At least 6 characters", show='●')
        self._p.pack(fill='x', pady=(0, 8))

        lf("Confirm Password")
        self._p2 = _entry(body, placeholder="Repeat password", show='●')
        self._p2.pack(fill='x', pady=(0, 4))
        self._p2.bind('<Return>', lambda e: self._do_register())

        self._err = ctk.CTkLabel(body, text="",
                                 font=ctk.CTkFont(size=11), text_color=C['err'])
        self._err.pack(pady=(2, 4))

        self._rbtn = _btn(body, "Create Account", self._do_register,
                          color=C['ok'], hover='#16a34a', tc='#0a0a0a', h=38, bold=True)
        self._rbtn.pack(fill='x')

        _div(card, pady=(10, 6))
        _btn(card, "← Back to sign in",
             lambda: self.app.show('login'),
             color='transparent', hover=C['bg2'], tc=C['accent'], h=28).pack(pady=(0, 12))

    def on_show(self, **kw):
        self._lazy_build()
        if hasattr(self, '_err'):
            self._err.configure(text="")

    def _do_register(self):
        u, em = self._u.get().strip(), self._em.get().strip()
        p, p2 = self._p.get(), self._p2.get()
        if not all([u, em, p, p2]):
            self._err.configure(text="Please fill in all fields.")
            return
        if len(u) < 3:
            self._err.configure(text="Username must be at least 3 characters.")
            return
        if '@' not in em:
            self._err.configure(text="Invalid email address.")
            return
        if p != p2:
            self._err.configure(text="Passwords do not match.")
            return
        if len(p) < 6:
            self._err.configure(text="Password must be at least 6 characters.")
            return

        self._rbtn.configure(state='disabled', text="Creating…")
        self._err.configure(text="")

        def _do():
            ok, msg = self.app.auth.sign_up(u, em, p)
            self.after(0, lambda: self._done(ok, msg, u))

        threading.Thread(target=_do, daemon=True).start()

    def _done(self, ok, msg, username):
        self._rbtn.configure(state='normal', text="Create Account")
        if ok:
            self.app.current_user = username
            self.app.show('main')
        else:
            self._err.configure(text=f"⚠  {msg}")


# ---------------------------------------------------------------------------
# Forgot-password screen (3-step flow)
# ---------------------------------------------------------------------------

class ForgotScreen(Screen):
    def _lazy_build(self):
        if hasattr(self, '_built'):
            return
        self._built = True
        self._username = ""
        self._step = 1

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._card = ctk.CTkFrame(self, fg_color=C['card'], corner_radius=16,
                                  border_width=1, border_color=C['border'])
        self._card.grid(row=0, column=0, padx=20, pady=60, sticky='n')
        self._card.configure(width=420, height=360)
        self._card.grid_propagate(False)

        hdr = ctk.CTkFrame(self._card, fg_color=C['acc_dim'], corner_radius=12)
        hdr.pack(pady=(28, 0), padx=36, fill='x')
        ctk.CTkLabel(hdr, text="🔑  Reset Password",
                     font=ctk.CTkFont(size=20, weight='bold'),
                     text_color=C['accent']).pack(pady=14)

        self._step_lbl = ctk.CTkLabel(self._card, text="",
                                      font=ctk.CTkFont(size=12), text_color=C['fg1'])
        self._step_lbl.pack(pady=(10, 0))
        _div(self._card, pady=(6, 8))

        self._body = ctk.CTkFrame(self._card, fg_color='transparent')
        self._body.pack(fill='x', padx=32)

        self._err = ctk.CTkLabel(self._card, text="",
                                 font=ctk.CTkFont(size=11), text_color=C['err'])
        self._err.pack()

        self._abtn = _btn(self._card, "", self._action, h=38, bold=True)
        self._abtn.pack(fill='x', padx=32, pady=(4, 8))

        _btn(self._card, "← Back to sign in",
             lambda: self.app.show('login'),
             color='transparent', hover=C['bg2'], tc=C['accent'], h=28).pack()

    def on_show(self, **kw):
        self._lazy_build()
        self._username = ""
        self._show_step(1)

    def _clear(self):
        for w in self._body.winfo_children():
            w.destroy()

    def _show_step(self, step):
        self._clear()
        self._step = step
        self._err.configure(text="")
        if step == 1:
            self._step_lbl.configure(text="Step 1 of 3 – Enter your username")
            ctk.CTkLabel(self._body, text="Username", font=ctk.CTkFont(size=11),
                         text_color=C['fg1'], anchor='w').pack(fill='x', pady=(0, 3))
            self._f1 = _entry(self._body, placeholder="Your username")
            self._f1.pack(fill='x')
            self._f1.bind('<Return>', lambda e: self._action())
            self._abtn.configure(text="Send Code")
        elif step == 2:
            self._step_lbl.configure(text="Step 2 of 3 – Verification code")
            ctk.CTkLabel(self._body, text="Code sent to your email",
                         font=ctk.CTkFont(size=11), text_color=C['fg1'],
                         anchor='w').pack(fill='x', pady=(0, 3))
            self._f1 = _entry(self._body, placeholder="10-character code")
            self._f1.pack(fill='x')
            self._f1.bind('<Return>', lambda e: self._action())
            self._abtn.configure(text="Verify Code")
        elif step == 3:
            self._step_lbl.configure(text="Step 3 of 3 – New password")
            ctk.CTkLabel(self._body, text="New password", font=ctk.CTkFont(size=11),
                         text_color=C['fg1'], anchor='w').pack(fill='x', pady=(0, 3))
            self._f1 = _entry(self._body, placeholder="New password", show='●')
            self._f1.pack(fill='x', pady=(0, 8))
            ctk.CTkLabel(self._body, text="Confirm password", font=ctk.CTkFont(size=11),
                         text_color=C['fg1'], anchor='w').pack(fill='x', pady=(0, 3))
            self._f2 = _entry(self._body, placeholder="Repeat password", show='●')
            self._f2.pack(fill='x')
            self._abtn.configure(text="Reset Password")

    def _action(self):
        self._abtn.configure(state='disabled')
        self._err.configure(text="")
        if self._step == 1:
            u = self._f1.get().strip()
            if not u:
                self._err.configure(text="Please enter your username.")
                self._abtn.configure(state='normal')
                return
            self._username = u

            def _do():
                ok, msg = self.app.auth.send_reset_code(u)
                self.after(0, lambda: self._handle(ok, msg, next_step=2))

            threading.Thread(target=_do, daemon=True).start()
        elif self._step == 2:
            code = self._f1.get().strip()
            if not code:
                self._err.configure(text="Please enter the code.")
                self._abtn.configure(state='normal')
                return

            def _do():
                ok, msg = self.app.auth.verify_code(self._username, code)
                self.after(0, lambda: self._handle(ok, msg, next_step=3))

            threading.Thread(target=_do, daemon=True).start()
        elif self._step == 3:
            p1, p2 = self._f1.get(), self._f2.get()
            if p1 != p2:
                self._err.configure(text="Passwords do not match.")
                self._abtn.configure(state='normal')
                return
            if len(p1) < 6:
                self._err.configure(text="Password must be at least 6 characters.")
                self._abtn.configure(state='normal')
                return

            def _do():
                ok, msg = self.app.auth.reset_password(self._username, p1)
                self.after(0, lambda: self._handle(ok, msg, done=True))

            threading.Thread(target=_do, daemon=True).start()

    def _handle(self, ok, msg, next_step=None, done=False):
        self._abtn.configure(state='normal')
        if ok:
            if done:
                messagebox.showinfo("Done", "Password reset! Please sign in.")
                self.app.show('login')
            else:
                self._show_step(next_step)
        else:
            self._err.configure(text=f"⚠  {msg}")


# ---------------------------------------------------------------------------
# Main / torrent list screen
# ---------------------------------------------------------------------------

class MainScreen(Screen):
    """Torrent list screen.

    Each row's widgets (progress bar, labels) are stored in self._rows keyed
    by torrent id. The _tick() loop reads directly from MOCK_TORRENTS dicts
    so it shows live data whether it came from the real engine or demo logic.
    Note: demo mock torrents without a 'ctrl' key never mutate on their own —
    they are static display unless you interact with the row.
    """

    def __init__(self, parent, app):
        super().__init__(parent, app)
        self._rows: dict[int, dict] = {}
        self._built = False

    def _lazy_build(self):
        if self._built:
            return
        self._built = True

        top = ctk.CTkFrame(self, fg_color=C['bg1'], corner_radius=0, height=52)
        top.pack(fill='x', side='top')
        top.pack_propagate(False)
        ctk.CTkLabel(top, text="⬡  PyTorrent",
                     font=ctk.CTkFont(size=17, weight='bold'),
                     text_color=C['accent']).pack(side='left', padx=20)
        r = ctk.CTkFrame(top, fg_color='transparent')
        r.pack(side='right', padx=16)
        self._ulbl = ctk.CTkLabel(r, text="",
                                  font=ctk.CTkFont(size=12), text_color=C['fg1'])
        self._ulbl.pack(side='left', padx=(0, 12))
        _btn(r, "Sign out", self._logout,
             color=C['bg2'], hover=C['err_dim'], tc=C['fg1'], w=80, h=28).pack(side='left')

        act = ctk.CTkFrame(self, fg_color=C['card'], corner_radius=0, height=46)
        act.pack(fill='x')
        act.pack_propagate(False)
        _btn(act, "＋  Add Torrent", self._add_torrent, w=140, h=32, bold=True
             ).pack(side='left', padx=14, pady=7)
        _btn(act, "▶  Resume All", lambda: None,
             color=C['bg2'], hover=C['border'], w=110, h=32).pack(side='left', padx=(0, 5), pady=7)
        _btn(act, "⏸  Pause All", lambda: None,
             color=C['bg2'], hover=C['border'], w=100, h=32).pack(side='left', padx=(0, 5), pady=7)
        self._dl = ctk.CTkLabel(act, text="↓ 0.00 MB/s",
                                font=ctk.CTkFont(size=12, weight='bold'),
                                text_color=C['accent'])
        self._dl.pack(side='right', padx=14)

        hdr = ctk.CTkFrame(self, fg_color=C['bg1'], corner_radius=0, height=28)
        hdr.pack(fill='x')
        hdr.pack_propagate(False)
        for txt, exp, w in [("Name", True, 0), ("Size", False, 80),
                            ("Progress", False, 100), ("Speed ↓", False, 90),
                            ("Peers", False, 60), ("Status", False, 120), ("ETA", False, 60)]:
            ctk.CTkLabel(hdr, text=txt,
                         font=ctk.CTkFont(size=10, weight='bold'),
                         text_color=C['fg2'],
                         anchor='w' if exp else 'center',
                         width=w if not exp else 0).pack(
                side='left', fill='x' if exp else None,
                expand=exp, padx=(14 if txt == "Name" else 4, 4))

        self._list = ctk.CTkScrollableFrame(self, fg_color=C['bg0'])
        self._list.pack(fill='both', expand=True)

        sb = ctk.CTkFrame(self, fg_color=C['bg1'], corner_radius=0, height=24)
        sb.pack(fill='x', side='bottom')
        sb.pack_propagate(False)
        self._sb = ctk.CTkLabel(sb, text="Ready.",
                                font=ctk.CTkFont(size=10), text_color=C['fg2'])
        self._sb.pack(side='left', padx=12)

    def on_show(self, **kw):
        self._lazy_build()
        self._ulbl.configure(text=f"Signed in as  {self.app.current_user or ''}")
        self._populate()
        self._tick()

    def _populate(self):
        for w in self._list.winfo_children():
            w.destroy()
        self._rows = {}
        for t in MOCK_TORRENTS:
            self._add_row(t)

    def _add_row(self, t: dict):
        row = ctk.CTkFrame(self._list, fg_color=C['card'], corner_radius=8,
                           border_width=1, border_color=C['border'], height=56)
        row.pack(fill='x', padx=12, pady=4)
        row.pack_propagate(False)

        nf = ctk.CTkFrame(row, fg_color='transparent')
        nf.pack(side='left', fill='x', expand=True, padx=(12, 4))
        ctk.CTkLabel(nf, text=t['name'],
                     font=ctk.CTkFont(size=12, weight='bold'),
                     text_color=C['fg0'], anchor='w').pack(fill='x', pady=(8, 1))
        pc = (C['accent'] if t['status'] == 'downloading' else
              C['ok'] if t['status'] in ('complete', 'seeding') else C['warn'])
        pb = ctk.CTkProgressBar(nf, height=3, progress_color=pc, fg_color=C['bg2'])
        pb.pack(fill='x', pady=(0, 7))
        pb.set(t['progress'])

        ctk.CTkLabel(row, text=t['size'], width=80, anchor='center',
                     font=ctk.CTkFont(size=11), text_color=C['fg1']).pack(side='left', padx=4)
        pct = ctk.CTkLabel(row, text=f"{int(t['progress'] * 100)}%",
                           width=100, anchor='center',
                           font=ctk.CTkFont(size=12, weight='bold'),
                           text_color=C['fg0'])
        pct.pack(side='left', padx=4)
        spd = ctk.CTkLabel(row,
                           text=f"{t['speed_dl']:.2f} MB/s" if t['speed_dl'] else "—",
                           width=90, anchor='center',
                           font=ctk.CTkFont(size=11),
                           text_color=C['accent'] if t['speed_dl'] else C['fg2'])
        spd.pack(side='left', padx=4)
        prl = ctk.CTkLabel(row, text=str(t['peers']), width=60, anchor='center',
                           font=ctk.CTkFont(size=11), text_color=C['fg1'])
        prl.pack(side='left', padx=4)

        bf = ctk.CTkFrame(row, fg_color='transparent', width=120)
        bf.pack(side='left', padx=6)
        bf.pack_propagate(False)
        _badge(bf, t['status']).pack(expand=True)

        ctk.CTkLabel(row, text=t['eta'], width=60, anchor='center',
                     font=ctk.CTkFont(size=11), text_color=C['fg1']).pack(side='left', padx=6)
        _btn(row, "›", lambda tid=t['id']: self.app.show_download(tid),
             color=C['bg2'], hover=C['acc_dim'], tc=C['fg0'],
             w=30, h=30).pack(side='right', padx=10)

        self._rows[t['id']] = {'pb': pb, 'pct': pct, 'spd': spd, 'prl': prl}

    def _tick(self):
        """Refresh row widgets from the live torrent dicts every second."""
        if not self.winfo_exists():
            return
        total = 0.0
        for t in MOCK_TORRENTS:
            ws = self._rows.get(t['id'])
            if ws:
                try:
                    ws['pb'].set(t['progress'])
                    ws['pct'].configure(text=f"{int(t['progress'] * 100)}%")
                    ws['spd'].configure(
                        text=f"{t['speed_dl']:.2f} MB/s" if t['speed_dl'] else "—",
                        text_color=C['accent'] if t['speed_dl'] else C['fg2'])
                    ws['prl'].configure(text=str(t['peers']))
                    total += t.get('speed_dl', 0.0)
                except Exception:
                    pass
        try:
            self._dl.configure(text=f"↓ {total:.2f} MB/s")
            active = sum(1 for t in MOCK_TORRENTS if t['status'] == 'downloading')
            self._sb.configure(text=f"Active: {active}")
        except Exception:
            pass
        self.after(1000, self._tick)

    def _logout(self):
        self.app.auth.disconnect()
        self.app.current_user = None
        self.app.show('connect')

    def _add_torrent(self):
        """Open a .torrent file and start a real download via CtrlAdapter."""
        path = filedialog.askopenfilename(
            title="Open .torrent file",
            filetypes=[("Torrent files", "*.torrent"), ("All files", "*.*")])
        if not path:
            return

        name = os.path.basename(path)
        new_id = max((t['id'] for t in MOCK_TORRENTS), default=0) + 1
        new = {
            "id": new_id,
            "name": name,
            "size": "—",
            "size_bytes": 0,
            "tracker": "—",
            "info_hash": "—",
            "pieces": 0,
            "piece_size": "—",
            "status": "connecting",
            "progress": 0.0,
            "speed_dl": 0.0,
            "speed_ul": 0.0,
            "peers": 0,
            "seeds": 0,
            "eta": "—",
            "added": time.strftime('%Y-%m-%d %H:%M'),
            "ctrl": None,
        }
        MOCK_TORRENTS.append(new)
        self._add_row(new)
        self._sb.configure(text=f"Added: {name}")

        ctrl = CtrlAdapter(
            on_status=lambda m: self._sb.configure(text=m),
            on_finish=lambda: None,
            on_error=lambda e: self._sb.configure(text=f"Error: {e}"),
        )
        new["ctrl"] = ctrl
        ctrl.start(path, new)

        # After a short delay, flip status to 'downloading' once the engine starts
        def _activate():
            if new['status'] == 'connecting':
                new['status'] = 'downloading'

        self.after(2000, _activate)


# ---------------------------------------------------------------------------
# Download detail screen
# ---------------------------------------------------------------------------

class DownloadScreen(Screen):
    """Per-torrent download view with piece map, peer list and controls.

    Reads exclusively from the shared torrent dict so real engine stats appear
    here automatically without any extra wiring.
    """

    def __init__(self, parent, app):
        super().__init__(parent, app)
        self.torrent: Optional[dict] = None
        self._built = False

    def _lazy_build(self):
        if self._built:
            return
        self._built = True

        top = ctk.CTkFrame(self, fg_color=C['bg1'], corner_radius=0, height=46)
        top.pack(fill='x', side='top')
        top.pack_propagate(False)
        _btn(top, "← Downloads", lambda: self.app.show('main'),
             color='transparent', hover=C['bg2'], tc=C['accent'],
             w=120, h=32).pack(side='left', padx=12, pady=7)
        self._title = ctk.CTkLabel(top, text="",
                                   font=ctk.CTkFont(size=13, weight='bold'),
                                   text_color=C['fg0'])
        self._title.pack(side='left', padx=6)

        cont = ctk.CTkFrame(self, fg_color='transparent')
        cont.pack(fill='both', expand=True, padx=14, pady=10)
        cont.grid_columnconfigure(0, weight=3)
        cont.grid_columnconfigure(1, weight=2)
        cont.grid_rowconfigure(0, weight=1)

        lf = ctk.CTkFrame(cont, fg_color='transparent')
        lf.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
        rf = ctk.CTkFrame(cont, fg_color='transparent')
        rf.grid(row=0, column=1, sticky='nsew')

        self._build_left(lf)
        self._build_right(rf)

    def _build_left(self, p):
        pc = _card(p, "Download Progress")
        pc.pack(fill='x', pady=(0, 8))

        pr = ctk.CTkFrame(pc, fg_color='transparent')
        pr.pack(fill='x', padx=16, pady=(6, 4))
        self._pct = ctk.CTkLabel(pr, text="0%",
                                 font=ctk.CTkFont(size=36, weight='bold'),
                                 text_color=C['accent'])
        self._pct.pack(side='left')
        self._bframe = ctk.CTkFrame(pr, fg_color='transparent')
        self._bframe.pack(side='left', padx=14)

        self._pbar = ctk.CTkProgressBar(pc, height=10,
                                        progress_color=C['accent'], fg_color=C['bg2'])
        self._pbar.pack(fill='x', padx=16, pady=(0, 8))
        self._pbar.set(0)

        stats = ctk.CTkFrame(pc, fg_color=C['bg2'], corner_radius=8)
        stats.pack(fill='x', padx=16, pady=(0, 14))
        self._sl: dict[str, ctk.CTkLabel] = {}
        for lbl, key in [("DOWNLOAD", 'dl'), ("UPLOAD", 'ul'),
                         ("ETA", 'eta'), ("PEERS", 'peers')]:
            f = ctk.CTkFrame(stats, fg_color='transparent')
            f.pack(side='left', expand=True, fill='x', padx=10, pady=8)
            ctk.CTkLabel(f, text=lbl, font=ctk.CTkFont(size=9),
                         text_color=C['fg2']).pack()
            lb = ctk.CTkLabel(f, text="—",
                              font=ctk.CTkFont(size=13, weight='bold'),
                              text_color=C['fg0'])
            lb.pack()
            self._sl[key] = lb

        mc = _card(p, "Piece Map")
        mc.pack(fill='x', pady=(0, 8))
        self._pmap = tk.Canvas(mc, bg=C['card'], height=72, highlightthickness=0)
        self._pmap.pack(fill='x', padx=16, pady=(4, 14))

        cc = _card(p, "Controls")
        cc.pack(fill='x')
        br = ctk.CTkFrame(cc, fg_color='transparent')
        br.pack(fill='x', padx=16, pady=(4, 14))
        self._pbtn = _btn(br, "⏸  Pause", self._toggle_pause,
                          color=C['warn'], hover='#b45309', tc='#0a0a0a', w=110)
        self._pbtn.pack(side='left', padx=(0, 8))
        _btn(br, "✕  Cancel", self._cancel,
             color=C['err_dim'], hover=C['err'], w=100).pack(side='left', padx=(0, 8))
        _btn(br, "ℹ  Details",
             lambda: self.torrent and self.app.show_details(self.torrent['id']),
             color=C['bg2'], hover=C['acc_dim'], w=100).pack(side='left')

    def _build_right(self, p):
        pc = _card(p, "Connected Peers")
        pc.pack(fill='both', expand=True)

        hdr = ctk.CTkFrame(pc, fg_color='transparent')
        hdr.pack(fill='x', padx=16, pady=(0, 6))
        self._pcnt = ctk.CTkLabel(hdr, text="0",
                                  font=ctk.CTkFont(size=13, weight='bold'),
                                  text_color=C['accent'])
        self._pcnt.pack(side='right')

        self._dotf = ctk.CTkFrame(pc, fg_color='transparent')
        self._dotf.pack(fill='x', padx=16, pady=(0, 6))
        self._dots: list[ctk.CTkFrame] = []
        for _ in range(50):
            d = ctk.CTkFrame(self._dotf, width=8, height=8,
                             corner_radius=4, fg_color=C['dot_off'])
            d.pack(side='left', padx=1)
            self._dots.append(d)

        _div(pc, pady=(0, 6))

        ch = ctk.CTkFrame(pc, fg_color='transparent')
        ch.pack(fill='x', padx=16, pady=(0, 3))
        for t, w in [("IP Address", 120), ("↓ Speed", 70), ("Progress", 60)]:
            ctk.CTkLabel(ch, text=t, width=w, anchor='w',
                         font=ctk.CTkFont(size=10, weight='bold'),
                         text_color=C['fg2']).pack(side='left', padx=2)

        self._pscroll = ctk.CTkScrollableFrame(pc, fg_color=C['bg2'], corner_radius=8)
        self._pscroll.pack(fill='both', expand=True, padx=12, pady=(0, 12))

    def on_show(self, torrent_id=None, **kw):
        self._lazy_build()
        if torrent_id is None:
            return
        self.torrent = next((t for t in MOCK_TORRENTS if t['id'] == torrent_id), None)
        if not self.torrent:
            return
        self._title.configure(text=self.torrent['name'][:55])
        self._refresh_peers()
        self._draw_map()
        self._tick()

    def _tick(self):
        if not self.winfo_exists() or not self.torrent:
            return
        t = self.torrent
        try:
            self._pct.configure(text=f"{int(t['progress'] * 100)}%")
            self._pbar.set(t['progress'])
            self._sl['dl'].configure(
                text=f"{t['speed_dl']:.2f} MB/s" if t['speed_dl'] else "—",
                text_color=C['accent'] if t['speed_dl'] else C['fg2'])
            self._sl['ul'].configure(
                text=f"{t.get('speed_ul', 0):.2f} MB/s" if t.get('speed_ul') else "—")
            self._sl['eta'].configure(text=t['eta'])
            self._sl['peers'].configure(text=str(t['peers']))
            self._pcnt.configure(text=str(t['peers']))
            active = min(t['peers'], 50)
            for i, d in enumerate(self._dots):
                d.configure(fg_color=C['dot_on'] if i < active else C['dot_off'])
            for w in self._bframe.winfo_children():
                w.destroy()
            _badge(self._bframe, t['status']).pack()
            is_paused = t['status'] == 'paused'
            self._pbtn.configure(
                text="▶  Resume" if is_paused else "⏸  Pause",
                fg_color=C['ok'] if is_paused else C['warn'],
                hover_color='#16a34a' if is_paused else '#b45309',
                text_color='#0a0a0a')
            self._draw_map()
        except Exception:
            pass
        self.after(1000, self._tick)

    def _draw_map(self):
        """Draw a grid of coloured cells showing piece download progress."""
        if not self.torrent:
            return
        c = self._pmap
        c.delete('all')
        w = c.winfo_width() or 400
        cols = 60
        cell = max(4, w // cols)
        rows = 3
        done = int(self.torrent['progress'] * cols * rows)
        for i in range(cols * rows):
            x1 = (i % cols) * cell + 2
            y1 = (i // cols) * (cell + 2) + 2
            col = (C['accent'] if i < done - cols else
                   '#60a5fa' if i < done else C['bg2'])
            c.create_rectangle(x1, y1, x1 + cell - 2, y1 + cell - 2,
                               fill=col, outline='')

    def _refresh_peers(self):
        """Populate the peer list panel with placeholder rows."""
        for w in self._pscroll.winfo_children():
            w.destroy()
        t = self.torrent
        if not t:
            return
        for _ in range(min(t['peers'], 20)):
            ip = f"192.168.{random.randint(0, 255)}.{random.randint(1, 254)}"
            spd = f"{random.uniform(0.05, 0.5):.2f} MB/s" if t['speed_dl'] else "—"
            prg = f"{random.randint(30, 100)}%"
            row = ctk.CTkFrame(self._pscroll, fg_color='transparent')
            row.pack(fill='x', pady=1)
            ctk.CTkFrame(row, width=6, height=6, corner_radius=3,
                         fg_color=C['dot_on']).pack(side='left', padx=(0, 4))
            ctk.CTkLabel(row, text=ip, width=120, anchor='w',
                         font=ctk.CTkFont(size=10, family='Consolas'),
                         text_color=C['fg0']).pack(side='left')
            ctk.CTkLabel(row, text=spd, width=70, anchor='w',
                         font=ctk.CTkFont(size=10),
                         text_color=C['accent'] if spd != '—' else C['fg2']).pack(side='left')
            ctk.CTkLabel(row, text=prg, width=50, anchor='w',
                         font=ctk.CTkFont(size=10),
                         text_color=C['fg1']).pack(side='left')

    def _toggle_pause(self):
        """Pause or resume; also pauses/resumes the underlying CtrlAdapter."""
        if not self.torrent:
            return
        t = self.torrent
        ctrl: Optional[CtrlAdapter] = t.get('ctrl')
        if t['status'] == 'paused':
            t['status'] = 'downloading'
            # Re-start is not yet supported (would need a new CtrlAdapter);
            # for now only demo mode resets speed/peers.
            if ctrl is None:
                t['speed_dl'] = round(random.uniform(0.8, 1.5), 2)
                t['peers'] = random.randint(20, 50)
        else:
            t['status'] = 'paused'
            if ctrl:
                ctrl.stop()
            t['speed_dl'] = 0.0
            t['peers'] = 0
            t['eta'] = '—'

    def _cancel(self):
        if not self.torrent:
            return
        if messagebox.askyesno("Cancel", f"Cancel '{self.torrent['name']}'?"):
            ctrl: Optional[CtrlAdapter] = self.torrent.get('ctrl')
            if ctrl:
                ctrl.stop()
            MOCK_TORRENTS.remove(self.torrent)
            self.torrent = None
            self.app.show('main')


# ---------------------------------------------------------------------------
# Details screen
# ---------------------------------------------------------------------------

class DetailsScreen(Screen):
    def __init__(self, parent, app):
        super().__init__(parent, app)
        self.torrent: Optional[dict] = None
        self._built = False

    def _lazy_build(self):
        if self._built:
            return
        self._built = True

        top = ctk.CTkFrame(self, fg_color=C['bg1'], corner_radius=0, height=46)
        top.pack(fill='x', side='top')
        top.pack_propagate(False)
        _btn(top, "← Downloads", lambda: self.app.show('main'),
             color='transparent', hover=C['bg2'], tc=C['accent'],
             w=120, h=32).pack(side='left', padx=12, pady=7)
        ctk.CTkLabel(top, text="Torrent Details",
                     font=ctk.CTkFont(size=13, weight='bold'),
                     text_color=C['fg0']).pack(side='left', padx=6)

        scroll = ctk.CTkScrollableFrame(self, fg_color=C['bg0'])
        scroll.pack(fill='both', expand=True, padx=18, pady=10)

        ic = _card(scroll, "Torrent Information")
        ic.pack(fill='x', pady=(0, 10))
        self._ilbls: dict[str, ctk.CTkLabel] = {}
        for idx, (key, label) in enumerate([
            ('name', 'Name'), ('size', 'Total Size'),
            ('tracker', 'Tracker URL'), ('info_hash', 'Info Hash'),
            ('pieces', 'Pieces'), ('piece_size', 'Piece Size'),
            ('seeds', 'Seeds'), ('added', 'Added'),
        ]):
            row = ctk.CTkFrame(ic,
                               fg_color=C['bg2'] if idx % 2 == 0 else 'transparent',
                               corner_radius=6)
            row.pack(fill='x', padx=12, pady=1)
            ctk.CTkLabel(row, text=label, width=130, anchor='w',
                         font=ctk.CTkFont(size=11), text_color=C['fg1']).pack(
                side='left', padx=10, pady=6)
            lb = ctk.CTkLabel(row, text="—", anchor='w',
                              font=ctk.CTkFont(
                                  size=11,
                                  family='Consolas' if key == 'info_hash' else None),
                              text_color=C['fg0'])
            lb.pack(side='left', fill='x', expand=True, padx=6)
            self._ilbls[key] = lb

        sc = _card(scroll, "Current Status")
        sc.pack(fill='x', pady=(0, 10))
        self._srow = ctk.CTkFrame(sc, fg_color='transparent')
        self._srow.pack(fill='x', padx=16, pady=(0, 8))
        self._dpbar = ctk.CTkProgressBar(sc, height=8,
                                         progress_color=C['accent'], fg_color=C['bg2'])
        self._dpbar.pack(fill='x', padx=16, pady=(0, 14))
        self._dpbar.set(0)

        sr = ctk.CTkFrame(sc, fg_color=C['bg2'], corner_radius=8)
        sr.pack(fill='x', padx=16, pady=(0, 14))
        self._dstats: dict[str, ctk.CTkLabel] = {}
        for lbl, key in [("Progress", 'pct'), ("Speed ↓", 'dl'),
                         ("Peers", 'peers'), ("ETA", 'eta')]:
            f = ctk.CTkFrame(sr, fg_color='transparent')
            f.pack(side='left', expand=True, fill='x', padx=12, pady=8)
            ctk.CTkLabel(f, text=lbl, font=ctk.CTkFont(size=10),
                         text_color=C['fg2']).pack()
            lb = ctk.CTkLabel(f, text="—",
                              font=ctk.CTkFont(size=14, weight='bold'),
                              text_color=C['fg0'])
            lb.pack()
            self._dstats[key] = lb

        _btn(scroll, "▶  Resume / View Download",
             lambda: self.torrent and self.app.show_download(self.torrent['id']),
             h=36).pack(pady=(0, 10), padx=4, fill='x')

    def on_show(self, torrent_id=None, **kw):
        self._lazy_build()
        if torrent_id is None:
            return
        self.torrent = next((t for t in MOCK_TORRENTS if t['id'] == torrent_id), None)
        if not self.torrent:
            return
        t = self.torrent
        for key, lb in self._ilbls.items():
            val = str(t.get(key, '—'))
            if key == 'pieces':
                val = f"{t.get('pieces', '?')} × {t.get('piece_size', '?')}"
            lb.configure(text=val)
        for w in self._srow.winfo_children():
            w.destroy()
        _badge(self._srow, t['status']).pack(side='left')
        self._dpbar.set(t['progress'])
        self._dstats['pct'].configure(text=f"{int(t['progress'] * 100)}%")
        self._dstats['dl'].configure(
            text=f"{t['speed_dl']:.2f} MB/s" if t['speed_dl'] else "—")
        self._dstats['peers'].configure(text=str(t['peers']))
        self._dstats['eta'].configure(text=t['eta'])


# ---------------------------------------------------------------------------
# Admin screen
# ---------------------------------------------------------------------------

class AdminScreen(Screen):
    def _lazy_build(self):
        if hasattr(self, '_built'):
            return
        self._built = True

        top = ctk.CTkFrame(self, fg_color=C['adm_dim'], corner_radius=0, height=52)
        top.pack(fill='x', side='top')
        top.pack_propagate(False)
        ctk.CTkLabel(top, text="🛡  PyTorrent  –  Admin Panel",
                     font=ctk.CTkFont(size=17, weight='bold'),
                     text_color=C['admin']).pack(side='left', padx=20)
        _btn(top, "Sign out", self._logout,
             color=C['bg2'], hover=C['err_dim'], tc=C['fg1'],
             w=80, h=28).pack(side='right', padx=16)

        stats = ctk.CTkFrame(self, fg_color=C['card'], corner_radius=0, height=60)
        stats.pack(fill='x')
        stats.pack_propagate(False)
        self._stlbls: dict[str, ctk.CTkLabel] = {}
        for lbl, key, col in [
            ("Total connections", 'total', C['admin']),
            ("Authenticated",    'auth',  C['ok']),
            ("Active downloads", 'dl',    C['accent']),
            ("Idle",             'idle',  C['fg1']),
        ]:
            f = ctk.CTkFrame(stats, fg_color='transparent')
            f.pack(side='left', expand=True, fill='x', padx=16, pady=10)
            ctk.CTkLabel(f, text=lbl, font=ctk.CTkFont(size=10),
                         text_color=C['fg2']).pack()
            lb = ctk.CTkLabel(f, text="—",
                              font=ctk.CTkFont(size=22, weight='bold'),
                              text_color=col)
            lb.pack()
            self._stlbls[key] = lb

        hdr = ctk.CTkFrame(self, fg_color=C['bg1'], corner_radius=0, height=28)
        hdr.pack(fill='x')
        hdr.pack_propagate(False)
        for txt, w in [("IP Address", 140), ("User", 120),
                       ("Status", 120), ("Connected at", 130)]:
            ctk.CTkLabel(hdr, text=txt, width=w, anchor='w',
                         font=ctk.CTkFont(size=10, weight='bold'),
                         text_color=C['fg2']).pack(
                side='left', padx=(14 if txt == "IP Address" else 8, 4))

        self._clist = ctk.CTkScrollableFrame(self, fg_color=C['bg0'])
        self._clist.pack(fill='both', expand=True)

        bar = ctk.CTkFrame(self, fg_color=C['bg1'], corner_radius=0, height=36)
        bar.pack(fill='x', side='bottom')
        bar.pack_propagate(False)
        _btn(bar, "⟳  Refresh", self._refresh,
             color=C['adm_dim'], hover=C['admin'], tc=C['admin'],
             w=100, h=26).pack(side='left', padx=12, pady=5)
        self._rlbl = ctk.CTkLabel(bar, text="",
                                  font=ctk.CTkFont(size=10), text_color=C['fg2'])
        self._rlbl.pack(side='left', padx=8)

    def on_show(self, **kw):
        self._lazy_build()
        self._refresh()
        self._auto_refresh()

    def _refresh(self):
        for w in self._clist.winfo_children():
            w.destroy()

        def _do():
            ok, conns = self.app.auth.get_server_status()
            self.after(0, lambda: self._render(conns if ok else _DEMO_CONNECTIONS))

        threading.Thread(target=_do, daemon=True).start()

    def _render(self, conns: list):
        for w in self._clist.winfo_children():
            w.destroy()

        total = len(conns)
        auth  = sum(1 for c in conns if c.get('status') not in ('auth', 'connecting'))
        dl    = sum(1 for c in conns if c.get('status') == 'downloading')
        idle  = sum(1 for c in conns if c.get('status') == 'idle')

        try:
            self._stlbls['total'].configure(text=str(total))
            self._stlbls['auth'].configure(text=str(auth))
            self._stlbls['dl'].configure(text=str(dl))
            self._stlbls['idle'].configure(text=str(idle))
        except Exception:
            pass

        for idx, c in enumerate(conns):
            status = c.get('status', 'idle')
            dot_col = (C['ok']     if status == 'downloading' else
                       C['accent'] if status == 'idle' else C['warn'])
            row = ctk.CTkFrame(self._clist,
                               fg_color=C['card'] if idx % 2 == 0 else C['bg1'],
                               corner_radius=6, height=38)
            row.pack(fill='x', padx=8, pady=2)
            row.pack_propagate(False)

            ctk.CTkFrame(row, width=8, height=8, corner_radius=4,
                         fg_color=dot_col).pack(side='left', padx=(12, 6), pady=15)
            ctk.CTkLabel(row, text=c.get('ip', '—'), width=132, anchor='w',
                         font=ctk.CTkFont(size=11, family='Consolas'),
                         text_color=C['fg0']).pack(side='left', padx=4)
            ctk.CTkLabel(row, text=c.get('user', '—'), width=120, anchor='w',
                         font=ctk.CTkFont(size=11),
                         text_color=C['fg1']).pack(side='left', padx=4)
            bf = ctk.CTkFrame(row, fg_color='transparent', width=120)
            bf.pack(side='left', padx=4)
            bf.pack_propagate(False)
            _badge(bf, status).pack(expand=True)
            ctk.CTkLabel(row, text=c.get('at', '—'), width=130, anchor='w',
                         font=ctk.CTkFont(size=11),
                         text_color=C['fg2']).pack(side='left', padx=4)
            _btn(row, "✕ Kick", lambda ip=c.get('ip', ''): self._kick(ip),
                 color='transparent', hover=C['err_dim'], tc=C['err'],
                 w=70, h=24).pack(side='right', padx=10)

        self._rlbl.configure(text=f"Last refresh: {time.strftime('%H:%M:%S')}")

    def _kick(self, ip: str):
        if messagebox.askyesno("Kick", f"Disconnect {ip}?"):
            global _DEMO_CONNECTIONS
            _DEMO_CONNECTIONS = [c for c in _DEMO_CONNECTIONS if c.get('ip') != ip]
            self._refresh()

    def _auto_refresh(self):
        if not self.winfo_exists():
            return
        self._refresh()
        self.after(10_000, self._auto_refresh)

    def _logout(self):
        self.app.auth.disconnect()
        self.app.current_user = None
        self.app.show('connect')


# ---------------------------------------------------------------------------
# Auth client
# ---------------------------------------------------------------------------

class AuthClient:
    """Manages the encrypted connection to the auth server.

    All network calls happen on a background thread. Returns (bool, str):
    (success, human-readable message).
    """

    def __init__(self):
        self._sock: Optional[socket.socket] = None
        self._enc = None
        self.connected = False
        self.key_method = "RSA"

    def connect(self, ip: str = SERVER_IP,
                port: int = SERVER_PORT) -> tuple[bool, str]:
        if not _REAL_AUTH:
            self.connected = True
            return True, "Demo mode – no server needed"
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(6)
            self._sock.connect((ip, port))
            mode = (KeyExchangeMode.RSA if self.key_method == "RSA"
                    else KeyExchangeMode.DH)
            key_exchanging.send_data(self._sock, mode)
            sym = (perform_rsa(self._sock, True) if mode == KeyExchangeMode.RSA
                   else perform_dh(self._sock, True))
            self._enc = EncryptedSocket(self._sock, sym)
            self.connected = True
            return True, "Connected"
        except Exception as e:
            self.connected = False
            return False, str(e)

    def disconnect(self):
        self.connected = False
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass

    def sign_in(self, username: str, password: str) -> tuple[bool, str]:
        if not _REAL_AUTH:
            u = _DEMO_USERS.get(username)
            if u and u["password"] == password:
                return (True, ResponseCodes.AdminLoginSuccess
                        if username == ADMIN_USER else ResponseCodes.SignInSuccess)
            return False, "incorrect"
        try:
            self._enc.send_message(CommandCodes.SignIn, [username, password])
            r = self._enc.read_message()
            if r == ResponseCodes.AdminLoginSuccess: return True, "admin"
            if r == ResponseCodes.SignInSuccess:     return True, "ok"
            if r == ResponseCodes.SignInFailed:      return False, "Incorrect username or password."
            return False, "Server error."
        except Exception as e:
            return False, f"Connection error: {e}"

    def sign_up(self, username: str, email: str,
                password: str) -> tuple[bool, str]:
        if not _REAL_AUTH:
            if username in _DEMO_USERS:
                return False, "Username already taken."
            _DEMO_USERS[username] = {"password": password, "email": email}
            return True, "ok"
        try:
            self._enc.send_message(CommandCodes.SignUp, [username, email, password])
            r = self._enc.read_message()
            if r == ResponseCodes.SignUpSuccess:  return True, "ok"
            if r == ResponseCodes.TakenUsername:  return False, "Username already taken."
            return False, "Server error."
        except Exception as e:
            return False, f"Connection error: {e}"

    def send_reset_code(self, username: str) -> tuple[bool, str]:
        if not _REAL_AUTH:
            return ((True, "ok") if username in _DEMO_USERS
                    else (False, "Username not found."))
        try:
            self._enc.send_message(CommandCodes.SendCode, [username])
            r = self._enc.read_message()
            if r == ResponseCodes.CodeSent:      return True, "ok"
            if r == ResponseCodes.WrongUsername: return False, "Username not found."
            return False, "Server error."
        except Exception as e:
            return False, f"Connection error: {e}"

    def verify_code(self, username: str, code: str) -> tuple[bool, str]:
        if not _REAL_AUTH:
            return True, "ok"
        try:
            self._enc.send_message(CommandCodes.VerifyCode, [username, code])
            r = self._enc.read_message()
            if r == ResponseCodes.VerificationSuccess: return True, "ok"
            return False, "Incorrect code."
        except Exception as e:
            return False, f"Connection error: {e}"

    def reset_password(self, username: str, new_pw: str) -> tuple[bool, str]:
        if not _REAL_AUTH:
            if username in _DEMO_USERS:
                _DEMO_USERS[username]["password"] = new_pw
            return True, "ok"
        try:
            self._enc.send_message(CommandCodes.ResetPassword, [username, new_pw])
            r = self._enc.read_message()
            if r == ResponseCodes.ResetPasswordSuccess: return True, "ok"
            return False, "Server error."
        except Exception as e:
            return False, f"Connection error: {e}"

    def get_server_status(self) -> tuple[bool, list]:
        """Admin-only: returns (ok, list_of_connection_dicts)."""
        if not _REAL_AUTH:
            return True, list(_DEMO_CONNECTIONS)
        try:
            self._enc.send_message(CommandCodes.GetServerStatus, [ADMIN_USER])
            r = self._enc.read_message()
            code, entries = (self._enc.parse_message(r)
                             if hasattr(self._enc, 'parse_message') else (r, []))
            if code == ResponseCodes.ServerStatus:
                conns = []
                for e in entries:
                    parts = e.split('|')
                    if len(parts) == 3:
                        conns.append({"user": parts[0], "ip": parts[1],
                                      "port": parts[2], "status": "active", "at": "—"})
                return True, conns
            return False, []
        except Exception:
            return False, []


# ---------------------------------------------------------------------------
# App root
# ---------------------------------------------------------------------------

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_TITLE} v{APP_VER}")
        self.geometry("1100x680")
        self.minsize(860, 540)
        self.configure(fg_color=C['bg0'])

        self.current_user: str | None = None
        self.auth = AuthClient()

        self._screens: dict[str, Screen] = {}
        self._active: Screen | None = None
        self._init()
        self.show('connect')

    def _init(self):
        cont = ctk.CTkFrame(self, fg_color='transparent')
        cont.pack(fill='both', expand=True)
        for name, cls in [
            ('connect',  ConnectScreen),
            ('login',    LoginScreen),
            ('register', RegisterScreen),
            ('forgot',   ForgotScreen),
            ('main',     MainScreen),
            ('download', DownloadScreen),
            ('details',  DetailsScreen),
            ('admin',    AdminScreen),
        ]:
            s = cls(cont, self)
            s.place(relx=0, rely=0, relwidth=1, relheight=1)
            self._screens[name] = s

    def show(self, name: str, **kw):
        if self._active:
            self._active.lower()
        s = self._screens[name]
        s.lift()
        self._active = s
        s.on_show(**kw)

    def show_download(self, tid: int):
        self.show('download', torrent_id=tid)

    def show_details(self, tid: int):
        self.show('details', torrent_id=tid)


if __name__ == '__main__':
    App().mainloop()