import json
import os
import re
import argparse
import subprocess
import tempfile
import shutil
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

SOURCE_BUCKET = 'web-data-platform'
CUSTOMER_BUCKET = 'customer-bucket'  # TODO: Update with actual bucket name
LOCAL_DIR = './downloaded_jsons'
MAX_WORKERS = 20  # Parallel threads


def sanitize_path(path):
    """Remove emails from paths (replace with empty string)"""
    email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    return re.sub(email_pattern, '', path)


def log_operation(log_file, status, folder, result):
    """Write to processing log"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(log_file, 'a') as f:
        f.write(f"{timestamp} | {status:<10} | {folder} | {result}\n")


def upload_single_folder(call, dry_run, log_file, max_workers):
    """Upload a single folder to customer bucket with sanitized paths"""
    old_folder = call['folder']
    new_folder = call['sanitized_folder']

    old_s3_path = f"s3://{SOURCE_BUCKET}/{old_folder}/"
    new_s3_path = f"s3://{CUSTOMER_BUCKET}/{new_folder}/"

    try:
        if dry_run:
            print(f"  Would upload: {old_folder} -> {new_folder}")
            return True

        # Create temp directory for this folder
        with tempfile.TemporaryDirectory() as temp_dir:
            # Download folder from source
            download_cmd = ['aws', 's3', 'sync', old_s3_path, temp_dir, '--quiet']
            subprocess.run(download_cmd, check=True, capture_output=True)

            # Rename files locally to sanitize emails from filenames
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    old_file_path = os.path.join(root, file)
                    new_file_name = sanitize_path(file)
                    new_file_path = os.path.join(root, new_file_name)

                    if old_file_path != new_file_path:
                        shutil.move(old_file_path, new_file_path)

                    # Replace info.json with cleaned local version
                    if file == 'info.json':
                        local_info_path = os.path.join(LOCAL_DIR, old_folder, 'info.json')
                        if os.path.exists(local_info_path):
                            shutil.copy(local_info_path, new_file_path)

            # Upload to customer bucket with sanitized folder name
            upload_cmd = ['aws', 's3', 'sync', temp_dir, new_s3_path, '--quiet']
            subprocess.run(upload_cmd, check=True, capture_output=True)

            log_operation(log_file, 'CUSTOMER', new_folder, 'SUCCESS')

        return True

    except subprocess.CalledProcessError as e:
        log_operation(log_file, 'CUST_ERR', old_folder, str(e))
        print(f"  ERROR: {old_folder} - {e}")
        return False


def step5_upload_to_customer(dry_run=False, max_workers=20):
    """Upload valid calls to customer bucket with sanitized paths using parallel processing"""
    log_file = os.path.join(LOCAL_DIR, '_processing.log')

    with open(os.path.join(LOCAL_DIR, '_passed_calls.json'), 'r') as f:
        passed_calls = json.load(f)

    if dry_run:
        print("DRY RUN MODE - No files will be uploaded\n")

    total = len(passed_calls)
    print(f"{'[DRY RUN] ' if dry_run else ''}Uploading {total} valid calls to {CUSTOMER_BUCKET} with {max_workers} parallel workers...")

    uploaded_count = 0
    error_count = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(upload_single_folder, call, dry_run, log_file, max_workers): call
            for call in passed_calls
        }

        for future in as_completed(futures):
            if future.result():
                uploaded_count += 1
            else:
                error_count += 1

            if (uploaded_count + error_count) % 100 == 0:
                print(f"  Progress: {uploaded_count + error_count}/{total} ({uploaded_count} success, {error_count} errors)")

    print(f"\n{'[DRY RUN] ' if dry_run else ''}{'Would upload' if dry_run else 'Uploaded'} {uploaded_count} valid calls to {CUSTOMER_BUCKET}")
    if error_count > 0:
        print(f"WARNING: {error_count} errors - check _processing.log")

    if dry_run:
        print("\nRun without --dry-run to apply changes")

    return uploaded_count


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Upload valid calls to customer bucket')
    parser.add_argument('--dry-run', action='store_true', help='Preview changes without uploading files')
    parser.add_argument('--workers', type=int, default=20, help='Number of parallel workers (default: 20)')
    args = parser.parse_args()

    step5_upload_to_customer(dry_run=args.dry_run, max_workers=args.workers)
