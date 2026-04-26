import sys
from .commands import token

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "token":
        token.main()
    else:
        print("mcctl: unknown command")

if __name__ == "__main__":
    main()
