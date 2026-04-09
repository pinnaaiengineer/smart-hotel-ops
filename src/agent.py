"""
Agent: orchestrates the LLM planning loop.

Flow:
  1. Build initial messages (system prompt + user email).
  2. Call the LLM with all READ tools + submit_plan.
  3. If the model calls a read tool → execute it, append result, repeat.
  4. If the model calls submit_plan → parse the plan, exit loop.
  5. Return a PlanResult to the caller (main.py / tests).

The agent NEVER calls write tools.  All writes are delegated to Executor.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import anthropic

from . import config
from .pms import PMS
from .prompts import SYSTEM_PROMPT_TEMPLATE
from .tools import ALL_PLANNING_TOOLS, dispatch_read_tool

logger = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class PlanAction:
    action: str
    params: dict
    description: str


@dataclass
class PlanResult:
    action_plan: list[PlanAction]
    draft_reply: str
    requires_human_review: bool
    review_reason: str = ""
    iterations: int = 0
    tool_calls_log: list[dict] = field(default_factory=list)

    @property
    def has_write_actions(self) -> bool:
        return len(self.action_plan) > 0

    def display_plan(self) -> str:
        """Human-readable plan summary for approval UI."""
        lines = []

        if self.requires_human_review:
            lines.append("⚠️  REQUIRES HUMAN REVIEW")
            if self.review_reason:
                lines.append(f"   Reason: {self.review_reason}")
            lines.append("")

        if self.action_plan:
            lines.append("📋 Proposed Actions:")
            for i, step in enumerate(self.action_plan, 1):
                lines.append(f"  {i}. [{step.action}] {step.description}")
                # Show key params (excluding verbose ones)
                for k, v in step.params.items():
                    if k not in ("notes",):
                        lines.append(f"       {k}: {v}")
        else:
            lines.append("📋 No write actions required (read-only or info request).")

        lines.append("")
        lines.append("✉️  Draft Reply:")
        lines.append("-" * 50)
        lines.append(self.draft_reply)
        lines.append("-" * 50)

        return "\n".join(lines)


# ── Agent ─────────────────────────────────────────────────────────────────────

class HotelEmailAgent:
    """
    Runs the LLM planning loop for a single inbound email.

    Usage:
        agent = HotelEmailAgent(pms, current_date)
        plan = agent.plan(email_body, sender_email)
    """

    def __init__(
        self,
        pms: PMS,
        current_date: Optional[date] = None,
        model: str = config.LLM_MODEL,
        max_iterations: int = config.AGENT_MAX_ITERATIONS,
    ) -> None:
        self.pms = pms
        self.current_date = current_date or date.fromisoformat(config.MOCK_CURRENT_DATE)
        self.model = model
        self.max_iterations = max_iterations
        self._client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    def plan(self, email_body: str, sender_email: str = "") -> PlanResult:
        """
        Run the full planning loop for a single email.
        Returns a PlanResult without executing any write operations.
        """
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            hotel_name=self.pms.hotel["name"],
            hotel_address=self.pms.hotel["address"],
            current_date=self.current_date.isoformat(),
        )

        # Wrap the email so the LLM sees it in a clear, consistent format
        user_message = self._format_email_message(email_body, sender_email)

        messages = [
            {"role": "user", "content": user_message},
        ]

        tool_calls_log: list[dict] = []

        for iteration in range(1, self.max_iterations + 1):
            logger.debug("Agent iteration %d", iteration)

            response = self._client.messages.create(
                model=self.model,
                system=system_prompt,
                messages=messages,
                tools=ALL_PLANNING_TOOLS,
                tool_choice={"type": "any"},  # force the model to always use a tool
                max_tokens=config.LLM_MAX_TOKENS,
            )

            # Anthropic returns a list of content blocks (text and/or tool_use)
            assistant_content = response.content

            # Append the full assistant response to conversation history
            messages.append({"role": "assistant", "content": assistant_content})

            # Collect all tool_use blocks from the response
            tool_use_blocks = [
                block for block in assistant_content
                if block.type == "tool_use"
            ]

            if not tool_use_blocks:
                # Shouldn't happen with tool_choice={"type": "any"}, but guard anyway
                logger.warning("LLM returned no tool calls on iteration %d", iteration)
                continue

            plan_result: Optional[PlanResult] = None

            # Process every tool call in the response (can be batched by the model)
            tool_results: list[dict] = []

            for block in tool_use_blocks:
                tool_name = block.name
                args = block.input if isinstance(block.input, dict) else {}

                tool_calls_log.append({"tool": tool_name, "args": args})
                logger.debug("Tool call: %s(%s)", tool_name, args)

                if tool_name == "submit_plan":
                    # Terminal tool — parse and return
                    plan_result = self._parse_submit_plan(args, iteration, tool_calls_log)
                    # Still need to add a tool result so the message history is valid,
                    # but we won't make another LLM call after this.
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps({"status": "plan_received"}),
                        }
                    )
                else:
                    # Read tool — execute and collect result
                    result_json = dispatch_read_tool(
                        tool_name, args, self.pms, self.current_date
                    )
                    logger.debug("Tool result: %s", result_json[:200])
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_json,
                        }
                    )

            # Anthropic tool results go in a single "user" message
            messages.append({"role": "user", "content": tool_results})

            if plan_result is not None:
                return plan_result

        # Max iterations reached without submit_plan — return a safe fallback
        logger.error("Agent hit max iterations (%d) without submitting a plan", self.max_iterations)
        return PlanResult(
            action_plan=[],
            draft_reply=(
                "Dear Guest,\n\nThank you for your message. Our team is reviewing your request "
                "and will be in touch shortly.\n\nWarm regards,\n"
                f"{self.pms.hotel['name']} Reservations Team"
            ),
            requires_human_review=True,
            review_reason="Agent reached maximum iterations without completing a plan.",
            iterations=self.max_iterations,
            tool_calls_log=tool_calls_log,
        )

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _format_email_message(body: str, sender_email: str) -> str:
        if sender_email:
            return f"From: {sender_email}\n\n{body.strip()}"
        return body.strip()

    @staticmethod
    def _parse_submit_plan(args: dict, iteration: int, log: list[dict]) -> PlanResult:
        raw_plan = args.get("action_plan", [])
        actions = [
            PlanAction(
                action=step["action"],
                params=step.get("params", {}),
                description=step.get("description", step["action"]),
            )
            for step in raw_plan
        ]
        return PlanResult(
            action_plan=actions,
            draft_reply=args.get("draft_reply", ""),
            requires_human_review=args.get("requires_human_review", False),
            review_reason=args.get("review_reason", ""),
            iterations=iteration,
            tool_calls_log=log,
        )