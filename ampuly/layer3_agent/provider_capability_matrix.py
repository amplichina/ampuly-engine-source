"""What each Provider can do, in four states that cannot be collapsed into two.

"Supported" and "unsupported" hide the two answers that actually matter to a
delivery: *we have not built it yet*, and *nobody has checked*. Both of those
are debts. Folding them into "unsupported" makes a gap look like a decision,
and folding them into "supported" makes a plan look like a fact.

So a capability is one of:

  wired        the upstream supports it and we have implemented it, with
               evidence naming what proves it;
  not_built    the upstream supports it and we have not implemented it -- an
               outstanding item, not a property of the Provider;
  unsupported  the upstream genuinely cannot do it, with the reason;
  unverified   nobody has established which of the above is true. The default,
               because assuming is how a matrix stops being evidence.

A capability may only be ``wired`` with an evidence pointer. Declaring it
without one is refused: a claim nobody can check is worth less than an honest
"unverified".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

STATES = ("wired", "not_built", "unsupported", "unverified")

# The capabilities a delivery actually depends on. Named for what they do for
# the business, not for whichever method a vendor happens to expose.
CAPABILITIES = (
    "start_turn",
    "stream_events",
    "resume_session",
    "interrupt",
    "steer",
    "tool_approval",
    "collect_artifacts",
    "cancel",
    "budget_enforcement",
    "credential_isolation",
)


class CapabilityClaimError(ValueError):
    """A claim that cannot be checked is not allowed into the matrix."""


@dataclass(frozen=True)
class CapabilityClaim:
    capability: str
    state: str = "unverified"
    evidence: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if self.capability not in CAPABILITIES:
            raise CapabilityClaimError(f"unknown capability {self.capability!r}")
        if self.state not in STATES:
            raise CapabilityClaimError(f"unknown state {self.state!r} for {self.capability!r}")
        if self.state == "wired" and not str(self.evidence or "").strip():
            raise CapabilityClaimError(
                f"{self.capability!r} claims to be wired with no evidence; "
                "an unverifiable claim is worth less than an honest 'unverified'"
            )
        if self.state == "unsupported" and not str(self.note or "").strip():
            raise CapabilityClaimError(
                f"{self.capability!r} claims the upstream cannot do it, with no reason given"
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "state": self.state,
            "evidence": self.evidence,
            "note": self.note,
        }


@dataclass
class ProviderMatrix:
    """One Provider's row, with every capability accounted for."""

    provider: str
    claims: dict[str, CapabilityClaim] = field(default_factory=dict)

    def declare(self, capability: str, state: str, *, evidence: str = "", note: str = "") -> None:
        claim = CapabilityClaim(capability=capability, state=state, evidence=evidence, note=note)
        self.claims[claim.capability] = claim

    def claim(self, capability: str) -> CapabilityClaim:
        """Anything not declared is unverified, never assumed working."""
        return self.claims.get(capability) or CapabilityClaim(capability=capability)

    @property
    def outstanding(self) -> list[str]:
        """What is owed: built neither, or checked never."""
        return [c for c in CAPABILITIES if self.claim(c).state in {"not_built", "unverified"}]

    @property
    def production_ready(self) -> bool:
        """Never true from declarations alone.

        A matrix says what someone believes; production eligibility comes from
        the behaviour suite (F2) plus a real account (the V track). This
        property exists so nothing else is tempted to infer readiness here.
        """
        return False

    def to_payload(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "capabilities": [self.claim(c).to_payload() for c in CAPABILITIES],
            "outstanding": self.outstanding,
            "production_ready": self.production_ready,
        }


