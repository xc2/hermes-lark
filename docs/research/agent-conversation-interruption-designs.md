# Agent conversation history and Steer display design

Research date: 2026-09-15

## Scope

This document covers two Feishu/Lark presentation problems:

1. the context used when Hermes is first mentioned in an existing native
   thread; and
2. the visual ordering of CardKit output when a running turn is steered or
   emits an independent interactive message or artifact.

The runtime mode in scope is **Steer**. Queue-first and stop-and-send UI are
included only where needed to explain why they must not be confused with
Steer. This design does not add a new input mode or change Hermes session
semantics.

## Findings from other agent interfaces

Mature agent interfaces differ in layout, but expose the same important state
boundaries:

- A logical turn can contain commentary, tool activity, action-required items,
  artifacts, and a final answer. It is not forced into one mutable bubble.
- Commentary/progress is distinct from the terminal answer.
- Queue, Steer, and interrupt are distinct user actions.
- A user input is shown as pending until the runtime accepts its disposition.
- Interrupted, failed, and completed turns leave explicit transcript state.
- Completion can have a second signal such as a status, timestamp, terminal
  title, bell, notification, or a final item at the newest timeline position.

| Interface | Progress representation | Input while running | Completion/interruption |
| --- | --- | --- | --- |
| OpenAI Codex TUI/App Server | Protocol items distinguish `commentary` and `final_answer`; the TUI restores `Working` after interim output | Pending steer and queued follow-ups have separate sections; steer targets the active turn ID | Interrupted turns add an explicit transcript marker; completion can emit a system notification |
| VS Code Chat/GitHub Copilot | Reasoning and tool steps are separate from the answer | Add to Queue, Steer with Message, and Stop and Send are separate actions | Completed steps, timestamps, and notifications remain visible |
| Claude Code TUI | Task state and verbose transcript are separate from the final reply | Enter queues input near the prompt; Esc interrupts and releases queued work | Completed work remains in the transcript even after interruption |
| Gemini CLI | Narration, todos, and window-title state expose work and action requirements | Queue and mid-draft steering are separate interactions | Action-required and session-complete notifications are supported |
| Cursor Agent | Todo, thinking, tool, and final events are distinct | Pending messages can be reordered; queue, steer, and force-send differ | Completion and action-required notifications are available |
| GitHub Copilot cloud agent | A live session log shows tools and verification | A follow-up steers after the current tool call | The session has durable running/completed state |

Primary references:

