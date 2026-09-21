"""
test_mic.py — record from the USB mic and report the audio level.

Verifies the mic captures sound before wiring it into the agent / STT.
Uses the same params as the device mic handler (mono, float32, 16 kHz).

Usage on the Pi (inside the project venv):
    python test_mic.py                # 5s, uses input_device_index from config
    python test_mic.py 4              # record 4 seconds
    python test_mic.py 4 2            # record 4s from device index 2 (overrides config)

Then play it back:  aplay /tmp/mic_test.wav
"""

import sys

import numpy as np
import sounddevice as sd
import soundfile as sf

from config import load_config


def main():
    cfg = load_config().audio
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
    device = int(sys.argv[2]) if len(sys.argv) > 2 else cfg.input_device_index
    sr = cfg.sample_rate

    print("=== available audio devices ===")
    print(sd.query_devices())
    print("===============================")
    print(f">> recording {seconds:.0f}s from device index {device} @ {sr} Hz — speak now!")

    try:
        audio = sd.rec(
            int(seconds * sr),
            samplerate=sr,
            channels=1,
            dtype="float32",
            device=device,
        )
        sd.wait()
    except Exception as e:
        print(f"!! recording failed: {e}")
        print(">> Pick a device above with 'in' > 0 and pass its index, e.g.:")
        print(">>     python test_mic.py 4 <index>")
        return

    audio = audio.flatten()
    rms = float(np.sqrt(np.mean(audio ** 2)))
    peak = float(np.max(np.abs(audio)))
    sf.write("/tmp/mic_test.wav", audio, sr, subtype="PCM_16")

    print(f">> done. RMS={rms:.4f}  peak={peak:.4f}  -> saved /tmp/mic_test.wav")
    if rms < 0.001:
        print("!! level near silence — wrong device index, muted, or gain too low")
    else:
        print(">> good signal. Play it back with:  aplay /tmp/mic_test.wav")


if __name__ == "__main__":
    main()
