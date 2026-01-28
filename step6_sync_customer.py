import argparse
import subprocess
import sys

SANITIZED_BUCKET = 'web-data-platform-sanitized'


def step6_sync_to_customer(customer_bucket, dry_run=False, prefixes=None):
    """Sync voice-calls from sanitized bucket to customer bucket (excludes rejected-calls)"""

    if not customer_bucket:
        print("ERROR: Customer bucket name is required")
        print("Usage: python step6_sync_customer.py --bucket CUSTOMER_BUCKET_NAME")
        sys.exit(1)

    prefixes = prefixes or ['']

    if dry_run:
        print("DRY RUN MODE - No files will be synced\n")

    for prefix in prefixes:
        source = f"s3://{SANITIZED_BUCKET}/{prefix}"
        dest = f"s3://{customer_bucket}/{prefix}"

        print(f"Syncing: {source} -> {dest}")
        print("  (excluding rejected-calls/)\n")

        cmd = [
            'aws', 's3', 'sync',
            source, dest,
            '--exclude', '*',
            '--include', '*/recordings/voice-calls/*',
            '--exclude', '*/recordings/rejected-calls/*',
        ]

        if dry_run:
            cmd.append('--dryrun')

        try:
            result = subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            print(f"ERROR: Sync failed - {e}")
            sys.exit(1)

    print(f"\n{'[DRY RUN] ' if dry_run else ''}Sync complete!")
    if dry_run:
        print("\nRun without --dry-run to sync files")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Sync voice-calls to customer bucket (excludes rejected-calls)')
    parser.add_argument('--bucket', type=str, required=True, help='Customer bucket name')
    parser.add_argument('--dry-run', action='store_true', help='Preview changes without syncing')
    parser.add_argument('--prefix', type=str, nargs='+', default=[], help='Only sync these project prefixes')
    args = parser.parse_args()

    prefixes = args.prefix if args.prefix else None
    step6_sync_to_customer(customer_bucket=args.bucket, dry_run=args.dry_run, prefixes=prefixes)
