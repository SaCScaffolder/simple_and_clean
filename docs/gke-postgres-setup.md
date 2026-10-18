# GKE Autopilot + Cloud SQL (postgres) — One-time setup

This document walks you through the one-time-per-GCP-project setup
required to deploy a `servicectl`-scaffolded Go service to a real
GKE Autopilot cluster with managed Cloud SQL (postgres) backing it.

The end state, when you finish this document:

- A GKE Autopilot cluster exists in your GCP project
- A Cloud SQL (postgres 16) instance exists, private IP only, in the
  same VPC
- The Workload Identity Federation (WIF) pool, provider, and CI
  service account exist so GitHub Actions can deploy without
  JSON keys
- The Workload Identity binding between the k8s ServiceAccount and
  the GCP runtime service account is in place (so the
  cloudsql-proxy sidecar can mint ephemeral certs)
- The three GitHub repo secrets (`GCP_PROJECT_ID`,
  `GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_SERVICE_ACCOUNT`) are set
  on your scaffolded service's GitHub repo

If you only want a quick "what commands do I run" checklist, scroll
to the bottom. The narrative in between explains *why* each command
is there, so you can debug it when it breaks.

## What you need before you start

- A GCP project. The locked-in value for this portfolio piece is
  `sacscaffolder`, region `us-central1`, dev tier `db-f1-micro`.
  Adjust to your own values for your own project.
- A GitHub repo. Push the scaffolded service to GitHub first; the
  WIF provider is bound to a specific repo, so the bootstrap
  command needs the repo's `owner/name` to be resolvable from a
  `git remote get-url origin` call.
- `gcloud` CLI installed and authenticated
  (`gcloud auth login && gcloud config set project <your-project>`).
- `jq` on PATH (used by `bootstrap.sh` for JSON parsing).
- A copy of the scaffolded service in a working directory.

## Two halves of the setup

The setup splits into two halves because the second half needs the
first to be partially done:

1. **Bootstrap (one shell script).** Creates the WIF pool, WIF
   provider, and CI service account. Idempotent. Run this before
   the first `terraform apply`.
2. **Post-terraform (a handful of gcloud commands).** Creates the
   Workload Identity binding between the k8s ServiceAccount and
   the GCP runtime service account. Needs the cluster and the
   runtime SA to exist, so it runs after `terraform apply` has
   provisioned both.

The first half is automated by `infra/bootstrap.sh` (rendered by
`servicectl` from `src/servicectl/deploy/gcp/infra/bootstrap.sh.j2`).
The second half is documented below because it has to be done after
`terraform apply` and is a one-liner per project that doesn't
warrant its own script.

## Half 1: bootstrap.sh

```bash
# From the root of your scaffolded service:
bash infra/bootstrap.sh
```

What this does (one gcloud call per step, all idempotent):

1. **Enables APIs:** `iam.googleapis.com`,
   `iamcredentials.googleapis.com`, `sts.googleapis.com`,
   `cloudresourcemanager.googleapis.com`. The WIF federation needs
   all four. The `cloudsql` and `servicenetworking` APIs are NOT
   enabled here because Terraform's `google_project_service`
   resources do that as part of the apply.

2. **Creates the Workload Identity Pool** (named
   `<service>-github-pool`). The pool is the unit of trust; the
   provider inside it is what the GitHub OIDC token gets
   validated against.

3. **Creates the Workload Identity Provider** (named
   `<service>-github-provider`). Bound to your specific GitHub
   repo via the `attribute-condition`:
   `attribute.repository == '<owner>/<repo>'`. The issuer URI is
   `https://token.actions.githubusercontent.com` (the GitHub
   Actions OIDC token issuer). The attribute mapping extracts the
   GitHub repo, ref, and actor into the impersonated identity's
   claims so future audit logs show *which* GitHub workflow
   triggered the GCP action.

4. **Creates the CI service account** (named `<service>-ci`) and
   grants it the roles it needs to deploy:
   - `roles/run.admin` — for `gcloud run deploy` if you ever
     switch to Cloud Run
   - `roles/storage.admin` — for GCS buckets that Terraform state
     may live in
   - `roles/artifactregistry.admin` — for `gcloud artifacts` and
     docker push to GAR
   - `roles/iam.serviceAccountUser` — to use the runtime SA
   - `roles/cloudbuild.builds.editor` — for `gcloud builds submit`
     if you use it

