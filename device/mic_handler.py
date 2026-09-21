"""
device/mic_handler.py

Push-to-talk microphone input.

Two ways to trigger, used interchangeably:
  • Keyboard (needs an interactive tty): SPACE = ask, C = photo.
  • Physical push buttons (GPIO, work headless too): mic button = ask,
    camera button = photo. Each is a momentary single-click button.

"Ask" toggles recording: first trigger starts, the next stops & sends. The clip
is encoded as WAV and published to the MQTT audio topic. Speech-to-text happens
server-side (Gemini) — the Pi no longer runs Whisper.
"""

import asyncio
import base64
import io
import queue
import select
import sys
import termios
import time
import tty

import numpy as np
import sounddevice as sd
import soundfile as sf
from loguru import logger


class MicHandler:
    def __init__(self, cfg, mqtt_client, session, camera_handler=None, buttons_cfg=None):
        self.cfg = cfg
        self.buttons_cfg = buttons_cfg
        self.mqtt = mqtt_client
        self.session = session
        self.camera = camera_handler

        self._sample_rate = cfg.sample_rate
        self._tx_sample_rate = getattr(cfg, "transmit_sample_rate", cfg.sample_rate)
        self._device_index = cfg.input_device_index
        self._silence_threshold = cfg.silence_threshold
        self._silence_duration_ms = cfg.silence_duration_ms
        self._silence_samples = int(self._sample_rate * self._silence_duration_ms / 1000)
        self._max_clip_samples = int(self._sample_rate * cfg.max_clip_seconds)

        # Button presses arrive on a background thread (gpiozero); funnel them
        # into this queue so the blocking listen loop can consume them alongside
        # keyboard input. Items are the action strings "ask" / "photo".
        self._events: "queue.Queue[str]" = queue.Queue()
        self._buttons = []  # keep refs alive so gpiozero doesn't GC them

    async def listen_loop(self):
        """
        Async loop that runs the mic in a thread executor.
        Push-to-talk: SPACE (or the mic button) starts/stops each recording.
        """
        self._setup_buttons()
        has_tty = sys.stdin.isatty()

        if not has_tty and not self._buttons:
            logger.warning(
                "Mic push-to-talk needs an interactive terminal (stdin is not a "
                "tty) or a physical button, and neither is available — mic "
                "disabled. Run `python agent.py` directly in your SSH session to "
                "use the keyboard, or wire up the GPIO buttons."
            )
            return

        logger.info(
            f"Mic handler started (device={self._device_index}, push-to-talk)"
        )
        if has_tty:
            logger.info("🎙  SPACE = ask a question (SPACE again to stop & send)")
            if self.camera is not None:
                logger.info("📷  C = take a photo (vision)")
        if self._buttons:
            logger.info("🔘  Mic button = ask (press again to stop), Camera button = photo")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._blocking_listen_loop, has_tty)

    def _setup_buttons(self):
        """Wire up the two GPIO push buttons, if enabled and available.

        gpiozero fires `when_pressed` on its own background thread; the callback
        just drops an action onto the shared event queue. Gracefully no-ops on
        hardware without GPIO (e.g. a dev laptop) so the keyboard still works.
        """
        cfg = self.buttons_cfg
        if cfg is None or not getattr(cfg, "enabled", False):
            return

        try:
            from gpiozero import Button
        except Exception as e:
            logger.warning(f"gpiozero unavailable — physical buttons disabled ({e})")
            return

        try:
            mic_btn = Button(
                cfg.mic_pin, pull_up=cfg.pull_up, bounce_time=cfg.bounce_time
            )
            mic_btn.when_pressed = lambda: self._events.put("ask")
            self._buttons.append(mic_btn)

            if self.camera is not None:
                cam_btn = Button(
                    cfg.camera_pin, pull_up=cfg.pull_up, bounce_time=cfg.bounce_time
                )
                cam_btn.when_pressed = lambda: self._events.put("photo")
                self._buttons.append(cam_btn)

            logger.info(
                f"Buttons ready (ask=GPIO{cfg.mic_pin}"
                + (f", photo=GPIO{cfg.camera_pin}" if self.camera is not None else "")
                + ")"
            )
        except Exception as e:
            logger.error(f"Button setup failed — physical buttons disabled: {e}")
            self._buttons = []

    def _blocking_listen_loop(self, has_tty: bool):
        """Blocking push-to-talk loop — runs in a thread executor.

        With a tty, puts the terminal in cbreak mode so single SPACE presses are
        read without Enter (Ctrl+C still works). Triggers come from the keyboard
        and/or the GPIO buttons, merged via `_wait_for_trigger`.
        """
        block_size = int(self._sample_rate * 0.1)  # 100ms blocks
        fd = sys.stdin.fileno()
        old_attrs = termios.tcgetattr(fd) if has_tty else None
        try:
            if has_tty:
                tty.setcbreak(fd)
            while True:
                # Idle until the user presses a trigger (key or button).
                action = self._wait_for_trigger(has_tty)
                if action == "photo":
                    if self.camera is not None:
                        logger.info("📷 Capturing photo for vision...")
                        self.camera.capture_and_publish()
                    else:
                        logger.warning("No camera configured — ignoring photo trigger")
                    continue

                # action == "ask" → record an audio question
                logger.info("● Recording... (press SPACE / mic button to stop)")

                audio_buf: list[np.ndarray] = []
                # Open the stream fresh per clip so the buffer doesn't overflow
                # while idle between recordings.
                with sd.InputStream(
                    samplerate=self._sample_rate,
                    channels=1,
                    dtype="float32",
                    device=self._device_index,
                    blocksize=block_size,
                ) as stream:
                    while True:
                        block, _ = stream.read(block_size)
                        audio_buf.append(block.copy())

                        if self._stop_requested(has_tty):
                            break
                        if len(audio_buf) * block_size >= self._max_clip_samples:
                            logger.warning(
                                f"Hit max clip length "
                                f"({self._max_clip_samples / self._sample_rate:.0f}s), "
                                f"sending."
                            )
                            break

                audio = np.concatenate(audio_buf, axis=0).flatten()
                logger.info("■ Stopped — sending audio for transcription")
                self._encode_and_publish(audio)
        finally:
            if old_attrs is not None:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)

    def _wait_for_trigger(self, has_tty: bool) -> str:
        """Block until an "ask" or "photo" trigger arrives from either a button
        or the keyboard (SPACE = ask, C = photo). Returns the action string."""
        while True:
            # Physical buttons (drained first; gpiozero fills this from a thread).
            try:
                return self._events.get_nowait()
            except queue.Empty:
                pass

            # Keyboard, only if we have an interactive terminal.
            if has_tty:
                ready, _, _ = select.select([sys.stdin], [], [], 0.1)
                if ready:
                    ch = sys.stdin.read(1).lower()
                    if ch == " ":
                        return "ask"
                    if ch == "c":
                        return "photo"
            else:
                time.sleep(0.05)

    def _stop_requested(self, has_tty: bool) -> bool:
        """Non-blocking check: True if the user pressed SPACE or the mic button
        to stop the current recording. A photo trigger seen mid-recording is
        re-queued so it fires once recording ends rather than being lost."""
        try:
            ev = self._events.get_nowait()
            if ev == "ask":
                return True
            self._events.put(ev)  # e.g. "photo" — handle it after we stop
        except queue.Empty:
            pass

        if has_tty:
            ready, _, _ = select.select([sys.stdin], [], [], 0)
            if ready and sys.stdin.read(1) == " ":
                return True
        return False

    def _downsample(self, audio: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
        """Reduce the sample rate before transmit so the base64 WAV stays under
        the broker payload limit. STT does not benefit from rates above ~16k.

        For integer factors (e.g. 48k→16k) average each group of samples — a
        cheap anti-alias — then decimate; otherwise fall back to linear interp.
        """
        if dst_sr >= src_sr:
            return audio
        if src_sr % dst_sr == 0:
            factor = src_sr // dst_sr
            n = (len(audio) // factor) * factor
            return audio[:n].reshape(-1, factor).mean(axis=1).astype(np.float32)
        n_dst = int(round(len(audio) * dst_sr / src_sr))
        x_src = np.arange(len(audio))
        x_dst = np.linspace(0, len(audio) - 1, num=n_dst)
        return np.interp(x_dst, x_src, audio).astype(np.float32)

    def _encode_and_publish(self, audio: np.ndarray):
        """Encode the recorded clip as 16-bit PCM WAV and publish it for
        server-side transcription. STT + RAG happen on the server."""
        duration = len(audio) / self._sample_rate
        # Skip blips too short to contain speech.
        if duration < 0.3:
            return

        # Downsample (e.g. 48k→16k) so the payload fits the broker limit.
        audio = self._downsample(audio, self._sample_rate, self._tx_sample_rate)
        tx_sr = min(self._tx_sample_rate, self._sample_rate)

        logger.info(
            f"Captured {duration:.1f}s of audio — publishing for STT "
            f"(@{tx_sr} Hz)"
        )
        try:
            buf = io.BytesIO()
            sf.write(buf, audio, tx_sr, format="WAV", subtype="PCM_16")
            audio_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        except Exception as e:
            logger.error(f"Audio encoding error: {e}")
            return

        kb = len(audio_b64) / 1024
        logger.info(f"Audio payload {kb:.0f} KB")
        self.mqtt.publish_audio_query(audio_b64, mime_type="audio/wav")
