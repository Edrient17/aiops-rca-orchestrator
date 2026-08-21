Find the hosts this investigation is about. Which source knows a given name is
not fixed in advance: a machine may be named in a monitoring inventory, a log
index, an agent list, or in none of them.

<task>
For each name in `unresolved`, choose one tool from `tool_catalog` that could
list or match host names, and issue it as `tool_name` with `arguments_json`. Log
indices carry the name in a field such as `host.name`; agent lists carry it in
`name`.

`attempts` holds your earlier lookups and their responses. Read host names out
of those responses into `hosts`.
</task>

<constraints>
- Name only tools that appear in `tool_catalog`.
- Copy `host` exactly as the response spells it. Never put a name in `host_id`.
- Set `host_id` only when the response actually carries an identifier, otherwise
  null. Either is fine: later stages can address a host by name, so do not spend
  a lookup re-finding a host you have already named just to collect an id.
- `found_by` is the tool that produced the name.
- An empty response means that tool does not know this host. It is not evidence
  that the host does not exist. Look somewhere else.
- `host_selector` describes which hosts the report wants. Honour it as given.
</constraints>

<stopping>
Set `tool_name` to null and write `stop_reason` once every name is resolved or no
source is left worth trying.
</stopping>
