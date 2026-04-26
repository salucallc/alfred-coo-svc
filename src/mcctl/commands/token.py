import argparse

def main():
    parser = argparse.ArgumentParser(prog='mcctl token create', description='Create a one‑shot token')
    parser.add_argument('--site', required=True, help='Site code')
    parser.add_argument('--ttl', required=True, help='Time‑to‑live, e.g. 15m')
    parser.add_argument('--tenant', required=False, help='Tenant identifier (optional)')
    args = parser.parse_args()
    # Placeholder implementation – in real code this would call the backend service
    token_info = f"site={args.site} ttl={args.ttl}"
    if args.tenant:
        token_info += f" tenant={args.tenant}"
    print(f"Generated token for {token_info}")
