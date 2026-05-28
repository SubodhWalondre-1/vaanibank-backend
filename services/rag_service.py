"""
VaaniBank AI — RAG Service (Retrieval-Augmented Generation)
PSBs Hackathon 2026 | Team Vectora

Provides grounded banking knowledge retrieval for the LLM pipeline.
Sits between pre_detect_intent() and process_with_llm() in the orchestrator.

Architecture:
    Query → metadata filter → hybrid retrieval (dense + BM25) → RRF merge → rerank → top-k chunks

Key design decisions:
    - ChromaDB as local vector store (no external API, zero cost, persistent)
    - multilingual-e5-small for embeddings (supports all 10 Indian languages)
    - BM25 for exact banking term matching (CIBIL, NEFT, PMJDY, scheme names)
    - Reciprocal Rank Fusion to merge dense + sparse results
    - Cross-encoder reranking for final precision pass
    - In-memory BM25 index rebuilt on startup from ChromaDB documents
    - Thread-pool offload for all CPU-bound operations (embeddings, reranking)

Usage:
    from services.rag_service import rag_service

    chunks = await rag_service.retrieve(
        query="home loan documents required",
        intent="loan_enquiry",
        product="home_loan",
    )
    context_block = rag_service.format_context_for_llm(chunks)
"""

from __future__ import annotations

import os
import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("vaanibank.rag")

# ---------------------------------------------------------------------------
# Optional heavy dependencies — graceful degradation if not installed
# ---------------------------------------------------------------------------

try:
    import chromadb
    from chromadb.config import Settings as ChromaSettings
    _CHROMA_AVAILABLE = True
except ImportError:
    _CHROMA_AVAILABLE = False
    logger.warning("chromadb not installed — RAG vector search disabled")

try:
    import google.generativeai as genai
    _GEMINI_AVAILABLE = True
except ImportError:
    _GEMINI_AVAILABLE = False
    logger.warning("google-generativeai not installed — RAG Gemini embeddings disabled")

# Map _ST_AVAILABLE to _GEMINI_AVAILABLE to preserve existing dependency checks
_ST_AVAILABLE = _GEMINI_AVAILABLE

try:
    from rank_bm25 import BM25Okapi
    _BM25_AVAILABLE = True
except ImportError:
    _BM25_AVAILABLE = False
    logger.warning("rank-bm25 not installed — RAG keyword search disabled")

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    _YAML_AVAILABLE = False


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Path to the flat-file knowledge base relative to this file
_KB_PATH = Path(__file__).resolve().parent.parent / "knowledge_base"

# ChromaDB persistent storage path
_CHROMA_PATH = Path(__file__).resolve().parent.parent / "storage" / "chroma_db"

# Embedding model — Google Gemini API multilingual embedding model (3072 dimensions)
_GEMINI_EMBED_MODEL = "models/gemini-embedding-001"

# ChromaDB collection name (v2 uses 3072 dimensions)
_COLLECTION_NAME = "vaanibank_kb_v2"

# Retrieval parameters
_DENSE_CANDIDATES = 15    # how many dense results to fetch before reranking
_BM25_CANDIDATES  = 15    # how many BM25 results to fetch before reranking
_RRF_K            = 60    # RRF constant (standard value, do not change lightly)
_FINAL_TOP_K      = 4     # number of chunks returned to the LLM after reranking

# Minimum confidence threshold — chunks below this score are dropped
_MIN_SCORE_THRESHOLD = 0.10



# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class RetrievedChunk:
    """A single retrieved knowledge chunk with its metadata and relevance score."""
    chunk_id: str
    text: str
    metadata: Dict[str, Any]
    score: float                      # final relevance score after reranking


@dataclass
class RetrievalResult:
    """Container returned by RAGService.retrieve()."""
    chunks: List[RetrievedChunk]
    query_used: str                   # the (possibly rewritten) query used for retrieval
    retrieval_source: str             # "hybrid" | "dense_only" | "bm25_only" | "fallback"
    total_retrieved: int


# ---------------------------------------------------------------------------
# Chunking helpers
# ---------------------------------------------------------------------------

