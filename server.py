"""FastAPI voice interface for PayFlow AI.

Endpoints:
  GET  /                      → web dashboard
  GET  /api/customers         → list customers with open invoices
  GET  /api/calls             → recent call history
  GET  /api/call/{id}/log     → transcript + agent decision for a call
  POST /api/call/start        → trigger a Vapi outbound call
  POST /chat/completions      → Vapi custom-LLM hook (OpenAI-compatible)

Required .env vars:
  ANTHROPIC_API_KEY           (already set)
  VAPI_API_KEY                from app.vapi.ai → Account → API Keys
  VAPI_PHONE_NUMBER_ID        from app.vapi.ai → Phone Numbers (buy/import one)
  SERVER_URL                  public HTTPS URL, e.g. your ngrok tunnel
"""
from __future__ import annotations

import json
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
from app.agent import CollectionsAgent

app = FastAPI(title="PayFlow AI")

# Active agent sessions keyed by Vapi call ID
_sessions: dict[str, CollectionsAgent] = {}


# ── Web UI ─────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    return (Path(__file__).parent / "static" / "index.html").read_text()


# ── Customers ──────────────────────────────────────────────────────────
@app.get("/api/customers")
def list_customers():
    conn = db.get_connection()
    rows = conn.execute("""
        SELECT c.id, c.name, c.phone, c.email, c.payment_history,
               i.id AS invoice_id, i.balance, i.previous_attempts,
               CAST(julianday('now') - julianday(i.due_date) AS INTEGER) AS days_overdue
        FROM customers c
        JOIN invoices i ON i.customer_id = c.id
        WHERE i.status IN ('open', 'partial')
        ORDER BY i.balance DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Recent calls ───────────────────────────────────────────────────────
@app.get("/api/calls")
def recent_calls():
    conn = db.get_connection()
    rows = conn.execute("""
        SELECT ca.id, ca.started_at, ca.ended_at, ca.status, ca.outcome,
               ca.final_state, cu.name AS customer_name, i.balance
        FROM calls ca
        JOIN customers cu ON cu.id = ca.customer_id
        JOIN invoices i   ON i.id  = ca.invoice_id
        ORDER BY ca.started_at DESC
        LIMIT 20
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
        "SELECT speaker, message, state, created_at FROM conversation_turns "
        "WHERE call_id = ? ORDER BY turn_index",
        (call_id,),
    ).fetchall()
    decision = conn.execute(
        "SELECT outcome, reasoning, policy_checks FROM agent_decisions WHERE call_id = ?",
        (call_id,),
    ).fetchone()
    conn.close()
    return {
        "call": dict(call),
        "turns": [dict(t) for t in turns],
        "decision": {
            "outcome": decision["outcome"],
            "reasoning": json.loads(decision["reasoning"]),
            "policy_checks": json.loads(decision["policy_checks"]),
        } if decision else None,
    }


# ── Outbound call ──────────────────────────────────────────────────────
class CallRequest(BaseModel):
    phone_number: str   # E.164, e.g. "+14155550100"
    customer_id: str
    invoice_id: str


@app.post("/api/call/start")
async def start_call(body: CallRequest):
    vapi_key  = os.environ.get("VAPI_API_KEY", "")
    phone_id  = os.environ.get("VAPI_PHONE_NUMBER_ID", "")
    server_url = os.environ.get("SERVER_URL", "").rstrip("/")

    if not all([vapi_key, phone_id, server_url]):
        raise HTTPException(400, "Set VAPI_API_KEY, VAPI_PHONE_NUMBER_ID, SERVER_URL in .env")

    # Initialise agent and generate opening line (creates DB call record)
    agent = CollectionsAgent(
        customer_id=body.customer_id,
        invoice_id=body.invoice_id,
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
                        "model": "payflow-ai",
                    },
                    "voice": {
                        "provider": "11labs",
                        "voiceId": "21m00Tcm4TlvDq8ikWAM",  # Rachel
                    },
                    "firstMessage": first_message,
                    "metadata": {
                        "customer_id": body.customer_id,
                        "invoice_id": body.invoice_id,
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
        "vapi_call_id":    vapi_call_id,
        "internal_call_id": agent.call_id,
        "status": "initiated",
    }


# ── Vapi custom-LLM endpoint ───────────────────────────────────────────
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

    # Get cached agent or fall back to recreating it
    agent = _sessions.get(vapi_call_id)
    if agent is None:
        agent = CollectionsAgent(
            customer_id=metadata.get("customer_id", ""),
            invoice_id=metadata.get("invoice_id", ""),
            use_llm=True,
        )
        agent.opening_line()
        _sessions[vapi_call_id] = agent

    result = agent.step(last_user)

    if result.ended or result.escalated:
        agent.finalize()
        _sessions.pop(vapi_call_id, None)

    return _openai_response(result.agent_message)


def _openai_response(text: str) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
