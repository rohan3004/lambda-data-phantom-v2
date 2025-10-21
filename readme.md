<img src="https://r2cdn.perplexity.ai/pplx-full-logo-primary-dark%402x.png" style="height:64px;margin-right:32px"/>

# Complete Documentation: AWS Lambda S3 Scraper for Competitive Programming Statistics

## Project Overview

**Purpose:** Automatically process compressed HTML files (.gz) from competitive programming platforms (LeetCode, CodeChef, Codeforces, GeeksForGeeks) stored in S3, parse them, and generate a unified JSON summary report.

**Architecture:**

- **Trigger:** S3 PUT event when .gz files are uploaded
- **Processing:** Lambda function downloads, decompresses, and parses HTML using BeautifulSoup4
- **Output:** summary.json file containing aggregated statistics from all platforms

**S3 Bucket Structure:**

```
rohandev-digital-apigateway/
└── {report_id}/
    ├── raw/
    │   ├── codechef.gz
    │   ├── codeforces.gz
    │   ├── geeksforgeeks.gz
    │   └── leetcode.gz
    └── summary.json          ← Generated output
```


***

## Prerequisites

### Software Requirements

- **Python 3.9 or higher** installed on your machine
- **AWS CLI** (optional but recommended) - Install from https://aws.amazon.com/cli/
- **7-Zip** (for Windows deployment) - Download from https://www.7-zip.org/
- **AWS Account** with appropriate permissions


### AWS Services Required

- AWS Lambda
- Amazon S3
- AWS IAM
- Amazon CloudWatch (for logs)

***

## Complete Lambda Function Code

Save this as `lambda_function.py`:

