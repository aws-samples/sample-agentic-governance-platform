# Skills

Each subfolder here is one [Agent Skill](https://agentskills.io) the agent can use. The agent
loads them at startup with Strands' `AgentSkills` plugin.

## Add a skill

1. Create `skills/<name>/SKILL.md`. The folder name must equal the skill's `name`.
2. Start the file with frontmatter, then write the instructions:

   ```markdown
   ---
   name: claims-intake
   description: Collect the facts of a new insurance claim.
   ---
   Ask for the policy number first, then ...
   ```

3. Optional: put supporting files in `scripts/`, `references/` or `assets/` next to `SKILL.md`.

Only `name` and `description` go into the system prompt. The model calls the `skills` tool to
load the full instructions when it needs them. With no skill folders, the agent runs without the
`skills` tool.

Keep this README: the `Dockerfile` copies `skills/`, and the build fails if the folder is gone.
