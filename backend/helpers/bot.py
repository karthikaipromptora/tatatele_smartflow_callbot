import os
import wave
from dataclasses import dataclass
from pathlib import Path

from fastapi import WebSocket
from loguru import logger
from pipecat.frames.frames import Frame, TranscriptionFrame, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.sarvam.tts import SarvamTTSService
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from helpers.prompts import build_greeting, build_system_prompt
from helpers.smartflow import StreamStart

SAMPLE_RATE = 8000
RECORDINGS_DIR = Path(__file__).resolve().parent.parent / "recordings"


@dataclass
class CallResult:
    transcript: list[dict]
    recording_path: str | None


class TranscriptionLogger(FrameProcessor):
    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and direction == FrameDirection.DOWNSTREAM:
            logger.debug(f"STT: [{frame.text}] | lang: {frame.language}")
        await self.push_frame(frame, direction)


def _write_wav(path: Path, audio: bytes, sample_rate: int, num_channels: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(num_channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio)


async def run_bot(websocket: WebSocket, start: StreamStart, ctx: dict, ref_id: str) -> CallResult:
    # SmartFlow speaks the Twilio Media Streams wire format. Hang-up goes through
    # SmartFlow's own API, not Twilio's, so auto_hang_up must stay off.
    serializer = TwilioFrameSerializer(
        stream_sid=start.stream_sid,
        call_sid=start.call_sid,
        params=TwilioFrameSerializer.InputParams(
            twilio_sample_rate=SAMPLE_RATE,
            auto_hang_up=False,
        ),
    )

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            serializer=serializer,
        ),
    )

    llm = OpenAILLMService(
        api_key="local",
        base_url=os.getenv("LOCAL_LLM_URL", "http://164.52.198.104:8049/v1"),
        settings=OpenAILLMService.Settings(
            model=os.getenv("LOCAL_LLM_MODEL", "google/gemma-4-26B-A4B-it"),
        ),
    )

    stt = SarvamSTTService(
        api_key=os.getenv("SARVAM_API_KEY", ""),
        settings=SarvamSTTService.Settings(
            model="saarika:v2.5",
            vad_signals=True,
        ),
    )

    tts = SarvamTTSService(
        api_key=os.getenv("SARVAM_API_KEY", ""),
        settings=SarvamTTSService.Settings(
            model="bulbul:v3",
            voice=ctx["voice_id"],
            pace=0.9,
            temperature=0.8,
        ),
    )

    greeting_text = build_greeting(ctx)
    messages = [
        {"role": "system",    "content": build_system_prompt(ctx)},
        {"role": "user",      "content": "begin"},
        {"role": "assistant", "content": greeting_text},
    ]
    context = LLMContext(messages)

    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(user_turn_stop_timeout=0.7),
    )

    audiobuffer = AudioBufferProcessor(num_channels=1)
    recorded: dict = {}

    @audiobuffer.event_handler("on_audio_data")
    async def on_audio_data(buffer, audio, sample_rate, num_channels):
        recorded.update(audio=audio, sample_rate=sample_rate, num_channels=num_channels)

    pipeline = Pipeline([
        transport.input(),
        stt,
        TranscriptionLogger(),
        user_aggregator,
        llm,
        tts,
        transport.output(),
        audiobuffer,
        assistant_aggregator,
    ])

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            audio_in_sample_rate=SAMPLE_RATE,
            audio_out_sample_rate=SAMPLE_RATE,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info(f"[{ref_id}] Call started — callSid={start.call_sid}")
        await audiobuffer.start_recording()
        await task.queue_frames([TTSSpeakFrame(text=greeting_text)])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info(f"[{ref_id}] Call ended — callSid={start.call_sid}")
        await task.cancel()

    await PipelineRunner(handle_sigint=False).run(task)

    transcript = [
        {"role": m["role"], "text": m["content"]}
        for m in context.messages[2:]
        if m.get("role") in ("user", "assistant")
        and isinstance(m.get("content"), str)
        and m["content"].strip()
    ]

    recording_path = None
    if recorded.get("audio"):
        path = RECORDINGS_DIR / f"{ref_id}.wav"
        _write_wav(path, recorded["audio"], recorded["sample_rate"], recorded["num_channels"])
        recording_path = str(path)
        logger.info(f"[{ref_id}] Recording saved — {path.name}")

    return CallResult(transcript=transcript, recording_path=recording_path)
