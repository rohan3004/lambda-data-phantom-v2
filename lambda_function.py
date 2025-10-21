import boto3
import gzip
import json
import os
import re
import logging
from datetime import datetime, timedelta
from urllib.parse import unquote_plus
from bs4 import BeautifulSoup

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)


# --- AWS S3 Client ---
s3_client = boto3.client('s3')

# --- PART 1: PARSER FUNCTIONS AND CONFIG (Copied from your script) ---

def clean_value(value: str):
    if not isinstance(value, str) or value in ['__', '?']:
        return None
    cleaned_string = re.sub(r'[^\d-]', '', value)
    if cleaned_string:
        return int(cleaned_string)
    return None

def parse_leetcode_stats(html_content: str) -> dict:
    """Parses the HTML from a LeetCode profile page into a complete stats JSON."""
    if not html_content:
        return {"status": "error", "message": "Missing HTML content for LeetCode."}

    soup = BeautifulSoup(html_content, "html.parser")
    # Initialize with platform_specific nested dict
    stats = {"source": "LeetCode", "status": "success", "platform_specific": {}}

    try:
        # Renamed keys and used clean_value
        rating_div = soup.select_one("div.text-label-1.dark\\:text-dark-label-1.flex.items-center.text-2xl")
        if rating_div:
            stats["rating"] = clean_value(rating_div.text.strip())

        ranking_div = soup.select_one("div.text-label-1.dark\\:text-dark-label-1.font-medium.leading-\\[22px\\]")
        if ranking_div:
            stats["rank_global"] = clean_value(ranking_div.contents[0].strip())

        top_div = soup.select_one("div.absolute.left-0.top-0 div.text-label-1.dark\\:text-dark-label-1.text-2xl")
        if top_div:
            stats["platform_specific"]["top_percentage"] = top_div.text.strip()

        attended_div = soup.select_one("div.hidden.md\\:block div.text-label-1.dark\\:text-dark-label-1.font-medium")
        if attended_div:
            stats["contests_attended"] = clean_value(attended_div.text.strip())

        badge_imgs = soup.select("image[xlink\\:href*='/static/images/badges/'], img[src*='/static/images/badges/']")
        stats["platform_specific"]["badges"] = len(badge_imgs) - 1 if badge_imgs else 0

        # Difficulty counts
        solved_counts_div = soup.select(".flex.h-full.w-\\[90px\\].flex-none.flex-col.gap-2")
        if solved_counts_div:
            difficulties = solved_counts_div[0].find_all("div", recursive=False)
            if len(difficulties) == 3:
                easy = clean_value(difficulties[0].find_all("div")[1].text.split("/")[0].strip())
                medium = clean_value(difficulties[1].find_all("div")[1].text.split("/")[0].strip())
                hard = clean_value(difficulties[2].find_all("div")[1].text.split("/")[0].strip())
                stats["problems_solved_easy"] = easy
                stats["problems_solved_medium"] = medium
                stats["problems_solved_hard"] = hard
                stats["problems_solved_total"] = (easy or 0) + (medium or 0) + (hard or 0)

        # Submissions & Acceptance moved to platform_specific
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

        # ----- Heatmap → streaks -----
        svg = soup.select_one("div.lc-md\\:flex.hidden.h-auto.w-full.flex-1.items-center.justify-center svg") or soup.select_one("svg")
        if not svg:
            stats["streak_current"] = 0
            if "streak_max" not in stats:
                stats["streak_max"] = 0
            return stats

        date_rects = svg.select("g.month g.week rect[data-date]")
        current_streak = 0
        max_streak_calc = 0
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
        if "streak_max" not in stats: # Only use calculated if profile one isn't found
            stats["streak_max"] = max_streak_calc

    except Exception as e:
        stats["status"] = "error"
        stats["message"] = f"Failed to parse LeetCode HTML: {str(e)}"

    return stats


