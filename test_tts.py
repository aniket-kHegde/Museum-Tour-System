"""
test_tts.py — directly test the device TTSHandler (pyttsx3 / espeak).

No MQTT, no agent needed. Speaks a line and exits.

Usage on the Pi (inside the project venv):
    python test_tts.py
    python test_tts.py "any custom sentence to speak"
"""

import sys

from config import load_config
from tts_handler import TTSHandler


def main():
    text = sys.argv[1] if len(sys.argv) > 1 else "Hello, this is the device text to speech test."

    cfg = load_config()
    tts = TTSHandler(cfg.tts)

    print(f">> speaking: {text!r}")
    # Call the blocking path directly so the process doesn't exit before
    # the audio finishes (speak() runs in a daemon thread).
    tts._speak_blocking(text)
    print(">> done")


if __name__ == "__main__":
    main()
