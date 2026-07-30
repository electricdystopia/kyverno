# Working in `pkg/webhooks`

This directory owns admission HTTP handling and policy/resource webhook
orchestration. The repository root `AGENTS.md` still applies.

## Entry points and flow

- `server.go` builds the TLS HTTP server and registers every admission
  route. Route paths must stay aligned with `pkg/controllers/webhook/`.
- `types.go` defines the handler contracts passed into `NewServer`.
- `handlers/` is the transport middleware chain: decoding/admission,
  filtering, protection, role enrichment, metrics, tracing, and response
  conversion.
- `resource/handlers.go` retrieves and categorizes legacy policies and
  coordinates validation, mutation, image verification, generation, audit,
  events, and update requests.
- `resource/{vpol,mpol,ivpol,gpol}/` contains CEL-policy admission handlers;
  namespaced and cluster-scoped variants must remain behaviorally aligned.
- `policy/`, `exception/`, `celexception/`, and `globalcontext/` validate
  Kyverno configuration resources.
- `utils/policy_context_builder.go` converts admission requests into engine
  policy contexts.

The webhook configuration objects themselves are reconciled in
`pkg/controllers/webhook/`; changing a served route usually requires
checking that controller and the `webhooks/**` plus
`webhook-configurations/**` conformance suites.

## Local invariants

- Always return an `AdmissionResponse` carrying the request UID.
- Preserve warnings and Kubernetes status details when wrapping failures.
- Do not run unbounded work on the admission request path. Respect request
  cancellation/deadlines; background audit work must have its own bounded
  context and worker capacity.
- Preserve dry-run behavior. Admission dry runs must not create update
  requests, events with side effects, or background resources.
- Handler middleware order in `server.go` is security- and
  observability-relevant. Filtering, managed-resource protection, role/GVK
  enrichment, metrics, and admission decoding should not be reordered
  without focused tests.
- A mutating response must contain valid JSON patches in execution order.
  Image verification evaluates the request after legacy mutation patches.
- Cluster-scoped and namespaced CEL policy routes are paired. When adding or
  renaming one, verify both server registration and webhook reconciliation.
- Never log full AdmissionReview payloads unless the explicit debug dump
  option is enabled.

## Tests

```bash
go test ./pkg/webhooks/<changed-package>/...
go test ./pkg/webhooks/...
```

Use the existing fake handlers and admission request builders in nearby
tests. For externally visible changes, run the selected conformance suites:

```bash
chainsaw test --config test/conformance/chainsaw/webhooks/.chainsaw.yaml \
  test/conformance/chainsaw/webhooks
chainsaw test --config test/conformance/chainsaw/webhook-configurations/.chainsaw.yaml \
  test/conformance/chainsaw/webhook-configurations
```

Those commands assume a prepared cluster with the candidate Kyverno images;
CI performs that setup through `.github/actions/tests/conformance/run`.
Use `kyvernaut/path-test-map.yaml` for additional policy-specific suites.

## Automation boundary

Webhook edits may be drafted autonomously, but never auto-merge them based
only on unit tests: admission wiring has cluster-wide blast radius. Require
human review for changes to filters, managed-resource protection,
failure-policy handling, image verification, authentication/authorization
enrichment, TLS, or route/controller alignment.

See `../../kyvernaut/SAFE_AUTOMATION.md` for the full boundary.
