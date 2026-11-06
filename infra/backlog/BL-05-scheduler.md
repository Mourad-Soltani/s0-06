# BL-05: A scheduler for the periodic jobs

Status: backlog (ordered by the Head Chief, 2026-11-02)
Depends on: BL-04 (the reconciliation job), S3-03 (the Closer's sweep), the Scout

## Problem
Three jobs only help if something runs them regularly, and nothing does: the reconciliation job (settles sends stuck in SENDING), the Closer's
sweep (sends approved proposals after a rate limit or a kill-switch release), and the Scout (reads new signals). Each is a function today.

## Proposal
- One small runner that calls each job on its own interval (the reconciler every five minutes, the sweep every minute, the Scout as configured),
  with a lock so two copies never run the same job at once, and a jitter so they do not all fire together.
- Each run is logged by outcome counts (never content), and a job that fails raises an alert through the alert channel (cutover D2) without stopping
  the others.
- It must be safe to run under any process supervisor, and to stop mid-run: every job is already idempotent.

## Acceptance
Tests for the schedule, the lock, one job failing without stopping the others, and a clean stop. ADR-24 and ADR-23 updated.

## Priority
Required before the first real send: without it a stuck send is only noticed when someone runs the job by hand.
