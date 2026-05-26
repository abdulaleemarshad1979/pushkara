# Godavari Pushkaralu 2027 — Documentation

This folder contains the planning and reference documentation for taking the Pushkaralu codebase from a Render free-tier prototype to a live, govt-operated festival platform.

| File | What it covers |
|---|---|
| [`SYSTEM_DOCUMENTATION.md`](./SYSTEM_DOCUMENTATION.md) | Architecture, modules, APIs, data model, deployment topology, security model, observability, known tech debt, glossary. **Read this first.** |
| [`GOVERNMENT_REQUIREMENTS.md`](./GOVERNMENT_REQUIREMENTS.md) | Everything that must be sourced from Govt. AP / Govt. India: hosting tenancy, CCTV / telecom / weather data feeds, Mana Mitra & SMS gateway access, identity & directory data, MoUs, approvals, hardware, personnel, datasets, compliance, indicative costs. **Use this to drive department meetings.** |
| [`DEPLOYMENT_CHECKLIST.md`](./DEPLOYMENT_CHECKLIST.md) | Twelve sequential gates from "before any code is touched" to "post-festival shutdown". Treat each gate as required pass-through; do not skip ahead. |

## How to use these documents

1. **Project lead** — read all three end-to-end once. Use the TL;DRs at the bottom of the requirements & checklist files for executive briefings.
2. **DevOps / SRE** — focus on `SYSTEM_DOCUMENTATION.md` §3, §7, §8, §10 and `DEPLOYMENT_CHECKLIST.md` Gates 2-7.
3. **Govt liaison / Nodal Officer** — focus on `GOVERNMENT_REQUIREMENTS.md` §0, §5, §12 and `DEPLOYMENT_CHECKLIST.md` Gate 0.
4. **Auditor / CERT-In** — `SYSTEM_DOCUMENTATION.md` §7 (security model) and `DEPLOYMENT_CHECKLIST.md` Gate 7.
5. **Vendor team / new engineer onboarding** — `SYSTEM_DOCUMENTATION.md` §4 (code layout) + §5 (data model) + §6 (APIs).

## What's deliberately NOT in scope here

- Frontend implementation details — the dashboards are plain HTML/JS without a build step; `dashboards/admin.html`, `dashboards/index.html`, `dashboards/user.html` are self-documenting.
- Source-level walkthroughs — each module has detailed docstrings; this folder is for *planning*, not *training*.
- Vendor selection / RFP content — that belongs in a separate procurement document, not in the source repo.

## Contributing changes to these docs

These are living documents. As the festival approaches, expect to:

- Update `SYSTEM_DOCUMENTATION.md` §1.1 once the official date is confirmed.
- Tick boxes in `DEPLOYMENT_CHECKLIST.md` as each gate closes; commit the change with a short `docs(checklist): gate-N-step-X done` message.
- Add to `GOVERNMENT_REQUIREMENTS.md` §11 with actual quoted vendor numbers once procurement runs.

Pull requests for documentation changes should be reviewed by **at least** the Nodal Officer's representative and the lead engineer.
