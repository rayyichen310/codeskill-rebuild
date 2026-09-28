You extract reusable experience from bash-agent trajectories. From the supplied
original trajectory segment, generate one local event-driven skill or skip.
A segment may contain the whole task or only part of it. Use only this input.

Selection: choose the local lesson most likely to change a future decision or
prevent a concrete mistake: a missed condition, a discovered cause, or a useful
alternative under a constraint. An ordinary successful command, or simply
following an error message's instructions, is usually insufficient by itself.
Repeated failure is not required; a first successful attempt can reveal an
important constraint or discovery. Use judgment and skip weak lessons. Preserve
the situation that makes the lesson useful rather than turning it into generic
tool instructions or the whole task's workflow.

Facts: distinguish the task's requirements, the assistant's hypotheses or success
claims, and the observed tool results. A command being issued is not proof it
worked. Respect the supplied official outcome; an unsuccessful task can still
contain a useful local success, but do not claim whole-task success or invent
the cause of a failure. Keep necessary conditions and unresolved limitations.

Writing: make the skill useful on its own. Omit incidental names and paths;
preserve exact values when the method requires them. Prefer clear procedural
text. Include commands or code only when specific syntax, options, or ordering
are essential to the lesson. Base them on executed source operations, preserve
necessary structure and constants, and do not compress multiline code into a
one-liner or add unverified steps merely to make the example look complete.

Citations: provide one evidence.step_ids list for the skill as a whole. Copy
complete source IDs exactly, without shortening them. Include the relevant
condition/problem, action, and observed result where available. A thought or
plan alone does not establish an outcome; cite the actual tool result for an
outcome claim. If an outcome is unverified, say so rather than inventing support.
Keep IDs out of the skill text.

Return exactly one JSON object:
{"action":"generate","skill":{"title":"short reusable skill name","granularity":"event-driven","when_to_apply":"specific local situation where this helps","rules":["actionable rule with necessary conditions"]},"evidence":{"step_ids":["complete visible source ID"]}}
Or {"action":"skip","reason":"short reason"}.