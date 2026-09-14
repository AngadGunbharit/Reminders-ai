# Ava Reminds

An AI-powered voice calling agent that makes real outbound phone calls on your behalf — reminders, appointment confirmations, event invites, and more.

Built with Claude (Anthropic) for conversation, Vapi for voice telephony, and FastAPI for the backend.

---

## Features

- **AI voice calls** — Ava calls contacts and holds a natural conversation, not a robocall
- **Any purpose** — salon appointments, gym sessions, party invites, medical reminders, deliveries, birthdays
- **Suggestion templates** — pre-filled prompts to get started fast
- **Bulk campaigns** — upload a CSV of contacts and send the same message to everyone
- **Scheduled calls** — pick a future date/time, Ava calls automatically
- **Live transcript** — watch the conversation appear in real time
- **Call outcomes** — Confirmed, Declined, Callback Requested, Wrong Number, Completed
- **Campaign progress** — track each contact's status across a bulk send
- **Dark mode** — persists across sessions, respects system preference
- **Call history** — full sidebar log with transcripts and outcomes

---

## Stack

| Layer | Tech |
|---|---|
| LLM | Claude (claude-sonnet-4-6) via Anthropic SDK |
| Voice / telephony | Vapi (outbound calls, STT, TTS via ElevenLabs) |
| Backend | FastAPI + Python |
| Database | SQLite |
| Tunnel (dev) | ngrok |
| Frontend | Vanilla JS + HTML/CSS |

---

## How it works

1. You fill in a contact name, phone number, and describe the purpose of the call
2. The server generates an opening line via Claude and fires an outbound call via Vapi
3. Vapi calls the contact and streams their speech to our `/chat/completions` webhook
4. Claude responds turn-by-turn, driving the conversation naturally
5. When the conversation is complete, Claude calls `end_call` with the outcome
6. Everything is logged — transcript, outcome, notes — to SQLite

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/AngadGunbharit/Reminders-ai.git
cd Reminders-ai
pip install -r requirements.txt
```

### 2. Environment variables

Create a `.env` file in the root:

```
ANTHROPIC_API_KEY=your_anthropic_key
VAPI_API_KEY=your_vapi_private_key
VAPI_PHONE_NUMBER_ID=your_vapi_phone_number_id
SERVER_URL=https://your-public-url.ngrok-free.app
```

- **Anthropic key** — [console.anthropic.com](https://console.anthropic.com)
- **Vapi keys** — [app.vapi.ai](https://app.vapi.ai) → Account → API Keys (use the **private** key)
- **SERVER_URL** — public HTTPS URL pointing to this server (ngrok or deployed URL)

### 3. Initialise the database

```bash
python -m scripts.init_db
```

### 4. Expose locally with ngrok

```bash
ngrok http 8000
```

Copy the `https://` URL into `SERVER_URL` in your `.env`.

### 5. Run

```bash
python server.py
```

Open [http://localhost:8000](http://localhost:8000)

---

## API

| Method | Endpoint | Description |
|---|---|---|
| GET | `/` | Web dashboard |
| GET | `/api/calls` | Recent call history |
| GET | `/api/call/{id}/log` | Full transcript for a call |
| POST | `/api/call/start` | Start a single outbound call |
| POST | `/api/campaign/start` | Start a bulk campaign |
| GET | `/api/campaigns` | List all campaigns |
| GET | `/api/campaign/{id}` | Campaign detail + per-contact status |
| POST | `/chat/completions` | Vapi custom-LLM webhook (OpenAI-compatible) |

### Start a call

```bash
curl -X POST http://localhost:8000/api/call/start \
  -H "Content-Type: application/json" \
  -d '{
    "contact_name": "Sarah",
    "phone_number": "+14155550100",
    "purpose": "Remind Sarah of her dentist appointment tomorrow at 2pm. Ask her to confirm.",
    "scheduled_at": null
  }'
```

### Bulk campaign

```bash
curl -X POST http://localhost:8000/api/campaign/start \
  -H "Content-Type: application/json" \
  -d '{
    "purpose": "Remind everyone about the team offsite on Friday at 10am.",
    "contacts": [
      {"name": "Alice", "phone": "+14155550101"},
      {"name": "Bob",   "phone": "+14155550102"}
    ],
    "scheduled_at": null
  }'
```

### CSV format for bulk upload

```
name,phone
Alice,+14155550101
Bob,+14155550102
```

---

## Scheduled calls

Set `scheduled_at` to any future ISO datetime and the call fires automatically:

```json
"scheduled_at": "2024-12-01T09:00:00Z"
```

The background scheduler checks every 30 seconds and fires due calls.

---

## Reset the database

```bash
python -m scripts.init_db --reset
```
