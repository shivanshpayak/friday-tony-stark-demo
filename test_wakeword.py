"""Quick test: see wake word scores live as you speak."""
import numpy as np
import pyaudio
from openwakeword.model import Model

model = Model(wakeword_models=["hey_jarvis_v0.1"])
audio = pyaudio.PyAudio()
stream = audio.open(format=pyaudio.paInt16, channels=1, rate=16000, input=True, frames_per_buffer=1280)

print("Listening... say 'Hey Jarvis' (Ctrl+C to stop)")
print("Score > 0.5 = detection\n")

try:
    while True:
        raw = stream.read(1280, exception_on_overflow=False)
        data = np.frombuffer(raw, dtype=np.int16)
        prediction = model.predict(data)
        for name, score in prediction.items():
            if score > 0.01:  # show any non-zero scores
                bar = "█" * int(score * 50)
                print(f"{name}: {score:.3f} {bar}")
except KeyboardInterrupt:
    print("\nDone")
finally:
    stream.stop_stream()
    stream.close()
    audio.terminate()
