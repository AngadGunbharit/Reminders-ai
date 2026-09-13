"""FastAPI server for Ava Reminders.

Endpoints:
  GET  /                     → web dashboard
  GET  /api/calls            → recent call history
  GET  /api/call/{id}/log    → transcript for a call
  POST /api/call/start       → trigger a Vapi outbound call
  POST /chat/completions     → Vapi custom-LLM webhook (OpenAI-compatible)

Required .env:
  ANTHROPIC_API_KEY          your Anthropic key
  VAPI_API_KEY               from app.vapi.ai → Account → API Keys (private key)
  VAPI_PHONE_NUMBER_ID       from app.vapi.ai → Phone Numbers
  SERVER_URL                 public HTTPS URL (e.g. ngrok tunnel)
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

load_dotenv(Path(__file__).parent / ".env")

from app import db
from app.reminder_agent import ReminderAgent, OUTCOME_EMOJI

app = FastAPI(title="Ava Reminders")

# Active agent sessions keyed by Vapi call ID
_sessions: dict[str, ReminderAgent] = {}


# ── Web UI ─────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    return (Path(__file__).parent / "static" / "index.html").read_text()


# ── Recent calls ───────────────────────────────────────────────────────
@app.get("/api/calls")
def recent_calls():
    conn = db.get_connection()
    rows = conn.execute("""
        SELECT id, contact_name, contact_phone, purpose,
               started_at, ended_at, status, outcome, notes
        FROM calls
        ORDER BY started_at DESC
        LIMIT 30
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Call transcript ────────────────────────────────────────────────────
@app.get("/api/call/{call_id}/log")
def call_log(call_id: str):
    conn = db.get_connection()
    call = conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
    if not call:
        conn.close()
        raise HTTPException(404, "Call not found")
    turns = conn.execute(
        "SELECT speaker, message, created_at FROM conversation_turns "
        "WHERE call_id = ? ORDER BY turn_index",
        (call_id,),
    ).fetchall()
    conn.close()
    return {
        "call": dict(call),
        "turns": [dict(t) for t in turns],
    }


# ── Start outbound call ────────────────────────────────────────────────
class CallRequest(BaseModel):
    contact_name: str
    phone_number: str   # E.164, e.g. "+14155550100"
    purpose: str


@app.post("/api/call/start")
async def start_call(body: CallRequest):
    vapi_key   = os.environ.get("VAPI_API_KEY", "")
    phone_id   = os.environ.get("VAPI_PHONE_NUMBER_ID", "")
    server_url = os.environ.get("SERVER_URL", "").rstrip("/")

    if not all([vapi_key, phone_id, server_url]):
        raise HTTPException(400, "Set VAPI_API_KEY, VAPI_PHONE_NUMBER_ID, SERVER_URL in .env")

    agent = ReminderAgent(
        contact_name=body.contact_name,
        contact_phone=body.phone_number,
        purpose=body.purpose,
        use_llm=True,
    )
    first_message = agent.opening_line()

    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(
            "https://api.vapi.ai/call/phone",
            headers={"Authorization": f"Bearer {vapi_key}"},
            json={
                "phoneNumberId": phone_id,
                "customer": {"number": body.phone_number},
                "assistant": {
                    "name": "Ava",
                    "model": {
                        "provider": "custom-llm",
                        "url": f"{server_url}/chat/completions",
                        "model": "ava-reminders",
                    },
                    "voice": {
                        "provider": "11labs",
                        "voiceId": "21m00Tcm4TlvDq8ikWAM",  # Rachel
                    },
                    "firstMessage": first_message,
                    "metadata": {
                        "contact_name": body.contact_name,
                        "contact_phone": body.phone_number,
                        "purpose": body.purpose,
                    },
                },
            },
        )

    if resp.status_code not in (200, 201):
        raise HTTPException(502, f"Vapi error {resp.status_code}: {resp.text}")

    vapi_data    = resp.json()
    vapi_call_id = vapi_data["id"]
    _sessions[vapi_call_id] = agent

    return {
        "vapi_call_id":     vapi_call_id,
        "internal_call_id": agent.call_id,
        "status":           "initiated",
        "opening_line":     first_message,
    }


# ── Vapi custom-LLM webhook ────────────────────────────────────────────
@app.post("/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()

    call_info    = body.get("call", {})
    vapi_call_id = call_info.get("id", "")
    metadata     = call_info.get("metadata", {})
    messages     = body.get("messages", [])

    user_turns = [m for m in messages if m.get("role") == "user"]
    if not user_turns:
        return _openai_response("...")

    last_user = user_turns[-1]["content"]

    agent = _sessions.get(vapi_call_id)
    if agent is None:
        # Session missing (e.g. server restart) — recreate from metadata
        agent = ReminderAgent(
            contact_name=metadata.get("contact_name", "there"),
            contact_phone=metadata.get("contact_phone", ""),
            purpose=metadata.get("purpose", ""),
            use_llm=True,
        )
        agent.opening_line()
        _sessions[vapi_call_id] = agent

    reply, ended = agent.step(last_user)

    if ended:
        agent.finalize()
        _sessions.pop(vapi_call_id, None)

    return _openai_response(reply)


def _openai_response(text: str) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
