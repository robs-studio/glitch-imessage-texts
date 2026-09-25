# Changelog

## 0.1.0 (2026-09-25)

First public release.

- Reads the Messages database on a Mac, read-only, and files each day of texting as one dated line on the right person's card in Glitch.
- Keeps each conversation as a private transcript on your Mac, so "where did we leave off with <name>" is answered from what was actually said.
- Numbers Glitch does not know yet wait on a texts review list; a person is never created without your yes.
- A summary pass writes each day's line in plain words, in one background helper; a day not summarised within 3 days lands with its opening line.
- An optional morning run joins Glitch's morning catch-up once you approve it; it calls no AI model.
- Backfill of older texts, always a dry run first, with a copy of each card kept before its first backfilled line.
- Exclude and include: stop reading someone's texts, or start again, previewed first.
- One-time codes, PINs and card numbers are stripped before anything is written.
- Ships the `/texts` skill and a test suite that uses invented people and temporary folders only.
