# These files are skills built from agents

If you want to use these skills, we have documentation in this markdown page: https://github.com/packit/ai-workflows/blob/main/skills_installation.md

## How to build

```bash
claude --model claude-opus-4-6 --effort high "Please take a look at the BeeAI workflows implemented in agents directory. Please convert Workflow in {workflow_file} to Claude skill and save that skill to agents_as_skills directory.
Restrictions:
 - Pay attention to tools used by the workflow and do not omit them
 - Do not restrict tools that the skill can use
 - Specify arguments the skill uses as an input"
```
