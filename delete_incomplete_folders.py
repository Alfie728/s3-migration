import re
import argparse
import subprocess

SANITIZED_BUCKET = 'web-data-platform-sanitized'


def sanitize_path(path):
    """Remove emails from paths (same logic as step1)"""
    email_pattern = r'[a-zA-Z][a-zA-Z0-9._%+-]*@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    return re.sub(email_pattern, '', path)


def parse_missing_files_log(log_file):
    """Parse _missing_files.log and extract folder paths"""
    folders = set()

    with open(log_file, 'r') as f:
        for line in f:
            line = line.strip()
            # Skip headers and empty lines
            if not line or line.startswith('===') or line.startswith('Total'):
                continue
            # Each folder path contains /voice-calls/
            if '/voice-calls/' in line:
                folders.add(line)

    return folders


def delete_folders(folders, dry_run=False):
    """Delete folders from sanitized bucket"""
    if dry_run:
        print("DRY RUN MODE - No files will be deleted\n")

    total = len(folders)
    print(f"{'[DRY RUN] ' if dry_run else ''}Deleting {total} incomplete folders from {SANITIZED_BUCKET}...\n")

    deleted = 0
    errors = 0

    for folder in sorted(folders):
        # Convert to sanitized path
        sanitized_folder = sanitize_path(folder)
        s3_path = f"s3://{SANITIZED_BUCKET}/{sanitized_folder}/"

        if dry_run:
            print(f"  Would delete: {sanitized_folder}")
            deleted += 1
            continue

        try:
            cmd = ['aws', 's3', 'rm', s3_path, '--recursive', '--quiet']
            subprocess.run(cmd, check=True, capture_output=True)
            deleted += 1

            if deleted % 50 == 0:
                print(f"  Progress: {deleted}/{total}")

        except subprocess.CalledProcessError as e:
            print(f"  ERROR: {sanitized_folder} - {e}")
            errors += 1

    print(f"\n{'[DRY RUN] ' if dry_run else ''}{'Would delete' if dry_run else 'Deleted'} {deleted} folders")
    if errors > 0:
        print(f"WARNING: {errors} errors occurred")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Delete incomplete folders from sanitized bucket')
    parser.add_argument('--log', type=str, default='_missing_files.log', help='Missing files log to read')
    parser.add_argument('--dry-run', action='store_true', help='Preview changes without deleting')
    args = parser.parse_args()

    folders = parse_missing_files_log(args.log)
    print(f"Found {len(folders)} folders to delete from log\n")

    delete_folders(folders, dry_run=args.dry_run)
