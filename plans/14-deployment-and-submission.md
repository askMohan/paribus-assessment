# 14 - Deployment and submission

Prerequisite: [13](13-documentation-and-examples.md). This is the final planned implementation step.

## Outcome

The implemented application has a verified public URL and a source repository ready for the assignment submission.

## Tasks

- [x] **14.01** When this step is requested, inspect available hosting/repository configuration and account access. Choose a suitable provider; Render is the assignment's suggestion, not a mandatory dependency.
- [x] **14.02** Verify the selected hosting plan can run one application instance with one worker and persist the configured SQLite file. If persistent storage is unavailable, report the limitation rather than claiming restart-safe resume.
- [x] **14.03** Configure the build/start command, provider port, upstream URL, SQLite path, HTTP settings, and limits using the implemented configuration names.
- [x] **14.04** For the user-selected free demo, verify writable SQLite storage and explicitly document that no persistent disk is attached. Do not claim hosted restart-safe recovery.
- [x] **14.05** Publish the application when the implementation and this deployment step are authorized; do not deploy while merely working on earlier steps or planning documents.
- [x] **14.06** Check the public health endpoint and generated API documentation.
- [x] **14.07** Check valid/invalid validation-only requests and unknown-batch `404` without creating upstream records.
- [x] **14.08** Perform one explicitly intended, small end-to-end live upload when appropriate for the submission verification. Record its UUID, confirm `202`/`Location`, poll the documented result, and verify activation. Avoid repeated or large live test batches.
- [x] **14.09** Verify database persistence/recovery using the deployment's storage configuration and a controlled restart; document observed behavior accurately.
- [x] **14.10** Create or update the source repository without committing environment secrets, SQLite files, local artifacts, or credentials. Verify reviewer access and include all application/test/container files.
- [x] **14.11** Replace documentation placeholders with actual repository and hosted API URLs, including the API documentation URL.
- [x] **14.12** Check the implementation/submission checklist, summarize verification and limitations, and provide the two required submission links.

## Verification

- [x] Public health and API documentation URLs work from outside the local application environment.
- [x] The service is configured for one instance/worker; free-host SQLite is ephemeral and hosted restart-safe recovery is not claimed.
- [x] Live acceptance/results match the agreed field sets; local tests remain the main error-scenario evidence.
- [x] Source and public application links are real, accessible, and recorded in the README and submission summary.

## Completion notes

Completed on 2026-11-19 with the user-selected free-host limitation.

- **Submission:** source https://github.com/askMohan/paribus-assessment ; interactive API documentation https://paribus-assessment-kq37.onrender.com/docs ; API base https://paribus-assessment-kq37.onrender.com ; health https://paribus-assessment-kq37.onrender.com/health . Public repository/reviewer access and deployed endpoints were verified.
- **Hosting:** the user changed the choice to Render Free after the paid configuration required a payment method. No paid resources were created. Service `srv-dbuvr0u1egvs739qmkc0` reports plan `free`, one configured instance, Docker runtime, and zero disks. `render.yaml` passed both official schema validation and authenticated Render server-side validation. Service creation used the same settings through the official CLI; no Blueprint resource was created.
- **Runtime:** reuse the existing Dockerfile and its single-worker Uvicorn command with platform-provided `PORT`. No Docker command override, separate queue, or database server. `SQLITE_PATH=/data/hospitals.db`, the configured upstream URL, and documented HTTP/limit defaults apply. Automatic deploys are off. Initial deployment `dep-dbuvr161egvs739qmmdg` of commit `08b884f` reached `live`; subsequent changes are documentation/verification records only.
- **Public smoke checks:** `/health`, `/docs`, `/openapi.json` returned `200`; both valid CSV examples returned empty `204`; missing-address input returned the exact structured `422`; unknown batch returned `404`. Repeated the read-only/dry-run checks successfully after restart.
- **One intended live upload:** a single synthetic hospital in batch `15bafb2c-64c9-4fdc-8580-baa765e79f98` returned `202` with the correct `Location`. Polling showed processing and then `created_and_activated`: total/processed 1, failed 0, activated true, observed duration 5.669 seconds, upstream ID 2572. Strict response models verified the exact one-field acceptance and seven/four-field result. Completed resume returned `409`. No second upload was performed.
- **Controlled restart:** Render returned `200` and replaced instance suffix `hlfds` with `pxzq5`. After replacement the saved batch returned `404`, confirming ephemeral local state. A read-only Directory lookup confirmed the original hospital remained active upstream. This UUID is historical verification evidence, not a currently retrievable local batch. Do not re-upload it to reconstruct local history.
- **Storage limitation:** hosted restart-safe resume is unavailable on Render Free. Restart/redeploy loses local batches; idle spin-down can interrupt background work and cold starts delay requests. Resume is limited to surviving local state. Local Compose still uses a persistent named volume; durable hosted recovery requires another storage-capable plan/provider. README and the four-page technical design/PDF state this distinction; every updated PDF page was rendered and visually checked.
- **Repository/local checks:** all implementation, tests, examples, container/configuration files, and intended documentation are included. Runtime `.env`, SQLite, virtualenv, and credentials are excluded; common credential/private-key patterns were reviewed. PDF files are marked binary to preserve their bytes. Existing local database hash is unchanged. **277 tests passed in 18.54 seconds**; Ruff, Compose, dependency compatibility, response-example/link, and text whitespace checks pass. No application code or dependency changes were needed for deployment. Steps 01-14 are complete for this agreed assessment/demo scope.