```python
import boto3
import gzip
import json
import os
import re
import logging
from datetime import datetime, timedelta
from urllib.parse import unquote_plus
from bs4 import BeautifulSoup
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS S3 Client
s3_client = boto3.client('s3')


# ==================== UTILITY FUNCTIONS ====================

def clean_value(value: str):
    """Clean and convert string values to integers."""
    if not isinstance(value, str) or value in ['__', '?']:
        return None
    cleaned_string = re.sub(r'[^\d-]', '', value)
    if cleaned_string:
        return int(cleaned_string)
    return None


# ==================== PARSER FUNCTIONS ====================

def parse_leetcode_stats(html_content: str) -> dict:
    """Parse LeetCode profile HTML into structured stats."""
    if not html_content:
        return {"status": "error", "message": "Missing HTML content for LeetCode."}

    soup = BeautifulSoup(html_content, "html.parser")
    stats = {"source": "LeetCode", "status": "success", "platform_specific": {}}

    try:
        # Rating
        rating_div = soup.select_one("div.text-label-1.dark\\:text-dark-label-1.flex.items-center.text-2xl")
        if rating_div:
            stats["rating"] = clean_value(rating_div.text.strip())

        # Global Rank
        ranking_div = soup.select_one("div.text-label-1.dark\\:text-dark-label-1.font-medium.leading-\\[22px\\]")
        if ranking_div:
            stats["rank_global"] = clean_value(ranking_div.contents[^0].strip())

        # Top Percentage
        top_div = soup.select_one("div.absolute.left-0.top-0 div.text-label-1.dark\\:text-dark-label-1.text-2xl")
        if top_div:
            stats["platform_specific"]["top_percentage"] = top_div.text.strip()

        # Contests Attended
        attended_div = soup.select_one("div.hidden.md\\:block div.text-label-1.dark\\:text-dark-label-1.font-medium")
        if attended_div:
            stats["contests_attended"] = clean_value(attended_div.text.strip())

        # Badges
        badge_imgs = soup.select("image[xlink\\:href*='/static/images/badges/'], img[src*='/static/images/badges/']")
        stats["platform_specific"]["badges"] = len(badge_imgs) - 1 if badge_imgs else 0

        # Problems Solved by Difficulty
        solved_counts_div = soup.select(".flex.h-full.w-\\[90px\\].flex-none.flex-col.gap-2")
        if solved_counts_div:
            difficulties = solved_counts_div[^0].find_all("div", recursive=False)
            if len(difficulties) == 3:
                easy = clean_value(difficulties[^0].find_all("div")[^1].text.split("/")[^0].strip())
                medium = clean_value(difficulties[^1].find_all("div")[^1].text.split("/")[^0].strip())
                hard = clean_value(difficulties[^2].find_all("div")[^1].text.split("/")[^0].strip())
                stats["problems_solved_easy"] = easy
                stats["problems_solved_medium"] = medium
                stats["problems_solved_hard"] = hard
                stats["problems_solved_total"] = (easy or 0) + (medium or 0) + (hard or 0)

        # Submissions and Acceptance Rate
        progress_chart = soup.select_one(".relative.aspect-\\[1\\/1\\]")
        if progress_chart:
            submission_text = progress_chart.find("div", string=lambda t: t and "submission" in t.lower())
            if submission_text and submission_text.find_previous_sibling("span"):
                stats["platform_specific"]["total_submissions"] = clean_value(submission_text.find_previous_sibling("span").text.strip())

            acceptance_text = progress_chart.find("div", string=lambda t: t and "Acceptance" in t)
            if acceptance_text and acceptance_text.find_previous_sibling("div"):
                stats["platform_specific"]["acceptance_rate"] = acceptance_text.find_previous_sibling("div").text.strip()

        # Activity Stats
        activity_section = soup.find("div", class_="lc-md:flex-row")
        if activity_section:
            active_days_span = activity_section.find("span", string="Total active days:")
            if active_days_span and active_days_span.next_sibling:
                stats["platform_specific"]["total_active_days"] = clean_value(active_days_span.next_sibling.text.strip())

            max_streak_span = activity_section.find("span", string="Max streak:")
            if max_streak_span and max_streak_span.next_sibling:
                stats["streak_max"] = clean_value(max_streak_span.next_sibling.text.strip())

        # Streak Calculation from Heatmap
        svg = soup.select_one("div.lc-md\\:flex.hidden.h-auto.w-full.flex-1.items-center.justify-center svg") or soup.select_one("svg")
        current_streak = 0
        max_streak_calc = 0
        
        if svg:
            date_rects = svg.select("g.month g.week rect[data-date]")
            if date_rects:
                submission_map = {r.get("data-date"): int(r.get("data-count", "0")) for r in date_rects if r.get("data-date")}
                if submission_map:
                    dates = sorted(submission_map.keys(), key=lambda d: datetime.fromisoformat(d).date())
                    streak = 0
                    for d in dates:
                        streak = streak + 1 if submission_map.get(d, 0) > 0 else 0
                        if streak > max_streak_calc:
                            max_streak_calc = streak
                    
                    curr = datetime.fromisoformat(dates[-1]).date()
                    while curr.isoformat() in submission_map and submission_map[curr.isoformat()] > 0:
                        current_streak += 1
                        curr -= timedelta(days=1)
            else:
                rects = svg.select("g.month g.week rect.cursor-pointer")
                days_active = [(r.get("fill") or "").strip().startswith("var(--green") for r in rects]
                streak = 0
                for a in days_active:
                    streak = streak + 1 if a else 0
                    if streak > max_streak_calc:
                        max_streak_calc = streak
                for a in reversed(days_active):
                    if not a: break
                    current_streak += 1

        stats["streak_current"] = current_streak
        if "streak_max" not in stats:
            stats["streak_max"] = max_streak_calc

    except Exception as e:
        stats["status"] = "error"
        stats["message"] = f"Failed to parse LeetCode HTML: {str(e)}"

    return stats


def parse_codechef_stats(html_content: str) -> dict:
    """Parse CodeChef profile HTML into structured stats."""
    if not html_content:
        return {"status": "error", "message": "Missing HTML content for CodeChef."}
    
    soup = BeautifulSoup(html_content, "html.parser")
    stats = {"source": "CodeChef", "status": "success", "platform_specific": {}}
    
    try:
        # Contest Rank Stars
        contest_rank_span = soup.select_one('.user-details-container .rating')
        if contest_rank_span:
            stats['platform_specific']['contest_rank_stars'] = contest_rank_span.text.strip().replace('★', '')

        # Contests Attended
        contest_count_b = soup.select_one('.contest-participated-count b')
        if contest_count_b:
            stats['contests_attended'] = clean_value(contest_count_b.text.strip())

        # Problems Solved
        problems_solved_h3 = soup.find('h3', string=lambda t: 'Total Problems Solved' in t if t else False)
        if problems_solved_h3:
            stats['problems_solved_total'] = clean_value(problems_solved_h3.text.strip().split(':')[-1].strip())

        # Rating and Division
        rating_header = soup.select_one('.rating-header')
        if rating_header:
            rating_div = rating_header.select_one('.rating-number')
            if rating_div and rating_div.contents:
                rating_text = rating_div.contents[^0].strip()
                stats['rating'] = clean_value(rating_text)
            
            division_div = rating_header.find('div', string=lambda t: '(Div' in t if t else False)
            if division_div:
                stats['platform_specific']['division'] = division_div.text.strip().replace('(', '').replace(')', '')

        # Ranks
        rating_ranks_ul = soup.select_one('.rating-ranks ul')
        if rating_ranks_ul:
            for li in rating_ranks_ul.find_all('li'):
                rank_value_tag = li.find('strong')
                if rank_value_tag:
                    rank_value = clean_value(rank_value_tag.text.strip())
                    li_text = li.text.strip()
                    if 'Global Rank' in li_text:
                        stats['rank_global'] = rank_value
                    elif 'Country Rank' in li_text:
                        stats['rank_country'] = rank_value

    except Exception as e:
        stats["status"] = "error"
        stats["message"] = f"Failed to parse CodeChef HTML: {str(e)}"
    
    return stats


def parse_codeforces_stats(html_content: str) -> dict:
    """Parse Codeforces profile HTML into structured stats."""
    if not html_content:
        return {"status": "error", "message": "Missing HTML content for Codeforces."}
    
    soup = BeautifulSoup(html_content, "html.parser")
    stats = {"source": "Codeforces", "status": "success", "platform_specific": {}}
    
    try:
        # User Info
        info_div = soup.find('div', class_='info')
        if info_div:
            for li in info_div.find_all('li'):
                li_text = li.text.strip()
                
                if "Contest rating:" in li_text:
                    rating_span = li.find('span', class_='user-gray')
                    stats['rating'] = clean_value(rating_span.text.strip())
                    
                    max_rating_span = li.find('span', class_='smaller')
                    if max_rating_span:
                        max_rank = max_rating_span.find('span', class_='user-gray')
                        max_rating_value = max_rank.find_next_sibling('span')
                        stats['platform_specific']['max_rank'] = max_rank.text.strip().replace(',', '') if max_rank else None
                        stats['rating_max'] = clean_value(max_rating_value.text.strip())
                
                elif "Contribution:" in li_text:
                    contribution_span = li.find('span')
                    stats['platform_specific']['contribution'] = clean_value(contribution_span.text.strip())
        
        # Activity Stats
        activity_footer = soup.find('div', class_='_UserActivityFrame_footer')
        if activity_footer:
            counters = activity_footer.find_all('div', class_='_UserActivityFrame_counter')
            for counter in counters:
                value_div = counter.find('div', class_='_UserActivityFrame_counterValue')
                description_div = counter.find('div', class_='_UserActivityFrame_counterDescription')
                if value_div and description_div:
                    value = clean_value(value_div.text)
                    key_text = description_div.text.strip()
                    if "solved for all time" in key_text:
                        stats['problems_solved_total'] = value
                    elif "in a row max" in key_text:
                        stats['streak_max'] = value

    except Exception as e:
        stats["status"] = "error"
        stats["message"] = f"Failed to parse Codeforces HTML: {str(e)}"
    
    return stats


def parse_geeksforgeeks_stats(html_content: str) -> dict:
    """Parse GeeksForGeeks profile HTML into structured stats."""
    if not html_content:
        return {"status": "error", "message": "Missing HTML content for GeeksForGeeks."}
    
    soup = BeautifulSoup(html_content, "html.parser")
    stats = {"source": "GeeksForGeeks", "status": "success"}
    
    try:
        # Current Streak
        streak_div = soup.select_one('.circularProgressBar_head_mid_streakCnt__MFOF1')
        if streak_div:
            stats['streak_current'] = clean_value(streak_div.contents[^0].strip())

        # Score Cards
        score_cards = soup.select('.scoreCard_head__nxXR8')
        if len(score_cards) >= 3:
            total_problems_div = score_cards[^1].select_one('.scoreCard_head_left--score__oSi_x')
            if total_problems_div:
                stats['problems_solved_total'] = clean_value(total_problems_div.text.strip())
            
            contest_rating_div = score_cards[^2].select_one('.scoreCard_head_left--score__oSi_x')
            if contest_rating_div:
                stats['rating'] = clean_value(contest_rating_div.text.strip())

        # Problems by Difficulty
        problem_nav = soup.select('.problemNavbar_head_nav__a4K6P')
        for item in problem_nav:
            text = item.text.strip()
            match = re.search(r'([A-Z]+)\s*\((\d+)\)', text)
            if match:
                difficulty = match.group(1).lower()
                count = clean_value(match.group(2))
                stats[f'problems_solved_{difficulty}'] = count

    except Exception as e:
        stats["status"] = "error"
        stats["message"] = f"Failed to parse GeeksForGeeks HTML: {str(e)}"
        
    return stats


# Parser Configuration
PROFILES_CONFIG = {
    "codechef": {"parser": parse_codechef_stats},
    "codeforces": {"parser": parse_codeforces_stats},
    "geeksforgeeks": {"parser": parse_geeksforgeeks_stats},
    "leetcode": {"parser": parse_leetcode_stats}
}


# ==================== LAMBDA HANDLER ====================

def lambda_handler(event, context):
    """
    Main Lambda handler function.
    Triggered by S3 PUT events for .gz files.
    Processes all .gz files in report folder and generates summary.json.
    """
    try:
        logger.info(f"Lambda invoked with event: {json.dumps(event)}")
        
        # Extract S3 event information
        if 'Records' not in event or len(event['Records']) == 0:
            raise ValueError("No S3 records found in event")
            
        record = event['Records'][^0]['s3']
        bucket_name = record['bucket']['name']
        triggering_key = unquote_plus(record['object']['key'])
        
        logger.info(f"Processing - Bucket: {bucket_name}, Key: {triggering_key}")
        
        # Extract report_id (parent directory)
        # Example: "mixed_checktest/raw/codechef.gz" -> "mixed_checktest"
        full_path = os.path.dirname(triggering_key)  # "mixed_checktest/raw"
        report_id = os.path.dirname(full_path)        # "mixed_checktest"
        
        if not report_id:
            logger.error(f"Could not determine report_id from key: {triggering_key}")
            return {'statusCode': 400, 'body': 'Invalid object key structure'}
        
        logger.info(f"Report ID: {report_id}")
        aggregated_stats = {}
        
        # List all .gz files in the raw/ subfolder
        raw_folder = f"{report_id}/raw/"
        response = s3_client.list_objects_v2(Bucket=bucket_name, Prefix=raw_folder)
        
        if 'Contents' not in response:
            logger.warning(f"No objects found for prefix: {raw_folder}")
            return {'statusCode': 200, 'body': 'No files to process'}
        
        gz_files = [obj['Key'] for obj in response.get('Contents', []) 
                   if obj['Key'].endswith('.gz')]
        
        logger.info(f"Found {len(gz_files)} .gz files: {gz_files}")
        
        # Process each platform file
        for key in gz_files:
            platform = os.path.basename(key).replace('.gz', '')
            
            if platform not in PROFILES_CONFIG:
                logger.warning(f"Skipping unknown platform: {platform}")
                continue
            
            logger.info(f"Processing platform: {platform}")
            
            try:
                # Use unique temp file names to avoid conflicts
                temp_gz_path = f"/tmp/{report_id.replace('/', '_')}_{os.path.basename(key)}"
                
                # Download and decompress
                s3_client.download_file(bucket_name, key, temp_gz_path)
                logger.info(f"Downloaded {key} to {temp_gz_path}")
                
                with gzip.open(temp_gz_path, 'rt', encoding='utf-8') as f:
                    html_content = f.read()
                
                logger.info(f"Decompressed {platform}, content length: {len(html_content)}")
                
                # Parse content using platform-specific parser
                parser_func = PROFILES_CONFIG[platform]['parser']
                aggregated_stats[platform] = parser_func(html_content)
                
                logger.info(f"Parsed {platform} successfully")
                
                # Clean up temp file
                os.unlink(temp_gz_path)
                
            except Exception as e:
                logger.error(f"Error processing {platform}: {e}", exc_info=True)
                aggregated_stats[platform] = {
                    "status": "error",
                    "message": str(e)
                }
        
        # Upload summary.json to parent directory (one level up from raw/)
        if aggregated_stats:
            summary_key = f"{report_id}/summary.json"
            summary_content = json.dumps(aggregated_stats, indent=4)
            
            logger.info(f"=== UPLOAD START ===")
            logger.info(f"Uploading summary to: {summary_key}")
            
            try:
                # Upload to S3
                put_response = s3_client.put_object(
                    Bucket=bucket_name,
                    Key=summary_key,
                    Body=summary_content.encode('utf-8'),
                    ContentType='application/json'
                )
                logger.info(f"PutObject response ETag: {put_response.get('ETag')}")
                
                # Verify upload
                head_response = s3_client.head_object(Bucket=bucket_name, Key=summary_key)
                logger.info(f"✓ VERIFIED: File exists, size={head_response['ContentLength']}")
                
            except ClientError as e:
                logger.error(f"❌ S3 ERROR: {e.response['Error']['Code']}")
                logger.error(f"Message: {e.response['Error']['Message']}")
                raise
            
            logger.info(f"=== UPLOAD SUCCESS ===")
            logger.info(f"Summary content:\n{summary_content}")
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Report processing complete',
                'report_id': report_id,
                'files_processed': len(gz_files),
                'summary_key': f"{report_id}/summary.json"
            })
        }
        
    except Exception as e:
        logger.error(f"Lambda execution failed: {e}", exc_info=True)
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
        }
    finally:
        # Cleanup /tmp directory
        try:
            for file in os.listdir('/tmp'):
                file_path = os.path.join('/tmp', file)
                if os.path.isfile(file_path):
                    os.unlink(file_path)
        except Exception as cleanup_error:
            logger.warning(f"Cleanup error: {cleanup_error}")
```


