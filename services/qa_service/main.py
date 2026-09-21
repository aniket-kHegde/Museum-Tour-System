"""
services/qa_service/main.py

Subscribes to voice query events.
Runs a RAG pipeline:
  1. Embed user question
  2. Search Qdrant for relevant exhibit chunks
  3. Send context + question to Claude / GPT-4o
  4. Publish grounded TTS answer back to device

The LLM is instructed to answer ONLY from retrieved exhibit content,
so answers are always grounded in the museum's actual knowledge base.
"""

import asyncio
import base64
import os
import sys
from typing import Optional

import anthropic
from dotenv import load_dotenv
from loguru import logger
from qdrant_client import QdrantClient
from qdrant_client.http.models import Filter, FieldCondition, MatchValue
from sentence_transformers import SentenceTransformer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.mqtt_subscriber import MQTTSubscriberService
from shared.redis_client import get_device_session, check_voice_rate_limit
from shared.metrics import timed, now as _perf

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", None)

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic")   # "anthropic", "gemini", or "openai"
LLM_MODEL = os.getenv("LLM_MODEL", "claude-sonnet-4-5")
STT_MODEL = os.getenv("STT_MODEL", LLM_MODEL)   # Gemini model used for audio->text
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""

# Used when the visitor has not triggered a beacon yet. Also compared against as
# a sentinel in _transcribe_audio, so it must be a single shared constant.
DEFAULT_EXHIBIT_TITLE = "this exhibit"

SIMILARITY_THRESHOLD = 0.35   # minimum cosine similarity to include a chunk
TOP_K = 3                      # number of chunks to retrieve
MAX_ANSWER_TOKENS = 180        # keep answers short for TTS

# The one sentence the guide falls back to. Defined once: the model is told to
# say it verbatim, and the no-results path returns it directly, so the visitor
# hears the same words whichever branch produced them.
NO_ANSWER_REPLY = (
    "That's a great question — I don't have that detail on hand, "
    "but one of our guides would love to help."
)

SYSTEM_PROMPT = f"""\
You are a knowledgeable guide at a memorial museum devoted to Dr. B.R. Ambedkar —
his writings, the Constitution he helped draft, and the memorials to his life.
Answer the visitor's question using ONLY the exhibit information provided in the context below.
Keep your answer to 2-4 sentences — it will be read aloud, so avoid lists or bullet points.
Use a warm, engaging tone as if giving a guided tour.
Treat the subject with the seriousness it deserves; do not editorialise or take political sides.
If the answer is not in the provided context, say exactly:
"{NO_ANSWER_REPLY}"
Do not mention that you are an AI or that you are searching a database.
Do not say "according to the context" or similar meta-phrases.
"""

# Shown (and spoken) when Gemini returns a 429 / quota error, so the visitor
# hears a useful "try again shortly" instead of a misleading audio error.
BUSY_MESSAGE = (
    "I'm getting a lot of questions right now. "
    "Please wait a few seconds and ask again."
)


def _is_rate_limit_error(e: Exception) -> bool:
    """True if a Gemini exception is a 429 / quota (RESOURCE_EXHAUSTED) error."""
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    if code == 429:
        return True
    s = str(e)
    return "RESOURCE_EXHAUSTED" in s or "429" in s


