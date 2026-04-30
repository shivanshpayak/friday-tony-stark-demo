"""Audio analysis tools (listening to ambient noise or humming)."""
import os
import wave
import base64
import httpx
import pyaudio
import asyncio
import winsound
from mcp.server.fastmcp import FastMCP


def _ping_recording_started() -> None:
    """Short async system beep so the user knows recording started.
    Non-blocking; failures are swallowed because the ping is non-essential."""
    try:
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass

def _record_and_analyze(duration_sec: int) -> str:
    """Synchronous function to record audio and call Gemini REST."""
    chunk = 1024
    format = pyaudio.paInt16
    channels = 1
    rate = 16000
    
    p = pyaudio.PyAudio()
    try:
        stream = p.open(format=format, channels=channels, rate=rate, input=True, frames_per_buffer=chunk)
        frames = []
        for _ in range(0, int(rate / chunk * duration_sec)):
            frames.append(stream.read(chunk))
    except Exception as e:
        return f"Microphone capture failed: {e}"
    finally:
        try:
            stream.stop_stream()
            stream.close()
        except: pass
        p.terminate()
        
    # Write to memory (or temp file if easier)
    # Using a temp file is safest for the wave module
    temp_file = "temp_capture.wav"
    try:
        with wave.open(temp_file, 'wb') as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(p.get_sample_size(format))
            wf.setframerate(rate)
            wf.writeframes(b''.join(frames))
            
        with open(temp_file, "rb") as f:
            audio_data = f.read()
    finally:
        if os.path.exists(temp_file):
            try: os.remove(temp_file)
            except: pass

    b64_data = base64.b64encode(audio_data).decode("utf-8")
    api_key = os.getenv("GOOGLE_API_KEY")
    # Using Gemini 2.5 Flash for fast multimodal audio reasoning
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    
    payload = {
        "contents": [{
            "parts": [
                {"text": "Listen to this raw audio. The user is either humming a melody or playing a song out loud. Try your absolute best to identify the specific song name and artist. Be extremely concise, output just the predicted song name if you are confident."},
                {"inline_data": {
                    "mimeType": "audio/wav",
                    "data": b64_data
                }}
            ]
        }]
    }
    
    try:
        response = httpx.post(url, json=payload, timeout=30.0)
        response.raise_for_status()
        result = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        return result.strip()
    except Exception as e:
        return f"Gemini audio analysis failed: {e}"


def register(mcp: FastMCP):
    
    @mcp.tool(name="recognize_song_humming")
    async def recognize_song_humming(duration: int = 8) -> str:
        """
        Record real-time audio from the microphone and use Gemini Multimodal AI to identify what song the user is humming, singing, or playing in the background.
        Use this when the user says "what song am I humming" or "shazam this".
        """
        # Audible cue that recording is starting — the LiveKit "thinking" light
        # alone gave no feedback that the tool had actually fired. Sleep briefly
        # so the beep finishes before the mic opens and bleeds into the take.
        _ping_recording_started()
        await asyncio.sleep(0.35)
        result = await asyncio.get_event_loop().run_in_executor(None, _record_and_analyze, duration)
        return f"Audio Analysis Result: {result}"
