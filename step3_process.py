import json
import os
import argparse
from datetime import datetime

LOCAL_DIR = './downloaded_jsons'

# Fields to keep at participant level
PARTICIPANT_KEEP_FIELDS = ['channel', 'demographics']

# Fields to keep at root level
ROOT_KEEP_FIELDS = ['duration', 'participants', 'topic']


def clean_info_json(data):
    """Clean info.json to only keep allowed fields"""
    # Keep only allowed root fields
    cleaned = {k: v for k, v in data.items() if k in ROOT_KEEP_FIELDS}

    # Clean each participant to only keep allowed fields
    if 'participants' in cleaned:
        cleaned_participants = []
        for participant in cleaned['participants']:
            cleaned_participant = {k: v for k, v in participant.items() if k in PARTICIPANT_KEEP_FIELDS}
            cleaned_participants.append(cleaned_participant)
        cleaned['participants'] = cleaned_participants

    return cleaned


def is_call_passed(data):
    """Check if all participants passed QA review"""
    for participant in data.get('participants', []):
        qa_review = participant.get('QAReview', {})
        if not qa_review.get('isPassed', False):
            return False
    return True


def log_operation(log_file, status, folder, result):
    """Write to processing log"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(log_file, 'a') as f:
        f.write(f"{timestamp} | {status:<10} | {folder} | {result}\n")


def step3_process_jsons(dry_run=False):
    """Process all downloaded JSONs: clean PII and categorize"""
    passed_calls = []
    failed_calls = []
    log_file = os.path.join(LOCAL_DIR, '_processing.log')

    if dry_run:
        print("DRY RUN MODE - No files will be modified\n")

    for root, dirs, files in os.walk(LOCAL_DIR):
        for file in files:
            if file != 'info.json':
                continue

            local_path = os.path.join(root, file)
            s3_key = os.path.relpath(local_path, LOCAL_DIR)
            folder = os.path.dirname(s3_key)

            try:
                # Read JSON
                with open(local_path, 'r') as f:
                    data = json.load(f)

                # Check pass/fail BEFORE cleaning (preserve original isPassed)
                passed = is_call_passed(data)

                # Clean PII
                cleaned_data = clean_info_json(data)

                # Save cleaned version (skip in dry run)
                if not dry_run:
                    with open(local_path, 'w') as f:
                        json.dump(cleaned_data, f, indent=2)

                # Categorize (folder paths already sanitized from Step 1)
                call_info = {
                    's3_key': s3_key,
                    'folder': folder
                }

                if passed:
                    passed_calls.append(call_info)
                    if not dry_run:
                        log_operation(log_file, 'PROCESSED', folder, 'PASSED')
                else:
                    failed_calls.append(call_info)
                    if not dry_run:
                        log_operation(log_file, 'PROCESSED', folder, 'FAILED')

            except Exception as e:
                if not dry_run:
                    log_operation(log_file, 'ERROR', folder, str(e))
                else:
                    print(f"  ERROR: {folder} - {e}")

    # Save manifests (skip in dry run)
    if not dry_run:
        with open(os.path.join(LOCAL_DIR, '_passed_calls.json'), 'w') as f:
            json.dump(passed_calls, f, indent=2)

        with open(os.path.join(LOCAL_DIR, '_failed_calls.json'), 'w') as f:
            json.dump(failed_calls, f, indent=2)

    print(f"{'[DRY RUN] ' if dry_run else ''}Processed {len(passed_calls) + len(failed_calls)} files")
    print(f"   - Passed: {len(passed_calls)}")
    print(f"   - Failed: {len(failed_calls)}")

    if dry_run:
        print("\nRun without --dry-run to apply changes")

    return passed_calls, failed_calls


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Process info.json files: clean PII and categorize')
    parser.add_argument('--dry-run', action='store_true', help='Preview changes without modifying files')
    args = parser.parse_args()

    step3_process_jsons(dry_run=args.dry_run)
