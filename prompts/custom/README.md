# Custom prompts

These are reconstruction decisions, not CODESKILL paper prompts: historical
trace description, MiniLM candidate pairing, and evidence compaction. Each
call records its prompt file hash and any context state.

The `r015_*code_examples*.md` and historically named
`r015_*preserve_code_examples.md` files are explicit R015 rebuild variants.
They add evidence-bound adaptable examples and explicit retain/revise/remove/add
lifecycle decisions without changing the transcriptions under `prompts/paper/`.

`r012_fig07_event_extraction_evidence.md` is the Fig. 7 variant with the raw
`source_entry_id` evidence contract that R012–R015 runs used under
`prompts/paper/`; it was moved here when `prompts/paper/` was restored to the
faithful transcription (docs/DECISIONS.md P1).

`p1_*.md` are the P1 lean-runner prompts (docs/DECISIONS.md §3, P1 option B):
Figs. 6–7 verbatim plus one `when_to_apply` rule each and an `evidence_steps`
sidecar field; a pairing prompt over command sequences; and the user-message
templates (`p1_user_templates.md`).
