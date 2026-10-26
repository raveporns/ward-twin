# WARD TWIN RBAC Staging Setup and Fixture Plan

Status: planning only. No Render service, Supabase project, account, database
row, seed, cleanup, push, or deployment was created or changed for this plan.

## 1. Readiness audit

- The current local configuration reports `APP_ENV=local`; no staging URL,
  staging credentials, Render manifest, `.env.example`, Alembic configuration,
  or migration directory was found in the repository.
- `requirements.txt` includes Alembic, but the repository does not contain
  migration artifacts. The application issues SQL against the `ward_twin`
  schema directly.
- The application settings are `APP_ENV`, `JWT_ISSUER`, `JWT_AUDIENCE`,
  `JWT_SECRET`, `GATEWAY_API_KEY`, `GATEWAY_PRODUCER_ID`,
  `GATEWAY_ALLOWED_DEVICES`, `DATABASE_URL`, and `DB_SCHEMA`.
- `APP_ENV` accepts only `local` or `production`. For a staging deployment,
  use `APP_ENV=production` so `/api/v1/dev/token` is not mounted. Do not add a
  `staging` value without a separate code change and review.
- The authentication implementation uses `staff_profiles`,
  `user_credentials`, `account_approvals`, and `ward_access`. Human account
  credentials are handled by the application; `auth_user_id` exists in the
  observed `staff_profiles` schema but the inspected login flow authenticates
  using `user_credentials`.
- SQL in the application references these clinical/data tables:
  `wards`, `patients`, `encounters`, `devices`, `producers`,
  `device_assignments`, `observation_readings`,
  `observation_measurements`, `processing_state`, `risk_predictions`,
  `alerts`, `assessments`, `import_jobs`, and `import_rows`.
- The repository does not define the full DDL or a staging migration baseline.
  A read-only catalog query was previously run against the locally configured
  database, but that connection was not identified as Staging. Its output must
  not be treated as the Staging schema. Before any fixture DML, capture the
  actual Staging columns, nullability, defaults, indexes, checks, and foreign
  keys from that Supabase project's catalog.
- `main.py` starts the prediction worker in the FastAPI lifespan in the
  current worktree. Staging must be isolated because submitted observations
  can trigger background prediction writes and may create alerts.

## 2. Render Staging service setup

1. In Render, create a new Web Service from the WARD TWIN repository. Give it
   an unmistakable staging name, for example `ward-twin-api-staging`.
2. Select a staging branch or commit deliberately. Disable automatic deploys
   until a maintainer explicitly enables them. Do not point this service at
   the production Supabase project.
3. Configure the service:
   - Build command: `pip install -r requirements.txt`
   - Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - Health check path: `/healthz`
4. Add the environment variables listed below in Render's Environment page.
   Enter secret values directly in Render's secret-capable UI; never put them
   in this document, source control, screenshots, shell history, or test
   reports.
5. Keep the staging URL private or restrict access using the organization's
   normal staging access controls. Do not configure production custom domains.
6. Confirm the deployed service reports `APP_ENV=production` using a
   non-secret configuration check. Confirm `/api/v1/dev/token` is absent
   (404) using a GET/POST check that does not issue tokens.
7. Confirm Render deployment logs contain no credentials, JWTs, API keys, or
   database URLs. Verify that the lifespan prediction worker is expected and
   that it can only access the staging database.

### Render environment variable names

Set all required variables using staging-only values:

| Variable | Required | Staging guidance |
|---|---:|---|
| `APP_ENV` | Yes | Set to `production` to disable local development auth. |
| `DATABASE_URL` | Yes | Supabase Staging PostgreSQL connection URI; use a dedicated least-privilege application role and TLS. |
| `DB_SCHEMA` | Yes | `ward_twin`, after confirming that the staging project has this schema. |
| `JWT_SECRET` | Yes | Generate a new random secret unique to Staging; do not reuse Production. |
| `JWT_ISSUER` | Yes | Use a Staging-specific issuer distinct from Production. |
| `JWT_AUDIENCE` | Yes | Use a Staging-specific audience distinct from Production. |
| `GATEWAY_API_KEY` | Yes | Generate a Staging-only key; never reuse the Production key. |
| `GATEWAY_PRODUCER_ID` | No | Set to a unique staging producer registered in the staging database. |
| `GATEWAY_ALLOWED_DEVICES` | No | Restrict to explicitly registered synthetic staging devices. |

Do not use a production URL/key as a temporary fallback. If a setting is
missing, stop and fix the staging configuration before running tests.

## 3. Supabase Staging project and schema verification

1. Create a separate Supabase project named to identify it as Staging, in the
   approved organization and region. Do not clone production patient data.
2. Record the project reference and database host in the team's secure
   environment inventory. Do not put database passwords or connection URIs
   in this repository.
