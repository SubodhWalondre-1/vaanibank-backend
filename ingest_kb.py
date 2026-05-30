"""
VaaniBank AI — Knowledge Base Ingestion Script
PSBs Hackathon 2026 | Team Vectora

One-time (and on-update) script to parse, chunk, embed, and store all
knowledge base markdown files into ChromaDB for RAG retrieval.

Run from the backend/ directory:
    python ingest_kb.py

Re-run after adding or updating any file in knowledge_base/.
Existing chunks are skipped (idempotent) — only new files are embedded.
Use --force to re-embed everything from scratch.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Bootstrap: ensure backend/ root is importable so services can be imported
_BACKEND_DIR = Path(__file__).resolve().parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("vaanibank.ingest_kb")


async def run(force: bool = False) -> None:
    """
    Main ingestion routine.

    Args:
        force: When True, drop and recreate the ChromaDB collection before
               ingesting so every chunk is re-embedded from scratch.
    """
    from services.rag_service import rag_service, _CHROMA_PATH, _COLLECTION_NAME, _KB_PATH

    logger.info("=== VaaniBank Knowledge Base Ingestion ===")
    logger.info("KB path      : %s", _KB_PATH)
    logger.info("ChromaDB path: %s", _CHROMA_PATH)

    # Count source files
    md_files = list(_KB_PATH.rglob("*.md"))
    logger.info("Source files : %d markdown file(s) found", len(md_files))
    for f in md_files:
        logger.info("  %s", f.relative_to(_KB_PATH))

    if not md_files:
        logger.error("No .md files found in knowledge_base/ — nothing to ingest.")
        return

    # Force mode: drop existing collection so everything is re-embedded
    if force:
        logger.warning("--force flag set: deleting existing ChromaDB collection '%s'", _COLLECTION_NAME)
        try:
            import chromadb
            from chromadb.config import Settings as ChromaSettings
            _CHROMA_PATH.mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(
                path=str(_CHROMA_PATH),
                settings=ChromaSettings(anonymized_telemetry=False),
            )
            client.delete_collection(_COLLECTION_NAME)
            logger.info("Existing collection deleted.")
        except Exception as exc:
            logger.warning("Could not delete collection (may not exist): %s", exc)

    # Initialise RAG service (loads models + connects ChromaDB)
    logger.info("Loading embedding model (first run may take 1-2 minutes to download)...")
    await rag_service.ensure_ready()

    # Run ingestion
    logger.info("Starting ingestion...")
    count = await rag_service.ingest_knowledge_base(str(_KB_PATH))

    if count > 0:
        logger.info("Successfully ingested %d new chunks.", count)
    else:
        logger.info("No new chunks to ingest (all already in ChromaDB).")

    # Verify final state
    if rag_service._collection is not None:
        total = rag_service._collection.count()
        logger.info("ChromaDB collection '%s' now contains %d total chunks.", _COLLECTION_NAME, total)

    # Quick smoke test: retrieve for a common banking query
    logger.info("Running smoke test retrieval...")
    result = await rag_service.retrieve(
        query="home loan documents required CIBIL score",
        intent="loan_enquiry",
        product="home_loan",
        top_k=3,
    )
    if result.chunks:
        logger.info(
            "Smoke test passed: retrieved %d chunk(s) | source=%s | top_score=%.3f",
            len(result.chunks),
            result.retrieval_source,
            result.chunks[0].score,
        )
        for i, chunk in enumerate(result.chunks, 1):
            logger.info(
                "  Chunk %d: %s | %s",
                i,
                chunk.metadata.get("source_file", "?"),
                chunk.text[:80].replace("\n", " "),
            )
    else:
        logger.warning(
            "Smoke test returned no chunks — check that knowledge_base/ files "
            "have YAML front-matter and valid content."
        )

    logger.info("=== Ingestion complete ===")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest VaaniBank AI knowledge base into ChromaDB"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Drop existing collection and re-embed all chunks from scratch",
    )
    args = parser.parse_args()
    asyncio.run(run(force=args.force))


if __name__ == "__main__":
    main()
