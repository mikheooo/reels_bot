# Router and Prioritization policy

## Audited original contract

Before this integration, `app/worker/prioritization.py` exposed:

- `score_content(transcript, analysis_summary=None) -> PriorityScore`;
- five Gemini-produced signals in the inclusive range 0.0-1.0: importance,
  virality, novelty, views potential and audience value;
- deterministic weights `0.25 / 0.25 / 0.15 / 0.15 / 0.20`;
- `overall`, computed as the weighted sum;
- `publish = overall >= PUBLISH_THRESHOLD`, default `0.60`;
- free-text reasons from the structured model response.

The model call and reasons were LLM-dependent. JSON parsing, Pydantic range
validation, weighted scoring and the publish threshold were deterministic. The
module had focused unit tests but no production call site: `process_video`
called Router and immediately derived `AnalysisPolicy` from Router alone.

## Canonical flow and typed result

```text
transcript + visual evidence
  -> calibrated RouterDecision
  -> PriorityScore(transcript + Router summary)
  -> PriorityGateResult
  -> CombinedPolicyResult
  -> fact/business/technical/specialized analysis
  -> task, user and channel delivery decisions
```

`PriorityGateResult` records the score, tier, decision, both thresholds,
reasons, policy version and any fallback code. `CombinedPolicyResult` records
the original Router policy, effective policy, independent user/channel
decisions and every suppressed action. Both are defined in
`app/worker/priority_policy.py`.

The decision is persisted in `jobs.qa_reasons` before expensive downstream
work and again in the final payload:

```json
{
  "router": {"primary_type": "HOW_TO", "risk": "LOW"},
  "priority": {
    "tier": "LOW",
    "decision": "DEPRIORITIZED",
    "publish_threshold": 0.6,
    "deprioritize_below": 0.4,
    "reasons": ["..."]
  },
  "policy": {
    "router_policy": {"run_fact_check": false, "create_tasks": true},
    "effective_policy": {"run_fact_check": false, "create_tasks": false},
    "user_delivery": true,
    "channel_publication": false,
    "suppressed_actions": ["create_tasks", "channel_publication"]
  }
}
```

No database migration is required; `qa_reasons` and `delivery_status` are
existing JSON columns. Historical rows without these keys remain valid.

## Score bands and behavior

- `HIGH / ACCEPTED`: `overall >= PUBLISH_THRESHOLD` (default 0.60). Preserve
  Router policy and allow channel publication.
- `LOW / DEPRIORITIZED`: `overall < DEPRIORITIZE_THRESHOLD` (default 0.40).
  Suppress optional technical details, personal relevance, task creation and
  channel publication when they would otherwise run.
- `AMBIGUOUS / AMBIGUOUS_CONTINUE`: score from 0.40 up to 0.60. Preserve Router
  policy and channel decision; uncertainty does not silently drop the result.

User delivery is always true at this decision boundary. It is independent of
the public-channel decision.

## Safety invariant

Priority controls usefulness and public distribution, not safety. The combined
policy cannot turn off a Router-required fact-check, strict fact-check or
business-check. In particular, low-priority HIGH-risk material retains strict
verification and existing `REVIEW_REQUIRED` behavior.

## Failure semantics

The priority call has a configurable timeout, default 120 seconds. Exceptions,
timeouts, missing transcript, malformed scores, out-of-range values and a
`publish` flag inconsistent with thresholds produce
`AMBIGUOUS / FALLBACK_CONTINUE`:

- Router fact/business safety policy is preserved;
- user analysis continues;
- channel publication is withheld;
- failure type is persisted;
- `delivery_status.priority=FAILED_FALLBACK:<type>`, so a completed user
  delivery becomes `PARTIAL`, never a false `DONE`.

Offline release evaluation replays recorded Router and priority outputs and
asserts policy outcomes rather than fragile exact model behavior. Live Gemini
is not an offline release dependency.
