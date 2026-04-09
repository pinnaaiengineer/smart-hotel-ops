#!/usr/bin/env python3
"""
Hotel AI Email Agent — CLI

Usage:
    python main.py                        # interactive mode
    python main.py --mode autonomous      # override approval mode
    python main.py --mode human           # force human approval
    python main.py --demo <1|2|3>         # run a pre-baked demo scenario

Environment variables (or .env file):
    ANTHROPIC_API_KEY   required
    APPROVAL_MODE       human | autonomous   (default: human)
    MOCK_CURRENT_DATE   YYYY-MM-DD           (default: 2025-04-18)
    LLM_MODEL           (default: claude-haiku-4-5)
"""
from __future__ import annotations

import argparse
import sys
import textwrap
from datetime import date

from src import config
from src.agent import HotelEmailAgent
from src.executor import execute_plan
from src.pms import PMS

def print_header(text: str) -> None:
    bar = "═" * 60
    print(f"\n{bar}")
    print(f"  {text}")
    print(f"{bar}")


def print_section(title: str, content: str) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print(f"{'─' * 60}")
    print(content)


def confirm(prompt: str) -> bool:
    while True:
        answer = input(f"\n{prompt} [y/n]: ").strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please enter y or n.")


def mock_send_email(to: str, body: str) -> None:
    """Simulate sending the reply email."""
    print_section(" Email sent (mock)", f"To: {to}\n\n{body}")


# ── Main ──────────────────────────────────────────────────────────────────────

def run(
    email_body: str,
    sender_email: str,
    approval_mode: str,
    pms: PMS,
    current_date: date,
    auto_approve: bool = False,  # used in tests / demo scripts
) -> dict:
    """
    Core runner — returns a result dict for programmatic use (tests, etc.).
    Also drives the CLI interaction when auto_approve=False.
    """
    print_header(f"Processing email from: {sender_email or '(unknown)'}")
    print(f"\nApproval mode: {approval_mode.upper()}")
    print(f"Today's date:  {current_date}\n")

    # ── Plan ──────────────────────────────────────────────────────────────────
    print(" Agent is analysing the email…")
    agent = HotelEmailAgent(pms=pms, current_date=current_date)
    plan = agent.plan(email_body, sender_email)

    print(f"\n[Agent used {plan.iterations} iteration(s), "
          f"{len(plan.tool_calls_log)} tool call(s)]")
    tool_names = [tc["tool"] for tc in plan.tool_calls_log]
    print(f"Tools called: {', '.join(tool_names)}")

    # ── Display plan ──────────────────────────────────────────────────────────
    print_section(" Agent Plan & Draft Reply", plan.display_plan())

    # ── Human review escalation ───────────────────────────────────────────────
    if plan.requires_human_review:
        print("\n  This request requires human review. No automated actions will be taken.")
        mock_send_email(sender_email, plan.draft_reply)
        return {"status": "escalated", "reason": plan.review_reason, "plan": plan}

    # ── Read-only ─────────────────────────────────────────────────────────────
    if not plan.has_write_actions:
        print("\n  No write actions needed. Sending informational reply.")
        mock_send_email(sender_email, plan.draft_reply)
        return {"status": "read_only", "plan": plan}

    # ── Execute ───────────────────────────────────────────────────────────────
    should_execute = False

    if approval_mode == "autonomous":
        print("\n Autonomous mode: executing plan automatically…")
        should_execute = True

    elif approval_mode == "human":
        if auto_approve:
            should_execute = True
        else:
            should_execute = confirm("👤 Approve this plan and send the reply?")

    if not should_execute:
        print("\n✋ Plan rejected. No actions taken.")
        return {"status": "rejected", "plan": plan}

    print("\n  Executing plan…")
    action_dicts = [
        {"action": a.action, "params": a.params, "description": a.description}
        for a in plan.action_plan
    ]
    exec_result = execute_plan(pms, action_dicts)

    print_section("  Execution Results", exec_result.summary)

    if exec_result.all_succeeded:
        # Personalise the reply with any generated reservation ID
        reply = plan.draft_reply
        for result in exec_result.results:
            if result.action == "create_reservation":
                res_id = result.result.get("id", "")
                if res_id and "PLACEHOLDER" in reply:
                    reply = reply.replace("PLACEHOLDER", res_id)
        mock_send_email(sender_email, reply)
        return {"status": "executed", "plan": plan, "execution": exec_result}
    else:
        print("\n Some actions failed. Review the errors above.")
        return {"status": "partial_failure", "plan": plan, "execution": exec_result}


def main() -> None:
    parser = argparse.ArgumentParser(description="Hotel AI Email Agent")
    parser.add_argument(
        "--mode",
        choices=["human", "autonomous"],
        default=None,
        help="Override APPROVAL_MODE from environment",
    )
    parser.add_argument(
        "--demo",
        choices=["1", "2", "3"],
        default=None,
        help="Run a pre-built demo scenario",
    )
    args = parser.parse_args()

    if not config.ANTHROPIC_API_KEY:
        print(" ANTHROPIC_API_KEY is not set. Please add it to your .env file.")
        sys.exit(1)

    approval_mode = args.mode or config.APPROVAL_MODE
    current_date = date.fromisoformat(config.MOCK_CURRENT_DATE)
    pms = PMS(config.DATA_PATH)

    if args.demo:
        scenario = DEMO_SCENARIOS[args.demo]
        print_header(f"Demo Scenario {args.demo}: {scenario['label']}")
        print(f"\nFrom: {scenario['sender']}")
        print(f"\nEmail:\n{textwrap.indent(scenario['body'], '  ')}")
        run(
            email_body=scenario["body"],
            sender_email=scenario["sender"],
            approval_mode=approval_mode,
            pms=pms,
            current_date=current_date,
        )
    else:
        # Interactive mode
        print_header("Hotel AI Email Agent — Interactive Mode")
        print(f"\nApproval mode: {approval_mode.upper()}")
        print("Type your email below. Enter a blank line to finish.\n")

        sender = input("From (email address): ").strip()
        print("Email body (blank line to submit):")
        lines = []
        while True:
            line = input()
            if not line.strip():
                break
            lines.append(line)
        body = "\n".join(lines)

        if not body.strip():
            print("No email body entered. Exiting.")
            sys.exit(0)

        run(
            email_body=body,
            sender_email=sender,
            approval_mode=approval_mode,
            pms=pms,
            current_date=current_date,
        )


if __name__ == "__main__":
    main()