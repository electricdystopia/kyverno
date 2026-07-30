# Working in `test/conformance`

This directory contains cluster-level Kyverno conformance tests, primarily
Chainsaw suites under `chainsaw/`. The repository root `AGENTS.md` still
applies.

## Structure

- Each top-level `chainsaw/<suite>/` corresponds to a CI job or a deliberate
  suite subdivision.
- `.chainsaw.yaml` defines suite discovery, cleanup, execution, namespace,
  and timeout behavior.
- Individual cases use `chainsaw-test.yaml`; manifests referenced by steps
  live beside the test.
- `chainsaw/_step-templates/` contains shared policy creation/readiness
  steps. Prefer those templates over copying wait logic.
- `.github/workflows/tests-conformance.yaml` defines the CI matrix.
  `.github/actions/tests/conformance/run/action.yaml` is the canonical
  cluster setup and invocation path.
- `kyvernaut/path-test-map.yaml` maps source paths to these suites and
  records intentionally unmapped top-level directories.

## Test-writing conventions

- Include the Chainsaw schema comment and use the API version already used
  by neighboring tests.
- Keep each test self-contained and deterministic. Create all prerequisites,
  assert the observable outcome, and rely on Chainsaw cleanup or explicit
  cleanup for cluster-scoped resources.
- Use unique resource names/namespaces. Do not depend on execution order or
  state left by another test.
- Assert both sides of policy behavior where practical: the resource which
  should pass and the resource which should be denied/mutated/generated.
- For asynchronous controllers, assert eventual state with bounded
  timeouts; do not add fixed sleeps as synchronization.
- Keep timeouts at the narrowest suite/case scope and justify increases.
- Avoid public network dependencies unless the suite explicitly exists to
  exercise one and CI provisions it.
- A regression test should fail against the unfixed behavior and pass with
  the fix. Avoid assertions that merely prove a resource was created.

## Running tests

The CI-equivalent action creates KinD, loads candidate Kyverno images,
installs CRDs/Kyverno, and then runs:

```bash
cd test/conformance/chainsaw/<suite>
chainsaw test --config .chainsaw.yaml
```

Running that command locally assumes the same cluster preparation. Use the
repository KinD targets from the root `AGENTS.md`, or mirror
`.github/actions/tests/conformance/run/action.yaml`.

To select suites for a source diff:

```bash
python3 kyvernaut/scope_tests.py --git-diff <base>...HEAD
```

When adding or removing a top-level suite, update
`kyvernaut/path-test-map.yaml`. Its validation CI intentionally fails if
the suite is neither mapped nor explicitly acknowledged.

## Automation boundary

Agents may add focused regression fixtures and reuse existing templates.
Require human review before:

- weakening/removing an assertion or marking a test skipped/quarantined;
- increasing broad suite timeouts to hide instability;
- changing global `.chainsaw.yaml` behavior;
- changing image-signature, attestation, exception/bypass, RBAC, or webhook
  security expectations;
- deleting coverage without a replacement.

Never treat a passing new fixture alone as proof that the relevant source
behavior is covered; run the mapped unit and conformance scope. See
`../../kyvernaut/SAFE_AUTOMATION.md`.