def _parse_yaml_frontmatter(text: str) -> tuple[dict, str]:
    """
    Parse YAML front-matter block from a markdown file.

    Returns:
        (metadata_dict, body_without_frontmatter)
    """
    if not _YAML_AVAILABLE or not text.startswith("---"):
        return {}, text

    try:
        # Find closing --- delimiter
        end_idx = text.index("---", 3)
        yaml_block = text[3:end_idx].strip()
        body = text[end_idx + 3:].strip()
        metadata = yaml.safe_load(yaml_block) or {}
        return metadata, body
    except (ValueError, Exception):
        return {}, text


def _chunk_by_doc_type(text: str, doc_type: str, source_file: str) -> List[str]:
    """
    Split a document body into retrievable chunks based on its doc_type.

    Chunking strategy per type:
        faq      → split on H2 sections (## heading)
        sop      → split on Step N: lines
        glossary → split on H2 category sections
        compliance → split on H2 sections
        default  → split on H2 sections with 800-char fallback
    """
    chunks: List[str] = []

    if doc_type in ("faq", "compliance", "glossary"):
        # Split on H2 headings (## Section Name)
        sections = re.split(r"\n(?=## )", text)
        for section in sections:
            section = section.strip()
            if len(section) > 40:  # skip trivially short fragments
                chunks.append(section)

    elif doc_type == "sop":
        # Split on Step N: pattern — each step is its own chunk
        # Also keep any preamble before the first step
        parts = re.split(r"\n(?=Step \d+:)", text)
        for part in parts:
            part = part.strip()
            if len(part) > 30:
                chunks.append(part)

    else:
        # Default: split on H2 sections; if section > 800 chars split further
        sections = re.split(r"\n(?=## )", text)
        for section in sections:
            section = section.strip()
            if not section:
                continue
            if len(section) > 800:
                # Hard split at paragraph boundaries
                paragraphs = section.split("\n\n")
                buffer = ""
                for para in paragraphs:
                    if len(buffer) + len(para) < 800:
                        buffer += "\n\n" + para
                    else:
                        if buffer.strip():
                            chunks.append(buffer.strip())
                        buffer = para
                if buffer.strip():
                    chunks.append(buffer.strip())
            else:
                chunks.append(section)

    # Fallback: if no chunks were produced, use the full text as one chunk
    if not chunks:
        chunks = [text[:1200]]

    return chunks


# ---------------------------------------------------------------------------
# RAG Service
# ---------------------------------------------------------------------------

