# 1. Architecture

I split the system into three runtime concerns: surface control, discovery, and deterministic replay.

The browser layer is the common boundary. During discovery, the system observes the current UI, sends a compact observation and screenshot to Gemini, receives one structured next action, validates that action against the observed element capabilities, executes it, and records only the successful reusable action. Replay does not ask a model what to do. It executes the saved capability directly.

I used Playwright because it provides reliable browser control, screenshots, DOM inspection, HTML capture, waiting primitives, and several locator strategies without requiring a large framework. The saved artifact is not Playwright source code. It stores normalized actions and locator descriptions, so the replay contract remains readable and leaves room for another surface adapter later.

The concrete target is a local synthetic credit-union service console. I chose a local target instead of a public commerce site because it lets the implementation exercise states that matter in the stated environment: member-not-found, permission denial, session expiry, validation errors, and an irreversible account action. It also avoids real credentials, real PII, third-party rate limits, and external site terms.

The implementation is intentionally single-process and file-based. I did not add queues, databases, worker pools, or tenant services because they would add infrastructure without improving the core discovery → artifact → deterministic replay path being evaluated.

# 2. Artifact schema

The capability artifact is the production contract. It is versioned and validated with Pydantic. It records capability identity, application family/version, runtime inputs, declared outputs, ordered actions, locator stacks, a final checkpoint, and discovery metadata.

Concrete discovery values are parameterized. For example, the member identifier used during discovery becomes `{{member_id}}` in the saved step. Replay substitutes the invocation value at runtime. That turns one successful discovery run into a reusable capability instead of a recording tied to one member.

Each UI target is stored as an ordered locator stack. The current preference is stable identifiers first: `id`, `name`, semantic `data-field`, and `aria-label`; semantic role/name is used next, with exact visible text only as a fallback. Replay tries the saved strategies in order and requires a visible match.

I chose a locator stack rather than a single selector because one selector is unnecessarily brittle. I also avoided storing the raw model transcript as replay logic. Discovery may use model reasoning, but production replay depends on a small typed contract that is easy to review and debug.

The generated `lookup_savings_balance.json` artifact in this repository was produced by a successful Gemini-driven discovery run using `gemini-3.5-flash-lite`. The repository also contains example artifacts so the schema and handoff path can be inspected without consuming another model call. The examples are documentation fixtures, not substitutes for the generated discovery artifact.

# 3. Determinism & error handling

Replay does not initialize a Gemini client. Given an artifact and runtime inputs, it executes the same ordered steps, substitutes parameters, resolves the saved locator stack, performs the action, extracts declared outputs, checks runtime outcomes, and verifies the final checkpoint.

The result contract distinguishes three kinds of runtime state.

A business outcome means the application behaved correctly but returned a meaningful domain result. `member_not_found`, `permission_denied`, and validation errors are returned as `business_outcome` rather than being reported as automation crashes.

A recoverable condition means the automation cannot safely continue on its own but the live session is still useful. The synthetic application uses session expiry for this case. The system pauses and routes the live session to human intervention instead of discarding it.

A hard failure means the replay engine could not satisfy the artifact contract. Examples include an unresolved locator, an action timeout, or a failed final checkpoint. The run stops at the failing step and records a structured error plus richer evidence such as a screenshot and HTML snapshot.

Discovery also has explicit safeguards. The model may only choose actions supported by the selected observed element. A type action cannot be applied to a heading, table cell, label, or other non-editable element. Stale or re-rendered targets are detected and fed back into the discovery loop rather than being blindly executed.

The recorded evidence demonstrates the intended separation. Discovery used Gemini to find the procedure once. Replay then ran the generated capability with member `67890` without a model call and returned the savings balance. A separate replay with `99999` returned `member_not_found` as a business outcome.

I did not add open-ended LLM recovery to replay because that would weaken the deterministic execution guarantee. A later extension could permit one bounded, policy-checked recovery step, but it should remain explicit and separately observable.

# 4. Heterogeneity & multi-tenant

The seam I would preserve is:

`surface adapter -> normalized observation/action -> capability artifact`

The current adapter uses browser DOM state plus screenshots. A legacy web adapter could rely more heavily on accessibility information, coordinates, frames, or nested tables. A desktop adapter could implement the same normalized operations over OS accessibility APIs. The discovery loop and replay artifact would not need to know whether the underlying application is a modern browser, legacy web UI, or desktop surface.

For multi-tenant reuse, I would associate capabilities with an application family and compatible version range, then keep tenant-specific differences as overrides rather than copying the whole capability. A base artifact would hold the common flow. A tenant override would replace only the route, locator, checkpoint, or policy field that differs for that institution.

Replay telemetry should be grouped by application family, tenant, application version, capability, step, and locator strategy. A rise in fallback-locator use, checkpoint failures, or intervention frequency would be a useful drift signal. I would gate unattended execution when reliability falls below an approval threshold instead of silently falling back to model reasoning.

I did not implement multi-tenant infrastructure because the assignment asks for a credible design path, not premature scaling plumbing.

# 5. Escalation & handoff

The handoff mechanism is real even though the operator interface is intentionally minimal.

When replay reaches an irreversible control or a recoverable state, the system writes an intervention record, captures the current screen, marks control as human, and pauses without closing the Playwright browser context.

The operator acts in that exact browser session. After completing the manual action, the operator returns to the terminal, records a short note, and signals resume. The system captures the resumed URL and a second screenshot, marks control back to automation, and continues using the same session.

This preserves the control-transfer seam that matters in production:

`automation owns session -> pause -> human owns session -> resume -> automation owns session`

If no interactive operator is available, replay returns an escalated or recoverable result rather than pretending the action completed.

A production operator console would sit on top of the same mechanism and add authenticated operator identity, session leasing, concurrency protection, queueing, authorization, and stronger audit controls.

# 6. Safety

Navigation is checked against an explicit host allowlist before execution. The artifact schema restricts replay to a small action vocabulary. Discovery validates model-selected actions against the observed element's supported operations. Controls marked risky or irreversible are not executed unattended by default.

Secrets are loaded from environment variables and `.env` is excluded from version control. The repository contains only `.env.example`, which has no credential value. The logger redacts common API-key, bearer-token, password, token, and secret patterns. The bundled application contains only synthetic data.

Extracted values are returned to the caller but are not written raw into the discovery event stream. Failure evidence is intended for debugging, so a production deployment would need stricter retention, encryption, and access-control policies than this take-home implementation.

`--allow-risky` exists for controlled testing, but the intended flow is explicit human intervention.

This implementation is not presented as a complete financial-institution security model. A production version would additionally need scoped service identity, tenant isolation, encrypted evidence, retention policies, tamper-evident audit logs, operator authentication, approval policies by action type, and strict authorization around capability invocation.

# 7. Cuts

I deliberately kept one browser surface, one local target application, JSON artifact storage, a terminal-based operator acknowledgement, and a small test suite.

I did not build a distributed queue, remote browser farm, tenant administration UI, desktop adapter, approval portal, model-assisted replay recovery, or full co-browsing console. Those are useful production capabilities, but they are not necessary to demonstrate the core record-once / replay-many design.


If I continued the project, the next addition would be multi-run stability and approval gating. I would replay a generated capability over a fixed set of positive and negative inputs, collect reliability by step and locator strategy, and promote the artifact from draft to approved only after it passes a defined stability threshold.

That extension would strengthen the production execution contract without changing the central architecture: a model discovers the workflow once, a typed artifact captures the reusable capability, and deterministic replay becomes the normal execution path.
