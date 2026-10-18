# Deploying to GKE Autopilot + Cloud SQL — end-to-end narrative

This document follows a single scaffolded service from the
`servicectl init` command all the way through to "I clicked the
URL and got a green HTTPS page that can read from postgres."

It is the *narrative* companion to
[`gke-postgres-setup.md`](gke-postgres-setup.md) (the one-time
setup) and [`architecture.md`](architecture.md#postgres-on-gke)
(the design rationale). Read those first if you want the *why*;
this one is the *how*, in order, with the actual outputs you
should expect at each step.

If you only have two minutes, scroll to the **TL;DR** at the
bottom. It's a one-screen checklist.

## The locked-in config this doc assumes

Everything in this doc is calibrated to one concrete config. If
your values differ, substitute them in the right places; the
flow is the same.

| Setting | Value | Why this value |
|---|---|---|
| Service name | `demo` | Used in every GCP resource name, every k8s resource name, and every GitHub secret. |
| Template | `go-webapi` | The Go HTTP service template; ships with `/healthz` and `/readyz` endpoints on port 8080. |
| CI provider | `github-actions` | The only CI provider supported for the GKE deploy path. |
| Deploy target | `gcp-gke-autopilot` | GKE Autopilot, not the older GKE Standard mode. |
| Database | `postgres` | `--db=postgres` triggers the cloudsql module + the proxy sidecar. |
| GCP project | `sacscaffolder` | Locked-in for the portfolio piece. |
| GCP region | `us-central1` | Lowest cost for Always Free tier eligible resources. |
| Postgres tier (dev) | `db-f1-micro` | $9/mo; plenty for a portfolio service. |
| Cluster name | `sac-portfolio-cluster` | The name in the `google_container_cluster` resource. |
| Domain | (deferred) | Use nip.io for dev. Real domain is a follow-up PR. |

The exact `servicectl init` command:

```bash
servicectl init demo \
    --template=go-webapi \
    --deploy=gcp-gke-autopilot \
    --db=postgres \
    --gcp-project-id=sacscaffolder \
    --gcp-region=us-central1 \
    --ci-provider=github-actions \
    --registry=gar \
    --output-dir=./demo
```

(Substitute `servicectl` for the right command on your machine;
the wheel is `pip install servicectl` and the wrapper is
`servicectl-go` for the Go-binary crowd.)

## What gets generated

A scaffolded service for this config emits, in order:

1. **The Go service itself** (`main.go`, `go.mod`, `Dockerfile`,
   `internal/handlers/...`, etc.) — same as the local-deploy
   variant.
2. **The CI workflow** (`.github/workflows/ci.yml` if it didn't
   exist) and the **deploy workflow** (`deploy.yml` at the
   service root, per the existing convention).
3. **The GCP deploy tree** (`infra/`, `k8s/`, `deploy.yml`):
   - `infra/main.tf.j2`, `infra/variables.tf.j2`,
     `infra/outputs.tf.j2` — root Terraform config that calls the
     `gke_autopilot` and `cloudsql` modules.
   - `infra/modules/gke_autopilot/` — the cluster.
   - `infra/modules/cloudsql/` — the postgres instance, the
     Secret Manager password, the VPC, the IAM grants
     (PR 1, #135).
   - `infra/bootstrap.sh.j2` — the one-time WIF setup
     (see [`gke-postgres-setup.md`](gke-postgres-setup.md)).
   - `infra/{dev,staging,prod}.tfvars.j2` — per-env overrides.
   - `k8s/base/` — the Service, Deployment, ServiceAccount,
     cloudsql-proxy, Secret, Ingress, ManagedCertificate
     (PR 2, #136).
   - `k8s/overlays/dev/` and `k8s/overlays/prod/` — the
     kustomize overlays.

The `.servicectl.json` file at the service root records the
flags you passed to `init`, so `servicectl refresh` can
regenerate the scaffold later without re-asking.

## The deploy flow, step by step

### 1. Scaffold

```bash
$ servicectl init demo --template=go-webapi --deploy=gcp-gke-autopilot \
    --db=postgres --gcp-project-id=sacscaffolder --gcp-region=us-central1 \
    --ci-provider=github-actions --registry=gar --output-dir=./demo
  rendered 27 templated files
  copied 14 static files
  wrote .servicectl.json
  wrote README.md
  wrote CHANGELOG.md
  git init done; no remote configured
$ cd demo
$ cat .servicectl.json
{
  "schema_version": 1,
  "service_name": "demo",
  "template": "go-webapi",
  "deploy_target": "gcp-gke-autopilot",
  ...
  "gcp_region": "us-central1",
  "gcp_project_id": "sacscaffolder",
  "db": "postgres"
}
```

### 2. Push to GitHub

```bash
$ git remote add origin git@github.com:SaCScaffolder/sac-portfolio-demo.git
$ git add -A
$ git commit -m "feat: initial scaffold (go-webapi on gke-autopilot + postgres)"
$ git push -u origin main
```

`git commit` is *not* part of the scaffold's job — that's the
caller's. The scaffolder leaves the working tree dirty on
purpose so you can review what got generated before you commit.

### 3. Run the bootstrap (one-time per project)

This is the half described in
[`gke-postgres-setup.md`](gke-postgres-setup.md#half-1-bootstrapsh).
Creates the WIF pool, the WIF provider, and the CI service
account, then prints the three GitHub secrets.

```bash
$ bash infra/bootstrap.sh
==> Project:       sacscaffolder
==> GitHub repo:   SaCScaffolder/sac-portfolio-demo
==> WIF pool:      demo-github-pool
==> WIF provider:  demo-github-provider
==> CI service acct: demo-ci
==> Enabling required APIs...
==> Creating Workload Identity Pool (idempotent)...
==> Creating Workload Identity Provider (idempotent)...
==> Creating CI service account (idempotent)...
==> Granting CI service account roles...
==> Binding WIF provider to CI service account...

=================================================================
  Bootstrap complete. Set these as GitHub repo secrets:
  (Settings -> Secrets and variables -> Actions -> New repository secret)
=================================================================
  GCP_PROJECT_ID               = sacscaffolder
  GCP_WORKLOAD_IDENTITY_PROVIDER = projects/sacscaffolder/locations/global/workloadIdentityPools/demo-github-pool/providers/demo-github-provider
  GCP_SERVICE_ACCOUNT          = demo-ci@sacscaffolder.iam.gserviceaccount.com
=================================================================
```

Copy the three values into your GitHub repo's *Settings → Secrets
and variables → Actions → New repository secret*. The names must
be exact.

### 4. First push triggers CI

Your push to main from step 2 already triggered CI; if you set
the secrets in between, the next push (or a re-run) will pick
them up. The deploy job does, in order:

1. Authenticate to GCP via WIF (no JSON keys, no `GCP_SA_KEY`
   secret anywhere).
2. Build the Docker image and push to GAR.
3. `terraform init` → `terraform plan` → `terraform apply` —
   creates the cluster, the VPC, the Cloud SQL instance, the
   Secret Manager password, the runtime SA, and all IAM grants.
   This step is the longest: **8-12 minutes** for a fresh
   project (the cluster alone takes ~5 min; Cloud SQL is ~3 min;
   the rest is fast).
4. `gcloud container clusters get-credentials` to populate
   `~/.kube/config`.
5. **Sync `db-credentials` k8s Secret from Secret Manager** —
   fetches the random postgres password from the Secret Manager
   value created in step 3 and writes it to a k8s Secret.
6. `kustomize edit set image` to inject the freshly-pushed
   image into the dev overlay.
7. `kubectl apply -k overlays/dev` — creates the Service,
   Deployment, cloudsql-proxy, Ingress, ManagedCertificate.
8. **Wait for the GCE Load Balancer IP, `sed` the placeholder
   out of the overlay, re-apply.** Takes 30-90s for the LB to
   provision.
9. Smoke test: `curl https://<host>/healthz` (hard gate) and
   `curl https://<host>/readyz` (soft warning).

### 5. Do the Workload Identity binding (post-terraform, one-time)

The cloudsql-proxy sidecar in the pod needs to call the Cloud
SQL Admin API to mint ephemeral TLS certs. The k8s SA in the pod
is annotated with `iam.gke.io/gcp-service-account: demo-runtime@...`,
but GKE also needs the *GCP side* of the binding — the runtime
SA has to allow the k8s SA to impersonate it. This is one
gcloud command, idempotent, and you do it once per project
*after* the cluster and runtime SA both exist:

```bash
$ gcloud iam service-accounts add-iam-policy-binding \
    demo-runtime@sacscaffolder.iam.gserviceaccount.com \
    --project=sacscaffolder \
    --role=roles/iam.workloadIdentityUser \
    --member="serviceAccount:default:dev-demo"
```

(This is documented in
[`gke-postgres-setup.md`](gke-postgres-setup.md#half-2-post-terraform-workload-identity-binding)
with the full why.)

If you skip this, the deploy still succeeds (the `/readyz`
check is a soft warning, not a hard fail), but `/readyz` will
return 503 and any code path that hits the DB will fail. The
deploy.yml comment in step 9 calls this out explicitly so you
can find it in the CI log.

### 6. Re-push (or re-run the deploy workflow)

After step 5, re-trigger the deploy:

```bash
$ git commit --allow-empty -m "chore: retrigger deploy after WI binding"
$ git push origin main
```

This re-runs the full deploy pipeline. The Terraform plan is
idempotent (no changes since last apply), the `kubectl apply`
is idempotent (no changes since last apply), and the smoke test
should now go green on `/readyz` because the cloudsql-proxy can
actually reach the Cloud SQL Admin API to mint certs.

### 7. Verify

```bash
# In another terminal, with the deploy done:
$ kubectl get ingress
NAME      CLASS    HOSTS                              ADDRESS        PORTS   AGE
dev-demo  <none>   demo.35.192.0.1.nip.io             35.192.0.1     80      4m

$ curl -sI https://demo.35.192.0.1.nip.io/healthz | head -1
HTTP/2 200

$ curl -sI https://demo.35.192.0.1.nip.io/readyz | head -1
HTTP/2 200
```

The cert is issued by Google Trust Services (via
`networking.gke.io/v1` ManagedCertificate) once nip.io's
wildcard DNS resolves the hostname to the LB IP. The cert is
auto-renewed.

## What you see if something goes wrong

The deploy workflow's logs are the primary debugging surface.
A few common failures:

- **`/readyz` returns 503, but `/healthz` returns 200.** The
  Workload Identity binding in step 5 is missing or wrong. The
  CI log will show `::warning::Readyz did not return 200 within
  2 minutes` with the cloudsql-proxy logs in the pod (`kubectl
  logs <pod> -c cloudsql-proxy`) showing 403 from
  `sqladmin.googleapis.com`.

- **GCE LB doesn't provision an IP within 5 minutes.** Rare;
  usually a region issue or a quota issue. The deploy still
  succeeds (the patch step exits 0 on timeout); only HTTPS is
  broken. Check `kubectl describe ingress dev-demo` and the
  GKE events.

- **Terraform apply fails with "API not enabled."** The
  `google_project_service` resources in `main.tf.j2` are
  supposed to enable the cloudsql and servicenetworking APIs,
  but they need `serviceusage.serviceUsageConsumer` on the CI
  service account. If your GCP project has stricter Org
  policies, you may need to enable the APIs manually in the
  console first, then re-run.

- **`ManagedCertificate` is stuck in `Provisioning`.** DNS
  isn't resolving. For nip.io, this means the LB IP
  substitution step didn't run. For real domains, the A
  record isn't pointed at the LB yet.

## Teardown

When you're done with the demo:

```bash
$ kubectl delete -k k8s/overlays/dev
$ cd infra && terraform destroy -auto-approve && cd ..
$ gcloud iam workload-identity-pools delete demo-github-pool \
    --project=sacscaffolder --location=global
$ gcloud iam service-accounts delete demo-ci@sacscaffolder.iam.gserviceaccount.com \
    --project=sacscaffolder
```

Total time: ~3-4 minutes. Drops the cost to zero
(see [`gke-postgres-setup.md`](gke-postgres-setup.md#cost-expectations-locked-in-config)
for the cost breakdown).

## TL;DR

```bash
# Scaffold
servicectl init demo --template=go-webapi --deploy=gcp-gke-autopilot \
    --db=postgres --gcp-project-id=sacscaffolder --gcp-region=us-central1 \
    --ci-provider=github-actions --registry=gar --output-dir=./demo
cd demo

# Push to GitHub
git remote add origin git@github.com:SaCScaffolder/sac-portfolio-demo.git
git add -A
git commit -m "feat: initial scaffold"
git push -u origin main

# One-time per project: WIF setup
bash infra/bootstrap.sh
# Copy the three printed secrets into the GitHub repo's secrets.

# Wait for CI to finish the first deploy (~10 min).

# One-time per project: Workload Identity binding for the cloudsql-proxy
gcloud iam service-accounts add-iam-policy-binding \
    demo-runtime@sacscaffolder.iam.gserviceaccount.com \
    --project=sacscaffolder \
    --role=roles/iam.workloadIdentityUser \
    --member="serviceAccount:default:dev-demo"

# Re-push to pick up the binding
git commit --allow-empty -m "chore: retrigger deploy after WI binding"
git push origin main

# Verify
curl -sI https://demo.35.192.0.1.nip.io/healthz    # HTTP/2 200
curl -sI https://demo.35.192.0.1.nip.io/readyz     # HTTP/2 200

# Teardown (when you're done)
kubectl delete -k k8s/overlays/dev
cd infra && terraform destroy -auto-approve && cd ..
gcloud iam workload-identity-pools delete demo-github-pool --project=sacscaffolder --location=global
gcloud iam service-accounts delete demo-ci@sacscaffolder.iam.gserviceaccount.com --project=sacscaffolder
```
