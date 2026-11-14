"""What master control is allowed to do, declared once and in one place.

Today master control emits one JSON object naming at most one of three
actions, and the handler executes it. Turning that into a tool loop makes the
question "what can it reach" a live one every iteration instead of once per
turn, so the answer has to exist as something testable before the loop does.

This module is that answer. It is a declaration, not an executor: it says
which tools exist, which are reads and which are writes, and which existing
handler each write is required to go through. Nothing here calls anything.

Three rules it exists to hold, each because breaking it is easy and quiet:

**One command, one path.** Every write names the handler method that already
implements it -- the same one the console button and the Feishu gate use.
That single funnel is what makes the idempotency key and the checkpoint
compare-and-set mean anything. A tool that reached the domain service directly
would be a second path to the same command, and the two would agree right up
until they didn't.

**A restricted set is a list, never a default.** The tool registry once read
an empty allow-list as *unrestricted*, so "no tools configured" and "every tool
in the registry" were the same value and a loop inheriting that reached the
shell. That is fixed at the registry now. :func:`allowed_tool_names` still
refuses to return an empty list, for its own reason: a turn with nothing to
call cannot look anything up, and would answer from its opening snapshot while
appearing to work.

**Reads and writes are not the same risk.** A read can run and be shown. A
write spends money, sends things, or changes an order, so each is marked and
each carries the receipt it must produce. Marking them together and sorting it
out at the call site is how a write ends up somewhere a read was expected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ampuly.core.native_permissions import NATIVE_PERMISSION_EFFECTS


@dataclass(frozen=True)
class MasterTool:
    """One thing master control may do, and what doing it commits to.

    ``handler`` names the console handler method that must perform a write.
    It is a name rather than a callable so this declaration can be read and
    tested without importing the console, and so a rename that orphans a tool
    is caught by a test rather than at runtime.
    """

    name: str
    summary: str
    writes: bool
    handler: str = ""
    needs_owner: bool = False
    spends_money: bool = False
    # What the permission layer is told this tool does. Every master tool
    # reaches the product's own store through the console handler and nothing
    # else, which is `runtime_internal`. It is a field rather than a constant
    # because a tool that one day reaches further has to say so here, where it
    # already says whether it is a write -- and because leaving it off entirely
    # is the failure this exists to prevent: an undeclared tool is approved as
    # "no declared native permission effects" and then runs outside the sandbox
    # every declared tool runs inside.
    effects: tuple[str, ...] = ("runtime_internal",)

    def __post_init__(self) -> None:
        if self.writes and not self.handler:
            raise ValueError(f"{self.name}: a write must name the handler it goes through")
        if not self.writes and self.handler:
            raise ValueError(f"{self.name}: a read must not name a write handler")
        if not self.effects:
            raise ValueError(f"{self.name}: a tool with no declared effects is approved as unknown")
        unknown = set(self.effects) - NATIVE_PERMISSION_EFFECTS
        if unknown:
            raise ValueError(
                f"{self.name}: effect(s) the permission policy does not know: "
                + ", ".join(sorted(unknown))
            )


# Reads. Each answers a question the owner actually asks, from the authoritative
# store rather than from the model's memory of an earlier turn.
_READS: tuple[MasterTool, ...] = (
    MasterTool(name="master_resume_flow_draft", summary=(
        "Read ONE exact stored workflow draft by source reference (project/message/sha256), including its current "
        "materialized package revision when present. Never choose a latest conversation or rebase a changed draft. "
        "The reply carries the original draft card so its existing builder draft can be continued."),
        writes=False, effects=("local_read",)),
    MasterTool(name="master_propose_component_candidate", summary=(
        "Draft ONE missing Skill or subflow as a structured untested candidate, anchored to an exact original "
        "workflow-draft reference and step_id. Skill details: skill_id, method, acceptance. Flow details: intent "
        "in master_propose_flow_draft format. Only preview: no install, test, admission, publication or execution."),
        writes=False, effects=("local_read",)),
    MasterTool(
        name="master_list_orders",
        summary="The project's orders with status, what each is waiting on, and how long.",
        writes=False,
    ),
    MasterTool(
        name="master_order_detail",
        summary="One order: its stage, its pending checkpoints, and its recorded cost.",
        writes=False,
    ),
    MasterTool(
        name="master_list_blockers",
        summary="What is waiting on a person right now, with who and for how long.",
        writes=False,
    ),
    MasterTool(
        name="master_capabilities",
        summary="What this installation actually has: tools that reach outside, model, executor.",
        writes=False,
    ),
    MasterTool(
        name="master_process_drafts",
        summary="Process descriptions being built, and what each still cannot answer.",
        writes=False,
    ),
    # policy (M1): work starts from a work order, so the one thing master_create_order needs to know is
    # which flows this project takes orders on and what each asks for. Read from the loaded flow packages (disk) and
    # the project's adoptions; another project's flow is not named.
    MasterTool(
        name="master_list_order_flows",
        summary=(
            "The flows this project takes orders on right now, each with its start fields (id, label, required) and "
            "the sites its browser steps may open; and the ones it does not, with why. master_create_order drafts on these."
        ),
        writes=False,
        effects=("local_read",),
    ),
    MasterTool(
        name="master_connector_candidates",
        summary="Generated connector candidates in this project, with status and locked fingerprint; no credentials.",
        writes=False,
        effects=("local_read",),
    ),
    MasterTool(
        name="master_improvement_proposals",
        summary=(
            "Evidence-bound improvement proposals in this project: safe metadata, target fingerprint and status only; "
            "no evidence body, candidate body, credentials or local paths."
        ),
        writes=False,
        effects=("local_read",),
    ),
    # The two reads that touch disk. Their reach is the policy
    # decision -- candidate packages and one named order's deliverables --
    # and it is enforced in master_files, not by this summary. `local_read`
    # is declared because it is true: these are the only master tools whose
    # answer comes from the filesystem rather than the store.
    MasterTool(
        name="master_list_files",
        summary=(
            "Files inside one candidate process package (by package_id) or one order's "
            "deliverables (by task_id or order_id, passed as task_id). With neither, the candidates on offer. "
            "Nothing else on disk."
        ),
        writes=False,
        effects=("local_read",),
    ),
    MasterTool(
        name="master_preflight_candidate",
        summary=(
            "The console's read-only preflight of one candidate package with the given intake fields: "
            "blockers, warnings and the simulated plan. No model call, no writes. Do this before a trial."
        ),
        writes=False,
        effects=("local_read",),
    ),
    MasterTool(
        name="master_read_file",
        summary=(
            "A window of one text file from a candidate package (package_id + path) or one order's "
            "deliverables (task_id or order_id as task_id, + path). Images and binaries are described, not read."
        ),
        writes=False,
        effects=("local_read",),
    ),
    # The policy decision: drafting or pre-building a workflow, master control calls every component that
    # may help, and people test and admit them. Looking up is a read; nothing is downloaded or admitted by it. It reads
    # this machine only: the master runtime asks nobody, so no master tool reaches the network
    # (tests/test_policyless_runtimes_cannot_reach.py); the online market is whatever the owner's 组件库 page loaded.
    MasterTool(
        name="master_search_components",
        summary=(
            "Components that may help a workflow: the internal library (usable on steps now), the local resource "
            "catalog and the online market results the owner's Components page already loaded or searched. Paid skills "
            "left out. Nothing is downloaded; an outside component you put on a step becomes a candidate the owner tries "
            "and admits."
        ),
        writes=False,
        effects=("local_read",),
    ),
    # A02's second half since the policy decision: master control only proposes a node-table
    # change. The proposal is checked exactly as a save would be (preview_package_nodes), saves nothing, and
    # rides on the reply; the owner reviews it on the builder canvas, applies it to a draft and saves it
    # there. A read, then, and an honest one -- but owner-only and once per message, enforced where it runs.
    MasterTool(
        name="master_propose_candidate_nodes",
        summary=(
            "Propose a new node table for one draft or testing package. Needs the version you read "
            "via master_read_file and a new version. NOTHING IS SAVED: the owner reviews the change on the "
            "builder canvas, applies it and saves it there. Refused on a stale version or an invalid table."
        ),
        writes=False,
        effects=("local_read",),
    ),
    # 4.7 Q04-B: a NEW flow from the owner's words and files. The model hands over a structured requirement spec; the
    # console compiles it (real internal-library components only, placeholders where nothing fits, roles from a saved
    # team, the graph checked) into a draft shown on a card. Nothing is created until the owner opens it on the builder.
    MasterTool(
        name="master_propose_flow_draft",
        summary=(
            "Propose a NEW workflow from the owner's requirement: give the steps as a structured spec (who does each, "
            "what it delivers and needs, which capability it wants). The console matches real internal-library "
            "components, keeps placeholders where none fits, checks the graph and shows the draft on a card. NOTHING "
            "IS CREATED OR SAVED: the owner opens it on the builder and saves it there."
        ),
        writes=False,
        effects=("local_read",),
    ),
    # 总控施工模式 (policy, B1): the build jobs of this project and the lines one may start from. Read from
    # the job records (master_build.py) and the loaded packages; another project's line is not listed.
    MasterTool(
        name="master_list_builds",
        summary=(
            "This project's build jobs -- each with its state in words, the owner's goal, its draft and what its last "
            "hand-over said -- and the business lines a build may start from (line_id). job_id is for "
            "master_continue_build only."
        ),
        writes=False,
        effects=("local_read",),
    ),
)


# Writes. The original operational verbs remain, and narrowly governed draft
# producers have since joined them. The loop adds iterations, not a bypass:
# every entry still names the sole console handler that implements it.
#
# The fourth, interrupting a turn, is new and is deliberately *narrower* than
# what was already here. Without it the only way to stop a turn from the
# conversation was to stop the whole order, so "never mind, do this instead"
# cost the run. Adding it reduces the damage of the available options rather
# than widening what master control may do.
_WRITES: tuple[MasterTool, ...] = (
    # policy (M1, option B): work starts from a work order and 安安 only drafts one. The order is created
    # through the console's own entry and waits in 业务工单 「待接单」 for the owner; nothing runs, no session is
    # opened and nothing is spent, so it is not marked as spending. Still a write (an order row exists afterwards) and
    # still the owner's.
    MasterTool(
        name="master_create_order",
        summary=(
            "Draft ONE work order on a flow this project takes orders on (workflow_id from master_list_order_flows), "
            "its start fields filled from the owner's words. It waits in 业务工单 「待接单」 for the owner to release; "
            "nothing runs, no session starts, nothing is spent."
        ),
        writes=True,
        handler="_master_draft_order",
        needs_owner=True,
    ),
    MasterTool(
        name="master_reply_checkpoint",
        summary="Answer one waiting checkpoint, approve or deny, only on an explicit instruction.",
        writes=True,
        handler="_master_control_verb",
        needs_owner=True,
    ),
    MasterTool(
        name="master_stop_order",
        summary="Stop one running order. What already happened is not rolled back.",
        writes=True,
        handler="_master_control_verb",
        needs_owner=True,
    ),
    MasterTool(
        name="master_interrupt_turn",
        summary=(
            "Ask the turn an order is running right now to stop, leaving the "
            "order open. Use this for 'never mind, do this instead'; stopping "
            "the order is the other tool."
        ),
        writes=True,
        handler="_master_control_verb",
        needs_owner=True,
    ),
    MasterTool(
        name="master_steer_turn",
        summary=(
            "Send a correction or new constraint into the turn an order is running right now. "
            "Acceptance does not prove execution. The order and turn keep running. "
            "If delivery is unknown, do not resend, interrupt, or resume without a new owner instruction."
        ),
        writes=True,
        handler="_master_control_verb",
        needs_owner=True,
    ),
    MasterTool(
        name="master_delegate_browser",
        summary=(
            "Give one open order a browser session scoped to the websites the owner named: "
            "allowed domains, whether the agent may see screenshots, whether it may download, "
            "and for how many minutes it holds control. The order's browser tools refuse "
            "anything outside that scope."
        ),
        writes=True,
        handler="_master_delegate_browser",
        needs_owner=True,
    ),
    # A02's third step. policy: startable from the
    # conversation, owner only, once per message, and it spends -- the
    # offline runner puts the draft through the real scheduler and the real
    # model, in a home of its own with no customer, account or outbound
    # send. Same claim/timeout/settle/round record as the console button;
    # a failed round is kept and the next run is a new round.
    # A19: "这单先别提醒". A preference about attention, not a decision:
    # the checkpoint stays pending, nothing is approved or cancelled, the
    # reminder is held until a time the owner chose. Owner only, recorded
    # with who and why.
    MasterTool(
        name="master_snooze_blocker",
        summary=(
            "Hold the reminder for one pending checkpoint until a time (ISO timestamp). It stays pending "
            "and nothing is decided; use when the owner says not to be reminded about it for now."
        ),
        writes=True,
        handler="_master_snooze_blocker",
        needs_owner=True,
    ),
    MasterTool(
        name="master_run_offline_trial",
        summary=(
            "Run one saved process description offline (spec_id, optional version) and report what it "
            "established: outcome, the step that failed and why. Spends model calls; the record is kept."
        ),
        writes=True,
        handler="_master_run_trial",
        needs_owner=True,
        spends_money=True,
        effects=("workspace_write",),
    ),
    MasterTool(
        name="master_generate_connector_candidate",
        summary=(
            "Turn a structured connector contract from this conversation into an inert declarative candidate. "
            "For a catalog component include catalog_id and its displayed catalog_fingerprint; the formal "
            "contract source may resolve a missing implementation-source blocker. It does not test, approve, "
            "register, enable, install dependencies, or contact the platform."
        ),
        writes=True,
        handler="_master_generate_connector_candidate",
        needs_owner=True,
        effects=("workspace_write",),
    ),
    MasterTool(
        name="master_create_improvement_proposal",
        summary=(
            "Persist one evidence-bound process, connector or Skill improvement as an inert draft. "
            "It does not test, approve, stage, activate, publish or share anything. Given only a target and the "
            "owner's one-sentence request, the server drafts the candidate from the asset and materializes it, inert."
        ),
        writes=True,
        handler="_master_create_improvement_proposal",
        needs_owner=True,
        effects=("workspace_write",),
    ),
    MasterTool(
        name="master_create_improvement_from_acceptance",
        summary=(
            "Record one process improvement drafted from an order's durable customer rejection. The order's "
            "verified event chain, installed workflow version and rejection event bind the draft; the caller "
            "supplies only the title, rationale and candidate. Inert: nothing is materialized, tested, approved or active."
        ),
        writes=True,
        handler="_master_create_improvement_from_acceptance",
        needs_owner=True,
        effects=("workspace_write",),
    ),
    # 总控施工模式 (policy, B1): the owner's own console only. A build works in a DRAFT of this project and
    # asks the owner before it goes online or outside the draft; going live, sending, paying and deleting stay the
    # owner's. It writes a draft and a job record (workspace_write) and spends model calls on its own capped budget.
    MasterTool(
        name="master_start_build",
        summary=(
            "Start ONE build job when the owner asks to build a business line or to change one (its skills, steps, "
            "prompts or tests): it works on its own in a draft of this project -- a copy of the line (line_id from "
            "master_list_builds) or a new blank line -- checks and tries the draft offline, and hands a candidate over "
            "in this conversation. It never goes live, sends, pays or deletes. One build at a time per project; it "
            "spends model calls up to the build cap."
        ),
        writes=True,
        handler="_master_start_build",
        needs_owner=True,
        spends_money=True,
        effects=("workspace_write",),
    ),
    MasterTool(
        name="master_continue_build",
        summary=(
            "Let a build job that stopped or handed over its candidate go on with the owner's next instruction "
            "(job_id from master_list_builds); it carries on in the same session and remembers what it did. A build "
            "that is still working is steered or stopped from its card instead."
        ),
        writes=True,
        handler="_master_continue_build",
        needs_owner=True,
        spends_money=True,
        effects=("workspace_write",),
    ),
)


MASTER_TOOLS: tuple[MasterTool, ...] = _READS + _WRITES


def by_name(name: str) -> MasterTool | None:
    clean = str(name or "").strip()
    return next((tool for tool in MASTER_TOOLS if tool.name == clean), None)


def read_tools() -> tuple[MasterTool, ...]:
    return tuple(tool for tool in MASTER_TOOLS if not tool.writes)


def write_tools() -> tuple[MasterTool, ...]:
    return tuple(tool for tool in MASTER_TOOLS if tool.writes)


def allowed_tool_names(*, include_writes: bool = True) -> list[str]:
    """The exact tool names a master-control turn may call.

    Never empty, and no longer for the reason it once was. The registry used to
    read an empty allow-list as "no restriction" and hand back everything; it
    now grants nothing, so this is no longer what stands between a master turn
    and the shell.

    It still refuses, because a turn with no tools cannot look anything up. It
    would answer from the snapshot it began with -- which is exactly the
    behaviour the loop exists to replace -- and it would look like it worked. A
    caller that wants a read-only turn gets the read tools; a caller that wants
    neither has a bug, and hearing about it is better than a turn that silently
    degrades.
    """
    tools = MASTER_TOOLS if include_writes else read_tools()
    names = [tool.name for tool in tools]
    if not names:
        raise ValueError(
            "a master turn with no tools cannot look anything up; it would "
            "answer from its opening snapshot and look like it worked"
        )
    return names


def write_handlers() -> dict[str, str]:
    """Which console handler each write tool must go through."""
    return {tool.name: tool.handler for tool in write_tools()}


def describe_for_prompt(*, include_writes: bool = True) -> list[dict[str, Any]]:
    """The tool list as the assistant should be told about it.

    ``spends_money`` is surfaced deliberately: the owner is the one paying, and
    a tool that costs should read as costing before it is called, not in the
    receipt afterwards.
    """
    tools = MASTER_TOOLS if include_writes else read_tools()
    return [
        {
            "name": tool.name,
            "summary": tool.summary,
            "writes": tool.writes,
            **({"spends_money": True} if tool.spends_money else {}),
        }
        for tool in tools
    ]
