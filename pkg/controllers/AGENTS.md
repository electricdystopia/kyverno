# Working in `pkg/controllers`

This directory contains informer/workqueue controllers used by the Kyverno
binaries. The repository root `AGENTS.md` still applies.

## Entry points and layout

- `controller.go` defines the common `Run(context.Context, workers)` shape.
- Each subdirectory normally exposes `NewController`, a controller name and
  worker count, informer event handlers, queue processing, and reconciliation.
- Binary wiring lives under `cmd/`. Adding a controller is incomplete until
  its constructor, informer lifecycle, leader-election placement, and worker
  count are wired into the correct binary.
- `webhook/` reconciles admissionregistration objects and must stay aligned
  with routes in `pkg/webhooks/server.go`.
- `report/{resource,background,aggregate}/` owns report creation, scanning,
  and aggregation.
- `cleanup/`, `deleting/`, `globalcontext/`, `policystatus/`, and
  `admissionpolicygenerator/` reconcile their corresponding resources.
- `generic/` contains reusable configmap, logging, and webhook controllers;
  changes there can affect more than one binary.

## Controller conventions

- Informer callbacks should enqueue small, stable keys and return quickly.
  Put API writes and expensive computation in reconciliation workers.
- Read through listers where eventual consistency is acceptable. If a live
  API fallback is required for correctness, make it explicit and test cache
  miss/not-found behavior.
- Treat not-found on a deleted object as a normal reconciliation outcome.
  Distinguish retriable API failures from permanent validation errors.
- Use the typed rate-limiting queue consistently: forget successful or
  terminal items, rate-limit retriable failures, and always call `Done`.
- Honor context cancellation and shut queues/workers down cleanly.
- Make reconciliation idempotent. Reprocessing the same key must converge
  without duplicate resources, events, or unbounded status writes.
- Use conflict retries for read-modify-write updates where another
  controller or user can update the object concurrently.
- Keep owner references, labels, finalizers, and generated object names
  stable unless a migration plan accompanies the change.
- New watched resources or API verbs may require RBAC/chart changes. Do not
  silently broaden permissions.

## Tests

Run the changed controller package and then the controller tree:

```bash
go test ./pkg/controllers/<controller>/...
go test ./pkg/controllers/...
```

Prefer fake clients/informers and direct reconciliation tests. Cover add,
update, delete/tombstone, cache miss, conflict/retry, context cancellation,
and idempotent requeue behavior as relevant.

Controller behavior often needs integration or chainsaw coverage. Consult
`kyvernaut/path-test-map.yaml`; examples include:

- `cleanup/**` for cleanup controllers;
- `reports/**` and `openreports/**` for report controllers;
- `webhooks/**` and `webhook-configurations/**` for webhook reconciliation;
- deleting/generating policy suites for their controllers.

## Automation boundary

Localized bug fixes with fake-client regression tests may be drafted
autonomously. Require human review for RBAC changes, finalizer/deletion
semantics, cross-namespace writes, webhook configuration, report ownership,
security exceptions, or changes that increase controller privileges or
blast radius.

Never edit generated clients/listers/informers by hand. Modify API sources
and run code generation under the repository-wide rules instead. See
`../../kyvernaut/SAFE_AUTOMATION.md`.
