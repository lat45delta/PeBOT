"""PCM en memoria: Discord 48 kHz estéreo ↔ Gemini 16/24 kHz mono."""
import audioop
from collections import deque
import queue
import threading

import discord

DISCORD_FRAME_BYTES = 3840  # 20 ms, 48 kHz, 16 bits, dos canales.
SILENCE = bytes(DISCORD_FRAME_BYTES)


class InputMixer:
    def __init__(self):
        self.users = {}
        self.state = None

    def feed(self, user_id, pcm):
        if not pcm or len(pcm) % DISCORD_FRAME_BYTES:
            return False
        if user_id not in self.users and len(self.users) >= 16:
            return False
        frames = self.users.setdefault(user_id, deque(maxlen=10))
        for offset in range(0, len(pcm), DISCORD_FRAME_BYTES):
            frames.append(pcm[offset:offset + DISCORD_FRAME_BYTES])
        return True

    def take(self):
        if not any(self.users.values()):
            return None
        output = bytearray()
        for _ in range(5):  # Un envío de 100 ms en lugar de 50 llamadas por segundo.
            frames = [audioop.tomono(frames.popleft(), 2, 0.5, 0.5)
                      for frames in self.users.values() if frames]
            mixed = bytes(DISCORD_FRAME_BYTES // 2)
            for frame in frames:
                mixed = audioop.add(mixed, audioop.mul(frame, 2, 1 / len(frames)), 2)
            output.extend(mixed)
        self.users = {user: frames for user, frames in self.users.items() if frames}
        result, self.state = audioop.ratecv(bytes(output), 2, 1, 48000, 16000, self.state)
        return result

    def silence(self):
        result, self.state = audioop.ratecv(bytes(9600), 2, 1, 48000, 16000, self.state)
        return result

    def clear(self):
        self.users.clear()
        self.state = None


class StreamingPCM(discord.AudioSource):
    def __init__(self, volume=0.8):
        self.frames = queue.Queue(maxsize=1500)  # Hasta 30 s de salida; memoria acotada.
        self.pending = bytearray()
        self.state = None
        self.volume = volume
        self.finished = False
        self.aborted = threading.Event()

    def is_opus(self):
        return False

    def feed(self, pcm):
        if self.finished or self.aborted.is_set():
            return
        if len(pcm) % 2:
            raise ValueError('El audio PCM debe contener muestras completas de 16 bits.')
        mono, self.state = audioop.ratecv(pcm, 2, 1, 24000, 48000, self.state)
        self.pending.extend(audioop.tostereo(audioop.mul(mono, 2, self.volume), 2, 1, 1))
        while len(self.pending) >= DISCORD_FRAME_BYTES:
            self.frames.put_nowait(bytes(self.pending[:DISCORD_FRAME_BYTES]))
            del self.pending[:DISCORD_FRAME_BYTES]

    def finish(self):
        if not self.finished and self.pending:
            self.frames.put_nowait(bytes(self.pending).ljust(DISCORD_FRAME_BYTES, b'\x00'))
        self.pending.clear()
        self.finished = True

    def read(self):
        if self.aborted.is_set():
            return b''
        try:
            return self.frames.get(timeout=0.04 if not self.finished else 0)
        except queue.Empty:
            return b'' if self.finished else SILENCE

    def abort(self):
        self.aborted.set()
        self.pending.clear()
        self.finished = True
        while not self.frames.empty():
            try:
                self.frames.get_nowait()
            except queue.Empty:
                break

    def cleanup(self):
        self.abort()