def native_matrix() -> ProviderMatrix:
    """Our own runtime, reached through the harness bridge."""
    matrix = ProviderMatrix("native")
    matrix.declare("start_turn", "wired", evidence="tests/test_harness_native_bridge.py")
    matrix.declare("stream_events", "wired", evidence="tests/test_harness_native_bridge.py::RecordTests")
    matrix.declare("resume_session", "wired", evidence="tests/test_harness_native_bridge.py::PauseTests")
    matrix.declare(
        "interrupt", "wired", evidence="tests/test_harness_native_bridge.py::PauseTests",
        note="cancels the owned turn coroutine and awaits runtime cleanup; session becomes suspended, in-flight effects are not claimed rolled back",
    )
    matrix.declare(
        "steer", "wired", evidence="tests/test_native_runs_through_harness.py::SteerTests",
        note="uses the queue the loop already reads between steps; a steer with no running turn to reach returns false rather than vanishing",
    )
    matrix.declare("tool_approval", "wired", evidence="tests/test_permission_chain_only_narrows.py")
    matrix.declare("collect_artifacts", "wired", evidence="tests/test_harness_native_bridge.py::RecordTests")
    matrix.declare("cancel", "not_built", note="turn interruption is wired; a distinct durable business-run cancellation operation is not implemented by this bridge")
    matrix.declare(
        "budget_enforcement", "unverified", evidence="tests/test_native_model_budget.py",
        note="native task-bound model calls, retry counts and uncertain outcomes have offline coverage; monetary caps refuse dispatch until priced reservations exist, and all entry-point scope propagation remains unverified",
    )
    matrix.declare(
        "credential_isolation", "unverified", evidence="tests/test_credential_call_paths.py",
        note="customer image routes including reference edits have targeted tests; provider-wide task scope propagation and every credential sink remain unverified",
    )
    return matrix


def codex_sdk_matrix() -> ProviderMatrix:
    """Codex through the installed official SDK and its bundled app-server."""
    matrix = ProviderMatrix("codex_sdk")
    matrix.declare("start_turn", "wired", evidence="tests/test_harness_codex_sdk.py::ProtocolTests")
    matrix.declare(
        "stream_events", "wired", evidence="tests/test_harness_codex_sdk.py::StreamingTests",
        note="follows turn.stream(); a stream ending without turn/completed is refused, not reported done",
    )
    matrix.declare("resume_session", "wired", evidence="tests/test_harness_codex_sdk.py::ProtocolTests")
    matrix.declare(
        "interrupt", "wired", evidence="tests/test_harness_codex_sdk.py::InterruptTests",
        note="calls AsyncTurnHandle.interrupt(), retains the reusable thread and suspends only the current turn; "
             "verified against the official SDK-shaped injected transport, with a live-account check still in V-track",
    )
    matrix.declare(
        "steer", "wired", evidence="tests/test_harness_codex_sdk.py::SteerTests",
        note="the SDK hands back a handle to the running turn and that handle takes steering input; "
             "every refusal is written down with its reason -- no turn running, this SDK build cannot "
             "steer, or the call failed -- rather than one silent False. Verified against an injected "
             "client; a live steer on a real account is still V-track",
    )
    matrix.declare(
        "tool_approval", "unverified",
        note="2026-09-10: the SDK ships a default handler that accepts command execution and file changes, and "
             "this provider was building its client without one -- a library was signing for work we are meant "
             "to gate, unrecorded. Approvals now resolve the official request threadId back to exactly one product "
             "session; an unknown or ambiguous id is denied before any gate runs. With no gate bound the answer is "
             "also no. The official typed low-level API is now configured as on-request with no model reviewer, "
             "and its reader thread waits on the existing durable owner checkpoint. This remains unverified "
             "rather than wired because the component is not yet selected by the production engine and nobody "
             "has yet seen the path fire on a real account",
    )
    matrix.declare(
        "collect_artifacts", "wired", evidence="tests/test_harness_codex_sdk.py::ArtifactTests",
        note="completed items are recorded as the provider described them; after restart the hot cache is rebuilt "
             "from official thread/read and a partial history is refused rather than reported complete",
    )
    matrix.declare("cancel", "unverified", note="not checked")
    matrix.declare(
        "budget_enforcement", "wired", evidence="tests/test_harness_codex_sdk.py::BudgetTests",
        note="turn count only: Codex bills its own account, so the model cost is not ours to hold",
    )
    matrix.declare("credential_isolation", "unverified", note="the SDK reads its own auth; per-customer keys not mapped")
    return matrix


