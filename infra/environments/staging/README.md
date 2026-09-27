# infra/environments/staging

Root module for the staging environment. Intended to point at a real (but non-production)
cloud account once P0-02's plan has been reviewed - see docs/KICKOFF.md: "stop and report
before anything touches a real account." Until that review happens, treat this directory as
a target shape, not something to `apply`.
