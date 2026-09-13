"""The orchestrator: state machine + policy engine + tools + (LLM or
offline heuristic) reasoning, wired into one call.

Two reasoning backends:
  - LLM mode (default when ANTHROPIC_API_KEY is set): Claude drives the
    conversation via tool use, constrained each turn to the state machine's
    current state and the policy-approved tools for it.
  - Offline mode (no API key, or use_llm=False): a small deterministic
    rule-based responder that walks the exact same state machine and calls
    the exact same tools. It exists so the DB layer, policy engine, tool
    implementations, and state machine can be exercised and demoed with
    zero API cost/latency — it's the default backend for scripts/run_demo.py
    (pass --llm to use Claude instead). It is not a
    substitute for the LLM's language understanding; keep scenario inputs
    reasonably explicit when using it (see app/scenarios.py).

Either way, every turn and every tool call is logged to the DB, and
finalize() writes the structured "Agent Decision" explanation.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from . import db
from . import policy as policy_engine
from . import tools
from .state_machine import CallStateMachine, State, detect_interrupt
from .tool_schemas import tools_for_state

COMPANY_NAME = "Acme Co"
AGENT_NAME = "Ava"


_STATE_GOALS = {
    "DISCUSS_BALANCE": (
        "Confirm the customer is aware of the outstanding balance. "
        "As soon as they acknowledge it (even if they say they can't pay in full), "
        "call advance_state immediately in this same turn. Do NOT ask follow-up questions here."
    ),
    "UNDERSTAND_SITUATION": (
        "Ask ONE short question to find out if they can make any payment today and how much. "
        "The moment they give you any amount or say yes/no, call advance_state in this same turn. "
        "Do not ask multiple questions or probe further."
    ),
    "DETERMINE_ABILITY_TO_PAY": (
        "You MUST call get_payment_options right now (pass any amount the customer mentioned as proposed_upfront). "
        "Do NOT say anything to the customer first — call the tool immediately, then call advance_state, "
        "then present the options to the customer."
    ),
    "CHECK_OPTIONS": (
        "Present the payment options from get_payment_options to the customer. "
        "Use ONLY the numbers returned by the tool — never invent figures. "
        "Ask which option they'd like, then call advance_state."
    ),
    "NEGOTIATE": (
        "If the customer counter-proposes an amount, call get_payment_options with that amount, "
        "then present the result. "
        "If the customer agrees to an option (says yes / ok / that works / sounds good / go ahead): "
        "do NOT ask for more confirmation — execute the plan immediately in this same turn: "
        "(1) call advance_state, (2) call advance_state again, (3) call make_payment with the "
        "upfront amount they agreed to, (4) call schedule_payment for each installment using the "
        "exact amounts and dates from the get_payment_options result, (5) call advance_state, "
        "(6) then write your summary to the customer."
    ),
    "CONFIRM_ACTION": (
        "The customer has already agreed. Do NOT ask for more confirmation. "
        "Execute immediately: call advance_state, then call make_payment for the upfront amount, "
        "then call schedule_payment for each installment, then write your summary."
    ),
    "EXECUTE_ACTION": (
        "Call make_payment for the upfront amount, then call schedule_payment for each installment. "
        "Then call advance_state."
    ),
    "SUMMARIZE": (
        "Give one sentence confirming everything is set, call add_account_note with a brief summary, "
        "then call advance_state."
    ),
}


def system_prompt(state: State, policy: dict, customer_id: str, custom_instructions: str = "") -> str:
    goal = _STATE_GOALS.get(state.value, "Continue the call and advance_state when this step is complete.")
    return f"""You are {AGENT_NAME}, an AI collections agent calling on behalf of {COMPANY_NAME}.

## CRITICAL RULES — follow these exactly every turn:

1. STATE MACHINE: You are in state {state.value}. You can ONLY access tools unlocked by the
   current state. To unlock the next state's tools you MUST call advance_state first.
   Always call advance_state (and any other required tools) BEFORE writing your reply text.

2. GOAL THIS TURN: {goal}

