"""
services/vision_service/main.py

Subscribes to camera capture events.
Sends the image to a vision-capable LLM (Claude or GPT-4o).
Injects the current exhibit context so the LLM can give a relevant description.
Publishes a spoken description back to the device.
"""

import base64
import io
import os
import sys

import anthropic
from dotenv import load_dotenv
from loguru import logger
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.mqtt_subscriber import MQTTSubscriberService
from shared.redis_client import get_device_session
from shared.metrics import timed

load_dotenv()

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic")
VISION_MODEL = os.getenv("VISION_MODEL", "claude-opus-4-5")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
MAX_IMAGE_PX = 1024      # resize longest side to this before sending
MAX_ANSWER_TOKENS = 160

VISION_PROMPT = """\
You are a helpful guide at a memorial museum devoted to Dr. B.R. Ambedkar.
The visitor is standing near an exhibit and has taken a photo of something.
Look at the image and describe what you see in 2-3 sentences.
If the image shows a book, document, panel, or text, describe what is visible.
If it shows a statue, monument, photograph, or object near the exhibit, describe that.
Keep the description engaging and suitable for spoken audio — no bullet points or lists.
Connect what you see to the exhibit the visitor is standing at where that is natural,
but do not invent historical detail that is not in the image or the context.
"""

CONTEXT_PROMPT = """\
The visitor is currently at the exhibit: "{title}" — {author} ({year}).
They have taken a photo and want to know what they are looking at.
"""


class VisionService(MQTTSubscriberService):
    """
    Handles camera capture events from museum devices.
    Topic: museum/+/device/+/cam
    """

    TOPICS = ["museum/+/device/+/cam"]

    def __init__(self):
        super().__init__()
        if LLM_PROVIDER == "anthropic":
            self.llm = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        elif LLM_PROVIDER == "gemini":
            from google import genai
            self.llm = genai.Client(api_key=GEMINI_API_KEY)
        else:
            import openai
            self.llm = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        logger.info(f"Vision Service ready. Model: {LLM_PROVIDER}/{VISION_MODEL}")

    def handle_message(self, topic: str, payload: dict) -> dict | None:
        device_id = payload.get("device_id")
        museum_id = payload.get("museum_id")
        image_b64 = payload.get("image_b64")

        if not image_b64:
            return None

        logger.info(f"Camera capture from device={device_id}")

        timings: dict = {}
        meta: dict = {"img_bytes_in": len(image_b64)}

        # Get exhibit context from session (written by beacon_service)
        session = get_device_session(device_id) or {}
        exhibit_title = session.get("exhibit_title") or "an exhibit"
        exhibit_author = session.get("exhibit_author") or "attribution unknown"
        exhibit_year = session.get("exhibit_year") or "year unknown"

        # Resize image if needed to keep payload reasonable
        try:
            with timed(timings, "resize_ms"):
                image_b64_processed = self._resize_image(image_b64)
        except Exception as e:
            logger.warning(f"Image resize failed: {e}, using original")
            image_b64_processed = image_b64
        meta["img_bytes_out"] = len(image_b64_processed)

        try:
            with timed(timings, "vision_llm_ms"):
                description = self._describe_image(
                    image_b64_processed, exhibit_title, exhibit_author, exhibit_year
                )
        except Exception as e:
            logger.exception(f"Vision LLM error: {e}")
            description = (
                "I was unable to analyse this photo. "
                "Please try taking another photo with better lighting."
            )
            meta["stage"] = "vision_error"

        meta["answer_chars"] = len(description)
        return {
            "response_type": "tts",
            "text": description,
            "exhibit_id": session.get("exhibit_id"),
            "_timings": timings,
            "_meta": meta,
        }

    def _resize_image(self, image_b64: str) -> str:
        """Resize image so longest side <= MAX_IMAGE_PX, return new base64."""
        raw = base64.b64decode(image_b64)
        img = Image.open(io.BytesIO(raw))

        w, h = img.size
        if max(w, h) > MAX_IMAGE_PX:
            scale = MAX_IMAGE_PX / max(w, h)
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=75)
        return base64.b64encode(buf.getvalue()).decode("utf-8")

    def _describe_image(
        self, image_b64: str, title: str, author: str, year: str
    ) -> str:
        context = CONTEXT_PROMPT.format(
            title=title, author=author, year=year or "unknown year"
        )
        full_prompt = VISION_PROMPT + "\n\n" + context

        if LLM_PROVIDER == "anthropic":
            response = self.llm.messages.create(
                model=VISION_MODEL,
                max_tokens=MAX_ANSWER_TOKENS,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": image_b64,
                                },
                            },
                            {"type": "text", "text": full_prompt},
                        ],
                    }
                ],
            )
            return response.content[0].text.strip()
        elif LLM_PROVIDER == "gemini":
            from google.genai import types as genai_types
            response = self.llm.models.generate_content(
                model=VISION_MODEL,
                contents=[
                    genai_types.Part.from_bytes(
                        data=base64.b64decode(image_b64),
                        mime_type="image/jpeg",
                    ),
                    full_prompt,
                ],
                config=genai_types.GenerateContentConfig(
                    max_output_tokens=MAX_ANSWER_TOKENS,
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                ),
            )
            return (response.text or "").strip()
        else:
            data_uri = f"data:image/jpeg;base64,{image_b64}"
            response = self.llm.chat.completions.create(
                model=VISION_MODEL,
                max_tokens=MAX_ANSWER_TOKENS,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": data_uri}},
                            {"type": "text", "text": full_prompt},
                        ],
                    }
                ],
            )
            return response.choices[0].message.content.strip()


if __name__ == "__main__":
    logger.info("Starting Vision Service")
    service = VisionService()
    service.run()
