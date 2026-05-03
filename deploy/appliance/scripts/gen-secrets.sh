#!/bin/bash

# Generate secure secrets for appliance setup.

import secrets

print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(32))
print('SOUL_API_KEY_ROOT=sk_soul_root_' + secrets.token_urlsafe(32))
