# Authelia File Backend Configuration

This directory contains the initial configuration for Authelia using a simple file-based user backend. It is intended for air‑gap deployments where an external identity provider may not be available.

## Files
- `configuration.yml` – Authelia main configuration enabling the file backend.
- `users_database.yml` – Sample users database with an admin account (password hash placeholder).
- `README.md` – This documentation.

## Usage
1. Ensure the `AUTHELIA_SESSION_SECRET` environment variable is set for session security.
2. Mount this directory into the Authelia container at `/config`.
3. After starting the service, verify that the `/auth/` endpoint renders and you can log in with the `admin` user.

## Updating Users
Edit `users_database.yml` and add additional users following the same structure. Re‑hash passwords using Authelia's password hasher (`authelia hash generate`) and restart the container.
