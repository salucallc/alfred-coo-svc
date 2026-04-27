import click
from mcctl.client import MCClient

@click.group()
def policy():
    """Policy related commands."""
    pass

@policy.command(name="push")
@click.option("--immediate", is_flag=True, help="Push policy immediately, interrupting endpoints.")
def push(immediate: bool):
    """Push a policy to the fleet.

    If `--immediate` is provided, endpoints will be interrupted within 5 seconds and any in‑flight tasks will be re‑queued with `requeue_reason=policy_immediate`.
    """
    client = MCClient()
    # The internal API expects a JSON payload; for now we forward the flag.
    payload = {"immediate": immediate}
    response = client.post_json("/v1/fleet/policy/push", json=payload)
    if response.status_code == 200:
        click.echo("Policy push successful.")
    else:
        click.echo(f"Policy push failed: {response.text}", err=True)

# Expose the group to the top‑level command registry (if used).
# In src/mcctl/commands/__init__.py, the group is imported elsewhere.
