# 14 - Deployment and submission

Prerequisite: [13](13-documentation-and-examples.md). This is the final planned implementation step.

## Outcome

The implemented application has a verified public URL and a source repository ready for the assignment submission.

## Tasks

- [ ] **14.01** When this step is requested, inspect available hosting/repository configuration and account access. Choose a suitable provider; Render is the assignment's suggestion, not a mandatory dependency.
- [ ] **14.02** Verify the selected hosting plan can run one application instance with one worker and persist the configured SQLite file. If persistent storage is unavailable, report the limitation rather than claiming restart-safe resume.
- [ ] **14.03** Configure the build/start command, provider port, upstream URL, SQLite path, HTTP settings, and limits using the implemented configuration names.
- [ ] **14.04** Configure a persistent volume/disk and verify permissions. Do not rely on an ephemeral application filesystem for recovery claims.
- [ ] **14.05** Publish the application when the implementation and this deployment step are authorized; do not deploy while merely working on earlier steps or planning documents.
- [ ] **14.06** Check the public health endpoint and generated API documentation.
- [ ] **14.07** Check valid/invalid validation-only requests and unknown-batch `404` without creating upstream records.
- [ ] **14.08** Perform one explicitly intended, small end-to-end live upload when appropriate for the submission verification. Record its UUID, confirm `202`/`Location`, poll the documented result, and verify activation. Avoid repeated or large live test batches.
- [ ] **14.09** Verify database persistence/recovery using the deployment's storage configuration and a controlled restart; document observed behavior accurately.
- [ ] **14.10** Create or update the source repository without committing environment secrets, SQLite files, local artifacts, or credentials. Verify reviewer access and include all application/test/container files.
- [ ] **14.11** Replace documentation placeholders with actual repository and hosted API URLs, including the API documentation URL.
- [ ] **14.12** Check the root definition of done, summarize verification and limitations, and provide the two required submission links.

## Verification

- [ ] Public health and API documentation URLs work from outside the local application environment.
- [ ] The final environment actually uses one worker and durable SQLite storage when restart recovery is claimed.
- [ ] Live acceptance/results match the agreed field sets; local tests remain the main error-scenario evidence.
- [ ] Source and public application links are real, accessible, and recorded in the README and submission summary.

## Completion notes

Not started.