def cli_adapter_matrix(provider: str) -> ProviderMatrix:
    """A CLI turn as the harness drives it, and little else.

    Claude Code takes this row as it stands. OpenCode starts here and then
    corrects two claims in ``opencode_matrix``; this docstring does not
    say the two CLIs behave alike.
    """
    matrix = ProviderMatrix(provider)
    # A turn now goes through the harness contract with its session and events
    # recorded (the wrapper), which is what this row is about. Whether a real
    # Claude Code or OpenCode process behaves as expected is still V-track.
    matrix.declare(
        "start_turn", "wired", evidence="tests/test_harness_recording.py",
        note="through the recording wrapper; a real provider process is not verified here",
    )
    matrix.declare(
        "stream_events", "wired", evidence="tests/test_harness_recording.py::RecordTests",
        note="whatever the adapter emits is recorded in order; a CLI that emits nothing records nothing",
    )
    matrix.declare(
        "resume_session", "wired", evidence="tests/test_harness_recording.py::MappingTests",
        note="the mapping survives a restart; a failed provider resume is recorded as failed",
    )
    matrix.declare(
        "interrupt", "wired", evidence="tests/test_harness_cli_turn.py::StoppedTurnTests",
        note="the process is stopped and the turn ends as cancelled carrying what it had written; there is no resuming the same turn afterwards",
    )
    matrix.declare("steer", "unsupported", note="a CLI turn takes no mid-flight input")
    matrix.declare("tool_approval", "not_built", note="the provider runs its own tools; our gates do not see them")
    matrix.declare(
        "collect_artifacts", "wired", evidence="tests/test_harness_cli_turn.py::TurnArtifactTests",
        note="the workspace is compared before and after the turn; created, modified and deleted are kept apart, and a scan that hit its bound says so rather than reading as complete",
    )
    matrix.declare(
        "cancel", "wired", evidence="tests/test_harness_cli_turn.py::StoppedTurnTests",
        note="a deadline or an interrupt stops the provider and returns a cancelled turn with its files, rather than raising and losing them",
    )
    matrix.declare(
        "budget_enforcement", "not_built",
        note="a per-turn wall clock bounds how long a turn may take, which is not a spend budget: the provider's own model calls are billed to its own account and our budget does not hold them",
    )
    matrix.declare("credential_isolation", "unverified", note="the CLI reads its own credentials")
    return matrix


def cursor_matrix() -> ProviderMatrix:
    """Cursor through its own CLI, which answers its own approvals.

    The same recording wrapper covers the turn as for the other CLIs, so those
    rows read the same. The difference is `tool_approval`: Cursor emits its own
    interaction events and resolves them itself, which our adapter deliberately
    does not treat as prompts -- so our gates never see its tool decisions.
    """
    matrix = cli_adapter_matrix("cursor")
    matrix.declare(
        "tool_approval", "unsupported",
        note="cursor-agent answers its own approval events; treating that stream as our prompt "
             "would create cards nobody can act on, so its tool decisions are never ours to gate",
    )
    return matrix


def opencode_matrix() -> ProviderMatrix:
    """OpenCode through its own CLI, which is not Claude Code under another name.

    It shared Claude Code's row until this one existed, which read as "these
    two behave alike". Two of those claims were wrong for OpenCode, and neither
    was wrong in OpenCode's favour.
    """
    matrix = cli_adapter_matrix("opencode")
    matrix.declare(
        "tool_approval", "unverified",
        note="an approval bridge exists (--permission-handler), gated on an upstream flag nobody "
             "has confirmed against a real binary; until somebody runs one, whether our gates see "
             "its tool decisions is unknown rather than answered",
    )
    matrix.declare(
        "credential_isolation", "not_built",
        note="the isolated home redirects OPENCODE_CONFIG_DIR and mirrors config, agents, commands "
             "and plugins -- never an auth file, the way the Codex adapter mirrors auth.json. The "
             "child gets OPENCODE_API_KEY if it was set on purpose and nothing else, so a turn runs "
             "on whatever global login the machine holds; per-customer routing is implementable and "
             "unimplemented",
    )
    return matrix


def all_matrices() -> list[ProviderMatrix]:
    return [
        native_matrix(),
        codex_sdk_matrix(),
        cli_adapter_matrix("claude_code"),
        opencode_matrix(),
        # Corrected 2026-09-08: this row previously said Cursor had no local
        # candidate at all, and `adapters/cursor_adapter.py` had been there the
        # whole time. An empty row read as "nothing exists" rather than
        # "nobody checked", which is the confusion this whole file exists to
        # prevent -- so it now gets the same treatment as the other CLIs.
        cursor_matrix(),
    ]
