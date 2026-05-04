"""Audio recording, Opus encoding, and playback for the GUI client."""

from __future__ import annotations

import queue
import threading
from typing import Callable, Optional

import numpy as np

try:
    import sounddevice as sd
except ImportError:
    sd = None

try:
    from opuslib import Encoder, Decoder
except ImportError:
    Encoder = None
    Decoder = None


class AudioEngine:
    """Handles audio capture, Opus encoding, and playback.

    Protocol expects:
      - Sample rate: 16000 Hz
      - Channels: mono
      - Opus frame: 20ms = 320 samples per frame
      - Server sends Opus; client decodes to PCM for playback
    """

    ENCODER_APPLICATION = 2048  # opuslib.APPLICATION_VOIP

    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        frame_size: int = 320,
        use_opus: bool = True,
    ):
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_size = frame_size
        self.use_opus = use_opus

        self._opus_encoder: Optional[Encoder] = None
        self._opus_decoder: Optional[Decoder] = None
        self._input_stream: Optional[sd.InputStream] = None
        self._output_stream: Optional[sd.OutputStream] = None
        self._playback_queue: queue.Queue[bytes] = queue.Queue()
        self._playback_thread: Optional[threading.Thread] = None
        self._running = False
        self._recording = False
        self._on_frame: Optional[Callable[[bytes], None]] = None

        if self.use_opus:
            if Encoder is not None:
                self._opus_encoder = Encoder(
                    self.sample_rate,
                    self.channels,
                    self.ENCODER_APPLICATION,
                )
            if Decoder is not None:
                self._opus_decoder = Decoder(
                    self.sample_rate,
                    self.channels,
                )

    def set_frame_callback(self, callback: Callable[[bytes], None]) -> None:
        """Set callback invoked for each captured audio frame."""
        self._on_frame = callback

    @property
    def state(self) -> str:
        return "recording" if self._recording else "idle"

    def start_recording(self) -> None:
        """Start capturing audio from the microphone."""
        if self._recording:
            return
        if sd is None:
            raise RuntimeError("sounddevice not installed")
        self._running = True
        self._input_stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="int16",
            blocksize=self.frame_size,
            callback=self._on_audio_frame,
        )
        self._input_stream.start()
        self._recording = True

    def stop_recording(self) -> None:
        """Stop capturing audio."""
        if not self._recording:
            return
        self._recording = False
        if self._input_stream:
            self._input_stream.stop()
            self._input_stream.close()
            self._input_stream = None

    def _on_audio_frame(
        self,
        indata: np.ndarray,
        frames: int,
        status: sd.CallbackFlags,
        userdata: None,
    ) -> None:
        """Called by sounddevice for each audio block."""
        if status:
            return
        pcm_data = indata.tobytes()
        if self._on_frame is None:
            return

        if self.use_opus and self._opus_encoder is not None:
            encoded = self._opus_encoder.encode(pcm_data, frames)
            self._on_frame(encoded)
        else:
            self._on_frame(pcm_data)

    def enqueue_audio(self, audio_data: bytes) -> None:
        """Enqueue audio data for playback (called from WS thread)."""
        self._playback_queue.put(audio_data)
        self._ensure_playback_thread()

    def _ensure_playback_thread(self) -> None:
        """Start playback thread if not running."""
        if self._playback_thread is not None and self._playback_thread.is_alive():
            return
        self._playback_thread = threading.Thread(target=self._playback_loop, daemon=True)
        self._playback_thread.start()

    def _playback_loop(self) -> None:
        """Drain the playback queue by streaming to sounddevice."""
        if sd is None:
            return
        try:
            with sd.OutputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                blocksize=self.frame_size,
            ) as stream:
                while True:
                    try:
                        data = self._playback_queue.get(timeout=0.5)
                    except queue.Empty:
                        if not self._recording:
                            break
                        continue

                    pcm = self._decode_audio(data)
                    arr = np.frombuffer(pcm, dtype=np.int16).copy()
                    stream.write(arr)
        except Exception:
            pass

    def _decode_audio(self, data: bytes) -> bytes:
        """Decode Opus to PCM. Falls back to raw PCM if decoding fails."""
        if not self.use_opus or not data:
            return data
        if self._opus_decoder is None:
            return data
        try:
            return self._opus_decoder.decode(data, self.frame_size)
        except Exception:
            return data

    def stop_playback(self) -> None:
        """Clear the playback queue and stop current playback."""
        cleared = 0
        while not self._playback_queue.empty():
            try:
                self._playback_queue.get_nowait()
                cleared += 1
            except queue.Empty:
                break
        if cleared > 0:
            pass

    def close(self) -> None:
        """Release all audio resources."""
        self.stop_recording()
        self._running = False
        self.stop_playback()