***

## Deployment Steps (Windows)

### Step 1: Set Up Project Directory

```powershell
# Create project directory
mkdir C:\lambda-scraper
cd C:\lambda-scraper

# Save lambda_function.py in this directory
```


### Step 2: Install Dependencies

```powershell
# Create package directory
mkdir package
cd package

# Install BeautifulSoup4 for Lambda (Linux x86_64 architecture)
pip install --platform manylinux2014_x86_64 `
    --target=. `
    --implementation cp `
    --python-version 3.9 `
    --only-binary=:all: `
    --upgrade beautifulsoup4

# Return to main directory
cd ..
```

**Important:** The `--platform manylinux2014_x86_64` flag ensures Linux-compatible packages for Lambda.

### Step 3: Create Deployment ZIP

**Option A: Using PowerShell**

```powershell
# Zip dependencies
cd package
Compress-Archive -Path * -DestinationPath ..\deployment.zip
cd ..

# Add Lambda function to root of zip
Compress-Archive -Path lambda_function.py -Update -DestinationPath deployment.zip
```

**Option B: Using 7-Zip (More Reliable)**

```powershell
# Add 7z to PATH
$env:PATH += ";C:\Program Files\7-Zip"

# Create zip
cd package
7z a -tzip ..\deployment.zip *
cd ..
7z a -tzip deployment.zip lambda_function.py
```


### Step 4: Verify ZIP Structure

Your deployment.zip should contain:

```
deployment.zip
├── lambda_function.py          ← At root level
├── bs4/                        ← BeautifulSoup
├── soupsieve/                  ← Dependency
└── beautifulsoup4-*.dist-info/ ← Package metadata
```

**Critical:** `lambda_function.py` must be at the ROOT, not in a subfolder.

***

## AWS Configuration

### Step 1: Create IAM Role

1. **Go to** AWS IAM Console → Roles
2. **Click** "Create role"
3. **Select** "Lambda" as trusted entity
4. **Click** "Next"
5. **Name:** `S3ReportProcessorLamdaRole`
6. **Create role**

### Step 2: Attach IAM Policy

1. **Open** the role `S3ReportProcessorLamdaRole`
2. **Click** "Add permissions" → "Create inline policy"
3. **Select** JSON tab
4. **Paste** this policy:
```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "ListBucketPermission",
            "Effect": "Allow",
            "Action": "s3:ListBucket",
            "Resource": "arn:aws:s3:::rohandev-digital-apigateway"
        },
        {
            "Sid": "ObjectLevelPermissions",
            "Effect": "Allow",
            "Action": [
                "s3:GetObject",
                "s3:PutObject",
                "s3:DeleteObject"
            ],
            "Resource": "arn:aws:s3:::rohandev-digital-apigateway/*"
        },
        {
            "Sid": "CloudWatchLogsPermissions",
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents"
            ],
            "Resource": "arn:aws:logs:*:*:*"
        }
    ]
}
```

5. **Name:** `S3BucketAccessPolicy`
6. **Click** "Create policy"

**Important Notes:**

- `s3:ListBucket` requires bucket ARN WITHOUT `/*`
- `s3:GetObject` and `s3:PutObject` require bucket ARN WITH `/*`


### Step 3: Create Lambda Function

1. **Go to** AWS Lambda Console
2. **Click** "Create function"
3. **Select** "Author from scratch"
4. **Function name:** `GenerateReportSummary`
5. **Runtime:** Python 3.9 (or later)
6. **Execution role:** Use existing role → `S3ReportProcessorLamdaRole`
7. **Click** "Create function"

### Step 4: Upload Deployment Package

1. **In the Lambda function**, go to "Code" tab
2. **Click** "Upload from" → ".zip file"
3. **Select** `deployment.zip`
4. **Click** "Save"

### Step 5: Configure Lambda Settings

**Runtime Settings:**

1. **Click** "Runtime settings" → "Edit"
2. **Handler:** `lambda_function.lambda_handler`
3. **Click** "Save"

**General Configuration:**

1. **Click** "Configuration" tab → "General configuration" → "Edit"
2. **Memory:** `512 MB` (recommended for HTML parsing)
3. **Timeout:** `60 seconds` (or higher for large files)
4. **Ephemeral storage:** `512 MB` (default is fine)
5. **Click** "Save"

### Step 6: Configure S3 Trigger

**From Lambda Console:**

1. **Click** "Add trigger"
2. **Select** "S3"
3. **Bucket:** `rohandev-digital-apigateway`
4. **Event type:** PUT (or "All object create events")
5. **Prefix:** `{report_id}/raw/` (optional - filters specific folder)
6. **Suffix:** `.gz` (only trigger on .gz files)
7. **Check** "Recursive invocation" acknowledgment
8. **Click** "Add"

**From S3 Console (Alternative):**

1. **Go to** S3 bucket → Properties tab
2. **Scroll to** "Event notifications"
3. **Click** "Create event notification"
4. **Event name:** `LambdaTriggerForGzFiles`
5. **Event types:** Check "PUT"
6. **Suffix:** `.gz`
7. **Destination:** Lambda function → `GenerateReportSummary`
8. **Click** "Save changes"

***

## Testing Procedures

### Test 1: Manual Lambda Test

1. **Go to** Lambda Console → Test tab
2. **Create new event:**
```json
{
  "Records": [
    {
      "s3": {
        "bucket": {
          "name": "rohandev-digital-apigateway"
        },
        "object": {
          "key": "mixed_checktest/raw/codechef.gz"
        }
      }
    }
  ]
}
```

3. **Click** "Test"
4. **Check** execution result

**Expected Output:**

- Status: 200
- Message: "Report processing complete"
- Check CloudWatch logs for detailed execution


### Test 2: Upload Test File to S3

1. **Prepare** test .gz files (compressed HTML)
2. **Upload to** `s3://rohandev-digital-apigateway/test-report/raw/codechef.gz`
3. **Lambda should trigger automatically**
4. **Check** CloudWatch logs: `/aws/lambda/GenerateReportSummary`
5. **Verify** `s3://rohandev-digital-apigateway/test-report/summary.json` exists

### Test 3: Verify Output Format

Download and check the summary.json:

```json
{
    "codechef": {
        "source": "CodeChef",
        "status": "success",
        "rating": 1477,
        "rank_global": 30662,
        "problems_solved_total": 389,
        "contests_attended": 3
    },
    "leetcode": {
        "source": "LeetCode",
        "status": "success",
        "rating": 1684,
        "rank_global": 107603,
        "problems_solved_total": 618,
        "streak_current": 146
    }
}
```


***

## CloudWatch Monitoring

### View Logs

1. **Go to** CloudWatch Console → Log groups
2. **Select** `/aws/lambda/GenerateReportSummary`
3. **Click** latest log stream

### Key Log Messages

**Success indicators:**

```
[INFO] Lambda invoked with event: {...}
[INFO] Found 4 .gz files: [...]
[INFO] Downloaded mixed_checktest/raw/codechef.gz
[INFO] Parsed codechef successfully
[INFO] ✓ VERIFIED: File exists, size=1234
[INFO] === UPLOAD SUCCESS ===
```

**Error indicators:**

```
[ERROR] Lambda execution failed: ...
[ERROR] ❌ S3 ERROR: AccessDenied
```


### CloudWatch Insights Queries

**Find Errors:**

```
filter @message LIKE /ERROR/ or @message LIKE /Failed/
| sort @timestamp desc
| limit 20
```

**Track Processing Time:**

```
filter @type = "REPORT"
| stats avg(@duration), max(@duration), min(@duration)
```


***

## Troubleshooting Guide

### Issue 1: "Runtime.ImportModuleError: No module named 'lambda_function'"

**Cause:** Incorrect file naming or ZIP structure

**Solution:**

1. Ensure file is named `lambda_function.py`
2. Verify Handler is set to `lambda_function.lambda_handler`
3. Check ZIP structure - `lambda_function.py` must be at root level
4. Recreate ZIP using steps in Deployment section

### Issue 2: "AccessDenied: s3:ListBucket"

**Cause:** Missing or incorrect IAM permissions

**Solution:**

1. Go to IAM → Roles → `S3ReportProcessorLamdaRole`
2. Verify policy includes:
    - `s3:ListBucket` on `arn:aws:s3:::bucket-name` (no /*)
    - `s3:GetObject`, `s3:PutObject` on `arn:aws:s3:::bucket-name/*` (with /*)
3. Apply the complete policy from "AWS Configuration" section

### Issue 3: "No module named 'bs4'"

**Cause:** BeautifulSoup4 not included in deployment package

**Solution:**

1. Delete existing deployment.zip
2. Recreate package with correct pip install command (Linux-compatible)
3. Ensure `--platform manylinux2014_x86_64` flag is used
4. Verify `bs4/` folder exists in zip root

### Issue 4: Lambda Times Out

**Cause:** Insufficient timeout or memory

**Solution:**

1. Configuration → General configuration → Edit
2. Increase Timeout to 120-300 seconds
3. Increase Memory to 512-1024 MB
4. More memory = more CPU power = faster processing

### Issue 5: File Uploaded Successfully but Not Visible in S3

**Cause:** Wrong output path or IAM permission issue

**Solution:**

1. Check CloudWatch logs for actual upload path
2. Verify IAM policy has `s3:PutObject` with `/*`
3. Check if file is in different location than expected
4. Use verification code to confirm upload:
```python
# Add after put_object
s3_client.head_object(Bucket=bucket_name, Key=summary_key)
logger.info("✓ File verified in S3")
```


### Issue 6: Summary.json in Wrong Folder

**Cause:** Incorrect report_id extraction

**Solution:**

- Ensure code uses `os.path.dirname(os.path.dirname(triggering_key))`
- This extracts parent directory: `mixed_checktest/raw/file.gz` → `mixed_checktest`


### Issue 7: Lambda Triggered Multiple Times

**Cause:** S3 trigger configured incorrectly, causing recursive invocation

**Solution:**

1. Ensure output file (summary.json) doesn't match trigger pattern
2. Use suffix filter `.gz` to only trigger on compressed files
3. Don't put summary.json in `raw/` folder

***

## Maintenance and Best Practices

### Regular Maintenance Tasks

**Weekly:**

- Review CloudWatch logs for errors
- Check S3 bucket for orphaned temp files
- Monitor Lambda execution metrics (duration, errors, throttles)

**Monthly:**

- Review IAM permissions (principle of least privilege)
- Update BeautifulSoup4 and dependencies
- Archive old CloudWatch logs to reduce costs

**Quarterly:**

- Test with new platform HTML structures (websites change)
- Update parser functions if needed
- Review and optimize Lambda timeout and memory settings


### Cost Optimization

1. **Set appropriate timeout** - Don't use maximum if not needed
2. **Right-size memory** - Monitor actual usage in CloudWatch
3. **Use S3 Lifecycle policies** - Archive or delete old reports after 90 days
4. **CloudWatch log retention** - Set to 30 days instead of forever

### Security Best Practices

1. **Least Privilege IAM** - Only grant necessary S3 permissions
2. **No hardcoded credentials** - Use IAM roles only
3. **S3 bucket encryption** - Enable default encryption
4. **VPC configuration** - If processing sensitive data, run Lambda in VPC
5. **Version control** - Store Lambda code in Git repository

### Code Version Control

**Recommended Structure:**

```
lambda-scraper/
├── lambda_function.py       ← Main code
├── requirements.txt         ← Dependencies list
├── deploy.ps1              ← Deployment script
├── test_event.json         ← Test event template
├── README.md               ← Project documentation
└── .gitignore              ← Ignore deployment.zip
```

**requirements.txt:**

```
beautifulsoup4==4.12.2
boto3==1.28.85
```


### Deployment Automation Script

Save as `deploy.ps1`:

```powershell
# Configuration
$FunctionName = "GenerateReportSummary"
$PythonVersion = "3.9"
$Region = "us-east-1"

Write-Host "Starting deployment..." -ForegroundColor Green

# Clean up
Remove-Item -Path deployment.zip -ErrorAction SilentlyContinue
Remove-Item -Path package -Recurse -ErrorAction SilentlyContinue

# Create package
New-Item -ItemType Directory -Path package | Out-Null
Set-Location package

# Install dependencies
Write-Host "Installing dependencies..." -ForegroundColor Yellow
pip install --platform manylinux2014_x86_64 `
    --target=. `
    --implementation cp `
    --python-version $PythonVersion `
    --only-binary=:all: `
    --upgrade beautifulsoup4

# Create deployment package
Write-Host "Creating deployment package..." -ForegroundColor Yellow
Compress-Archive -Path * -DestinationPath ..\deployment.zip
Set-Location ..
Compress-Archive -Path lambda_function.py -Update -DestinationPath deployment.zip

# Upload to Lambda
Write-Host "Uploading to Lambda..." -ForegroundColor Yellow
aws lambda update-function-code `
    --function-name $FunctionName `
    --zip-file fileb://deployment.zip `
    --region $Region

Write-Host "Deployment complete!" -ForegroundColor Green
```

Run with: `.\deploy.ps1`

***

## Summary Output Format

The Lambda function generates this standardized JSON format:

```json
{
    "platform_name": {
        "source": "Platform Name",
        "status": "success|error",
        "message": "Error message if status is error",
        
        // Common fields (when available)
        "rating": 1234,
        "rank_global": 12345,
        "rank_country": 1234,
        "problems_solved_total": 123,
        "problems_solved_easy": 40,
        "problems_solved_medium": 50,
        "problems_solved_hard": 33,
        "contests_attended": 12,
        "streak_current": 10,
        "streak_max": 50,
        "rating_max": 1500,
        
        // Platform-specific data
        "platform_specific": {
            // Fields unique to this platform
        }
    }
}
```


***

## Quick Reference Commands

### Deploy Lambda

```powershell
cd C:\lambda-scraper
.\deploy.ps1
```


### Test Lambda

```powershell
aws lambda invoke `
    --function-name GenerateReportSummary `
    --payload file://test_event.json `
    response.json
```


### View Recent Logs

```powershell
aws logs tail /aws/lambda/GenerateReportSummary --follow
```


### Upload Test File

```powershell
aws s3 cp codechef.gz s3://rohandev-digital-apigateway/test-report/raw/
```


### Download Summary

```powershell
aws s3 cp s3://rohandev-digital-apigateway/test-report/summary.json ./
```


***

## Contact and Support

**CloudWatch Log Group:** `/aws/lambda/GenerateReportSummary`

**S3 Bucket:** `rohandev-digital-apigateway`

**IAM Role:** `S3ReportProcessorLamdaRole`

**Lambda Function:** `GenerateReportSummary`

**Region:** Check your AWS Console for current region

***

## Revision History

- **v1.0** - Initial implementation with BeautifulSoup4 parsing
- **v1.1** - Fixed IAM permissions for S3 access
- **v1.2** - Corrected output path to parent directory
- **v1.3** - Added verification and enhanced logging

***

This documentation covers the complete setup, deployment, configuration, testing, and maintenance of the AWS Lambda S3 scraper system. All steps are reproducible and tested on Windows 11 with PowerShell.
<span style="display:none">[^3][^4][^5][^6][^7][^8][^9]</span>

<div align="center">⁂</div>

[^1]: https://docs.aws.amazon.com/lambda/latest/dg/best-practices.html

[^2]: https://aws.amazon.com/blogs/architecture/best-practices-for-developing-on-aws-lambda/

[^3]: https://lumigo.io/learn/top-10-aws-lambda-best-practices/

[^4]: https://docs.aws.amazon.com/lambda/latest/dg/welcome.html

[^5]: https://dev.to/harithzainudin/5-best-practices-for-aws-lambda-function-design-standards-1564

[^6]: https://www.reddit.com/r/aws/comments/1d29cvb/best_way_to_document_lambdas/

[^7]: https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtime-environment.html

[^8]: https://serverlessland.com/content/service/lambda/guides/aws-lambda-fundamentals/aws-lambda-function-design-best-practices

[^9]: https://docs.aws.amazon.com/lambda/latest/dg/getting-started.html

