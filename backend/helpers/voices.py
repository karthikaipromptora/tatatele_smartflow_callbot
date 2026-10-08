import base64
import hashlib
import os
from pathlib import Path

import aiohttp

from helpers.prompts import DEFAULT_CONTEXT, build_greeting

# Sarvam bulbul:v3 speakers (https://docs.sarvam.ai/api-reference/text-to-speech/convert).
_MALE = [
    "shubh", "aditya", "rahul", "rohan", "amit", "dev", "ratan", "varun", "manan", "sumit", "kabir", "aayan",
    "ashutosh", "advait", "anand", "tarun", "sunny", "mani", "gokul", "vijay", "mohit", "rehan", "soham",
]
_FEMALE = [
    "ritu", "priya", "neha", "pooja", "simran", "kavya", "ishita", "shreya", "roopa", "tanya", "shruti",
    "suhani", "kavitha", "rupali",
]
VOICES = sorted(
    [{"id": v, "name": v.capitalize(), "gender": "Male"} for v in _MALE]
    + [{"id": v, "name": v.capitalize(), "gender": "Female"} for v in _FEMALE],
    key=lambda v: v["name"],
)
VOICE_IDS = {v["id"] for v in VOICES}
DEFAULT_VOICE = "shubh"

LANGUAGE_CODES = {"English": "en-IN", "Hindi": "hi-IN"}
PREVIEW_SAMPLE_RATE = 8000  # what customers actually hear on a phone line
MAX_PREVIEW_CHARS = 300

CACHE_DIR = Path(__file__).resolve().parent.parent / "voice_cache"
TTS_URL = "https://api.sarvam.ai/text-to-speech"


class VoicePreviewError(RuntimeError):
    pass


def sample_text(language: str) -> str:
    return build_greeting({**DEFAULT_CONTEXT, "language": language})


async def preview_audio(session: aiohttp.ClientSession, voice: str, language: str, text: str) -> Path:
    """Return a cached WAV of `voice` speaking `text`, synthesising it with the bot's TTS settings if needed."""
    key = hashlib.sha256(f"{voice}|{language}|{text}".encode()).hexdigest()[:20]
    path = CACHE_DIR / f"{voice}-{LANGUAGE_CODES[language]}-{key}.wav"
    if path.is_file():
        return path

    payload = {
        "text": text,
        "target_language_code": LANGUAGE_CODES[language],
        "speaker": voice,
        "model": "bulbul:v3",
        "pace": 0.9,
        "temperature": 0.8,
        "speech_sample_rate": PREVIEW_SAMPLE_RATE,
    }
    async with session.post(
        TTS_URL,
        json=payload,
        headers={"api-subscription-key": os.getenv("SARVAM_API_KEY", "")},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        if resp.status != 200:
            raise VoicePreviewError(f"Sarvam TTS error (HTTP {resp.status}): {(await resp.text())[:200]}")
        data = await resp.json()

    audios = data.get("audios") or []
    if not audios:
        raise VoicePreviewError("Sarvam TTS returned no audio")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(base64.b64decode(audios[0]))
    tmp.replace(path)
    return path