3. Configure database network restrictions and TLS according to the
   organization policy. Create a dedicated application database role for the
   staging API rather than using a project owner/service role.
4. Apply the approved schema using the team's authoritative database
   provisioning source. This repository currently has no migration files, so
   do not fabricate DDL from endpoint SQL. Reconcile the staging schema with
   the authoritative schema owner before proceeding.
5. With a read-only catalog role and a confirmed Staging connection, record:
   - `current_database()`, `current_schema()`, and the database host/project
     reference from Supabase settings;
   - columns, data types, nullability, defaults, identity/generated status;
   - primary keys, unique indexes, check constraints, and all foreign keys
     (including `ON DELETE` behavior);
   - required seed/reference rows, including actual `wards` identifiers and
     the producer/device prerequisites.
   The following examples are read-only and must only be run after the
   operator has independently confirmed the Supabase Staging project:

   ```sql
   SELECT current_database(), current_schema(), inet_server_addr();

   SELECT table_name, column_name, data_type, is_nullable,
          column_default, is_identity, is_generated
   FROM information_schema.columns
   WHERE table_schema = 'ward_twin'
     AND table_name IN (
       'staff_profiles', 'user_credentials', 'account_approvals',
       'ward_access', 'wards', 'patients', 'encounters', 'producers',
       'devices', 'device_assignments', 'observation_readings',
       'observation_measurements', 'processing_state', 'risk_predictions',
       'alerts', 'assessments', 'import_jobs', 'import_rows'
     )
   ORDER BY table_name, ordinal_position;

   SELECT parent.relname AS table_name, constraint_row.conname,
          pg_get_constraintdef(constraint_row.oid) AS definition
   FROM pg_constraint AS constraint_row
   JOIN pg_class AS parent ON parent.oid = constraint_row.conrelid
   JOIN pg_namespace AS schema_row ON schema_row.oid = parent.relnamespace
   WHERE schema_row.nspname = 'ward_twin'
     AND parent.relname IN (
       'staff_profiles', 'user_credentials', 'account_approvals',
       'ward_access', 'wards', 'patients', 'encounters', 'producers',
       'devices', 'device_assignments', 'observation_readings',
       'observation_measurements', 'processing_state', 'risk_predictions',
       'alerts', 'assessments', 'import_jobs', 'import_rows'
     )
   ORDER BY parent.relname, constraint_row.conname;
   ```

   These queries do not prove that the connection is Staging by themselves;
   match the returned host/database to the Supabase dashboard and approved
   project reference before proceeding. Query `wards` separately for
   `WARD-A`/`WARD-B` after target confirmation.
6. Confirm `WARD-A` and `WARD-B` are present. If either is absent, stop and ask
   the staging owner whether to create dedicated synthetic wards. Never
   create wards or alter shared reference data as part of an unapproved seed.
7. Save the catalog output as a reviewed, non-secret staging schema snapshot
   outside source control or in the approved migration process. The snapshot
   must identify its Staging project and capture date.

## 4. Synthetic account and Ward fixture plan

Use unique, clearly synthetic identifiers and email addresses under the
reserved domain `example.invalid`. Use no real names, real email addresses,
patient identifiers, or clinical data.

### Human test identities

Create the following disposable identities only after read-only discovery
confirms no conflicting account exists:

| Test identity | Role | Ward membership | Purpose |
|---|---|---|---|
| `rbac-admin-a` | `admin` | `WARD-A` only | Admin endpoint access and explicit denial for `WARD-B`. |
| `rbac-doctor-a` | `doctor` | `WARD-A` only | Clinical reads/writes and cross-Ward denial. |
| `rbac-nurse-a` | `nurse` | `WARD-A` only | Observations, assessments, alert acknowledgement, imports. |
| `rbac-viewer-a` | `viewer` | `WARD-A` only | Read-only allow cases and write denials. |
| `rbac-doctor-b` | `doctor` | `WARD-B` only | Optional positive control for Ward B and cross-Ward denial. |
| `rbac-nurse-b` | `nurse` | `WARD-B` only | Optional positive control for Ward B and cross-Ward denial. |

If Staging already contains approved, explicitly designated test accounts,
prefer reusing them after verifying their role and Ward access. Do not use
staff accounts or modify existing users without the staging owner's approval.
Ward membership is sourced from `ward_access`, not a role claim supplied by
the client.

Register disposable non-admin identities through `/api/v1/auth/register`;
registration creates pending accounts and does not let a client select a
role. An already authorized staging admin can approve doctor, nurse, and
viewer accounts with the existing admin approval API and assign the planned
Ward IDs. The current API does not approve a new admin or change an existing
role. If no approved staging admin exists, stop and have the database owner
bootstrap one through a reviewed, staging-only procedure after account and
schema checks. Never create an admin through public registration.

