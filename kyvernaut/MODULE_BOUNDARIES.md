# Kyverno module-boundary evaluation

## Decision

Retain the current federated repository layout. Keep Kyverno's product code in
the root module, keep the two build utilities as isolated nested modules, and
continue consuming `github.com/kyverno/api` and `github.com/kyverno/sdk` as
versioned external modules. Do not introduce a repository-wide `go.work` file
or move API/SDK sources into this repository without a separate design review.

This is an evaluation outcome, not an assertion that a monorepo is inherently
bad. The current evidence does not justify its migration and governance cost.
`module-boundaries.yaml` records the decision in machine-readable form and CI
fails if the observed module topology silently drifts.

## Observed topology

| Boundary | Current form | Why it is a boundary |
|---|---|---|
| `github.com/kyverno/kyverno` | Root Go module | Product binaries, controllers, engine, CLI, charts, tests, and release automation move together. |
| `hack/api-group-resources` | Nested Go module | Build/code-generation utility with Kubernetes client dependencies that need not enter the product module graph. |
| `hack/controller-gen` | Nested Go module | Pinned code-generation toolchain isolated from runtime dependencies. |
| `github.com/kyverno/api` | External versioned module | Published API contract consumed through an explicit version. |
| `github.com/kyverno/sdk` | External versioned module | Published integration surface consumed through an explicit version. |

There is no checked-in `go.work`. The root module's dependency graph therefore
uses the same published API and SDK versions in local development and CI that
downstream consumers can resolve, rather than silently replacing them with
adjacent source trees.

## Why not combine API and SDK here now?

A monorepo would make some coordinated source edits atomic, but atomic source
commits are not the same as compatible published modules. API and SDK consumers
still need versioned artifacts, compatibility policy, and release ordering.
Moving the repositories would also:

- enlarge checkout, ownership, CI, and review blast radius for independent
  library changes;
- make it easier for local workspace replacements to conceal version-skew
  defects that appear for external consumers;
- require migration of release automation, security boundaries, issue/PR
  history, branch protection, and downstream import assumptions;
- create a new governance decision about whether all modules share releases,
  support windows, and maintainer authority.

No repository evidence collected for this project demonstrates that those
costs are lower than the current dependency-update cost. Kyvernaut already
classifies and safely tests pinned dependency changes, reducing one source of
cross-repository maintenance without changing ownership boundaries.

## Why not split the product module further?

Kyverno's controllers, engine, CLI, generated clients, CRDs, and conformance
fixtures are released and tested as one product. Code generation crosses many
of these directories, and admission behavior often requires atomic changes
across API use, engine logic, webhook/controller wiring, CLI support, and
tests. Additional product modules would add replace/version choreography while
preserving the same release unit.

The two `hack/` modules are different: they are executable build tools with
their own dependency graphs and no runtime ownership. Their existing isolation
is useful and should remain.

## Revisit criteria

Reopen this decision with measured evidence if cross-repository release
ordering becomes a recurring source of incidents, version-skew failures
outweigh independent releases, shared ownership/release policy is adopted, or
CI and contributor data show that a migration's larger blast radius is worth
the cost. Any proposal should include:

1. module/version and downstream compatibility policy;
2. release, security, ownership, and branch-protection migration;
3. CI impact and a plan that tests published-module consumption without local
   workspace replacements;
4. rollback and repository-history handling.

Until then, the smallest maintainable change is to document and validate the
current boundaries rather than perform a speculative repository merge.
