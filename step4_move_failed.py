import json
import os
import argparse
import subprocess
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

SOURCE_BUCKET = 'web-data-platform'
LOCAL_DIR = './downloaded_jsons'
MAX_WORKERS = 20  # Parallel threads


def log_operation(log_file, status, folder, result):
    """Write to processing log"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(log_file, 'a') as f:
        f.write(f"{timestamp} | {status:<10} | {folder} | {result}\n")


def move_single_folder(call, dry_run, log_file):
    """Move a single folder from voice-calls to rejected-calls"""
    old_folder = call['folder']
    new_folder = old_folder.replace('/voice-calls/', '/rejected-calls/')

    old_s3_path = f"s3://{SOURCE_BUCKET}/{old_folder}/"
    new_s3_path = f"s3://{SOURCE_BUCKET}/{new_folder}/"

    try:
        if dry_run:
            print(f"  Would move: {old_folder}")
            return True

        # Use aws s3 sync + rm (faster than mv for folders)
        sync_cmd = ['aws', 's3', 'sync', old_s3_path, new_s3_path, '--quiet']
        subprocess.run(sync_cmd, check=True, capture_output=True)

        rm_cmd = ['aws', 's3', 'rm', old_s3_path, '--recursive', '--quiet']
        subprocess.run(rm_cmd, check=True, capture_output=True)

        log_operation(log_file, 'MOVED', old_folder, 'rejected-calls')
        return True

    except subprocess.CalledProcessError as e:
        log_operation(log_file, 'MOVE_ERR', old_folder, str(e))
        print(f"  ERROR: {old_folder} - {e}")
        return False


def step4_move_failed_calls(dry_run=False, max_workers=20):
    """Move failed calls from voice-calls/ to rejected-calls/ using parallel processing"""
    log_file = os.path.join(LOCAL_DIR, '_processing.log')

    with open(os.path.join(LOCAL_DIR, '_failed_calls.json'), 'r') as f:
        failed_calls = json.load(f)

    if dry_run:
        print("DRY RUN MODE - No files will be moved\n")

    total = len(failed_calls)
    print(f"{'[DRY RUN] ' if dry_run else ''}Moving {total} failed calls with {max_workers} parallel workers...")

    moved_count = 0
    error_count = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(move_single_folder, call, dry_run, log_file): call
            for call in failed_calls
        }

        for future in as_completed(futures):
            if future.result():
                moved_count += 1
            else:
                error_count += 1

            if (moved_count + error_count) % 100 == 0:
                print(f"  Progress: {moved_count + error_count}/{total} ({moved_count} success, {error_count} errors)")

    print(f"\n{'[DRY RUN] ' if dry_run else ''}{'Would move' if dry_run else 'Moved'} {moved_count} failed calls")
    if error_count > 0:
        print(f"WARNING: {error_count} errors - check _processing.log")

    if dry_run:
        print("\nRun without --dry-run to apply changes")

    return moved_count


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Move failed calls to rejected-calls folder')
    parser.add_argument('--dry-run', action='store_true', help='Preview changes without moving files')
    parser.add_argument('--workers', type=int, default=20, help='Number of parallel workers (default: 20)')
    args = parser.parse_args()

    step4_move_failed_calls(dry_run=args.dry_run, max_workers=args.workers)