class RAGService:
    """
    Core RAG service for VaaniBank AI.

    Provides hybrid retrieval (dense vector + BM25 keyword) with cross-encoder
    reranking over a curated banking knowledge base.

    The service initialises lazily on first use so that import does not block
    application startup. Call await rag_service.ensure_ready() explicitly
    during app startup to pre-warm the models.
    """

    def __init__(self) -> None:
        self._ready: bool = False
        self._init_lock = asyncio.Lock()

        # Embedding + reranking models (loaded lazily)
        self._embedder: Optional[Any] = None
        self._reranker: Optional[Any] = None

        # ChromaDB client and collection
        self._chroma_client: Optional[Any] = None
        self._collection: Optional[Any] = None

        # In-memory BM25 index (rebuilt from ChromaDB documents at startup)
        self._bm25_index: Optional[Any] = None
        self._bm25_docs: List[str] = []          # raw text for each BM25 document
        self._bm25_ids:  List[str] = []          # chunk_id for each BM25 document
        self._bm25_meta: List[Dict] = []         # metadata for each BM25 document

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def ensure_ready(self) -> None:
        """
        Initialise models and knowledge base if not already done.
        Safe to call multiple times — initialisation runs only once.
        """
        if self._ready:
            return
        async with self._init_lock:
            if self._ready:
                return
            await asyncio.to_thread(self._init_sync)
            self._ready = True
            logger.info("RAGService initialised successfully")

    async def retrieve(
        self,
        query: str,
        intent: str = "general",
        product: Optional[str] = None,
        top_k: int = _FINAL_TOP_K,
    ) -> RetrievalResult:
        """
        Retrieve the most relevant knowledge chunks for a customer query.

        Args:
            query:   Customer query text (may be multilingual).
            intent:  Pre-detected intent for metadata filtering.
            product: Specific product if known (e.g. 'home_loan').
            top_k:   Number of final chunks to return (default 4).

        Returns:
            RetrievalResult containing ranked chunks ready for LLM injection.
        """
        if not self._ready:
            await self.ensure_ready()

        # If dependencies are missing, return empty result gracefully
        if not _CHROMA_AVAILABLE or not _ST_AVAILABLE:
            logger.warning("RAG dependencies missing — returning empty retrieval")
            return RetrievalResult(
                chunks=[], query_used=query,
                retrieval_source="fallback", total_retrieved=0,
            )

        try:
            return await asyncio.to_thread(
                self._retrieve_sync, query, intent, product, top_k
            )
        except Exception as exc:
            logger.error("RAG retrieval failed (non-fatal): %s", exc, exc_info=True)
            return RetrievalResult(
                chunks=[], query_used=query,
                retrieval_source="fallback", total_retrieved=0,
            )

    async def ingest_knowledge_base(self, kb_path: Optional[str] = None) -> int:
        """
        Parse, chunk, embed, and store all knowledge base documents.

        This is called once at startup if the collection is empty,
        or manually via the ingest_kb.py script after KB updates.

        Returns:
            Number of chunks ingested.
        """
        if not self._ready:
            await self.ensure_ready()

        path = Path(kb_path) if kb_path else _KB_PATH
        return await asyncio.to_thread(self._ingest_sync, path)

    async def rewrite_query(
        self,
        current_query: str,
        conversation_history: List[Dict[str, str]],
        intent: str,
    ) -> str:
        """
        Rewrite a short follow-up query into a standalone retrieval query.

        Example:
            history: ["home loan 40 lakh chahiye"]
            current: "documents phir?"
            rewritten: "home loan 40 lakh ke liye kaunse documents chahiye"

        Uses Groq LLM (already available in the pipeline). If the query is
        already long enough (>= 6 words), returns it unchanged to save latency.
        """
        # Skip rewriting if query is already standalone
        word_count = len(current_query.strip().split())
        if word_count >= 6 or len(conversation_history) < 2:
            return current_query

        try:
            from services.ai_service import ai_service
            last_two = conversation_history[-2:]
            history_text = " | ".join(
                f"{m['role']}: {m['content'][:80]}" for m in last_two
            )
            rewrite_prompt = (
                f"Conversation context: {history_text}\n"
                f"Intent: {intent}\n"
                f"Short follow-up query: {current_query}\n\n"
                "Rewrite the follow-up query as a complete standalone search query "
                "in the same language. Return ONLY the rewritten query, nothing else."
            )
            completion = await ai_service._call_groq_with_fallback(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "user", "content": rewrite_prompt}],
                max_tokens=80,
                temperature=0.0,
                timeout=8.0,
            )
            rewritten = (completion.choices[0].message.content or "").strip()
            if rewritten and len(rewritten) > len(current_query):
                logger.debug(
                    "Query rewritten: '%s' → '%s'", current_query[:60], rewritten[:80]
                )
                return rewritten
        except Exception as exc:
            logger.debug("Query rewrite failed (non-fatal): %s", exc)

        return current_query

    @staticmethod
    def format_context_for_llm(result: RetrievalResult) -> str:
        """
        Format retrieved chunks into a structured context block for LLM injection.

        The block is designed to be inserted into the conversation history
        before the actual customer query, so the LLM treats it as authoritative
        banking knowledge rather than a user message.
        """
        if not result.chunks:
            return ""

        lines = [
            "[BANKING KNOWLEDGE — For factual banking questions, answer ONLY from "
            "this context. If the answer is not here, say staff confirmation is required.]",
        ]
        for i, chunk in enumerate(result.chunks, 1):
            doc_type = chunk.metadata.get("doc_type", "reference")
            product  = chunk.metadata.get("product", "")
            label    = f"{doc_type}" + (f" | {product}" if product else "")
            lines.append(f"\n[Source {i} — {label}]")
            lines.append(chunk.text)

        lines.append("\n[END OF BANKING KNOWLEDGE]")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Synchronous internals (run in thread pool via asyncio.to_thread)
    # ------------------------------------------------------------------

    def _get_embeddings_sync(self, texts: str | List[str], is_document: bool = False) -> List[float] | List[List[float]]:
        """
        Fetch embeddings from Google Gemini API.
        """
        task_type = "retrieval_document" if is_document else "retrieval_query"
        try:
            if isinstance(texts, str):
                res = genai.embed_content(
                    model=_GEMINI_EMBED_MODEL,
                    content=texts,
                    task_type=task_type,
                )
                return res['embedding']
            
            # Batch of texts
            all_embeddings = []
            batch_size = 50
            for i in range(0, len(texts), batch_size):
                batch = texts[i:i+batch_size]
                res = genai.embed_content(
                    model=_GEMINI_EMBED_MODEL,
                    content=batch,
                    task_type=task_type,
                )
                all_embeddings.extend(res['embedding'])
            return all_embeddings
        except Exception as exc:
            logger.error("Gemini embedding API call failed: %s", exc)
            raise exc

    def _init_sync(self) -> None:
        """
        Configure Gemini API and connect to ChromaDB.
        Runs in a thread pool so it does not block the event loop.
        """
        if not _CHROMA_AVAILABLE or not _ST_AVAILABLE:
            logger.warning("RAG skipping init — required packages not installed")
            return

        from config import settings
        logger.info("Configuring RAG Gemini embedding model: %s", _GEMINI_EMBED_MODEL)
        genai.configure(api_key=settings.GEMINI_API_KEY)

        # Connect to ChromaDB (creates DB if it does not exist)
        _CHROMA_PATH.mkdir(parents=True, exist_ok=True)
        self._chroma_client = chromadb.PersistentClient(
            path=str(_CHROMA_PATH),
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        self._collection = self._chroma_client.get_or_create_collection(
            name=_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},   # cosine similarity for semantic search
        )

        # Auto-ingest if collection is empty (first run)
        existing_count = self._collection.count()
        if existing_count == 0:
            logger.info("ChromaDB collection empty — running initial knowledge base ingestion")
            self._ingest_sync(_KB_PATH)
        else:
            logger.info(
                "ChromaDB collection loaded: %d chunks in '%s'",
                existing_count, _COLLECTION_NAME,
            )
            # Rebuild BM25 index from existing ChromaDB data
            self._rebuild_bm25_index()

    def _ingest_sync(self, kb_path: Path) -> int:
        """
        Walk the knowledge base directory, chunk all markdown files,
        embed them, and store in ChromaDB + rebuild BM25 index.

        Skips files that have already been ingested (by chunk_id comparison).
        """
        if not _CHROMA_AVAILABLE or not _ST_AVAILABLE or self._collection is None:
            return 0

        md_files = list(kb_path.rglob("*.md"))
        if not md_files:
            logger.warning("No .md files found in knowledge base at %s", kb_path)
            return 0

        all_ids:      List[str]        = []
        all_texts:    List[str]        = []
        all_metadata: List[Dict]       = []

        for md_file in md_files:
            try:
                raw = md_file.read_text(encoding="utf-8")
                metadata, body = _parse_yaml_frontmatter(raw)

                # Ensure essential metadata fields have defaults
                metadata.setdefault("intent",   "general")
                metadata.setdefault("product",  "general")
                metadata.setdefault("doc_type", "reference")
                metadata.setdefault("language", "hi")
                metadata.setdefault("source_file", md_file.name)

                doc_type = metadata["doc_type"]
                chunks   = _chunk_by_doc_type(body, doc_type, md_file.name)

                for idx, chunk_text in enumerate(chunks):
                    chunk_id = f"{md_file.stem}__chunk_{idx}"
                    chunk_meta = {**metadata, "chunk_index": idx}
                    # ChromaDB metadata values must be str/int/float/bool
                    chunk_meta = {
                        k: str(v) if not isinstance(v, (str, int, float, bool)) else v
                        for k, v in chunk_meta.items()
                    }
                    all_ids.append(chunk_id)
                    all_texts.append(chunk_text)
                    all_metadata.append(chunk_meta)

                logger.debug("Chunked %s → %d chunks", md_file.name, len(chunks))

            except Exception as exc:
                logger.error("Failed to process %s: %s", md_file, exc)

        if not all_ids:
            return 0

        # Get existing IDs to avoid duplicate ingestion
        existing = set(self._collection.get(ids=all_ids)["ids"])
        new_ids   = [i for i in all_ids if i not in existing]
        new_texts = [t for i, t in zip(all_ids, all_texts)  if i not in existing]
        new_meta  = [m for i, m in zip(all_ids, all_metadata) if i not in existing]

        if not new_ids:
            logger.info("All %d chunks already in ChromaDB — skipping ingestion", len(all_ids))
            self._rebuild_bm25_index()
            return 0

        # Embed all new chunks in one batch via Gemini API
        logger.info("Embedding %d new chunks via Gemini API...", len(new_ids))
        embeddings = self._get_embeddings_sync(new_texts, is_document=True)

        # Store in ChromaDB in batches of 100 (ChromaDB recommendation)
        batch_size = 100
        for start in range(0, len(new_ids), batch_size):
            end = start + batch_size
            self._collection.add(
                ids=new_ids[start:end],
                embeddings=embeddings[start:end],
                documents=new_texts[start:end],
                metadatas=new_meta[start:end],
            )

        logger.info(
            "Ingested %d new chunks into ChromaDB collection '%s'",
            len(new_ids), _COLLECTION_NAME,
        )

        # Rebuild BM25 index to include newly ingested documents
        self._rebuild_bm25_index()
        return len(new_ids)

    def _rebuild_bm25_index(self) -> None:
        """
        Build an in-memory BM25 index from all documents in ChromaDB.

        BM25 handles exact banking term matching that dense embeddings may miss —
        abbreviations like CIBIL, PMJDY, NEFT, scheme names, product codes.
        """
        if not _BM25_AVAILABLE or self._collection is None:
            return

        try:
            all_docs = self._collection.get(include=["documents", "metadatas"])
            self._bm25_docs  = all_docs["documents"] or []
            self._bm25_ids   = all_docs["ids"]        or []
            self._bm25_meta  = all_docs["metadatas"]  or []

            # Tokenise by whitespace + lowercase for BM25
            tokenised = [doc.lower().split() for doc in self._bm25_docs]
            self._bm25_index = BM25Okapi(tokenised)
            logger.info("BM25 index built: %d documents", len(self._bm25_docs))
        except Exception as exc:
            logger.warning("BM25 index build failed: %s", exc)
            self._bm25_index = None

    def _retrieve_sync(
        self,
        query: str,
        intent: str,
        product: Optional[str],
        top_k: int,
    ) -> RetrievalResult:
        """
        Execute hybrid retrieval: dense + BM25 → RRF merge → rerank → top-k.

        Steps:
            1. Build metadata filter for ChromaDB (narrows search space before ANN)
            2. Dense vector retrieval on filtered collection
            3. BM25 keyword retrieval on full index (metadata filter applied post-hoc)
            4. Reciprocal Rank Fusion to merge both ranked lists
            5. Return top_k chunks
        """
        assert self._collection is not None, "collection not initialised"

        # --- Step 1: Build ChromaDB metadata filter ----------------------------
        # where clause narrows the HNSW ANN search to relevant intent/product docs
        where_clause = self._build_where_clause(intent, product)

        # --- Step 2: Dense vector retrieval ------------------------------------
        query_embedding = self._get_embeddings_sync(query, is_document=False)

        dense_results: Dict[str, float] = {}   # chunk_id → similarity score
        dense_texts:   Dict[str, str]   = {}
        dense_meta:    Dict[str, Dict]  = {}

        try:
            n_dense = min(_DENSE_CANDIDATES, self._collection.count())
            chroma_response = self._collection.query(
                query_embeddings=[query_embedding],
                n_results=max(1, n_dense),
                where=where_clause if where_clause else None,
                include=["documents", "metadatas", "distances"],
            )
            if chroma_response["ids"] and chroma_response["ids"][0]:
                for cid, doc, meta, dist in zip(
                    chroma_response["ids"][0],
                    chroma_response["documents"][0],
                    chroma_response["metadatas"][0],
                    chroma_response["distances"][0],
                ):
                    # ChromaDB returns cosine distance (0 = identical); convert to similarity
                    similarity = max(0.0, 1.0 - dist)
                    dense_results[cid] = similarity
                    dense_texts[cid]   = doc
                    dense_meta[cid]    = meta
        except Exception as exc:
            logger.warning("Dense retrieval failed: %s", exc)

        # --- Step 3: BM25 keyword retrieval ------------------------------------
        bm25_results: Dict[str, float] = {}   # chunk_id → BM25 score

        if self._bm25_index is not None and self._bm25_docs:
            tokenised_query = query.lower().split()
            bm25_scores = self._bm25_index.get_scores(tokenised_query)

            # Sort by score descending; apply metadata filter post-hoc
            scored = sorted(enumerate(bm25_scores), key=lambda x: x[1], reverse=True)
            count = 0
            for doc_idx, score in scored:
                if count >= _BM25_CANDIDATES:
                    break
                if score < 0.01:     # skip zero/noise scores
                    continue
                cid  = self._bm25_ids[doc_idx]
                meta = self._bm25_meta[doc_idx]

                # Apply same intent filter as dense retrieval
                if intent and intent != "general":
                    doc_intent = meta.get("intent", "general")
                    if doc_intent not in (intent, "general"):
                        continue

                bm25_results[cid] = float(score)
                if cid not in dense_texts:
                    dense_texts[cid] = self._bm25_docs[doc_idx]
                    dense_meta[cid]  = meta
                count += 1

        # --- Step 4: Reciprocal Rank Fusion ------------------------------------
        # RRF score = sum(1 / (rank + k)) across both lists
        # Standard k=60 reduces sensitivity to very high-ranked outliers
        rrf_scores: Dict[str, float] = {}

        for rank, cid in enumerate(
            sorted(dense_results, key=dense_results.get, reverse=True)
        ):
            rrf_scores[cid] = rrf_scores.get(cid, 0.0) + 1.0 / (rank + _RRF_K)

        for rank, cid in enumerate(
            sorted(bm25_results, key=bm25_results.get, reverse=True)
        ):
            rrf_scores[cid] = rrf_scores.get(cid, 0.0) + 1.0 / (rank + _RRF_K)

        # Keep top candidates
        candidates = sorted(rrf_scores, key=rrf_scores.get, reverse=True)[:top_k * 3]

        if not candidates:
            return RetrievalResult(
                chunks=[], query_used=query,
                retrieval_source="fallback", total_retrieved=0,
            )

        final_scores: Dict[str, float] = {}
        for cid in candidates:
            final_scores[cid] = rrf_scores[cid]

        # --- Step 6: Assemble final result -------------------------------------
        ranked = sorted(final_scores, key=final_scores.get, reverse=True)
        retrieved_chunks: List[RetrievedChunk] = []

        for cid in ranked[:top_k]:
            score = final_scores[cid]
            # Since RRF scores are normalized rank sums and not semantic confidence,
            # we do not apply _MIN_SCORE_THRESHOLD to RRF candidates so we don't drop
            # relevant grounding results.
            retrieved_chunks.append(RetrievedChunk(
                chunk_id=cid,
                text=dense_texts.get(cid, ""),
                metadata=dense_meta.get(cid, {}),
                score=score,
            ))

        # Determine retrieval source label for observability
        if dense_results and bm25_results:
            source = "hybrid"
        elif dense_results:
            source = "dense_only"
        elif bm25_results:
            source = "bm25_only"
        else:
            source = "fallback"

        logger.info(
            "RAG retrieved %d chunks | intent=%s | source=%s | top_score=%.3f",
            len(retrieved_chunks), intent, source,
            retrieved_chunks[0].score if retrieved_chunks else 0.0,
        )

        return RetrievalResult(
            chunks=retrieved_chunks,
            query_used=query,
            retrieval_source=source,
            total_retrieved=len(rrf_scores),
        )
    @staticmethod
    def _build_where_clause(intent: str, product: Optional[str]) -> Optional[Dict]:
        """
        Build a ChromaDB metadata filter to narrow the ANN search space.

        Strategy:
            - If intent is specific: match intent OR general docs (compliance, glossary)
            - If product is known: additionally filter by product OR general product docs
            - For 'general' intent: no filter (search everything)

        ChromaDB where syntax: {"$or": [...]} for multi-value matching.
        """
        if not intent or intent == "general":
            return None   # no filter — search full collection

        # Match documents for this specific intent plus always-relevant general docs
        where: Dict = {
            "$or": [
                {"intent": {"$eq": intent}},
                {"intent": {"$eq": "general"}},
            ]
        }

        return where


# ---------------------------------------------------------------------------
# Module-level singleton — import this everywhere
# ---------------------------------------------------------------------------

rag_service = RAGService()
