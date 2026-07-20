# BitTorrent Peer in Python

A from-scratch implementation of a **BitTorrent peer** written in Python. The project supports both **leeching** and **seeding**, communicates with standard BitTorrent trackers and peers, and includes a separate encrypted authentication server with a CustomTkinter graphical interface.

> **Educational project:** Developed as a cybersecurity final project to learn networking, distributed systems, cryptography, and the BitTorrent protocol.

---

## Features

* BitTorrent Wire Protocol (BEP-3)
* Downloading (Leecher)
* Uploading (Seeder)
* Bencoding encoder/decoder
* HTTP tracker support
* Piece verification using SHA-1
* Asynchronous networking (`asyncio`)
* CustomTkinter graphical interface
* Separate encrypted authentication server
* RSA / Diffie-Hellman key exchange
* AES-CBC encrypted communication

---

## Project Structure

```
BitTorrent/
│
├── auth/              # Authentication server and cryptography
├── core/              # BitTorrent protocol implementation
├── gui/               # CustomTkinter GUI
├── torrents/          # Torrent files
├── downloads/         # Downloaded files
└── main.py
```

---

## Requirements

* Python 3.11+
* customtkinter
* cryptography

Install dependencies:

```bash
pip install -r requirements.txt
```

---

## Running

Start the authentication server:

```bash
python auth/server.py
```

Run the client:

```bash
python main.py
```

---

## Supported Features

* Single-file torrents
* Compact and dictionary tracker responses
* Piece hashing and verification
* Simultaneous downloading and uploading
* Incoming and outgoing peer connections

---

## Limitations

* Single-file torrents only
* No DHT (Distributed Hash Table)
* No Peer Exchange (PEX)
* No Magnet Link support
* No tit-for-tat or optimistic unchoking algorithm
* HTTP trackers only (if UDP trackers are not implemented)

---

## Technologies

* Python
* asyncio
* sockets
* SHA-1
* RSA
* Diffie-Hellman
* AES-CBC
* CustomTkinter

---

## License

This project was created for educational purposes.
