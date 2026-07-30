# Working in `pkg/engine`

This directory implements the legacy Kyverno policy engine. The repository
root `AGENTS.md` still applies; this file adds engine-specific guidance.

## Entry points and flow

- `engine.go` contains `NewEngine` and the public execution paths:
  `Validate`, `Mutate`, `Generate`, `VerifyAndPatchImages`, and
  `ApplyBackgroundChecks`.
- `api/engine.go` is the consumer-facing `Engine` interface. Response,
  policy-context, rule-response, and statistics contracts also live under
  `api/`.
- `policycontext/` owns the mutable policy evaluation context. The
  top-level `policy_context.go` exports compatibility aliases.
- `handlers/validation/` and `handlers/mutation/` instantiate rule
  handlers. `validate/`, `mutate/`, and the top-level operation files
  coordinate rule execution.
- `context/`, `jmespath/`, `operator/`, and `variables/` are shared
  expression/context primitives. A change there can affect validate,
  mutate, and generate simultaneously.
- `internal/` contains matching, exception, image-verification, and
  response helpers used by the operation paths.

The typical execution order is policy match, rule match, JSON-context
checkpoint, context loading, handler execution, context restore/update,
response aggregation, statistics, and metrics. Preserve this ordering when
changing a rule path.

## Local invariants

- Treat `PolicyContext` and its JSON context as request-scoped mutable state.
  Preserve checkpoint/restore behavior around every rule so values loaded
  for one rule cannot leak into another.
- Return structured `EngineResponse`/`RuleResponse` results. Do not replace
  policy failures with process-level errors or panics.
- Keep admission and background semantics distinct. In particular, match
  behavior involving old resources, delete operations, exclusions, and
  namespace data needs coverage for both paths where applicable.
- Mutation ordering is observable. A later handler may evaluate the resource
  patched by an earlier rule; do not reorder handlers as cleanup.
- Preserve response statistics, tracing spans, logging context, and policy
  engine metrics when adding an early return.
- Shared expression/context changes are medium-to-high blast radius even
  when their unit diff is small.

## Tests

Run the narrow package first, then the engine tree:

```bash
go test ./pkg/engine/<changed-package>/...
go test ./pkg/engine/...
```

Use table-driven tests beside the changed code. When behavior is visible at
admission or background level, add or update the corresponding chainsaw
suite selected by `kyvernaut/path-test-map.yaml`:

- validation: `validate/**` and `policy-validation/**`;
- mutation: `mutate/**`;
- generation/background: `generate/**`;
- shared engine primitives: validate, mutate, generate, and range-operator
  suites.

Run `python3 kyvernaut/scope_tests.py --git-diff <base>...HEAD` from the
repository root to obtain the current machine-readable selection.

## Automation boundary

Ordinary localized engine changes may be drafted autonomously with focused
unit and conformance tests. Require human review for:

- image verification or attestation semantics, including calls into
  `pkg/image`, `pkg/cosign`, or `pkg/notary`;
- changes which weaken matching, exclusions, policy exceptions, failure
  policy, or deny behavior;
- public contracts under `pkg/engine/api`;
- a change whose only test coverage comes from the generic `pkg/` fallback.

Do not edit API types or generated clients as a side effect of engine work.
See `../../kyvernaut/SAFE_AUTOMATION.md` for repository-wide boundaries.