3. NUMBERS: Never state a dollar figure, plan, or date that didn't come from get_payment_options.
   Always call get_payment_options and read back its exact numbers.

4. ESCALATION: Call escalate_to_human ONLY if the customer explicitly disputes the invoice,
   describes a hardship/no income, threatens legal action, requests a manager, or is hostile.
   NEVER escalate a cooperative customer. "Yes", "go ahead", "that works" = process the payment.

5. POLICY:
   - Minimum upfront: {policy['min_upfront_payment_pct']}% of balance
   - Max plan length: {policy['max_payment_plan_days']} days, up to {policy['allowed_installments']} installments
   - Max discount: {policy['max_discount_pct']}%

6. STYLE: Keep replies short, like a real phone call.
{f"7. SPECIAL INSTRUCTIONS FOR THIS CALL: {custom_instructions}" if custom_instructions else ""}

Customer ID for tool calls: {customer_id}
"""


def _extract_amount(text: str) -> Optional[float]:
    """Best-effort dollar-amount extraction for offline mode. Prioritizes
    "I can do/pay/afford $X" proposal phrasing; falls back to a bare $amount
    but skips it if it's describing what the customer *doesn't* have
    (e.g. "I don't have $1,280 right now" should not read as an offer)."""
    t = text.lower()
    m = re.search(r"(?:do|pay|afford|put down|manage|give you|come up with)\s+\$?\s?(\d[\d,]*(?:\.\d{1,2})?)", t)
    if m:
        return float(m.group(1).replace(",", ""))
    m = re.search(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)", t)
    if m:
        window = t[max(0, m.start() - 25):m.start()]
        if re.search(r"don'?t have|can'?t|cannot|not able|no way", window):
            return None
        return float(m.group(1).replace(",", ""))
    return None


def _is_affirmative(text: str) -> bool:
    return bool(re.search(r"\b(yes|yeah|yep|sure|ok|okay|works|sounds good|agree|deal|let'?s do it)\b", text.lower()))


def _is_full_payment(text: str) -> bool:
    return bool(re.search(r"\b(full|all of it|entire|whole balance|pay it off|pay it all)\b", text.lower()))


@dataclass
class TurnResult:
    agent_message: str
    state: State
    escalated: bool = False
    ended: bool = False


@dataclass
class CollectionsAgent:
    customer_id: str
    invoice_id: str
    use_llm: bool = field(default_factory=lambda: bool(os.environ.get("ANTHROPIC_API_KEY")))
    custom_instructions: str = ""
    call_id: str = field(init=False)
    sm: CallStateMachine = field(default_factory=CallStateMachine)
    policy: dict = field(init=False)
    turn_index: int = field(default=0, init=False)
    context: dict[str, Any] = field(default_factory=dict, init=False)
    _llm_messages: list = field(default_factory=list, init=False)
    _client: Any = field(default=None, init=False)

    def __post_init__(self):
        self.policy = policy_engine.load_policy()
        with db.tx() as conn:
            self.call_id = db.insert(conn, "calls", {
                "customer_id": self.customer_id,
                "invoice_id": self.invoice_id,
                "started_at": db.now(),
                "status": "in_progress",
                "policy_version": self.policy["version"],
            })
        if self.use_llm:
            import anthropic  # lazy import: offline mode needs no dependency
            self._client = anthropic.Anthropic()

    # ── logging helpers ────────────────────────────────────────────────

    def _log_turn(self, speaker: str, message: str) -> None:
        with db.tx() as conn:
            db.insert(conn, "conversation_turns", {
                "call_id": self.call_id,
                "turn_index": self.turn_index,
                "speaker": speaker,
                "message": message,
                "state": self.sm.current.value,
                "created_at": db.now(),
            })
        self.turn_index += 1

    def _log_tool_call(self, tool_name: str, args: dict, result: dict) -> None:
        with db.tx() as conn:
            db.insert(conn, "tool_calls", {
                "call_id": self.call_id,
                "tool_name": tool_name,
                "arguments": db.json_dump(args),
                "result": db.json_dump(result),
                "created_at": db.now(),
            })

    def _call_tool(self, tool_name: str, args: dict) -> dict:
        result = tools.invoke(tool_name, self.call_id, args)
        self._log_tool_call(tool_name, args, result)
        if tool_name == "get_payment_options" and "proposed_upfront_evaluation" in result:
            self.context["last_policy_eval"] = result["proposed_upfront_evaluation"]
        if tool_name == "escalate_to_human":
            self.context["escalation"] = result
        if tool_name == "make_payment":
            self.context["agreed_upfront"] = args.get("amount", 0)
        if tool_name == "schedule_payment":
            installments = self.context.setdefault("installments", [])
            installments.append({"amount": args.get("amount"), "due_date": args.get("date")})
        return result

    def _handle_interrupt(self, customer_message: str) -> Optional[TurnResult]:
        trigger = detect_interrupt(customer_message)
        if trigger is None:
            return None
        action = policy_engine.check_escalation_trigger(trigger, self.policy) or "escalate"
        if action == "escalate":
            self._call_tool("escalate_to_human", {
                "customer_id": self.customer_id,
                "reason": trigger,
                "notes": f"Detected during {self.sm.current.value}: \"{customer_message}\"",
            })
            self.sm.escalate()
            reply = {
                "dispute": "I understand — I won't push further on this. I'm creating a dispute case and connecting you with a specialist who can review it.",
                "hardship": "I'm sorry to hear that. I'm not going to push for payment today — let me connect you with a specialist who handles hardship cases.",
                "legal_threat": "I understand. I'm passing this to a specialist right away so they can address it directly with you.",
                "manager_request": "Of course — connecting you with a specialist now.",
                "angry_customer": "I apologize for the frustration. I'll connect you with a specialist right away.",
            }.get(trigger, "Let me connect you with a specialist who can help.")
            return TurnResult(agent_message=reply, state=self.sm.current, escalated=True)
        else:  # end_call
            self.sm.end()
            return TurnResult(agent_message="I wasn't able to verify your identity, so I have to end the call here for security reasons.",
                               state=self.sm.current, ended=True)

    # ── public API ──────────────────────────────────────────────────────

    def opening_line(self) -> str:
        cust = self._call_tool("get_customer", {"customer_id": self.customer_id})
        self.context["customer"] = cust
        line = (f"Hi {cust['name']}, this is {AGENT_NAME} calling on behalf of {COMPANY_NAME}. "
                f"I'm calling regarding an outstanding invoice of ${cust['balance']:,.2f}. "
                f"Is now an okay time to talk?")
        self.sm.advance()  # INTRO -> IDENTIFY_CUSTOMER (already looked up)
        self.sm.advance()  # IDENTIFY_CUSTOMER -> DISCUSS_BALANCE
        self._log_turn("agent", line)
        return line

    def step(self, customer_message: str) -> TurnResult:
        self._log_turn("customer", customer_message)

        interrupt_result = self._handle_interrupt(customer_message)
        if interrupt_result is not None:
            self._log_turn("agent", interrupt_result.agent_message)
            return interrupt_result

        result = self._offline_step(customer_message) if not self.use_llm else self._llm_step(customer_message)
        self._log_turn("agent", result.agent_message)
        return result

    def finalize(self) -> dict:
        outcome, reasoning, policy_checks = self._build_decision()
        with db.tx() as conn:
            db.insert(conn, "agent_decisions", {
                "call_id": self.call_id,
                "outcome": outcome,
                "reasoning": db.json_dump(reasoning),
                "policy_checks": db.json_dump(policy_checks),
                "created_at": db.now(),
            })
            conn.execute(
                "UPDATE calls SET ended_at = ?, status = ?, outcome = ?, final_state = ?, summary = ? WHERE id = ?",
                (db.now(),
                 "escalated" if self.sm.current == State.ESCALATED else "completed",
                 outcome, self.sm.current.value,
                 f"Call ended in state {self.sm.current.value} with outcome {outcome}.",
                 self.call_id),
            )
        return {"call_id": self.call_id, "outcome": outcome, "reasoning": reasoning, "policy_checks": policy_checks}

    def _build_decision(self) -> tuple[str, list[str], dict]:
        if self.sm.current == State.ESCALATED:
            esc = self.context.get("escalation", {})
            return (
                "escalated",
                [f"Detected {esc.get('reason', 'an escalation trigger')} during the call.",
                 "Agent stopped negotiating per policy and handed off to a human specialist."],
                {"escalation_required": {"result": "pass", "detail": f"reason={esc.get('reason')}, priority={esc.get('priority')}"}},
            )

        balance = self.context.get("customer", {}).get("balance", 0)
        agreed = self.context.get("agreed_upfront")
        installments = self.context.get("installments", [])
        checks = policy_engine.run_policy_checks(balance, agreed or 0, installments, self.policy)
        checks_dict = {k: {"result": v.result, "detail": v.detail} for k, v in checks.items()}

        if agreed is not None and agreed >= balance - 0.01:
            outcome = "payment plan accepted (paid in full)" if agreed >= balance else "payment plan accepted"
            reasoning = [
                "Customer acknowledged the invoice.",
                "No dispute was raised.",
                f"Customer agreed to pay the full balance of ${balance:,.2f} today.",
            ]
        elif agreed is not None:
            outcome = "payment plan accepted"
            reasoning = [
                "Customer acknowledged the invoice.",
                "No dispute was raised.",
                f"Customer indicated ability to make a ${agreed:,.2f} upfront payment.",
                f"Selected the approved {len(installments) + 1}-payment plan.",
            ]
        else:
            outcome = "no resolution"
            reasoning = ["Call ended before a payment commitment was reached."]

        return outcome, reasoning, checks_dict

    # ── offline heuristic backend ───────────────────────────────────────

    def _offline_step(self, msg: str) -> TurnResult:
        state = self.sm.current

        if state == State.DISCUSS_BALANCE:
            if _is_full_payment(msg):
                self.context["proposed_upfront"] = self.context["customer"]["balance"]
                self.sm.goto(State.DETERMINE_ABILITY_TO_PAY)
                return self._check_options_reply()
            amt = _extract_amount(msg)
            if amt is not None:
                self.context["proposed_upfront"] = amt
                self.sm.goto(State.DETERMINE_ABILITY_TO_PAY)
                return self._check_options_reply()
            self.sm.goto(State.UNDERSTAND_SITUATION)
            return TurnResult(
                "I understand. Can I ask what's preventing you from paying the balance today?",
                self.sm.current,
            )

        if state == State.UNDERSTAND_SITUATION:
            self.sm.goto(State.DETERMINE_ABILITY_TO_PAY)
            if _is_full_payment(msg):
                self.context["proposed_upfront"] = self.context["customer"]["balance"]
            else:
                amt = _extract_amount(msg)
                if amt is not None:
                    self.context["proposed_upfront"] = amt
            return self._check_options_reply()

        if state == State.NEGOTIATE:
            amt = _extract_amount(msg)
            if amt is not None:
                self.context["proposed_upfront"] = amt
                return self._check_options_reply()
            if _is_affirmative(msg):
                return self._confirm_reply()
            return TurnResult("No problem — what amount could you put toward this today?", self.sm.current)

        if state == State.CONFIRM_ACTION:
            if _is_affirmative(msg):
                return self._execute_reply()
            return TurnResult("Understood — what would work better for you?", self.sm.current)

        return TurnResult("Thanks for letting me know — I've made a note of that on your account.", self.sm.current)

    def _check_options_reply(self) -> TurnResult:
        self.sm.goto(State.CHECK_OPTIONS)
        balance = self.context["customer"]["balance"]
        proposed = self.context.get("proposed_upfront")
        opts = self._call_tool("get_payment_options", {
            "customer_id": self.customer_id,
            "proposed_upfront": proposed,
        })
        self.sm.goto(State.NEGOTIATE)

        if proposed is not None and proposed >= balance - 0.01:
            self.context["pending_upfront"] = balance
            self.context["pending_installments"] = []
            self.sm.goto(State.CONFIRM_ACTION)
            return TurnResult(f"Great — I can process the full ${balance:,.2f} balance right now. Should I go ahead?", self.sm.current)

        evaln = opts.get("proposed_upfront_evaluation")
        if evaln and evaln["eligible"]:
            self.context["pending_upfront"] = proposed
            plan = policy_engine.build_payment_plan_installments(balance, proposed, self.policy)
            self.context["pending_installments"] = plan
            plan_desc = " and ".join(f"${i['amount']:,.2f} on {i['due_date']}" for i in plan)
            self.sm.goto(State.CONFIRM_ACTION)
            return TurnResult(
                f"I can set that up: ${proposed:,.2f} today, then {plan_desc}. Should I go ahead and schedule that?",
                self.sm.current,
            )

        min_upfront = (evaln or {}).get("minimum_upfront_payment") or round(balance * self.policy["min_upfront_payment_pct"] / 100, 2)
        return TurnResult(
            f"The lowest initial payment I can offer today is ${min_upfront:,.2f}. "
            f"If that's difficult, I can connect you with a specialist to discuss your situation.",
            self.sm.current,
        )

    def _confirm_reply(self) -> TurnResult:
        self.sm.goto(State.CONFIRM_ACTION)
        return TurnResult("Just to confirm — should I go ahead and set that up?", self.sm.current)

    def _execute_reply(self) -> TurnResult:
        self.sm.goto(State.EXECUTE_ACTION)
        balance = self.context["customer"]["balance"]
        upfront = self.context.get("pending_upfront", balance)
        installments = self.context.get("pending_installments", [])

        self._call_tool("make_payment", {"customer_id": self.customer_id, "amount": upfront})
        for inst in installments:
            self._call_tool("schedule_payment", {
                "customer_id": self.customer_id,
                "amount": inst["amount"],
                "date": inst["due_date"],
            })

        self.context["agreed_upfront"] = upfront
        self.context["installments"] = installments
        self.sm.goto(State.SUMMARIZE)

        summary_note = f"Collected ${upfront:,.2f} today" + (
            f"; scheduled {len(installments)} more installment(s)." if installments else "."
        )
        self._call_tool("add_account_note", {"customer_id": self.customer_id, "note": summary_note})
        self.sm.goto(State.END)

        if installments:
            return TurnResult(f"All set — ${upfront:,.2f} processed today, and the rest is scheduled as discussed. Thanks, and take care!", self.sm.current, ended=True)
        return TurnResult(f"All set — ${upfront:,.2f} has been processed and your balance is now paid in full. Thanks, and take care!", self.sm.current, ended=True)

    # ── LLM backend ──────────────────────────────────────────────────────

    def _llm_step(self, customer_message: str) -> TurnResult:
        self._llm_messages.append({"role": "user", "content": customer_message})

        while True:
            available = tools_for_state(self.sm.available_tools())
            response = self._client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=system_prompt(self.sm.current, self.policy, self.customer_id, self.custom_instructions),
                tools=available,
                messages=self._llm_messages,
            )
            self._llm_messages.append({"role": "assistant", "content": response.content})

            text_parts = [b.text for b in response.content if b.type == "text"]
            tool_uses = [b for b in response.content if b.type == "tool_use"]

            escalated = False
            ended = False
            tool_results = []
            for call in tool_uses:
                if call.name == "advance_state":
                    self.sm.advance()
                    tool_results.append({"type": "tool_result", "tool_use_id": call.id,
                                          "content": f"Now in state {self.sm.current.value}"})
                    continue
                try:
                    result = self._call_tool(call.name, call.input)
                except tools.ToolError as e:
                    result = {"error": str(e)}
                if call.name == "escalate_to_human":
                    escalated = True
                    self.sm.escalate()
                tool_results.append({"type": "tool_result", "tool_use_id": call.id, "content": db.json_dump(result)})

            if tool_results:
                self._llm_messages.append({"role": "user", "content": tool_results})

            if response.stop_reason != "tool_use":
                message = "\n".join(text_parts).strip() or "..."
                if self.sm.current == State.END:
                    ended = True
                return TurnResult(message, self.sm.current, escalated=escalated, ended=ended)