def parse_codechef_stats(html_content: str) -> dict:
    """Parses the HTML from a CodeChef profile page."""
    if not html_content:
        return {"status": "error", "message": "Missing HTML content for CodeChef."}
    
    soup = BeautifulSoup(html_content, "html.parser")
    stats = {"source": "CodeChef", "status": "success", "platform_specific": {}}
    try:
        contest_rank_span = soup.select_one('.user-details-container .rating')
        if contest_rank_span:
            stats['platform_specific']['contest_rank_stars'] = contest_rank_span.text.strip().replace('★', '')

        contest_count_b = soup.select_one('.contest-participated-count b')
        if contest_count_b:
            stats['contests_attended'] = clean_value(contest_count_b.text.strip())

        problems_solved_h3 = soup.find('h3', string=lambda t: 'Total Problems Solved' in t if t else False)
        if problems_solved_h3:
            stats['problems_solved_total'] = clean_value(problems_solved_h3.text.strip().split(':')[-1].strip())

        rating_header = soup.select_one('.rating-header')
        if rating_header:
            rating_div = rating_header.select_one('.rating-number')
            if rating_div and rating_div.contents:
                rating_text = rating_div.contents[0].strip()
                stats['rating'] = clean_value(rating_text)
            
            division_div = rating_header.find('div', string=lambda t: '(Div' in t if t else False)
            if division_div:
                stats['platform_specific']['division'] = division_div.text.strip().replace('(', '').replace(')', '')

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
    """Parses the HTML from a Codeforces profile page."""
    if not html_content:
        return {"status": "error", "message": "Missing HTML content for Codeforces."}
    
    soup = BeautifulSoup(html_content, "html.parser")
    stats = {"source": "Codeforces", "status": "success", "platform_specific": {}}
    
    try:
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
    """Parses the HTML from a GeeksForGeeks profile page."""
    if not html_content:
        return {"status": "error", "message": "Missing HTML content for GeeksForGeeks."}
    
    soup = BeautifulSoup(html_content, "html.parser")
    stats = {"source": "GeeksForGeeks", "status": "success"}
    
    try:
        streak_div = soup.select_one('.circularProgressBar_head_mid_streakCnt__MFOF1')
        if streak_div:
            stats['streak_current'] = clean_value(streak_div.contents[0].strip())

        score_cards = soup.select('.scoreCard_head__nxXR8')
        if len(score_cards) >= 3:
            total_problems_div = score_cards[1].select_one('.scoreCard_head_left--score__oSi_x')
            if total_problems_div:
                stats['problems_solved_total'] = clean_value(total_problems_div.text.strip())
            
            contest_rating_div = score_cards[2].select_one('.scoreCard_head_left--score__oSi_x')
            if contest_rating_div:
                stats['rating'] = clean_value(contest_rating_div.text.strip())

        problem_nav = soup.select('.problemNavbar_head_nav__a4K6P')
        for item in problem_nav:
            text = item.text.strip()
            match = re.search(r'([A-Z]+)\s*\((\d+)\)', text)
            if match:
                difficulty = match.group(1).lower() # Convert to lowercase
                count = clean_value(match.group(2))
                stats[f'problems_solved_{difficulty}'] = count

    except Exception as e:
        stats["status"] = "error"
        stats["message"] = f"Failed to parse GeeksForGeeks HTML: {str(e)}"
        
    return stats

PROFILES_CONFIG = {
    "codechef": {"parser": parse_codechef_stats},
    "codeforces": {"parser": parse_codeforces_stats},
    "geeksforgeeks": {"parser": parse_geeksforgeeks_stats},
    "leetcode": {"parser": parse_leetcode_stats}
}

# --- PART 2: LAMBDA HANDLER ---

