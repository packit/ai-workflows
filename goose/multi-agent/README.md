# Multi-agent collaboration with goose

## Example

Run multiple agents:

```bash
$ make \
  ROLE=executor \
  TASK="Given a package name, determine and return version of the package in CentOS Stream 10, \
  if present. You can use https://mirror.stream.centos.org/10-stream/ to find RPM packages \
  present in CentOS Stream 10." \
  run-goose-agent
```

```bash
$ make \
  ROLE=commander \
  TASK="Get name of a random package in Fedora (you can use https://packages.fedoraproject.org/index-static.html) \
  and instruct the executor agent to run its task with that package name as an input. \
  Once the executor is done, print the result. Then get another random package name \
  and repeat the steps 4 more times." \
  run-goose-agent
```

## How it works

- `goose-team` container runs the [GooseTeam](https://github.com/cliffhall/GooseTeam) MCP server
- all agents (goose instances) connect to the server, it allows them to register themselves
  and exchange messages
- communication protocol description is part of the recipe

## Concerns

- execution relies on an _event loop_, but LLMs have tendency to exit the loop despite
  being instructed not to
