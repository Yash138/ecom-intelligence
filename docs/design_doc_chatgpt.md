# Infrastructure Design — First Draft (Superseded)

> **Status:** Superseded by `docs/new_infra/infra_design.md` (v1.2). Do not use for active development.

## Document Update History

| Date | Sections Changed | Summary |
|---|---|---|
| 2026-04-22 | All | Initial first draft — ChatGPT-generated architecture recommendation |
| 2026-05-11 | Header | Marked as superseded; added title, update history, TOC |

## Table of Contents

- [Executive Recommendation](#executive-recommendation)
- [1. Recommended Target Architecture](#1-recommended-target-architecture)
- [2. How the Data Layers Should Actually Work](#2-how-the-data-layers-should-actually-work)
- [3. What to Use for Orchestration](#3-what-to-use-for-orchestration)
- [4. Repo Design: One Repo or Two?](#4-repo-design-one-repo-or-two)
- [5. Where Each Component Should Live](#5-where-each-component-should-live)
- [6. Recommended Execution Pattern](#6-recommended-execution-pattern)
- [7. Credentials and Secret Management](#7-credentials-and-secret-management)
- [8. CI/CD Design](#8-cicd-design)
- [9. Environments](#9-environments)
- [10. Minimal Infrastructure Blueprint](#10-minimal-infrastructure-blueprint)
- [11. Suggested Data Flow](#11-suggested-data-flow)
- [12. Table Design Rules](#12-table-design-rules)
- [13. Governance and Access](#13-governance-and-access)
- [14. What I Would Do If Designing From Zero](#14-what-i-would-do-if-i-were-designing-this-from-zero-for-you)
- [15. What Not to Do](#15-what-not-to-do)
- [16. Crisp Answers to Specific Uncertainties](#16-crisp-answers-to-your-specific-uncertainties)
- [17. Final Reference Diagram](#17-final-reference-diagram)

---

## Executive recommendation

From a startup standpoint, the right design is:

> **Object storage + Iceberg as the system of record, Trino for query, Prefect or Airflow for orchestration, containerized jobs for ingestion/transforms, and a managed secrets store.**

Do **not** make Postgres the main data store for vendor history if you already know you want an Iceberg-based stack.

Iceberg is designed to let engines like Trino and Spark safely work with the same tables on object storage. Trino’s Iceberg connector queries Iceberg tables directly, and Trino also supports direct access to object storage through its data lake connectors. ([Apache Iceberg][1])

---

# 1. Recommended target architecture

```text
GitHub
 ├─ repo 1: orchestration
 └─ repo 2: pipelines / shared libraries

CI/CD
 ├─ build Docker images
 ├─ run tests / lint / type-check
 ├─ push image to registry
 └─ deploy orchestration definitions

Object Storage (system of record)
 ├─ /landing/vendor_a/...
 ├─ /landing/vendor_b/...
 ├─ /raw/iceberg/...
 ├─ /clean/iceberg/...
 ├─ /transformed/iceberg/...
 └─ /curated/iceberg/...

Catalog layer
 └─ Iceberg catalog
    - REST catalog / Nessie / Glue / Hive metastore

Query layer
 └─ Trino
    - catalog_raw
    - catalog_clean
    - catalog_curated

Orchestration layer
 └─ Prefect or Airflow
    - schedules
    - retries
    - lineage-ish metadata
    - backfills
    - alerts

Execution layer
 └─ Python jobs in containers
    - ingest vendor APIs
    - validate
    - normalize
    - write Iceberg tables

Secrets & config
 └─ secret manager + parameter/config store

Observability
 ├─ logs
 ├─ metrics
 └─ alerts
```

---

# 2. How the data layers should actually work

Your proposed layers are correct, but I would refine them slightly.

## Recommended layers

### A. **Landing**

* exact vendor payloads
* raw JSON/CSV files as received
* immutable
* partitioned by vendor + ingestion date + batch/run id

This is your forensic layer.

### B. **Raw Iceberg**

* ingested into Iceberg with minimal normalization
* one table per source entity/feed
* schema still close to source
* add metadata columns:

  * `ingested_at`
  * `vendor`
  * `batch_id`
  * `source_file`
  * `record_hash`

### C. **Clean**

* schema standardized
* obvious bad rows quarantined
* types fixed
* null handling and required field rules applied

### D. **Transformed**

* business entities modeled
* joins, dedupe, slowly changing logic, standard dimensions/facts

### E. **Curated**

* analytics-ready marts
* stable semantic layer for BI / dashboards / product evaluation logic

## Important correction

Do **not** rely on only “raw Iceberg with JSON/CSV files from vendors”.

Keep both:

* **immutable file landing zone**
* **Iceberg tables derived from it**

Reason: when vendor schemas drift or parsing logic changes, you want the untouched original payloads available for replay.

Iceberg is a table format over files in object storage, designed for large analytic tables and multi-engine access. ([Apache Iceberg][1])

---

# 3. What to use for orchestration

## My recommendation: **Prefect first**

Use Prefect first unless you already know you need Airflow-level DAG sprawl, complex cross-team scheduling, or Airflow-specific ecosystem integrations.

Why:

* less operational overhead
* Python-native workflow authoring
* deployments define when/where/how flows run
* work pools/workers make execution placement flexible

Prefect’s docs describe deployments as the server-side representation of flows and work pools as the coordination channel between deployments and workers. ([Prefect][2])

## When to choose Airflow instead

Choose Airflow if:

* you expect many DAGs and many schedules quickly
* your team already knows Airflow
* you need its mature scheduling/admin ecosystem
* you want more conventional data-platform operating patterns

Airflow’s production docs explicitly describe a distributed architecture and recommend PostgreSQL or MySQL for the metadata DB rather than SQLite in production. ([Apache Airflow][3])

## My startup call

For your current stage:

> **Prefect is the cleaner startup choice.**

Airflow is stronger if your orchestration estate becomes large and multi-team.

---

# 4. Repo design: one repo or two?

You proposed:

* one repo for DAGs
* one repo for scripts

That is valid, but I would not start there unless you already have a lot of code and multiple people independently changing orchestration and logic.

## Recommended approach

### Start with **one repo**

Structure:

```text
data-platform/
  orchestration/
    prefect_flows/ or airflow_dags/
  pipelines/
    ingestion/
    raw_to_clean/
    clean_to_transformed/
    transformed_to_curated/
  libraries/
    vendors/
    io/
    validation/
    iceberg/
    common/
  infra/
    terraform/ or ansible/
    helm/ or docker/
  tests/
  pyproject.toml
  Dockerfile
```

## Why one repo first

* one CI/CD path
* one version boundary
* simpler imports/shared libraries
* easier atomic changes across orchestration + code

## Split into two repos later only if

* separate team ownership
* different release cadence
* DAG authors and pipeline authors diverge
* CI becomes too slow
* dependency conflicts become painful

### My call

> **Monorepo first. Split later if pain becomes real.**

---

# 5. Where each component should live

## A. Object storage

This is the foundation.

### Best options

* **AWS S3**
* **MinIO** if you want self-managed S3-compatible storage
* another S3-compatible vendor only after validation

Important nuance: Trino supports S3-compatible systems, but its documentation says only **AWS S3 and MinIO are tested for compatibility**; for others, you should test carefully. ([Trino][4])

### Recommendation

If you want the least integration risk with Trino + Iceberg:

> **Use AWS S3 unless cost or cloud strategy strongly pushes you elsewhere.**

---

## B. Iceberg catalog

Pick one catalog and standardize on it.

### Good choices

* **Nessie** if you want branch/tag semantics and versioned data workflows
* **REST catalog** for a cleaner neutral control plane
* **AWS Glue catalog** if you are fully on AWS

### My recommendation

* **If mostly AWS:** Glue or REST catalog
* **If you want Git-like table versioning workflows:** Nessie

---

## C. Query engine

* **Trino** as the interactive SQL layer
* separate catalogs for raw/clean/curated if helpful
* restrict write paths; not every layer should be writable by every process

---

## D. Orchestrator hosting

Two clean options:

### Option 1 — simplest startup

* one small VM for orchestrator control plane
* jobs execute as Docker containers on the same VM or a worker VM

### Option 2 — better separation

* orchestrator server on one VM
* worker VM(s) for jobs
* Trino and storage independent

### My recommendation

For now:

* **one orchestrator VM**
* **one worker VM**
* scale workers later

---

# 6. Recommended execution pattern

Do **not** run vendor scripts directly from the orchestrator host process.

Use this pattern:

1. CI builds a Docker image for pipeline code
2. Orchestrator triggers a flow/job
3. Worker runs that container
4. Container reads secrets/config
5. Writes landing files to object storage
6. Writes/merges Iceberg tables
7. Emits logs and metrics

This gives:

* reproducibility
* dependency isolation
* easy retries/backfills
* easier deployment

---

# 7. Credentials and secret management

You said there must be a place to store and manage credentials. Correct.

## Use a dedicated secrets store

Options:

* **AWS Secrets Manager**
* **HashiCorp Vault**
* cloud parameter store equivalents

## Do not store secrets in

* GitHub Actions secrets as the main long-term store for runtime
* `.env` files on servers
* Airflow Variables / Prefect Variables for sensitive production credentials unless they are backed by a proper secret backend

## Recommended split

* **Runtime secrets**: secret manager
* **Non-sensitive config**: config store / repo config files
* **GitHub deployment credentials**: GitHub secrets or OIDC role-based auth

---

# 8. CI/CD design

## Pipelines should do four things

### A. Validate code

* lint
* unit tests
* type checks
* lightweight integration tests

### B. Build image

* one versioned Docker image per commit/tag

### C. Publish artifact

* container registry

### D. Deploy

* register flows / deploy DAG changes
* update infra manifests if needed

## Recommended deployment model

* `main` branch → dev/staging
* release tag → prod

---

# 9. Environments

Do not start with five environments.

Start with:

* **dev**
* **prod**

If needed later:

* **staging**

## Environment separation should include

* separate object storage prefixes or buckets
* separate Iceberg namespaces/catalog config
* separate secrets
* separate orchestration work pools/queues
* separate Trino catalogs or schemas

---

# 10. Minimal infrastructure blueprint

## Startup-safe version

### Core infra

* **Object storage:** AWS S3
* **Iceberg catalog:** Nessie or Glue
* **Query engine:** Trino
* **Orchestrator:** Prefect
* **Execution:** Docker worker VM
* **Secrets:** Secrets Manager / Vault
* **Registry:** GitHub Container Registry or ECR
* **Observability:** Grafana + Loki/Prometheus, or cloud-native logging

### VMs

* `vm-orchestrator`
* `vm-worker-1`
* `vm-trino` if not already managed elsewhere

If Trino already exists in your ecosystem and you can reuse that operational pattern, keep Trino separate.

---

# 11. Suggested data flow

## Ingestion

```text
Vendor API
  → fetch to landing JSON/CSV in object storage
  → register batch metadata
  → parse to raw Iceberg
  → validate / quarantine
  → promote to clean
  → transform
  → publish curated marts
```

## Metadata you should maintain for every run

* run id
* vendor
* endpoint/feed
* extraction window
* schema version observed
* file count
* row count
* checksum/hash
* quality status
* promoted_at

Store this in lightweight control tables or the orchestrator metadata, not in ad hoc logs only.

---

# 12. Table design rules

## For Iceberg tables

* partition conservatively; do not over-partition
* write Parquet for tabular layers
* keep small-file control in mind
* compact periodically
* define clear primary business keys even if not enforced physically
* add audit columns everywhere

## Recommended file format by layer

* landing: original JSON/CSV
* raw/clean/transformed/curated: **Parquet-backed Iceberg**

---

# 13. Governance and access

You are small now, but set the pattern correctly.

## Create separate identities for:

* orchestrator
* ingestion jobs
* transform jobs
* analysts/query users
* CI/CD deployer

## Permission model

* landing write-only for ingestion role
* curated read-only for consumer role
* destructive table ops restricted to admin role

---

# 14. What I would do if I were designing this from zero for you

## Final recommendation

### Use:

* **AWS S3** as object storage
* **Iceberg** as the table format
* **Nessie** as catalog if you want data versioning workflows, otherwise **Glue/REST catalog**
* **Trino** for query
* **Prefect** for orchestration
* **One monorepo** initially
* **Dockerized jobs**
* **AWS Secrets Manager or Vault** for secrets
* **GitHub Actions** for CI/CD
* **One orchestrator VM + one worker VM** to start

---

# 15. What not to do

* do not make Postgres the historical warehouse
* do not start with two repos unless there is already ownership separation
* do not let orchestration server also become your general execution box forever
* do not skip immutable landing storage
* do not put vendor credentials directly inside DAGs/flows
* do not use unsupported S3-compatible storage with Trino in production without testing it first ([Trino][4])

---

# 16. Crisp answers to your specific uncertainties

## “Airflow or Prefect?”

**Prefect first.**

## “Two repos?”

**No. Start with one monorepo.**

## “Where should Airflow/Prefect be hosted?”

**Small dedicated VM for control plane, worker VM for execution.**

## “Where should credentials live?”

**Dedicated secret manager.**

## “What is the right startup design?”

**Object storage + Iceberg + Trino + containerized jobs + orchestration control plane.**

---

# 17. Final reference diagram

```text
                         GitHub
                           │
                     GitHub Actions
                           │
          ┌────────────────┼────────────────┐
          │                │                │
   build/test image   deploy flows     deploy infra
          │                │                │
          └──────────────┬─┴────────────────┘
                         │
                   Container Registry
                         │
                ┌────────┴─────────┐
                │                  │
        Prefect Server /       Worker VM(s)
        Airflow Control        Docker runtime
                │                  │
                └────────┬─────────┘
                         │
                    Run jobs
                         │
 Vendor APIs ──> Landing files in S3 ──> Raw Iceberg ──> Clean ──> Transformed ──> Curated
                         │                     │
                         └───────────── Catalog ─────────────┐
                                                             │
                                                           Trino
                                                             │
                                                   analytics / ad hoc SQL
```

If you want the next step, I’ll turn this into a **concrete deployment blueprint** with:

* exact repo structure
* exact folder layout
* VM count/specs
* CI/CD workflow files
* secrets model
* Prefect-vs-Airflow final choice memo.

[1]: https://iceberg.apache.org/?utm_source=chatgpt.com "Apache Iceberg - Apache Iceberg™"
[2]: https://docs.prefect.io/v3/concepts/deployments?utm_source=chatgpt.com "Deployments"
[3]: https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/overview.html?utm_source=chatgpt.com "Architecture Overview — Airflow 3.2.0 Documentation"
[4]: https://trino.io/docs/current/object-storage/file-system-s3.html?utm_source=chatgpt.com "S3 file system support"