- [Codex message phases](https://github.com/openai/codex/blob/7f01a84effccef40d4726c3ca12e6c839ec98d7a/codex-rs/protocol/src/models.rs#L938-L949)
- [Codex pending-input preview](https://github.com/openai/codex/blob/7f01a84effccef40d4726c3ca12e6c839ec98d7a/codex-rs/tui/src/bottom_pane/pending_input_preview.rs#L13-L31)
- [Codex turn steer parameters](https://github.com/openai/codex/blob/7f01a84effccef40d4726c3ca12e6c839ec98d7a/codex-rs/app-server-protocol/src/protocol/v2/turn.rs#L291-L320)
- [VS Code messages while a request is running](https://code.visualstudio.com/docs/chat/chat-overview#_send-messages-while-a-request-is-running)
- [Claude Code queued messages](https://code.claude.com/docs/en/interactive-mode#queue-messages-while-claude-works)
- [Gemini CLI keyboard shortcuts](https://geminicli.com/docs/reference/keyboard-shortcuts/)
- [Cursor planning](https://docs.cursor.com/en/agent/planning)
- [Copilot agent loop completion signals](https://docs.github.com/en/copilot/how-tos/copilot-sdk/features/agent-loop#sessionidle-vs-sessiontaskcomplete)
- [Pinned Hermes busy-input handler](https://github.com/NousResearch/hermes-agent/blob/cc4cab2f592e60a197e796506de9168f74baf3ea/gateway/run.py#L8351-L8730)
- [Pinned Hermes adapter lifecycle](https://github.com/NousResearch/hermes-agent/blob/cc4cab2f592e60a197e796506de9168f74baf3ea/gateway/platforms/base.py#L5554-L6328)

## Core model

### Native thread snapshot

A Feishu native thread is one shared object. The user and the bot must not have
different implied histories. On first activation, the session receives an
authoritative snapshot of every message before the mention, including every
downloadable image, video, audio item, and file. The current mention is not
duplicated in the snapshot.

The application/bot identity performs this read. No user access token is used.
The required application scopes are documented in
[`permissions/README.md`](../../permissions/README.md).

### Logical turn and physical display segment

A single logical Hermes turn can own multiple physical CardKit messages:

```text
Thread
`- Session
   `- Logical turn
      |- segment S1 (frozen)
      |- user Steer boundary
      |- segment S2 (active)
      |- action/artifact boundary
      `- segment S3 (terminal)
```

Only the latest physical segment can change. Older segments retain the output
that was visible at their boundary and never become active again.

### Boundary commit rules

```text
Inbound user message
        |
        v
Hermes busy dispatch
   | accepted as Steer --------> commit display boundary
   | queued/rejected ----------> do not commit Steer boundary
   ` interrupted/new turn -----> outside this design

Independent Feishu item
        |
        v
Send succeeds and returns message_id
        |
        `----------------------> commit display boundary
```

The normal Steer commit signal is Hermes' `Steered into current run` or
`Redirected current run` acknowledgement. Hermes can suppress that
acknowledgement and debounces repeated acknowledgements for 30 seconds. In that
case, the adapter observes the successful Steer-mode busy dispatch and confirms
that the event did not enter the pending queue before committing the boundary.

## Existing-thread activation timelines

### H1. Already active thread

The existing Hermes session is authoritative. No snapshot is imported again.

```text
Thread root: original request
|- User: earlier follow-up
|- Hermes: earlier response
`- User: new follow-up
        |
        `-> reuse existing session
```

### H2. First mention in a text-only thread

```text
Thread root: design proposal
|- User A: constraint one
|- User B: constraint two
`- User C: @Hermes review this
        |
        v
Session input
|- untrusted snapshot: root + User A + User B
`- new message: User C mention
```

### H3. Root contains text and an image or video

The root text and binary resources are both part of the first session input.

```text
Thread root
|- text: "The layout breaks here"
`- image: screenshot.png
   `- User: @Hermes investigate
              |
              v
Session input
|- snapshot text: "The layout breaks here"
|- media: cached screenshot.png
`- new message: "investigate"
```

```text
Thread root
|- text: "The transition flickers"
`- video: reproduction.mp4
   `- User: @Hermes identify the cause
              |
              v
Session input
|- snapshot text: "The transition flickers"
|- media: cached reproduction.mp4
`- new message: "identify the cause"
```

### H4. Earlier replies contain mixed resources

Ordering comes from the native thread snapshot, not the adapter's short-lived
event buffer.

```text
Thread root: incident report
|- User A: logs.txt
|- User B: screenshot.png
|- User A: "It started after deployment"
`- User C: @Hermes summarize the evidence
              |
              v
Session snapshot, in order
1. root text
2. logs.txt
3. screenshot.png
4. deployment note
5. current mention as the new message
```

### H5. Snapshot or resource retrieval is incomplete

The adapter fails closed instead of starting a session whose context differs
silently from the visible thread.

```text
Feishu thread visible to user
|- root text
|- screenshot.png
`- @Hermes investigate
        |
        v
resource download fails
        |
        v
No session is started
Hermes sends a visible retry/error response
```

## Steer-mode display timelines

Notation:

```text
[S1 RUNNING]    active streaming segment
[S1 CONTINUED]  frozen segment; the turn continues below
[S1 WAITING]    frozen segment; interaction is required below
[Q WAITING]     Question card
[A WAITING]     Approval, permission, or OAuth card
[S2 COMPLETE]   latest terminal segment
```

### 1. Ordinary streaming

```text
User: Investigate this issue

Agent [S1 RUNNING]
+-------------------------------+
| Generating...                 |
| Checking the source...        |
| terminal - Running            |
+-------------------------------+
                |
                | commentary/tool/answer deltas
                v
Agent [S1 COMPLETE]
+-------------------------------+
| Final findings...             |
| 2 tools - Completed           |
+-------------------------------+
```

### 2. Multiple commentary updates

Commentary stays in the progress area of the active segment. The final answer
replaces interim prose.

```text
User: Review the behavior

Agent [S1 RUNNING]
+-------------------------------+
| Reading the adapter...        |
| Checking the runtime path...  |
| Comparing UI ordering...      |
+-------------------------------+
                |
                v
Agent [S1 COMPLETE]
+-------------------------------+
| Review result...              |
+-------------------------------+
```

### 3. Heartbeat and context compaction

Operational status remains inside the active card and does not create loose
thread messages.

```text
User: Run a long investigation

Agent [S1 RUNNING]
+-------------------------------+
| Compacting context...         |
| Working - 2 min               |
| Current analysis...           |
+-------------------------------+
```

### 4. Tool execution without Steer

```text
User: Run the checks

Agent [S1 RUNNING]
+-------------------------------+
| terminal - Running            |
+-------------------------------+
                |
                v
Agent [S1 COMPLETE]
+-------------------------------+
| Checks passed.                |
| terminal - Completed          |
+-------------------------------+
```

### 5. Steer during Thinking

The user message is not a boundary until Hermes accepts it as a Steer.

```text
User: Fix the issue
Agent [S1 RUNNING]: Thinking...

User: First explain the cause

Agent [S1 CONTINUED]
+-------------------------------+
| Continued below               |
+-------------------------------+

Agent [S2 RUNNING]
+-------------------------------+
| Steered into current run      |
| Investigating the cause...    |
+-------------------------------+
```

### 6. Steer after commentary

Commentary produced before the boundary remains above it. New commentary goes
below it.

```text
User: Investigate the API
Agent [S1]: Checking requests and data structures...

User: Only inspect presentation

Agent [S1 CONTINUED]: Continued below
Agent [S2 RUNNING]: Now inspecting presentation only...
```

### 7. Steer while a tool is running

The old card retains a hand-off snapshot. A late tool result is routed through
the logical turn to the newest segment.

```text
User: Modify the implementation
Agent [S1]: source scan - Running

User: Do not change code yet

Agent [S1 CONTINUED]
+-------------------------------+
| Continued below               |
| source scan - Continued       |
+-------------------------------+

Agent [S2 RUNNING]
+-------------------------------+
| Switched to read-only review  |
| source scan - Completed       |
+-------------------------------+
```

### 8. Steer during answer streaming

The partial answer is preserved but marked non-terminal. The final answer is
self-contained and appears after the user correction.

```text
User: Propose a fix
Agent [S1]: "Change the interface and add..."

User: Do not change the interface; presentation only

Agent [S1 CONTINUED]: partial answer + Continued below
Agent [S2 COMPLETE]: presentation-only final plan...
```

### 9. Repeated Steer messages

Each accepted message creates a boundary even when Hermes suppresses a repeated
acknowledgement during its debounce interval.

```text
User: Investigate and fix
Agent [S1 RUNNING]

User: Analysis only
Agent [S1 CONTINUED]
Agent [S2 RUNNING]

User: Presentation layer only
Agent [S2 CONTINUED]
Agent [S3 COMPLETE]: presentation-layer analysis...
```

### 10. Question during streaming

A Question is an `action_required` boundary. The old segment stops loading
before the independent Question card.

```text
Agent [S1 RUNNING]: Current evidence...
                |
                v
Agent [S1 WAITING]: Waiting for your answer below
Agent [Q WAITING]: Which path? [Device] [Static review]
```

### 11. Continue after a Question answer

The Question updates in place. A new segment begins after it.

```text
Agent [S1 WAITING]
Agent [Q ANSWERED]: Device
Agent [S2 RUNNING]: Continuing with the selected path...
Agent [S2 COMPLETE]: Device findings...
```

### 12. Question before any answer content

An initial Thinking card becomes Waiting; it is not finalized with synthetic
success text.

```text
User: Deploy this
Agent [S1 WAITING]: Waiting for your answer below
Agent [Q WAITING]: Which environment? [Stage] [Production]
```

### 13. Question after a Steer

Both boundaries remain in one logical turn.

```text
User: Investigate all platforms
Agent [S1 RUNNING]
User: iOS only
Agent [S1 CONTINUED]
Agent [S2 RUNNING]: Inspecting iOS...
Agent [S2 WAITING]
Agent [Q WAITING]: May I use a physical device?
Agent [S3 COMPLETE]: Physical-device findings...
```

### 14. Steer while a Question is waiting

Ordinary Steer text does not masquerade as a structured form submission. The
Question remains actionable. If the Question is answered later, output reuses
the newest segment instead of creating a card above or outside the latest
timeline position.

```text
Agent [S1 WAITING]
Agent [Q WAITING]: May I use a physical device?

User: Continue with checks that do not need a device
Agent [S2 RUNNING]: Steered into current run

User answers Q later
Agent [Q ANSWERED]
Agent [S2 RUNNING]: continues here; no extra older-position segment
```

### 15. Command approval during streaming

```text
Agent [S1 RUNNING]: Preparing a command...
Agent [S1 WAITING]: Waiting for your approval below
Agent [A WAITING]: Run this command? [Once] [Session] [Always] [Deny]
```

### 16. Approval succeeds

```text
Agent [S1 WAITING]
Agent [A APPROVED]: Approved once
Agent [S2 RUNNING]: terminal - Running
Agent [S2 COMPLETE]: Command result...
```

### 17. Approval is denied

The denial and explanation stay at or below the approval position.

```text
Agent [S1 WAITING]
Agent [A DENIED]: Denied
Agent [S2 COMPLETE]
+-------------------------------+
| The command was not executed. |
| Earlier completed work stays. |
+-------------------------------+
```

### 18. Application permission or user OAuth

Both are `action_required` boundaries. Existing-thread snapshot reads are
different: those use the bot/application identity and never request user OAuth.

```text
Agent [S1 RUNNING]: Reading a Feishu resource...
Agent [S1 WAITING]: Authorization required below
Agent [A WAITING]: Grant application permission / Sign in
Agent [A RESOLVED]: Authorization complete
Agent [S2 RUNNING]: Continuing the resource read...
```

### 19. Native image, video, audio, file, or result artifact

A successfully delivered independent artifact is a display boundary. Later
assistant text must appear below it.

```text
Agent [S1 RUNNING]: Generating a report...
Agent [S1 CONTINUED]: Artifact produced; continued below
Agent artifact: report.pdf
Agent [S2 COMPLETE]: Summary and next steps...
```

Multiple consecutive artifacts move the continuation anchor to the last
successfully delivered artifact.

### 20. Successful completion after Steer

Only the newest segment is finalized.

```text
Agent [S1 CONTINUED]
User: Additional constraint
Agent [S2 COMPLETE]: Final result incorporating the constraint...
```

### 21. Failure after Steer

The originating Hermes dispatch owns the complete steered run. Its failure
closes the newest segment, even though the active input ID now names the Steer
message.

```text
Agent [S1 CONTINUED]
User: Additional constraint
Agent [S2 ERROR]: The request failed before a response completed.
```

### 22. Explicit `/stop`

`/stop` is an interrupt, not a Steer. It closes the active segment and does not
create a continuation segment.

```text
User: Run a long task
Agent [S1 RUNNING]
User: /stop
Agent [S1 STOPPED]
+-------------------------------+
| Stopped.                      |
| Completed side effects stay.  |
+-------------------------------+
```

### 23. Question card send failure

A Question boundary exists only after the card is visible. If delivery fails,
the current segment becomes an explicit Error instead of remaining Generating
or Waiting forever.

```text
Agent [S1 RUNNING]
Question send fails
Agent [S1 ERROR]: Unable to send the question card. Please try again.
```

### 24. Continuation CardKit creation failure

The old segment never reopens. Subsequent output falls back to one editable
ordinary message below the boundary.

```text
Agent [S1 CONTINUED]
User: Additional requirement
CardKit continuation creation fails
Agent ordinary message: continuing output...
Agent ordinary message edited in place: final output...
```

### 25. Late event for a frozen physical message ID

Stream consumers can retain the first card's message ID. Every old physical ID
therefore aliases the logical state, which routes late commentary, tool result,
edit, and terminal events to the newest segment.

```text
S1 is frozen
late tool result targets S1 message_id
logical turn resolves current segment = S2

Wrong: update S1
Right: update S2
```

## State-machine invariant

Every combination above follows one rule:

```text
accepted Steer or successfully delivered independent item
                         |
                         v
all Agent segments above it become permanently frozen
                         |
                         v
all later commentary, tool state, content, and terminal state
must use the newest segment below that boundary
```

The implementation also preserves these failure rules:

- a queued or rejected busy input does not create a Steer segment;
- a failed interaction/artifact send does not create an independent boundary;
- a failed continuation card uses an ordinary editable message;
- Question expiry releases the waiting CardKit route;
- successful, failed, and cancelled lifecycle completion closes the newest
  segment rather than a frozen predecessor.

## Review checklist

1. Does an accepted Steer always put later output below the user message?
2. Do repeated Steers work when busy acknowledgements are debounced?
3. Are commentary, tool activity, action-required state, and final output
   semantically distinct?
4. Can a frozen segment ever be edited again?
5. Does runtime completion close the newest segment after a Steer?
6. Do Question, Approval, authorization, and artifact messages become
   boundaries only after successful delivery?
7. Does a Question answer after another Steer reuse the newest valid segment?
8. Does continuation failure retain all later output in an editable fallback?
9. On first thread activation, does the session contain every visible preceding
   message and downloadable resource?
10. Does any incomplete native-thread snapshot fail visibly instead of silently
    starting with less context than the user sees?
