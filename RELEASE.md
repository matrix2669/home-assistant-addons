# Release Process

This repository follows the standalone `main`/`dev` release model. Feature branches normally target `dev`; this initial branch was based on `main` because `dev` did not exist. No release, tag, image publication, or deployment is part of the current task.

Before promotion or release, refresh workspace standards and run the reconciliation gate in `AGENT.md`. Require offline tests, manifest validation, dependency/source review at exact refs, container build for amd64 and aarch64, and live integration tests covering admin identity, Ingress, HTTP configure/restart, ambiguous disconnect, HA rollback and manual confirmation. A test suite alone is not production acceptance.

For release, synchronize `VERSION`, changelog, `config.yaml` version and release metadata. Build from the reviewed source commit using the app's device-build Dockerfile. Review the complete manifest and permission scope. Publish only after explicit release authorization and applicable approval; do not publish an image as a substitute for app source. Never silently change `latest` or replace an existing version artifact.
