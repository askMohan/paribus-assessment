# 14 - Deployment and submission

Prerequisite: [13](13-documentation-and-examples.md). This is the final planned implementation step.

## Outcome

The implemented application has a verified public URL and a source repository ready for the assignment submission.

## Tasks

- [x] **14.01** When this step is requested, inspect available hosting/repository configuration and account access. Choose a suitable provider; Render is the assignment's suggestion, not a mandatory dependency.
- [x] **14.02** Verify the selected hosting plan can run one application instance with one worker and persist the configured SQLite file. If persistent storage is unavailable, report the limitation rather than claiming restart-safe resume.
- [x] **14.03** Configure the build/start command, provider port, upstream URL, SQLite path, HTTP settings, and limits using the implemented configuration names.
- [ ] **14.04** Configure a persistent volume/disk and verify permissions. Do not rely on an ephemeral application filesystem for recovery claims.
- [ ] **14.05** Publish the application when the implementation and this deployment step are authorized; do not deploy while merely working on earlier steps or planning documents.
- [ ] **14.06** Check the public health endpoint and generated API documentation.
- [ ] **14.07** Check valid/invalid validation-only requests and unknown-batch `404` without creating upstream records.
- [ ] **14.08** Perform one explicitly intended, small end-to-end live upload when appropriate for the submission verification. Record its UUID, confirm `202`/`Location`, poll the documented result, and verify activation. Avoid repeated or large live test batches.
- [ ] **14.09** Verify database persistence/recovery using the deployment's storage configuration and a controlled restart; document observed behavior accurately.
- [x] **14.10** Create or update the source repository without committing environment secrets, SQLite files, local artifacts, or credentials. Verify reviewer access and include all application/test/container files.
- [ ] **14.11** Replace documentation placeholders with actual repository and hosted API URLs, including the API documentation URL.
- [ ] **14.12** Check the root definition of done, summarize verification and limitations, and provide the two required submission links.

## Verification

- [ ] Public health and API documentation URLs work from outside the local application environment.
- [ ] The final environment actually uses one worker and durable SQLite storage when restart recovery is claimed.
- [ ] Live acceptance/results match the agreed field sets; local tests remain the main error-scenario evidence.
- [ ] Source and public application links are real, accessible, and recorded in the README and submission summary.

## Completion notes

In progress on 2026-11-19; deployment and live verification remain outstanding.

- The user selected **Render with paid persistent storage**. `render.yaml` defines one Docker web service on the current `0.5c-512mb` compute plan, one instance, a 1 GB disk mounted at `/data`, and `SQLITE_PATH=/data/hospitals.db`. Official Render documentation confirms paid services support persistent disks and disk-backed services use a single instance. This verifies provider capability, not an already provisioned disk.
- Reuse the existing Dockerfile for build/start: one Uvicorn worker, `0.0.0.0`, platform-provided `PORT`, existing upstream URL and application HTTP/limit defaults. Health path is `/health`. Automatic deploys are off to avoid interrupting active batches on documentation pushes.
- Validated the Blueprint against Render's current official JSON schema. Reviewed tracked files and common token/private-key patterns; `.env`, SQLite files, and `.venv` are excluded. The intentional design PDF is part of the reviewer documentation.
- Public source repository verified without authentication: https://github.com/askMohan/paribus-assessment . Local `origin/main` matches the published initial implementation; Git push access was verified with a dry run. All application, test, example, and container files are present. The README now records the real source URL and deploy instructions.
- Local regression: **277 tests passed in 18.54 seconds**; Ruff, Compose configuration, and whitespace checks pass. No application behavior or dependency changes are needed for this deployment configuration.
- Installed official Render CLI v2.37.0 at `~/.local/bin/render`, verifying the release archive checksum. No Render credential was initially available. CLI authentication is pending; the first device-login process returned `slow_down`, so a fresh login was initiated. No API key belongs in the application `.env` or repository.
- Remaining: authenticate/select the intended workspace, provision service/disk and check actual permissions, deploy, verify public health/docs/validation/404, perform one small intended live upload, confirm saved state after a controlled restart, and record the actual hosted API/docs URLs. No hosted persistence, activation, or public URL is claimed yet.
