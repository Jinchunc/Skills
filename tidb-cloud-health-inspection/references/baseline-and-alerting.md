# Baseline and Alerting

## Comparable windows

Select the business timezone and compare windows with matching duration, dimensions, selector, unit, aggregation, step, and business phase. Store comparison validity and an invalid-reason field. Large sampling steps that smooth peaks must be disclosed.

A large relative change is an observation, not automatically a fault. Combine relative thresholds with absolute floors/caps and volume/sample guards.

## State and delivery

Persist previous status and alert set, first/last seen, last notified, value, criterion, evidence path, baseline metadata, sample count, run ID, and delivery outcome.

Notify on worsening transitions, material alert-set changes, or cooldown persistence. Recovery is silent by default. Deterministic severity does not require LLM interpretation.
