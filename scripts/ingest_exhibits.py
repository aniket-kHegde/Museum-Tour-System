#!/usr/bin/env python3
"""
scripts/ingest_exhibits.py

Seeds exhibit data from data/exhibits/seed_exhibits.json into:
  1. PostgreSQL  — exhibits + beacons tables
  2. Qdrant      — chunked + embedded for RAG search

Run once at setup, and again whenever exhibit content changes.

An exhibit with a null `beacon_uuid` gets no beacon row: it never triggers a
narration, but it is still stored and embedded, so visitors can ask about it by
voice from anywhere in the building.

A full run also PRUNES — any exhibit in this museum that is no longer in the
seed file is deleted from PostgreSQL and Qdrant. Without that, renaming an
exhibit id would orphan the old rows and vectors, and qa_service's fallback to
a whole-collection search would keep surfacing them. Pruning is skipped when
--exhibit-id names a single entry.

Usage:
    python scripts/ingest_exhibits.py
    python scripts/ingest_exhibits.py --museum-id my-museum
    python scripts/ingest_exhibits.py --exhibit-id amb-001   # single exhibit, no prune
"""

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import asyncpg
from dotenv import load_dotenv
from langchain_text_splitters import RecursiveCharacterTextSplitter
from loguru import logger
from qdrant_client import QdrantClient
from qdrant_client.http.models import (
    Distance,
    PointStruct,
    VectorParams,
    Filter,
    FieldCondition,
    MatchAny,
    MatchValue,
)
from sentence_transformers import SentenceTransformer

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

EXHIBITS_FILE = Path(__file__).parent.parent / "data" / "exhibits" / "seed_exhibits.json"

POSTGRES_DSN = (
    f"postgresql://{os.getenv('POSTGRES_USER', 'museum')}"
    f":{os.getenv('POSTGRES_PASSWORD', '')}"
    f"@{os.getenv('POSTGRES_HOST', 'localhost')}"
    f":{os.getenv('POSTGRES_PORT', '5432')}"
    f"/{os.getenv('POSTGRES_DB', 'museum_tour')}"
)

QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", None)

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
EMBEDDING_DIM = 384  # all-MiniLM-L6-v2 output size

CHUNK_SIZE = 512        # tokens
CHUNK_OVERLAP = 64


# ── Helpers ───────────────────────────────────────────────────────────────────

def collection_name(museum_id: str) -> str:
    return f"{museum_id}_knowledge"


def ensure_qdrant_collection(client: QdrantClient, museum_id: str):
    cname = collection_name(museum_id)
    existing = [c.name for c in client.get_collections().collections]
    if cname not in existing:
        client.create_collection(
            collection_name=cname,
            vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
        )
        logger.info(f"Created Qdrant collection: {cname}")
    else:
        logger.info(f"Qdrant collection already exists: {cname}")


async def seed_postgres(conn: asyncpg.Connection, exhibit: dict, museum_id: str):
    """Insert or update the exhibit row, and its beacon row if it has a beacon."""
    await conn.execute(
        """
        INSERT INTO exhibits (id, museum_id, title, author, year_created, genre,
                              location, tts_script, full_content, metadata)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        ON CONFLICT (id, museum_id) DO UPDATE SET
            title        = EXCLUDED.title,
            author       = EXCLUDED.author,
            year_created = EXCLUDED.year_created,
            genre        = EXCLUDED.genre,
            location     = EXCLUDED.location,
            tts_script   = EXCLUDED.tts_script,
            full_content = EXCLUDED.full_content,
            metadata     = EXCLUDED.metadata,
            updated_at   = NOW()
        """,
        exhibit["id"],
        museum_id,
        exhibit["title"],
        exhibit.get("author"),
        exhibit.get("year"),
        exhibit.get("genre"),
        exhibit.get("location"),
        exhibit["tts_script"],
        exhibit["full_content"],
        json.dumps({"source": "seed_exhibits.json"}),
    )

    if not exhibit.get("beacon_uuid"):
        # Archive entry: searchable and narratable, but nothing physical to walk up to.
        logger.info(f"  [postgres] upserted exhibit {exhibit['id']} (no beacon)")
        return

    await conn.execute(
        """
        INSERT INTO beacons (uuid, museum_id, exhibit_id, location)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (uuid, museum_id) DO UPDATE SET
            exhibit_id = EXCLUDED.exhibit_id,
            location   = EXCLUDED.location
        """,
        exhibit["beacon_uuid"],
        museum_id,
        exhibit["id"],
        exhibit.get("location"),
    )
    logger.info(
        f"  [postgres] upserted exhibit {exhibit['id']} + beacon {exhibit['beacon_uuid']}"
    )


