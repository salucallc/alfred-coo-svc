import argparse

def main():
    parser = argparse.ArgumentParser(prog='mcctl token create', description='Create a one‑shot token')
    parser.add_argument('--site', required=True, help='Site code')
    parser.add_argument('--ttl', required=True, help='Time‑to‑live, e.g. 15m')
    parser.add_argument('--tenant', required=False, help='Tenant ID (optional)')
    args = parser.parse_args()
    # Placeholder implementation – in real code this would call the backend service
    tenant_part = f" for tenant {args.tenant}" if args.tenant else ''
    print(f"Generated token for site {args.site}{tenant_part} with ttl {args.ttl}")
