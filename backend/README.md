# Tata Tele SmartFlow voice bot

Outbound payment-collection bot ("Arjun"). It uses Pipecat with Sarvam STT/TTS and a self-hosted LLM, and Tata Tele SmartFlow Voice Streaming is the telephony transport.

## How a call flows

```
UI "Call" button ─► POST /start ─► SmartFlow Click to Call API ─► SmartFlow dials customer
                                                                        │ answered
                            wss://<host>/ws  ◄──────────────────────────┘
        connected → start (streamSid, callSid, customParameters.ref_id) → media ⇄ media/clear → stop
```

- **`/start`** saves the customer context (name, amount, service, period, language, voice) in Postgres under a new `ref_id`. It then asks SmartFlow to dial, sending `ref_id` as a custom parameter.
- **`/ws`** is the SmartFlow **Static** WSS endpoint. It reads `ref_id` from the `start` event, loads the context and runs the bot. SmartFlow's wire format is the same as Twilio Media Streams (mulaw 8 kHz, base64), so the bot uses Pipecat's `TwilioFrameSerializer`.
- After the call, the transcript is saved to Postgres and a mixed recording to `recordings/<ref_id>.wav`. You can see both in the UI.

> **Status:** the Click to Call request in `helpers/smartflow.py:initiate_click_to_call` is still a stub because the API spec is pending. Until it's implemented, the Call button returns *"Click to Call is not integrated yet"*.

## Run

Requires Python 3.12–3.13 and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env      # fill in values
uv sync
uv run main.py            # serves on HOST:PORT (default 0.0.0.0:8011)
```

Open `http://<host>:8011/` for the test UI.

### Public WSS (required by SmartFlow)

SmartFlow only connects to `wss://`. Put a TLS reverse proxy in front of port 8011 that forwards WebSocket upgrades. With Caddy, for example:

```
bot.example.com {
    reverse_proxy 127.0.0.1:8011
}
```

## SmartFlow portal setup

1. **Channels Hub → VOICE Bot → Add VOICE Bot**: give it a name and description, and set WSS URL `wss://<your-domain>/ws`.
2. **API Connect → Click to Call Support API → Generate API Key**, and set the VOICE Bot above as its destination.

## Local test without SmartFlow

`tests/mock_smartflow_client.py` acts as the SmartFlow platform. It sends `connected` → `start` → `media` every 100 ms → `stop`, and the caller's speech is synthesised with Sarvam. It checks that:

- the greeting and the replies come back;
- every outgoing `media` payload is a multiple of 160 bytes and carries the right `streamSid`;
- talking over the bot produces a `clear`;
- the transcript, the call status and the recording are saved.

```bash
uv run main.py                                  # terminal 1
uv run python tests/mock_smartflow_client.py    # terminal 2
```

The bot audio exactly as "SmartFlow" received it is written to `tests/output/`.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET  | `/` | Test UI |
| POST | `/start` | `{phone_number, customer_name?, amount?, billing_period?, service_name?, language?, voice_id?}` |
| WS   | `/ws` | SmartFlow bi-directional stream |
| GET  | `/logs`, `/logs/{ref_id}` | Calls and transcript |
| GET  | `/recordings/{ref_id}` | Call recording (WAV) |
| GET  | `/health` | Liveness |
