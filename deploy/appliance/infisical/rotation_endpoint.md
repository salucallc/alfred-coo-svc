# Infisical Rotation Endpoint

The Infisical service exposes a rotation endpoint:

```
POST /api/v3/secrets/:id/rotate
```

When called, Infisical generates a new secret value. Agents poll for secret updates every 60 seconds; if the secret's `requires_restart` flag is true, the owning service restarts automatically, picking up the new value within ~90 seconds.

## Usage

```sh
curl -X POST http://<infisical-host>/api/v3/secrets/<secret-id>/rotate
```

Response returns the new secret value.

## Verification

After rotation, ensure the secret value changes and dependent services restart within 90 seconds.