Set unique, high-entropy passwords in the team's secret manager. Do not include
them in seed output or API reports. Delete disposable user rows only after
catalog-confirmed FK review and explicit owner approval.

## 5. Clinical and Gateway synthetic fixture plan

Use a unique run identifier, for example `RBAC-STG-<random-run-id>`, and
include it in synthetic text IDs and a separate fixture manifest. Generate
new UUIDs for UUID columns. Never reuse a fixture from a prior run without
proving it belongs to that run.

| Fixture | Intended shape from application SQL | Notes |
|---|---|---|
| Patient | `patients`: `patient_id`, `hn`, `display_name`, `birth_date`, `sex`; reads also reference `allergy_status`. | Use a synthetic name and identifiers; verify required columns, checks, and defaults in Staging. |
| Encounter | `encounters`: `encounter_id`, `patient_id`, `ward_id`, `an`, `bed_label`, `admitted_at`. | Create separate encounters in each approved test Ward only if needed. Use current, timezone-aware synthetic admission timestamps. |
| Gateway setup | `producers`, `devices`, `device_assignments`, with application SQL joining them by producer/device/Ward and assignment validity. | Exact columns and constraints must be discovered from Staging first. Register only a staging producer and synthetic device allowed by the staging API key. |
| Observation | `observation_readings` and `observation_measurements`; API payload includes UUID `reading_id`, `device_id` for IoT or `encounter_id` for manual, timezone-aware `measured_at`, and metric/value/unit. | Prefer API submission so validation, producer/device mapping, uniqueness, and processing-state effects are exercised. Use synthetic vitals such as heart rate only. |
| Processing/prediction | `processing_state` is updated by observation ingestion; worker writes `risk_predictions`. | The worker is asynchronous. Wait for processing to settle and record generated IDs; do not delete or reset shared processing state. |
| Alert | `alerts` references an encounter and prediction; application creates an open alert only from a qualifying ready prediction. | Do not invent a direct alert fixture until Staging FK/check/unique constraints are inspected. Prefer a synthetic encounter/observation that yields a qualifying prediction. If deterministic generation is unavailable, stop and agree on a reviewed Staging-only fixture procedure. |

Before any write test, verify the actual Staging schema for all referenced
tables, and confirm the model/runtime configuration cannot read production
data or contact production services.

## 6. Cleanup plan

No cleanup script is included or run by this planning task. A later script
must be written only after Staging catalog review and explicit approval.

### Required cleanup safeguards

- Require an explicit opt-in flag such as `WARD_TWIN_STAGING_RUN=1`, a
  separately configured expected Staging host/database/project reference, and
  an exact interactive confirmation containing the Staging project reference.
- Compare the connected database host and `current_database()` against
  operator-provided expected values. Fail closed on missing/mismatched values.
  `APP_ENV=production` alone is not proof of Staging and must never authorize
  data writes.
- Use a read-only preflight first. Print only the verified project reference,
  host/database identity, fixture IDs, and planned row counts; never print
  `DATABASE_URL` or credentials.
- Use a fixture manifest containing the run ID and exact owned primary keys.
  Cleanup must delete by those keys only; never use broad prefix deletion,
  `TRUNCATE`, unbounded deletes, or `CASCADE`.
- Inspect foreign keys and `ON DELETE` rules in the target project at runtime
  or compare against an approved schema snapshot. Abort if the schema differs
  from the reviewed snapshot.
- Delete in dependency order determined from the actual FK graph. Expected
  candidates include generated alerts, predictions, measurements, readings,
  import rows/jobs, assessments, device assignments, encounters, patients,
  devices/producers, account approvals, Ward memberships, credentials, and
  staff profiles. This is a candidate inventory, not an assumed FK order.
- Never delete shared wards or any row whose ownership is not proven by the
  current fixture manifest. Do not delete `processing_state` if it existed
  before the run or if other readings/predictions depend on its encounter.
- Before commit, verify every selected row belongs to the run and show
  per-table row counts for operator review. Run cleanup in a transaction and
  rollback on any mismatch. After commit, verify fixture IDs are absent.
- Preserve a non-secret audit record of run ID, project reference, deleted
  table counts, operator approval, and completion time.
- If a generated prediction or alert is referenced by another record, stop
  for manual review rather than deleting it or its parent.

### Suggested ownership approach

Use unique test-only PKs for patients, encounters, readings, and imports. For
tables without an ownership/tag column, record their exact generated PKs in
the manifest at insertion/response time. Do not add a database tag column
without a separate schema change approval.

## 7. RBAC API test checklist

All paths below use `/api/v1`. For each case, record endpoint, role, target
Ward, expected status, actual status, and PASS/FAIL only. Do not log request
headers, tokens, credentials, patient payloads, or response bodies.