def ingest_qdrant(
    client: QdrantClient,
    model: SentenceTransformer,
    splitter: RecursiveCharacterTextSplitter,
    exhibit: dict,
    museum_id: str,
):
    """Chunk exhibit content, embed, and upsert into Qdrant."""
    cname = collection_name(museum_id)

    # Drop this exhibit's existing chunks so a re-ingest replaces rather than piles up
    client.delete(
        collection_name=cname,
        points_selector=Filter(
            must=[FieldCondition(key="exhibit_id", match=MatchValue(value=exhibit["id"]))]
        ),
    )

    # Header + body. The payload keys below stay title/author/genre — qa_service
    # reads them by name — but the header wording suits monuments and documents
    # as much as books.
    full_text = (
        f"Exhibit: {exhibit['title']}\n"
        f"Attribution: {exhibit.get('author') or 'Unknown'}\n"
        f"Year: {exhibit.get('year') or 'Unknown'}\n"
        f"Category: {exhibit.get('genre') or 'Unknown'}\n\n"
        f"{exhibit['full_content']}"
    )

    chunks = splitter.split_text(full_text)
    logger.info(f"  [qdrant] {exhibit['id']}: {len(chunks)} chunks to embed")

    embeddings = model.encode(chunks, show_progress_bar=False, convert_to_numpy=True)

    points = [
        PointStruct(
            id=str(uuid.uuid4()),
            vector=embeddings[i].tolist(),
            payload={
                "exhibit_id": exhibit["id"],
                "museum_id": museum_id,
                "chunk_index": i,
                "text": chunks[i],
                "title": exhibit["title"],
                "author": exhibit.get("author"),
                "genre": exhibit.get("genre"),
            },
        )
        for i in range(len(chunks))
    ]

    client.upsert(collection_name=cname, points=points)
    logger.info(f"  [qdrant] upserted {len(points)} vectors for {exhibit['id']}")


async def prune(
    conn: asyncpg.Connection,
    client: QdrantClient,
    museum_id: str,
    keep_ids: list[str],
):
    """Delete anything in this museum that the seed file no longer describes.

    Without this, renaming an exhibit id leaves the old row and its vectors
    behind: the upsert writes the new id and the per-exhibit Qdrant delete only
    targets the new id, so nothing ever removes the old one. qa_service falls
    back to a whole-collection search when an exhibit-scoped search returns
    nothing, so those orphans would keep turning up in answers.
    """
    stale = await conn.fetch(
        "SELECT id FROM exhibits WHERE museum_id = $1 AND id <> ALL($2::text[])",
        museum_id,
        keep_ids,
    )
    if stale:
        ids = [r["id"] for r in stale]
        # beacons has ON DELETE CASCADE onto exhibits, so its rows go with them.
        await conn.execute(
            "DELETE FROM exhibits WHERE museum_id = $1 AND id <> ALL($2::text[])",
            museum_id,
            keep_ids,
        )
        logger.warning(f"  [postgres] pruned {len(ids)} stale exhibit(s): {', '.join(ids)}")

    client.delete(
        collection_name=collection_name(museum_id),
        points_selector=Filter(
            must=[FieldCondition(key="museum_id", match=MatchValue(value=museum_id))],
            must_not=[FieldCondition(key="exhibit_id", match=MatchAny(any=keep_ids))],
        ),
    )
    logger.info("  [qdrant] pruned vectors for exhibits no longer in the seed file")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(museum_id: str, exhibit_id: str | None):
    logger.info(f"Loading exhibits from {EXHIBITS_FILE}")
    all_exhibits = json.loads(EXHIBITS_FILE.read_text())
    exhibits = all_exhibits

    if exhibit_id:
        exhibits = [e for e in all_exhibits if e["id"] == exhibit_id]
        if not exhibits:
            logger.error(f"Exhibit ID '{exhibit_id}' not found in seed file")
            sys.exit(1)

    with_beacon = sum(1 for e in exhibits if e.get("beacon_uuid"))
    logger.info(
        f"Ingesting {len(exhibits)} exhibit(s) into museum '{museum_id}' "
        f"({with_beacon} with a beacon, {len(exhibits) - with_beacon} archive-only)"
    )

    # Load embedding model once (downloads on first run, ~90MB)
    logger.info(f"Loading embedding model: {EMBEDDING_MODEL}")
    model = SentenceTransformer(EMBEDDING_MODEL)

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        separators=["\n\n", "\n", ". ", " "],
    )

    # Qdrant setup
    qdrant = QdrantClient(
        host=QDRANT_HOST,
        port=QDRANT_PORT,
        api_key=QDRANT_API_KEY,
    )
    ensure_qdrant_collection(qdrant, museum_id)

    # PostgreSQL setup
    conn = await asyncpg.connect(POSTGRES_DSN)

    try:
        for exhibit in exhibits:
            logger.info(f"Processing: {exhibit['title']} ({exhibit['id']})")
            await seed_postgres(conn, exhibit, museum_id)
            ingest_qdrant(qdrant, model, splitter, exhibit, museum_id)

        if exhibit_id:
            # Single-exhibit mode: pruning here would delete every other exhibit.
            logger.info("Skipping prune (--exhibit-id names a single exhibit)")
        else:
            await prune(conn, qdrant, museum_id, [e["id"] for e in all_exhibits])

        logger.success(f"Done. {len(exhibits)} exhibit(s) ingested.")
        logger.info(f"Qdrant collection: {collection_name(museum_id)}")
        logger.info("You can now start the backend services.")

    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest exhibits into PostgreSQL + Qdrant")
    parser.add_argument(
        "--museum-id", default="demo-library", help="Museum ID (default: demo-library)"
    )
    parser.add_argument(
        "--exhibit-id",
        default=None,
        help="Re-ingest a single exhibit by ID (skips the prune step)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.museum_id, args.exhibit_id))
