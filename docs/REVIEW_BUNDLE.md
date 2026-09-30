# BlackBox review bundle

Updated 29 September 2026. **Development evidence — not a submission-ready certification.**

**Established fact:** this bundle collects the requirement mapping, proposed Round 2 content, current demo script, measured component checks, bounded network observations and captured development failures. The official Round 2 template and notice remain unverified. Failed outputs are included as failed evidence, not as examples of correct industrial advice.

Start with `docs/READINESS_2026-09-29.md`, then `docs/SIH26117_REQUIREMENTS.md` and `docs/ROUND2_SUBMISSION_BRIEF.md`. `docs/UPGRADE_REPORT_2026-09-28.md` retains the earlier implementation history. `data/LIVE_WORKFLOW_REPORT.md` lists captured attempts, including incomplete ones. Individual `evaluation.json` files distinguish the application's status from the inspected verdict. A `post_fix_check.json` is a later checker replay, not a changed historical result. Separate engine recalculation and numeric checks have deliberately narrower scope than complete industrial correctness.

`MANIFEST.json` lists the included files, byte counts and SHA-256 hashes. The bundle is made from an explicit allowlist. It excludes access keys, model weights, private runtime databases, answer keys and the bulk document corpus. Validation records contain synthetic task/review text and local model routes. Inspect the contents before sharing outside the team.

**Informed inference:** the competitive case depends on repeatable correct source-to-output workflows and credible evidence of confidentiality. The current results do not justify claims of readiness, independent expert accuracy or superiority to commercial tools.

**Unresolved:** official submission format and deadline; successful unseen industrial workflows; independent review; two automatic real-model routes; actual vision interpretation; venue GPU validation; second-machine offline installation. These must remain visible during any evaluator presentation.

**Established fact:** final demonstration hardware is undecided, as confirmed by the user. The included CPU timings do not establish performance on a future host.

Rebuild with `python scripts/build_review_bundle.py --out <new-file.zip>`. Existing archives are not overwritten. Hashes establish that the packaged files match this snapshot; they do not establish that the contents are correct or independently certified.