| Endpoint | Allowed roles / identity | Expected denial cases |
|---|---|---|
| `GET /wards/{ward_id}/patients` | admin, doctor, nurse, viewer with membership | Gateway: 401; human without Ward membership: 403. |
| `POST /observations` (manual) | admin, doctor, nurse with encounter Ward membership | viewer: 403; wrong Ward: 403. |
| `POST /observations` (IoT) | Gateway key and registered staging device/assignment | no/invalid key: 401; Gateway key on human routes: 401. |
| `GET /encounters/{id}/observations` | admin, doctor, nurse, viewer with encounter Ward membership | wrong Ward: 403; invalid token: 401. |
| `GET /encounters/{id}/twin` | admin, doctor, nurse, viewer with encounter Ward membership | wrong Ward: 403; invalid token: 401. |
| `GET /encounters/{id}/agent-context` | admin, doctor, nurse, viewer with encounter Ward membership | wrong Ward: 403; invalid token: 401. |
| `GET /encounters/{id}/alerts` | admin, doctor, nurse, viewer with encounter Ward membership | wrong Ward: 403; invalid token: 401. |
| `PUT /alerts/{id}/acknowledgement` | admin, doctor, nurse with alert Ward membership | viewer: 403; wrong Ward: 403. Uses disposable open synthetic alert; cleanup/reset required. |
| `POST /encounters/{id}/assessments` | admin, doctor, nurse with encounter Ward membership | viewer: 403; wrong Ward: 403. Uses unique `client_record_id`; cleanup required. |
| `GET /encounters/{id}/assessments` | admin, doctor, nurse, viewer with encounter Ward membership | wrong Ward: 403. |
| `GET /import-templates/{kind}` | admin, nurse | doctor, viewer: 403. |
| `POST /imports` | admin, nurse with membership in CSV Ward | doctor, viewer: 403; Ward mismatch: 403. Upload synthetic CSV; cleanup required. |
| `GET /imports/{id}` | admin, nurse with import Ward membership | doctor, viewer: 403; wrong Ward: 403. |
| `POST /imports/{id}/commit` | admin with import Ward membership | doctor, nurse, viewer: 403; wrong Ward: 403. Mutates synthetic patient/encounter data; cleanup required. |
| `GET /admin/accounts/pending` | admin | doctor, nurse, viewer: 403; missing/invalid token: 401. |
| `PATCH /admin/accounts/{id}/approve` | admin | doctor, nurse, viewer: 403; invalid payload: 422. Use disposable pending synthetic account. |

Additional checks:

- For admin, doctor, and nurse with only `WARD-A` membership, test each
  encounter/alert/import object in `WARD-B` and expect 403.
- Use a `WARD-B` doctor/nurse test identity as a positive control if both
  Wards exist and the owner approves those identities.
- Test missing and malformed bearer tokens (401), approved account with
  disallowed role (403), and a pending/suspended account (403).
- Test `/api/v1/dev/token` on the staging deployment configured with
  `APP_ENV=production`; expect 404. Do not request a development token.
- Health endpoints `/healthz` and `/readyz` remain operational checks, not
  role-protected clinical APIs.

## 8. Operator sequence and go/no-go checklist

### Before Staging setup

- [ ] Staging owner approves a separate Supabase project and Render service.
- [ ] Confirm project reference, host, database name, region, and isolation from
      Production through Supabase/Render consoles.
- [ ] Confirm no production patient data was copied into Staging.
- [ ] Create unique staging secrets and store them only in secret management.
- [ ] Confirm Render automatic deploys are disabled until explicitly approved.

### Before creating fixtures

- [ ] Capture and review Staging catalog/schema and all relevant foreign keys.
- [ ] Confirm `WARD-A`/`WARD-B` exist, or get explicit approval for synthetic
      test wards.
- [ ] Confirm four test roles and membership state using read-only queries.
- [ ] Confirm no test account identifiers conflict with existing accounts.
- [ ] Agree on alert-generation behavior and prediction worker observation.
- [ ] Review the proposed fixture manifest and cleanup deletion plan.
- [ ] Obtain explicit approval before running any seed or cleanup operation.

### Before running API matrix

- [ ] Confirm API hostname is the Staging Render service.
- [ ] Confirm database hostname/project reference is the Staging Supabase
      project.
- [ ] Confirm `/api/v1/dev/token` returns 404.
- [ ] Confirm no credentials/secrets are written to test logs.
- [ ] Use only synthetic fixtures; capture HTTP status codes and redact all
      other response content.
- [ ] Run cleanup only after reviewing exact IDs and row counts.

### Go/no-go

Production deployment is **NO-GO** until all permitted and denied matrix
scenarios pass, cross-Ward denials pass for admin/doctor/nurse, Gateway is
confined to IoT ingestion, the dev-token route is absent, and synthetic
fixtures are either cleaned up or explicitly retained by the Staging owner.
