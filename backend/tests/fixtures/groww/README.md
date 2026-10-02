# Groww contract fixtures — GRW-026

These files are **synthetic**. They are hand-built to match the response shapes
described in the official Groww documentation (verified 2026-09-17), and they
exist to test *this client*: envelope handling, error mapping, field parsing,
batch chunking and window splitting.

They are NOT recorded from a live account. Passing tests here do **not**
demonstrate that the live API behaves this way. Every endpoint whose exact path
or request body is inferred rather than documented is listed in
`docs/LIMITATIONS.md` and must be confirmed against a real account before LIVE
trading.

Production code must never import from this directory; the layering test
`test_fixtures_not_in_production` asserts it.
