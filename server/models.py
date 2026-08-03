"""
Core data models for the user-activity simulation platform.

These models are the contract between:
  - the scenario engine (resolves persona YAML -> concrete ActionSpecs)
  - the server API (dispatches ActionSpecs to agents over the OOB channel)
  - the agent (executes ActionSpecs, reports IntentRecord / CompletionRecord)
  - the scoring harness (reads the ground-truth ledger, compares to detection
    tool output)

Keeping these in one module means the server and agent share an identical
understanding of the wire format. Copy this file into both services, or
(better, once this becomes a real package) import it from a shared library.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class ActionType(str, Enum):
    WEB_BROWSE = "web_browse"
    EMAIL_SEND = "email_send"
    OFFICE_DOC = "office_doc"
    SMB_ACCESS = "smb_access"
    # Extend as new action modules are built. Keep this enum and the
    # agent's action registry (agent/actions/__init__.py) in sync.


class ActionSpec(BaseModel):
    """A single, fully-resolved unit of work dispatched to one agent.

    'Fully resolved' means all randomness (which target, how long, which
    template) has already been rolled by the scenario engine using the
    run's seed -- the agent should not need to make any nondeterministic
    choices itself. This is what makes runs reproducible: the ActionSpec
    *is* the ground truth of what was intended.
    """

    action_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str
    persona: str
    host: str
    action_type: ActionType
    params: dict[str, Any] = Field(default_factory=dict)
    intended_start: datetime
    should_alert: bool = False  # true only for injected malicious/red-team actions
    expected_artifacts: list[str] = Field(default_factory=list)


class IntentRecord(BaseModel):
    """Logged by the agent immediately BEFORE executing an action.

    This is written to the ledger before the action runs, so that even if
    the agent crashes mid-action, the ledger still shows intent -- useful
    both for debugging and for not silently losing ground truth.
    """

    action_id: str
    run_id: str
    host: str
    action_type: ActionType
    params: dict[str, Any]
    logged_at: datetime = Field(default_factory=datetime.utcnow)


class CompletionRecord(BaseModel):
    """Logged by the agent immediately AFTER executing an action."""

    action_id: str
    run_id: str
    host: str
    actual_start: datetime
    actual_end: datetime
    exit_status: str  # "success" | "failure" | "partial"
    observed_side_effects: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None


class AgentRegistration(BaseModel):
    host: str
    os: str  # "windows" | "linux"
    persona: Optional[str] = None
    agent_version: str = "0.1.0"


class PollResponse(BaseModel):
    actions: list[ActionSpec] = Field(default_factory=list)
