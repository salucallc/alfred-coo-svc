# BYO OIDC Integration

This document describes how to add a Bring-Your-Own OpenID Connect (OIDC) identity provider to Authelia.

## Steps

1. Copy `deploy/appliance/authelia/oidc_clients_template.yml` to a new file, e.g. `oidc_clients_myprovider.yml`.
2. Edit the file:
   - Replace `<PROVIDER>` with a friendly name.
   - Fill `client_id`, `client_secret`, and `redirect_uris` with values from your IdP.
3. Place the edited file under `deploy/appliance/authelia/` (or include it via the main `configuration.yml` `oidc` section).
4. Run the helper script:

   ```sh
   ./mc.sh auth add-oidc <provider> --client-id <client-id>
   ```

   This will merge the client definition into Authelia's configuration.
5. Restart Authelia or reload the Docker compose stack.

## Verification

After adding the client, navigate to `https://<your-appliance>/auth` and select the new provider.
Successful login should redirect back to the appliance dashboard.

## Risks

- Incorrect `redirect_uris` may cause login failure.
- Storing client secrets in plaintext is insecure; consider using vault integration.
