"""Admin CLI. Run inside the api container:

    docker compose exec api python -m app.cli create-key --name rag-service

or directly on a zero-container host, where DATABASE_URL names a sqlite file:

    python -m app.cli create-key --name rag-service
"""
import argparse
import asyncio

from app.config import get_settings
from app.db import create_database


async def _create_key(name: str, credits: int) -> str:
    db = create_database(get_settings().database_url)
    await db.connect()
    try:
        return await db.create_api_key(name, credits)
    finally:
        await db.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="gen-api")
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create-key", help="Create a new API key")
    create.add_argument("--name", required=True, help="Consumer name, e.g. rag-service")
    create.add_argument("--credits", type=int, default=10000, help="Initial credit balance")

    args = parser.parse_args()
    if args.command == "create-key":
        key = asyncio.run(_create_key(args.name, args.credits))
        print(key)


if __name__ == "__main__":
    main()
