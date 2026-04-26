# MC-Init Scripts

These scripts are executed on first boot of the appliance container.

## mint_soulkeys.sh

* Idempotently generates six soulkeys (one per internal service) and stores them under `/state/secrets/`.
* Inserts corresponding rows into the `_soulkeys` table if they do not already exist.

## register_allowlist.sh

* Registers the minted soulkeys for the COO service in the `_soulkey_allowlist` table.
* Ensures at least four entries exist, satisfying the acceptance criteria for SAL-2594.

Both scripts are designed to be safe to run multiple times.