def lambda_handler(event, context):
    """
    Processes .gz files from S3, parses competitive programming stats,
    generates summary.json, and deletes raw files.
    """
    try:
        logger.info(f"Lambda invoked with event: {json.dumps(event)}")
        
        record = event['Records'][0]['s3']
        bucket_name = record['bucket']['name']
        triggering_key = unquote_plus(record['object']['key'])
        
        logger.info(f"Processing - Bucket: {bucket_name}, Key: {triggering_key}")
        
        # Extract report_id
        full_path = os.path.dirname(triggering_key)
        report_id = os.path.dirname(full_path)
        
        if not report_id:
            logger.error(f"Could not determine report_id from key: {triggering_key}")
            return {'statusCode': 400, 'body': 'Invalid object key structure'}
        
        logger.info(f"Report ID: {report_id}")
        aggregated_stats = {}
        
        # List all .gz files
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
                temp_gz_path = f"/tmp/{report_id.replace('/', '_')}_{os.path.basename(key)}"
                
                s3_client.download_file(bucket_name, key, temp_gz_path)
                logger.info(f"Downloaded {key} to {temp_gz_path}")
                
                with gzip.open(temp_gz_path, 'rt', encoding='utf-8') as f:
                    html_content = f.read()
                
                logger.info(f"Decompressed {platform}, content length: {len(html_content)}")
                
                parser_func = PROFILES_CONFIG[platform]['parser']
                aggregated_stats[platform] = parser_func(html_content)
                
                logger.info(f"Parsed {platform} successfully")
                
                os.unlink(temp_gz_path)
                
            except Exception as e:
                logger.error(f"Error processing {platform}: {e}", exc_info=True)
                aggregated_stats[platform] = {
                    "status": "error",
                    "message": str(e)
                }
        
        # Upload summary
        if aggregated_stats:
            summary_key = f"{report_id}/summary.json"
            summary_content = json.dumps(aggregated_stats, indent=4)
            
            logger.info(f"=== UPLOAD START ===")
            logger.info(f"Uploading summary to: {summary_key}")
            
            try:
                put_response = s3_client.put_object(
                    Bucket=bucket_name,
                    Key=summary_key,
                    Body=summary_content.encode('utf-8'),
                    ContentType='application/json'
                )
                logger.info(f"PutObject response ETag: {put_response.get('ETag')}")
                
                head_response = s3_client.head_object(Bucket=bucket_name, Key=summary_key)
                logger.info(f"✓ VERIFIED: File exists, size={head_response['ContentLength']}")
                
            except ClientError as e:
                logger.error(f"❌ S3 ERROR: {e.response['Error']['Code']}")
                logger.error(f"Message: {e.response['Error']['Message']}")
                raise
            
            logger.info(f"=== UPLOAD SUCCESS ===")
            logger.info(f"Summary content:\n{summary_content}")
        
        # ==================== NEW: DELETE RAW FILES ====================
        logger.info(f"=== CLEANUP START ===")
        deleted_count = 0
        failed_deletions = []
        
        for key in gz_files:
            try:
                s3_client.delete_object(Bucket=bucket_name, Key=key)
                logger.info(f"✓ Deleted: {key}")
                deleted_count += 1
            except ClientError as e:
                logger.error(f"✗ Failed to delete {key}: {e}")
                failed_deletions.append(key)
        
        logger.info(f"=== CLEANUP COMPLETE ===")
        logger.info(f"Deleted {deleted_count}/{len(gz_files)} raw files")
        
        if failed_deletions:
            logger.warning(f"Failed to delete: {failed_deletions}")
        
        # Optionally: Delete the raw/ folder itself if empty
        try:
            # Check if raw folder is empty
            check_response = s3_client.list_objects_v2(
                Bucket=bucket_name, 
                Prefix=raw_folder,
                MaxKeys=1
            )
            
            if 'Contents' not in check_response:
                logger.info(f"Raw folder {raw_folder} is empty (all files cleaned up)")
        except Exception as e:
            logger.warning(f"Could not verify folder emptiness: {e}")
        # ==================== END CLEANUP ====================
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Report processing complete',
                'report_id': report_id,
                'files_processed': len(gz_files),
                'files_deleted': deleted_count,
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