5. **Binds the WIF provider to the CI service account.** The
   `principalSet` IAM binding says "any token from the WIF
   provider whose `attribute.repository` matches `<owner>/<repo>`
   can impersonate this SA." This is what makes WIF work
   passwordless.

6. **Prints the three GitHub secrets.** Copy these into your
   GitHub repo's *Settings → Secrets and variables → Actions →
   New repository secret*. The names must be exact:
   `GCP_PROJECT_ID`, `GCP_WORKLOAD_IDENTITY_PROVIDER`,
   `GCP_SERVICE_ACCOUNT`. (Don't rename them; the workflow
   references them by these exact names.)

Why this is a separate script instead of Terraform: WIF pool
creation has a `attribute-condition` that depends on the GitHub
repo, which is a deploy-time input, not a Terraform-time input.
Doing it in `bootstrap.sh` keeps the Terraform code free of
"what's your GitHub repo" questions.

## Half 2: post-terraform Workload Identity binding

The cloudsql-proxy sidecar (in
`k8s/base/cloudsql-proxy.yaml.j2`) needs to call the Cloud SQL
Admin API to mint ephemeral TLS certs. The k8s ServiceAccount it
runs as is annotated with
`iam.gke.io/gcp-service-account: <runtime-sa>@<project>.iam.gserviceaccount.com`,
but the annotation alone is half the binding — GKE also needs the
GCP side to allow the k8s SA to impersonate the runtime SA.

This is one command per project. The runtime SA is created by
Terraform (the `google_service_account.runtime` resource in
`infra/main.tf.j2`), and the k8s SA is created by `kubectl apply
-k` after Terraform. So the binding has to happen *after* both
exist, but it doesn't matter which comes first as long as both
exist before you run it.

```bash
# After `terraform apply` has run at least once
# and after `kubectl apply -k overlays/dev` has run at least once
RUNTIME_SA="<service>-runtime@<project>.iam.gserviceaccount.com"
K8S_SA="default:<service>"   # namespace:k8s-sa-name
gcloud iam service-accounts add-iam-policy-binding \
    "${RUNTIME_SA}" \
    --project="<project>" \
    --role="roles/iam.workloadIdentityUser" \
    --member="serviceAccount:${K8S_SA}"
```

For the locked-in config (`<service>=demo`, `<project>=sacscaffolder`,
dev overlay), the actual command is:

```bash
gcloud iam service-accounts add-iam-policy-binding \
    demo-runtime@sacscaffolder.iam.gserviceaccount.com \
    --project=sacscaffolder \
    --role=roles/iam.workloadIdentityUser \
    --member="serviceAccount:default:dev-demo"
```

Note the k8s SA name has the `dev-` prefix from the dev overlay's
`namePrefix`. The prod overlay's SA is `prod-demo`.

If you forget this step, the cloudsql-proxy sidecar will start
fine but its `generateEphemeralCert` calls to
`sqladmin.googleapis.com` will return 403, and `/readyz` will
return 503 with a "DB unreachable" log line. The deploy will still
succeed (the smoke test treats `/readyz` as a soft warning, not a
hard fail) — but the service won't be able to talk to postgres.

## What you do NOT need to do

A few things that look like they need manual setup but don't:

- **Enable the cloudsql / servicenetworking APIs.** Terraform's
  `google_project_service` resources do this in the
  `google_service_networking_connection` and
  `google_sql_database_instance` blocks. The bootstrap script's
  API list deliberately omits them so Terraform owns them.

- **Create the VPC or subnetwork.** The `cloudsql` module
  provisions a new VPC, the `pods` and `services` subnetworks,
  and the private services range. The `gke_autopilot` module
  consumes those by `self_link` (passed via the
  `network_self_link`, `pods_subnet_self_link`,
  `pods_secondary_range`, `services_secondary_range` variables
  in `infra/main.tf.j2`).

- **Create the runtime SA.** Terraform's
  `google_service_account.runtime` resource creates it. The
  `cloudsql` module grants it `roles/cloudsql.client` and
  `roles/secretmanager.secretAccessor` automatically.

