---
inclusion: fileMatch
fileMatchPattern: "frontend/**/*"
---

# Frontend Design and Engineering Standards

## Product feel
A serious developer-infrastructure product: the clarity of Linear, the developer focus of Stripe, the polish of Vercel. The Revenant identity is subtle — no horror or Halloween imagery, no gimmicks.

## Visual system
- Dark interface. Neutral near-black surfaces in three elevation steps; one restrained accent; semantic colours reserved for meaning.
- Confidence classes have fixed, distinct treatments used everywhere: **Observed**, **Inferred**, **Unknown**. Never rely on colour alone; pair with a label or icon.
- Typography: one sans family for UI, one monospace for paths, fields, code and diffs. A clear type scale; no more than three weights.
- Spacing on a 4 px grid. Generous whitespace; dense only inside data tables.
- Motion is brief (150–250 ms), purposeful, and disabled under `prefers-reduced-motion`.

## Behaviour
- Every screen renders real backend data. No hardcoded metrics, charts, activity or success states.
- Every visible control works, or is disabled with the reason shown.
- Each data view has loading, empty, error and populated states. Empty states say what to do next.
- Demo project data carries a visible "Demo data" label.
- Behavioural compatibility is never shown alone. It always appears as "X % — based on (verified + failed) of N behaviours; (passed + failed) of K planned checks executed", next to evidence coverage shown as "Y % — n of N behaviours", the five-state breakdown (verified, failed, not executed, unresolved, not assessed; summing to N), the unresolved and not-assessed lists, and the "tested compatibility, not guaranteed equivalence" statement. An undefined metric is shown as "—", never as 0 % or 100 %. Show the overlap-excluded figure and the "incomplete" and "low tested share" flags when the API returns them. Do not build a component that can render one metric without the others. Show verified behaviours split into fully and partially verified, and list the checks that did not run. Show the evidence version a run used and the "evidence has changed since this run" label when the API sets it; never present a past run's numbers as current when the reconstruction is stale.
- Traffic permissions are three separate switches, all off by default. Label them by effect: "Send recorded requests", "Send generated requests", "Allow operations that may change data (applies to both)". Show the permission matrix next to them. The third switch carries a warning naming the target and sends nothing on its own.
- Show every operation's effect as Read-only, State-changing or Unknown, with who declared it. Unknown is shown as needing a decision and is treated as state-changing. Do not offer "Read-only" for an operation with an unsafe method. Show the planned requests, by source and effect, before a run.
- Every run view states whether state-changing traffic was enabled, what was sent by source and effect, what was blocked, and which operations are unknown.
- Runtime request lists show persisted events only; show dropped-trace counters from health next to them. Never imply a trace exists when the API returns 404 for it.
- Credential values are write-only in the UI. A server-generated inbound key is displayed once, at creation.
- Content from imported evidence is rendered as text, never as HTML.

## Engineering
- TypeScript strict. No `any` in application code.
- All requests go through one typed API client that handles the token, errors and the "backend unreachable" state.
- The admin token is entered by the operator and kept in `sessionStorage`; never in `NEXT_PUBLIC_*`.
- Accessibility: keyboard reachable, visible focus, labelled controls, WCAG AA contrast, usable at 375 px.