class QAService(MQTTSubscriberService):
    """
    Handles voice queries from museum devices.

    Accepts either a text transcript (museum/+/device/+/voice) or raw audio
    (museum/+/device/+/audio). Audio is transcribed server-side with Gemini's
    native audio input, then both paths run the same RAG pipeline.
    """

    TOPICS = ["museum/+/device/+/voice", "museum/+/device/+/audio"]

    def __init__(self):
        super().__init__()
        logger.info(f"Loading embedding model: {EMBEDDING_MODEL}")
        self.embedder = SentenceTransformer(EMBEDDING_MODEL)

        self.qdrant = QdrantClient(
            host=QDRANT_HOST,
            port=QDRANT_PORT,
            api_key=QDRANT_API_KEY,
        )

        # Per-museum recognition vocabulary (exhibit titles + attributions), built lazily
        # from Qdrant and reused to bias speech-to-text toward correct proper nouns.
        self._vocab_cache: dict[str, str] = {}

        if LLM_PROVIDER == "anthropic":
            self.llm = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        elif LLM_PROVIDER == "gemini":
            from google import genai
            self.llm = genai.Client(api_key=GEMINI_API_KEY)
        else:
            import openai
            self.llm = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        logger.info(f"QA Service ready. LLM: {LLM_PROVIDER}/{LLM_MODEL}")

    def handle_message(self, topic: str, payload: dict) -> dict | None:
        device_id = payload.get("device_id")
        museum_id = payload.get("museum_id")

        # Per-request instrumentation (attached to the response as _timings/_meta).
        timings: dict = {}
        meta: dict = {"input": "audio" if topic.endswith("/audio") else "text"}

        # Rate limit up front — before spending a Gemini call on transcription.
        if not check_voice_rate_limit(device_id):
            return {
                "response_type": "tts",
                "text": "Please wait a moment before asking another question.",
                "_meta": {"rate_limited": True, "stage": "voice_rate_limit"},
            }

        # Current exhibit context from session — used both to bias STT toward the
        # exhibit the visitor is standing near and to scope the RAG search.
        session = get_device_session(device_id) or {}
        exhibit_id = session.get("exhibit_id")
        exhibit_title = session.get("exhibit_title", DEFAULT_EXHIBIT_TITLE)

        # Acquire the transcript: from raw audio (server-side STT) or text payload.
        if topic.endswith("/audio"):
            audio_b64 = payload.get("audio_b64")
            if not audio_b64:
                return None
            if LLM_PROVIDER != "gemini":
                logger.warning(
                    f"Audio query received but LLM_PROVIDER={LLM_PROVIDER} "
                    f"has no audio STT support"
                )
                return {
                    "response_type": "error",
                    "text": "Audio input isn't supported in this configuration.",
                }
            meta["audio_bytes"] = len(audio_b64)
            try:
                with timed(timings, "stt_ms"):
                    transcript = self._transcribe_audio(
                        audio_b64,
                        payload.get("mime_type", "audio/wav"),
                        self._vocab_hint(museum_id),
                        exhibit_title,
                    )
            except Exception as e:
                if _is_rate_limit_error(e):
                    logger.warning(f"STT rate-limited (429): {e}")
                    return {
                        "response_type": "tts", "text": BUSY_MESSAGE,
                        "_timings": timings,
                        "_meta": {**meta, "rate_limited": True, "stage": "stt"},
                    }
                logger.exception(
                    f"Audio transcription error ({type(e).__name__}): {e}"
                )
                return {
                    "response_type": "error",
                    "text": "I couldn't make out the audio. Please try again.",
                    "_timings": timings,
                    "_meta": {**meta, "stage": "stt_error"},
                }
        else:
            transcript = payload.get("transcript", "").strip()

        if not transcript:
            return None
        meta["query_chars"] = len(transcript)

        logger.info(
            f"Voice query | device={device_id} | exhibit={exhibit_id} | "
            f"query='{transcript[:60]}...'"
        )

        try:
            answer = self._rag_answer(
                transcript, museum_id, exhibit_id, exhibit_title, timings, meta
            )
        except Exception as e:
            if _is_rate_limit_error(e):
                logger.warning(f"RAG rate-limited (429): {e}")
                answer = BUSY_MESSAGE
                meta["rate_limited"] = True
                meta["stage"] = "rag"
            else:
                logger.exception(f"RAG pipeline error: {e}")
                answer = "I'm sorry, I had trouble finding an answer. Please try asking again."
                meta["stage"] = "rag_error"

        return {
            "response_type": "tts",
            "text": answer,
            "exhibit_id": exhibit_id,
            "query": transcript,
            "_timings": timings,
            "_meta": meta,
        }

    def _transcribe_audio(
        self,
        audio_b64: str,
        mime_type: str,
        vocab_hint: str = "",
        exhibit_title: str = DEFAULT_EXHIBIT_TITLE,
    ) -> str:
        """Transcribe a base64-encoded audio clip to text using Gemini's
        native audio input. Gemini-only (guarded by the caller).

        The prompt primes the model with the language, the current exhibit, and
        the museum's known exhibit names so proper nouns transcribe correctly
        (e.g. "Deekshabhoomi", "Chaitya Bhoomi", "Ambedkar", "Satyagraha").
        """
        from google.genai import types as genai_types

        instruction = (
            "You are transcribing a museum visitor's spoken question. "
            "The audio is in English, and may contain Indian proper nouns — "
            "names of people, places, books and Marathi or Pali terms. "
            "Return ONLY the verbatim transcription "
            "of what is said — no commentary, no translation, no quotation marks."
        )
        if vocab_hint:
            instruction += (
                "\n\nThe visitor may mention these exhibits — when you "
                f"hear one of these names, spell it exactly as written here:\n{vocab_hint}"
            )
        if exhibit_title and exhibit_title != DEFAULT_EXHIBIT_TITLE:
            instruction += f"\n\nThe visitor is currently standing near: {exhibit_title}."

        # Flash / Flash-Lite transcribe fine with thinking disabled (cheapest,
        # conserves free-tier quota); Pro requires a non-zero budget (min 128).
        thinking_budget = 128 if "pro" in STT_MODEL else 0
        response = self.llm.models.generate_content(
            model=STT_MODEL,
            contents=[
                genai_types.Part.from_bytes(
                    data=base64.b64decode(audio_b64),
                    mime_type=mime_type,
                ),
                instruction,
            ],
            config=genai_types.GenerateContentConfig(
                temperature=0.0,
                thinking_config=genai_types.ThinkingConfig(thinking_budget=thinking_budget),
            ),
        )

        # response.text can be None (or raise) when the response is empty/blocked;
        # surface why instead of letting it bubble up as a bare catch-all.
        transcript = (getattr(response, "text", None) or "").strip()
        if not transcript:
            feedback = getattr(response, "prompt_feedback", None)
            logger.warning(
                f"Empty transcription from {STT_MODEL}. prompt_feedback={feedback}"
            )
        else:
            logger.info(
                f"Transcribed audio ({len(transcript)} chars): {transcript[:80]}"
            )
        return transcript

    def _vocab_hint(self, museum_id: str) -> str:
        """Build (and cache) a comma-separated list of 'Title — Author' pairs for
        this museum's collection, used to bias STT toward correct proper nouns.

        Returns "" if the collection is empty/unreachable so transcription still
        runs without the hint.
        """
        if museum_id in self._vocab_cache:
            return self._vocab_cache[museum_id]

        hint = ""
        try:
            points, _ = self.qdrant.scroll(
                collection_name=f"{museum_id}_knowledge",
                with_payload=True,
                with_vectors=False,
                limit=500,
            )
            seen = []
            for p in points:
                payload = p.payload or {}
                title = (payload.get("title") or "").strip()
                author = (payload.get("author") or "").strip()
                if not title:
                    continue
                entry = f"{title} — {author}" if author else title
                if entry not in seen:
                    seen.append(entry)
            hint = "; ".join(seen)
            logger.info(f"STT vocab hint for {museum_id}: {len(seen)} titles")
        except Exception as e:
            logger.warning(f"Could not build STT vocab hint for {museum_id}: {e}")

        self._vocab_cache[museum_id] = hint
        return hint

    def _rag_answer(
        self,
        question: str,
        museum_id: str,
        exhibit_id: Optional[str],
        exhibit_title: str,
        timings: Optional[dict] = None,
        meta: Optional[dict] = None,
    ) -> str:
        timings = timings if timings is not None else {}
        meta = meta if meta is not None else {}

        # 1. Embed the question
        with timed(timings, "embed_ms"):
            query_vec = self.embedder.encode(question, convert_to_numpy=True).tolist()

        # 2. Build Qdrant filter — prefer current exhibit, fall back to whole museum
        collection = f"{museum_id}_knowledge"
        search_filter = None
        if exhibit_id:
            search_filter = Filter(
                must=[FieldCondition(key="exhibit_id", match=MatchValue(value=exhibit_id))]
            )

        with timed(timings, "search_ms"):
            results = self.qdrant.search(
                collection_name=collection,
                query_vector=query_vec,
                limit=TOP_K,
                score_threshold=SIMILARITY_THRESHOLD,
                query_filter=search_filter,
            )
        meta["retrieval_scope"] = "scoped" if exhibit_id else "museum"

        # If exhibit-scoped search returned nothing, search the whole museum
        if not results and exhibit_id:
            logger.info("No exhibit-scoped results; falling back to full museum search")
            meta["retrieval_scope"] = "fallback"
            with timed(timings, "search_fallback_ms"):
                results = self.qdrant.search(
                    collection_name=collection,
                    query_vector=query_vec,
                    limit=TOP_K,
                    score_threshold=SIMILARITY_THRESHOLD,
                )

        meta["n_results"] = len(results)
        meta["top_score"] = round(results[0].score, 4) if results else None

        if not results:
            meta["retrieval_scope"] = "none"
            return NO_ANSWER_REPLY

        # 3. Build context block from retrieved chunks
        context_parts = []
        for r in results:
            title = r.payload.get("title", "Unknown")
            author = r.payload.get("author", "Unknown")
            text = r.payload.get("text", "")
            context_parts.append(f"[{title} — {author}]\n{text}")

        context = "\n\n---\n\n".join(context_parts)

        user_message = (
            f"Exhibit the visitor is standing at: {exhibit_title}\n\n"
            f"Exhibit information:\n{context}\n\n"
            f"Visitor's question: {question}"
        )

        # 4. Call LLM
        _llm_t0 = _perf()
        if LLM_PROVIDER == "anthropic":
            response = self.llm.messages.create(
                model=LLM_MODEL,
                max_tokens=MAX_ANSWER_TOKENS,
                temperature=0.3,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
            answer = response.content[0].text.strip()
        elif LLM_PROVIDER == "gemini":
            from google.genai import types as genai_types
            response = self.llm.models.generate_content(
                model=LLM_MODEL,
                contents=user_message,
                config=genai_types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    max_output_tokens=MAX_ANSWER_TOKENS,
                    temperature=0.3,
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                ),
            )
            answer = (response.text or "").strip()
        else:
            response = self.llm.chat.completions.create(
                model=LLM_MODEL,
                max_tokens=MAX_ANSWER_TOKENS,
                temperature=0.3,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_message},
                ],
            )
            answer = response.choices[0].message.content.strip()

        timings["llm_ms"] = round((_perf() - _llm_t0) * 1000.0, 2)
        meta["answer_chars"] = len(answer)
        logger.info(f"LLM answer ({len(answer)} chars): {answer[:80]}...")
        return answer


if __name__ == "__main__":
    logger.info("Starting AI Q&A Service")
    service = QAService()
    service.run()
