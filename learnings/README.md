# Learnings

Durable, indexed notes on things we discovered the hard way — exchange quirks, library gotchas, infra surprises, design dead-ends. Anything that, if rediscovered six months from now, would cost an afternoon.

## What goes here vs. elsewhere

- **`CLAUDE.md`** — stable architectural decisions and invariants.
- **`.claude/rules/*.md`** — long-lived, prescriptive rules ("how we always do X").
- **`plan/`** — what we intend to build, in order.
- **`learnings/` (this dir)** — empirical findings. The shape of an external API. The reason we picked option B over A. A bug class we hit. Always tied to an observation, never aspirational.

If a learning matures into a rule we always follow, promote it into `.claude/rules/` and leave a one-line stub here pointing at it.

## File conventions

- One topic per file. Short and specific titles: `polymarket-idempotency-and-cancels.md`, not `polymarket-notes.md`.
- Use the template in [`_template.md`](_template.md). Frontmatter is required so the index is machine-regenerable.
- Date entries when the observation was made — APIs change, and a stale finding is worse than no finding.
- Cite evidence: a captured response, a log line, a commit, a doc URL. "I think it works this way" is not a learning; "I tried it and got this" is.

## Index

The index is grouped by topic. Add new entries under the right heading; create new headings as needed. Each line: `- [Title](file.md) — YYYY-MM-DD — one-line hook`.

### Polymarket

_(empty — populated during Phase 1)_

### Kalshi

_(empty — populated during Phase 8 pre-work)_

### Signals & data sources

_(empty)_

### Infra (Postgres, sops, docker, VPS)

_(empty)_

### Python / async / tooling

_(empty)_

### ML / RL

Findings from the ML sidetrack. Anything about feature drift, train/serve divergence, label leakage, OPE estimator behavior, or reward-shaping surprises goes here. Use `topic: ml` in frontmatter.

_(empty — populated during sidetrack M1+)_

### Design dead-ends

Things we tried and abandoned, with the reason. Stops us from re-trying them in six months.

_(empty)_
