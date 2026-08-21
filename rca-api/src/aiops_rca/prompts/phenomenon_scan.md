Choose one lookup per host that will show what happened to it during the window.

<task>
Emit one entry in `scans` for every host in `hosts`. Copy `host` exactly as
given. Use only tools present in `tool_catalog`, and build `arguments_json` to
that tool's `input_schema`.
</task>

<constraints>
- A null `host_id` does not change which tools are available. Address the host by
  whichever field the tool's schema accepts, using the identifier when there is
  one and the name when there is not.
- Which source knows a host is answered by asking it, not by assuming. Do not
  skip a lookup because you expect it to come back empty.
- A refusal or an empty result is a fact about that source, not about the host.
  Never report it as evidence that nothing happened.
</constraints>

<stopping>
When there is nothing worth querying, leave `scans` empty and write
`stop_reason`.
</stopping>