- **Set up the ManagedCertificate.** GKE's
  `networking.gke.io/v1` ManagedCertificate controller creates
  the cert once the Ingress has a `spec.rules[0].host` whose DNS
  resolves to the load balancer's IP. For dev (nip.io hostnames)
  this is automatic. For prod (real domain) the user has to point
  an A record at the load balancer's IP before the cert will
  issue.

- **Set the `db_tier`, `db_name`, `db_user` Terraform variables.**
  They live in `infra/<env>.tfvars.j2` and are set to
  `db-f1-micro` / `sac` / `sac` for dev. Change them there if you
  want a different size or name.

## Teardown

To delete everything this doc created (useful for keeping a
portfolio project under the GCP free tier):

```bash
# 1. Delete the k8s resources first (so the cluster can be
#    deleted without finalizer complaints).
kubectl delete -k k8s/overlays/dev

# 2. Destroy the Terraform state.
cd infra && terraform destroy -auto-approve && cd ..

# 3. Delete the WIF pool (this also deletes the provider and
#    the CI service account's binding, but leaves the SA itself).
gcloud iam workload-identity-pools delete <service>-github-pool \
    --project=<project> --location=global

# 4. Delete the CI service account.
gcloud iam service-accounts delete \
    <service>-ci@<project>.iam.gserviceaccount.com \
    --project=<project>
```

The runtime SA, the GAR repo, the VPC, and the Cloud SQL instance
are all destroyed by `terraform destroy`. The WIF pool is
*outside* Terraform's state (it lives in `bootstrap.sh`), so it
needs the manual delete. (A future improvement: move WIF creation
into Terraform as well, with the repo name as a `variable` —
filed as a follow-up; not in this milestone.)

## Cost expectations (locked-in config)

For `sacscaffolder` / `us-central1` / `db-f1-micro` / GKE
Autopilot, the expected monthly cost when always on:

| Resource | Tier | Monthly cost |
|---|---|---|
| GKE Autopilot cluster | 1 vCPU / 1 GB RAM pod | ~$15-30 (autopilot charges per-pod, so this depends on actual load) |
| Cloud SQL (postgres) | `db-f1-micro` | $9 |
| Artifact Registry | < 10 GB | < $1 |
| Cloud Load Balancer | 1 forwarding rule + 1 ingress | ~$18 (the LB is the most expensive line item) |
| ManagedCertificate | 1 cert | $0 |
| Egress | minimal (no external traffic in the portfolio piece) | < $1 |
| **Total** | | **~$45-60/mo** |

The LB is the big one. For a portfolio piece that's only ever
going to be hit by you and the hiring manager, the cheapest
option is to spin the cluster up just before the demo and tear
it down after. The teardown commands above take ~2 minutes and
drop the cost to zero.

## Quick checklist

```bash
# 1. Set the GitHub repo as origin (or pass GITHUB_REPO=owner/repo
#    to bootstrap.sh).
git remote add origin git@github.com:owner/repo.git

# 2. Run the bootstrap (creates WIF pool, provider, CI SA).
bash infra/bootstrap.sh

# 3. Set the three GitHub secrets that bootstrap.sh printed.
#    (Settings -> Secrets and variables -> Actions -> New repository secret)
#      GCP_PROJECT_ID
#      GCP_WORKLOAD_IDENTITY_PROVIDER
#      GCP_SERVICE_ACCOUNT

# 4. Push to main. CI runs the full deploy pipeline.
git push origin main

# 5. After CI succeeds, do the Workload Identity binding
#    (Half 2 above). This is a one-liner per project.
gcloud iam service-accounts add-iam-policy-binding \
    <service>-runtime@<project>.iam.gserviceaccount.com \
    --project=<project> \
    --role=roles/iam.workloadIdentityUser \
    --member="serviceAccount:default:dev-<service>"

# 6. Re-push (or run `gh workflow run deploy.yml` from the GitHub
#    UI). The deploy will pick up the binding and the /readyz
#    check should now go green.
```

Steps 5 and 6 are the "actually click the button" moment for the
postgres path. Steps 1-4 work even without postgres; step 5 is what
makes the cloudsql-proxy able to reach the database.
