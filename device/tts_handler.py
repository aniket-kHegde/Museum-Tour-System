"""
device/tts_handler.py

Text-to-speech output for the museum tour device.
Supports pyttsx3 (fully offline) and gTTS (requires internet, better voice).
Runs speech in a background thread so it doesn't block the MQTT loop.
"""

import io
import threading

from loguru import logger


class TTSHandler:
    def __init__(self, cfg):
        self.engine_name = cfg.engine   # "pyttsx3" or "gtts"
        self.rate = cfg.rate
        self.voice_id = cfg.voice_id
        self._lock = threading.Lock()
        self._engine = None

        if self.engine_name == "pyttsx3":
            self._init_pyttsx3()

    def _init_pyttsx3(self):
        try:
            import pyttsx3
            self._engine = pyttsx3.init()
            self._engine.setProperty("rate", self.rate)
            if self.voice_id:
                self._engine.setProperty("voice", self.voice_id)
            logger.info("TTS: pyttsx3 initialised")
        except Exception as e:
            logger.warning(f"pyttsx3 init failed: {e}. TTS will print to console.")
            self._engine = None

    def speak(self, text: str):
        """Speak text in a background thread. Returns immediately."""
        t = threading.Thread(target=self._speak_blocking, args=(text,), daemon=True)
        t.start()

    def _speak_blocking(self, text: str):
        with self._lock:   # prevent overlapping speech
            logger.info(f"TTS: '{text[:60]}...'")
            if self.engine_name == "pyttsx3" and self._engine:
                self._speak_pyttsx3(text)
            elif self.engine_name == "gtts":
                self._speak_gtts(text)
            else:
                # Fallback: just print (useful in simulator / headless dev)
                print(f"\n[SPEAK] {text}\n")

    def _speak_pyttsx3(self, text: str):
        try:
            self._engine.say(text)
            self._engine.runAndWait()
        except Exception as e:
            logger.error(f"pyttsx3 speak error: {e}")
            print(f"\n[SPEAK] {text}\n")

    def _speak_gtts(self, text: str):
        try:
            from gtts import gTTS
            import pygame

            tts = gTTS(text=text, lang="en", slow=False)
            buf = io.BytesIO()
            tts.write_to_fp(buf)
            buf.seek(0)

            pygame.mixer.init()
            pygame.mixer.music.load(buf)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                pygame.time.wait(100)
        except Exception as e:
            logger.error(f"gTTS speak error: {e}")
            print(f"\n[SPEAK] {text}\n")

    def stop(self):
        """Stop currently playing speech."""
        if self.engine_name == "pyttsx3" and self._engine:
            try:
                self._engine.stop()
            except Exception:
                pass
