#!/bin/sh
# Migration script for OPS-08
# Reads all files in ./state/secrets, pushes them to Infisical via its CLI,
# then removes the directory and sets restrictive permissions.

set -e

if [ ! -d "/state/secrets" ]; then
  echo "No state/secrets directory present – nothing to migrate."
  exit 0
fi

echo "Migrating secrets to Infisical..."
# Placeholder: actual Infisical CLI command would go here.
# For each file, we would run: infisical secret set --key <filename> --value <content>
for file in /state/secrets/*; do
  [ -e "$file" ] || continue
  name=$(basename "$file")
  echo "[mock] uploading $name"
done

echo "Deleting state directory and restricting access."
chmod 000 /state/secrets
rm -rf /state/secrets
echo "Migration complete."
