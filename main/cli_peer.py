import argparse
import asyncio
import logging

from core.client import Peer
from core.torrent import Torrent

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s"
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-t", "--torrent",
                        help=".torrent file to download")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="enable verbose output")

    args = parser.parse_args()
    if args.verbose:
        logging.basicConfig(level=logging.INFO)

    async def runner():
        client = Peer(Torrent(args.torrent))
        try:
            await client.start()
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        logging.info("Exiting...")


if __name__ == "__main__":
    main()
