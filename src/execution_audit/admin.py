"""Trusted user-only policy administration, separate from writer tools."""
import argparse
import json
from pathlib import Path
from uuid import uuid4
from .core import AuditStore


def main():
    parser = argparse.ArgumentParser(description='Explicitly update policy and record the change')
    parser.add_argument('--db', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--request-id', default=None)
    args = parser.parse_args()
    if not Path(args.db).is_file():
        parser.error('database does not exist; initialize it first')
    store = AuditStore(args.db)
    try:
        result = store.update_policy(config=json.loads(Path(args.config).read_text()), request_id=args.request_id or str(uuid4()))
        print(json.dumps(result))
    finally:
        store.close()


if __name__ == '__main__':
    main()
