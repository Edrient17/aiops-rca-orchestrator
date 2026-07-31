#!/usr/bin/env python3
"""Redeploys the main n8n workflow on the orchestrator host.

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
import subprocess
import sys
import time

WORKFLOW_ID = "aiops-main-v010"
SOURCE = "workflows/01-aiops-main.json"
PATCHED = "workflows/.redeploy-with-credentials.json"
PATCHED_IN_CONTAINER = "/opt/aiops/workflows/.redeploy-with-credentials.json"
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


# The workflow's own executionTimeout is the ceiling on how long a live run can
# last. Anything older is debris -- a crashed n8n leaves rows in `new` that never
# start, and those must not block deploys forever.
LIVE_WINDOW_SECONDS = 900


def in_flight():
    """Splits stuck executions into ones that could still be running and debris."""
    rows = psql(
        "select id || '|' || status || '|' || "
        "round(extract(epoch from (now() - \"createdAt\"))) "
        "from execution_entity where status in ('running','new','waiting') "
        "order by id;")
    live, stale = [], []
    for line in rows.splitlines():
        if line.count("|") != 2:
            continue
        execution_id, status, age = line.split("|")
        entry = (execution_id, status, int(float(age)))
        (live if entry[2] < LIVE_WINDOW_SECONDS else stale).append(entry)
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
    run(["git", "reset", "--hard", "origin/main", "--quiet"])
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
        print("  ignoring #{} ({}, stuck {}m) -- older than a run can last".format(
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
    result = run(["docker", "compose", "run", "--rm", "--entrypoint", "n8n",
                  "n8n-import", "import:workflow",
                  "--input=" + PATCHED_IN_CONTAINER])
    os.remove(patched_path)
    if result.returncode != 0:
        fail("import failed:\n" + (result.stderr or result.stdout)[-600:])
    print("  imported")

    print()
    print("== reactivate and reload ==")
    run(["docker", "compose", "run", "--rm", "--entrypoint", "n8n", "n8n-import",
         "update:workflow", "--id=" + WORKFLOW_ID, "--active=true"])
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
    print("  webhook                : " + (psql(
        'select "webhookPath" from webhook_entity;') or "(none registered)"))


if __name__ == "__main__":
    main()
