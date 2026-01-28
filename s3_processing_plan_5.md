# S3 Data Processing Plan

## Objective

Process voice call recordings to:
1. Copy data to sanitized bucket with emails removed from paths
2. Clean PII from `info.json` files
3. Separate failed calls from valid calls
4. Sync valid calls to customer's bucket

---

## Scripts

| Script | Purpose |
|--------|---------|
| `step1_copy_sanitized.py` | Copy files to sanitized bucket with emails removed from paths |
| `step3_process.py` | Clean PII from info.json and categorize pass/fail |
| `step5_move_failed.py` | Move failed calls to rejected-calls folder |
| `step6_sync_customer.py` | Sync to customer bucket (excludes rejected-calls) |
| `check_missing_files.py` | Check for incomplete folders (missing info.json, merged.wav, separate/) |
| `delete_incomplete_folders.py` | Delete incomplete folders from sanitized bucket |
| `copy_missing_files.py` | Copy missing files from original to sanitized bucket |

---

## Folder Structure

**Before (original):**
```
web-data-platform/[projectId]/recordings/voice-calls/
└── [date]-[email1]-[email2]-[sessionId]/
    ├── info.json
    ├── merged_xxx.wav
    └── separate/
        ├── [email1]-[sessionId]_audio_xxx.ogg
        └── [email2]-[sessionId]_audio_xxx.ogg
```

**After (sanitized):**
```
web-data-platform-sanitized/[projectId]/recordings/
├── voice-calls/          # Passed calls only
│   └── [date]---[sessionId]/
│       ├── info.json     # PII removed
│       ├── merged_xxx.wav
│       └── separate/
│           └── -[sessionId]_audio_xxx.ogg
└── rejected-calls/       # Failed calls
    └── ...
```

---

## Cleaned info.json Format

Only these fields are kept:

```json
{
  "duration": 902,
  "participants": [
    {
      "channel": "left",
      "demographics": {
        "backgroundInfo": {
          "gender": "Male",
          "dateOfBirth": "2000-05-18T00:00:00.000Z",
          "birthCity": { "city": "Pathankot, IN-PB", "yearsLived": 25 },
          "spokenLanguages": [...],
          "currentOccupation": "Student"
        }
      }
    }
  ],
  "topic": {
    "mainQuestion": "...",
    "category": "..."
  }
}
```

---

## Quick Start

```bash
# Install dependencies
pip install boto3
```

---

## Step 1: Copy to Sanitized Bucket

Copies files with emails removed from paths.

```bash
# Dry run
python3 step1_copy_sanitized.py --dry-run

# Specific projects only
python3 step1_copy_sanitized.py --prefix 692801d5ce882a630401bee8/ 692801d6ce882a630401beee/ --dry-run

# Run for real
python3 step1_copy_sanitized.py --workers 50
```

**Verify:** See [Data Integrity Verification](#utility-scripts-data-integrity-verification) below.

---

## Step 2: Download info.json Files

```bash
aws s3 cp s3://web-data-platform-sanitized/ ./downloaded_jsons/ \
  --recursive \
  --exclude "*" \
  --include "*/voice-calls/*/info.json"
```

---

## Step 3: Process JSONs (Clean PII + Categorize)

```bash
# Dry run
python3 step3_process.py --dry-run

# Run for real
python3 step3_process.py
```

**Output files:**
- `_passed_calls.json` - List of passed calls
- `_failed_calls.json` - List of failed calls
- `_processing.log` - Processing log

---

## Step 4: Upload Cleaned info.json

```bash
aws s3 cp ./downloaded_jsons/ s3://web-data-platform-sanitized/ \
  --recursive \
  --exclude "*" \
  --include "*/info.json" \
  --exclude "_*.json" \
  --exclude "*.log"
```

---

## Step 5: Move Failed Calls

Moves failed calls from `voice-calls/` to `rejected-calls/`.

```bash
# Dry run
python3 step5_move_failed.py --dry-run

# Run for real
python3 step5_move_failed.py --workers 20
```

---

## Step 6: Sync to Customer Bucket

Syncs `voice-calls/` only (excludes `rejected-calls/`).

```bash
# Dry run
python3 step6_sync_customer.py --bucket customer-bucket-name --dry-run

# Specific projects
python3 step6_sync_customer.py --bucket customer-bucket-name \
  --prefix 692801d5ce882a630401bee8/ 692801d6ce882a630401beee/ --dry-run

# Run for real
python3 step6_sync_customer.py --bucket customer-bucket-name
```

---

## Execution Flow

```
Step 1: Copy to sanitized bucket (emails removed from paths)
    ↓
Step 2: Download info.json files
    ↓
Step 3: Process JSONs (clean PII, categorize pass/fail)
    ↓
  Review _passed_calls.json and _failed_calls.json
    ↓
Step 4: Upload cleaned info.json
    ↓
Step 5: Move failed calls to rejected-calls/
    ↓
Step 6: Sync voice-calls/ to customer bucket
```

---

## Utility Scripts: Data Integrity Verification

Two-step verification process after Step 1:

### Step A: Remove folders that are incomplete at source

These folders are missing files in the **original** bucket (legitimately incomplete recordings).

```bash
# 1. Check original bucket for incomplete folders
python3 check_missing_files.py --bucket web-data-platform --output _missing_original.log

# 2. Delete those folders from sanitized bucket (they're incomplete at source)
python3 delete_incomplete_folders.py --log _missing_original.log --dry-run
python3 delete_incomplete_folders.py --log _missing_original.log
```

### Step B: Fix folders with copy failures

These folders are complete in the original bucket but missing files in sanitized bucket due to copy failures.

```bash
# 1. Check sanitized bucket for incomplete folders
python3 check_missing_files.py --bucket web-data-platform-sanitized --output _missing_sanitized.log

# 2. Copy missing files from original bucket
python3 copy_missing_files.py --log _missing_sanitized.log --dry-run
python3 copy_missing_files.py --log _missing_sanitized.log
```

### Verify

```bash
# Should show 0 missing files
python3 check_missing_files.py --bucket web-data-platform-sanitized
```

---

## IAM Permissions

The IAM user needs these permissions on the sanitized bucket:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::web-data-platform-sanitized"
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:GetObjectTagging",
        "s3:PutObject",
        "s3:PutObjectAcl",
        "s3:DeleteObject"
      ],
      "Resource": "arn:aws:s3:::web-data-platform-sanitized/*"
    }
  ]
}
```

---

## Rollback

Original bucket `web-data-platform` is **never modified** - always have a backup.

```bash
# Start over if needed
aws s3 rm s3://web-data-platform-sanitized/ --recursive
# Re-run from Step 1
```
