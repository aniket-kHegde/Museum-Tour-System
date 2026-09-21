"""
device/camera_handler.py

Captures a JPEG photo using the Raspberry Pi Camera (picamera2).
Resizes and base64-encodes it, then publishes via MQTT.

Falls back gracefully on non-Pi hardware (e.g. dev machines)
by capturing from the system webcam using OpenCV, or
generating a placeholder image if no camera is available.
"""

import base64
import io
import os

from loguru import logger
from PIL import Image


class CameraHandler:
    def __init__(self, cfg, mqtt_client, session):
        self.cfg = cfg
        self.mqtt = mqtt_client
        self.session = session

        self._width, self._height = cfg.resolution
        self._quality = cfg.jpeg_quality
        self._max_bytes = cfg.max_payload_kb * 1024

        self._backend = self._detect_backend()

    def _detect_backend(self) -> str:
        # Try picamera2 first (Raspberry Pi)
        try:
            import picamera2  # noqa: F401
            logger.info("Camera backend: picamera2 (Raspberry Pi)")
            return "picamera2"
        except ImportError:
            pass

        # Try OpenCV (USB webcam / laptop camera)
        try:
            import cv2
            cap = cv2.VideoCapture(0)
            if cap.isOpened():
                cap.release()
                logger.info("Camera backend: OpenCV (USB/webcam)")
                return "opencv"
        except ImportError:
            pass

        logger.warning("No camera found — will use placeholder images")
        return "placeholder"

    def capture_and_publish(self):
        """Capture a photo and publish it to the MQTT camera topic."""
        logger.info("Capturing photo...")
        try:
            image_b64 = self._capture()
            self.mqtt.publish_camera_capture(image_b64)
            logger.info(f"Camera capture published ({len(image_b64)} chars b64)")
        except Exception as e:
            logger.error(f"Camera capture failed: {e}")

    def _capture(self) -> str:
        if self._backend == "picamera2":
            return self._capture_picamera2()
        elif self._backend == "opencv":
            return self._capture_opencv()
        else:
            return self._placeholder()

    def _capture_picamera2(self) -> str:
        from picamera2 import Picamera2
        cam = Picamera2()
        config = cam.create_still_configuration(
            main={"size": (self._width, self._height)}
        )
        cam.configure(config)
        cam.start()
        buf = io.BytesIO()
        cam.capture_file(buf, format="jpeg")
        cam.stop()
        cam.close()
        return self._encode_and_resize(buf.getvalue())

    def _capture_opencv(self) -> str:
        import cv2
        cap = cv2.VideoCapture(0)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._height)
        ret, frame = cap.read()
        cap.release()
        if not ret:
            raise RuntimeError("OpenCV frame capture failed")
        _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self._quality])
        return self._encode_and_resize(buf.tobytes())

    def _placeholder(self) -> str:
        """Generate a simple placeholder JPEG for testing."""
        img = Image.new("RGB", (self._width, self._height), color=(180, 180, 200))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=self._quality)
        return base64.b64encode(buf.getvalue()).decode("utf-8")

    def _encode_and_resize(self, jpeg_bytes: bytes) -> str:
        """Resize if over max payload size, then base64-encode."""
        if len(jpeg_bytes) > self._max_bytes:
            img = Image.open(io.BytesIO(jpeg_bytes))
            scale = (self._max_bytes / len(jpeg_bytes)) ** 0.5
            new_w = int(img.width * scale)
            new_h = int(img.height * scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=self._quality)
            jpeg_bytes = buf.getvalue()
            logger.debug(f"Resized image to {new_w}×{new_h}, {len(jpeg_bytes)} bytes")

        return base64.b64encode(jpeg_bytes).decode("utf-8")
