#!/usr/bin/env python3
"""Redeploys the n8n workflows on the orchestrator host.

Importing a workflow replaces the stored node list, which drops the credential
assignments made in the UI, and n8n deactivates the workflow on import and only
picks up the change on restart. This script does all three in the right order:
it lifts the current credential assignments into the new workflow file, imports
that, reactivates, and restarts n8n.

Restarting n8n kills whatever it is executing. An investigation that dies
mid-flight cannot report its own failure either -- n8n tries to call the error
workflow with a database pool it has already closed -- so the request is left
stranded with no Slack message explaining why. The script therefore refuses to
run while an execution is in progress.

Run it on the orchestrator host, from the repository directory:

    python3 scripts/redeploy-workflow.py
    python3 scripts/redeploy-workflow.py --force   # ignore running executions
    python3 scripts/redeploy-workflow.py --wait 300  # wait for them to finish
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

WORKFLOW_ID = "aiops-main-v010"
SOURCE = "workflows/01-aiops-main.json"
PATCHED = "workflows/.redeploy-with-credentials.json"
PATCHED_IN_CONTAINER = "/opt/aiops/workflows/.redeploy-with-credentials.json"

# The error handler ships alongside the main workflow and needs none of the
# credential carry-over below: both of its HTTP nodes authenticate from $env, so
# there is nothing the UI holds that an import would drop. It still has to be
# redeployed here. n8n-import only ever runs once, guarded by its marker file, so
# without this a change to the error path would sit in the repository while the
# deployment kept running whatever was bootstrapped on day one.
ERROR_WORKFLOW_ID = "aiops-error-v010"
ERROR_IN_CONTAINER = "/opt/aiops/workflows/99-aiops-error-handler.json"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, cwd=REPO, **kwargs)


def psql(sql):
    result = run(["docker", "compose", "exec", "-T", "postgres", "psql",
                  "-U", "aiops", "-d", "aiops", "-tAc", sql])
    if result.returncode != 0:
        fail("database query failed:\n" + result.stderr.strip())
    return result.stdout.strip()


def fail(message):
    print("error: " + message, file=sys.stderr)
    sys.exit(1)


# Backstop for when the container start time cannot be read: an execution cannot
# outlive the workflow's own executionTimeout.
LIVE_WINDOW_SECONDS = 900


def n8n_started_epoch():
    """When the current n8n process started, or None if that cannot be read."""
    container = run(["docker", "compose", "ps", "-q", "n8n"]).stdout.strip()
    if not container:
        return None
    raw = run(["docker", "inspect", "--format", "{{.State.StartedAt}}",
               container]).stdout.strip()
    if not raw:
        return None
    # Docker reports nanoseconds; datetime parses at most microseconds.
    cleaned = re.sub(r"\.(\d{6})\d*", r".\1", raw.replace("Z", "+00:00"))
    try:
        return datetime.fromisoformat(cleaned).timestamp()
    except ValueError:
        return None


def in_flight():
    """Splits stuck executions into ones that could still be running and debris.

    An execution created before the current n8n process started cannot be
    running in it: n8n neither resumed nor discarded the row, so it is a
    leftover from a crash or restart and will sit there forever.
    """
    rows = psql(
        "select id || '|' || status || '|' || extract(epoch from \"createdAt\") "
        "|| '|' || round(extract(epoch from (now() - \"createdAt\"))) "
        "from execution_entity where status in ('running','new','waiting') "
        "order by id;")
    boot = n8n_started_epoch()
    live, stale = [], []
    for line in rows.splitlines():
        if line.count("|") != 3:
            continue
        execution_id, status, created, age = line.split("|")
        entry = (execution_id, status, int(float(age)))
        predates_boot = boot is not None and float(created) < boot
        too_old = int(float(age)) >= LIVE_WINDOW_SECONDS
        (stale if predates_boot or too_old else live).append(entry)
    return live, stale


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="restart even while executions are in progress")
    parser.add_argument("--wait", type=int, default=0, metavar="SECONDS",
                        help="wait up to this long for executions to finish")
    args = parser.parse_args()

    print("== pull ==")
    if run(["git", "fetch", "--quiet", "origin"]).returncode != 0:
        fail("could not fetch origin")
    # Checked, unlike the fetch above used to be on its own. A reset that fails
    # -- a lock left by another git process, an unwritable file -- leaves the
    # working tree on the old commit, and everything downstream would then
    # cheerfully deploy the previous version while printing the reset as done.
    reset = run(["git", "reset", "--hard", "origin/main", "--quiet"])
    if reset.returncode != 0:
        fail("could not reset to origin/main:\n" + (reset.stderr or reset.stdout).strip())
    print("  " + run(["git", "log", "--oneline", "-1"]).stdout.strip())

    print()
    print("== in-flight executions ==")
    live, stale = in_flight()
    deadline = time.time() + args.wait
    while live and time.time() < deadline:
        print("  {} still running, waiting...".format(len(live)))
        time.sleep(10)
        live, stale = in_flight()

    for execution_id, status, age in stale:
        print("  ignoring #{} ({}, stuck {}m) -- cannot be running any more".format(
            execution_id, status, age // 60))

    if live:
        for execution_id, status, age in live:
            print("  #{} {}, {}s old".format(execution_id, status, age))
        if not args.force:
            fail(
                "restarting now would kill them, and a killed execution cannot\n"
                "       report its own failure, so the request would be stranded with\n"
                "       no Slack message. Re-run with --wait 300, or --force if you\n"
                "       accept losing them.")
        print("  --force given, continuing anyway")
    else:
        print("  none in progress")

    print()
    print("== carry credential assignments across ==")
    rows = psql(
        "select n->>'name' || '\t' || (n->'credentials')::text "
        "from workflow_entity w, jsonb_array_elements(w.nodes::jsonb) n "
        "where w.id='{}' and n->'credentials' is not null;".format(WORKFLOW_ID))
    credentials = {}
    for line in rows.splitlines():
        if "\t" in line:
            name, blob = line.split("\t", 1)
            credentials[name] = json.loads(blob)
    if not credentials:
        fail("no credentials found on the deployed workflow; refusing to import "
             "a version that would have none")
    print("  found on {} node(s): {}".format(
        len(credentials), ", ".join(sorted(credentials))))

    with open(os.path.join(REPO, SOURCE), encoding="utf-8") as handle:
        workflow = json.load(handle)
    names = {node["name"] for node in workflow["nodes"]}
    missing = [name for name in credentials if name not in names]
    if missing:
        fail("these nodes no longer exist in the new workflow, so their "
             "credentials cannot be carried over: " + ", ".join(missing))
    for node in workflow["nodes"]:
        if node["name"] in credentials:
            node["credentials"] = credentials[node["name"]]

    patched_path = os.path.join(REPO, PATCHED)
    with open(patched_path, "w", encoding="utf-8") as handle:
        json.dump(workflow, handle, ensure_ascii=False, indent=2)

    print()
    print("== import ==")
    # The patched copy is scratch, and it lands inside workflows/, which the
    # next run resets with `git reset --hard`. That does not remove untracked
    # files, so anything left behind here stays for good. Removed in a finally
    # rather than after the call, so an exception -- docker missing, the daemon
    # down -- cannot strand it either. fail() raises SystemExit, which still
    # unwinds through this.
    try:
        result = run(["docker", "compose", "run", "--rm", "--entrypoint", "n8n",
                      "n8n-import", "import:workflow",
                      "--input=" + PATCHED_IN_CONTAINER])
        if result.returncode != 0:
            fail("import failed:\n" + (result.stderr or result.stdout)[-600:])
        print("  imported main workflow")
    finally:
        if os.path.exists(patched_path):
            os.remove(patched_path)

    result = run(["docker", "compose", "run", "--rm", "--entrypoint", "n8n",
                  "n8n-import", "import:workflow",
                  "--input=" + ERROR_IN_CONTAINER])
    if result.returncode != 0:
        fail("error handler import failed:\n" + (result.stderr or result.stdout)[-600:])
    print("  imported error handler")

    print()
    print("== reactivate and reload ==")
    for workflow_id in (ERROR_WORKFLOW_ID, WORKFLOW_ID):
        run(["docker", "compose", "run", "--rm", "--entrypoint", "n8n",
             "n8n-import", "update:workflow", "--id=" + workflow_id,
             "--active=true"])
    run(["docker", "compose", "restart", "n8n"])

    for attempt in range(30):
        time.sleep(5)
        container = run(["docker", "compose", "ps", "-q", "n8n"]).stdout.strip()
        health = run(["docker", "inspect", "--format",
                      "{{if .State.Health}}{{.State.Health.Status}}"
                      "{{else}}{{.State.Status}}{{end}}", container]).stdout.strip()
        if health == "healthy":
            print("  n8n healthy after {}s".format((attempt + 1) * 5))
            break
    else:
        fail("n8n did not become healthy")

    time.sleep(8)  # webhook registration lands a moment after the health check
    print()
    print("== verify ==")
    print("  nodes with credentials : " + psql(
        "select count(*) from workflow_entity w, jsonb_array_elements(w.nodes::jsonb) n "
        "where w.id='{}' and n->'credentials' is not null;".format(WORKFLOW_ID)))
    print("  active                 : " + psql(
        "select active from workflow_entity where id='{}';".format(WORKFLOW_ID)))
    print("  error handler active   : " + psql(
        "select active from workflow_entity where id='{}';".format(
            ERROR_WORKFLOW_ID)))
    print("  webhook                : " + (psql(
        'select "webhookPath" from webhook_entity;') or "(none registered)"))

    # A tool node that reaches an authenticated server without a credential does
    # not fail here -- it fails halfway through an investigation, as
    # "Authentication failed", because n8n drops the header silently when it
    # cannot resolve one. The carry-over above only covers nodes the deployed
    # workflow already had, so a newly added MCP node arrives bare and nothing
    # upstream notices. Checked after the import rather than before, because the
    # imported workflow is what actually runs.
    bare = psql(
        "select string_agg(n->>'name', ', ') "
        "from workflow_entity w, jsonb_array_elements(w.nodes::jsonb) n "
        "where w.id='{}' and n->>'type' like '%mcpClientTool%' "
        "and n->'credentials' is null;".format(WORKFLOW_ID))
    if bare:
        fail("MCP tool node(s) deployed without a credential: " + bare +
             "\nAssign the credential in the n8n UI, then run this again -- the "
             "carry-over will keep it from then on.")
    print("  mcp nodes authenticated: yes")


if __name__ == "__main__":
    main()
