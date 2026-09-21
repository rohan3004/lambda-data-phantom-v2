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


def is_bot_challenge_page(html_content: str) -> bool:
    """Detect anti-bot interstitials (e.g. Cloudflare 'Just a moment...') that a
    scraper may have captured instead of the real profile page.

    When a scrape is blocked, the saved HTML is the challenge page, which has no
    profile data. Detecting it lets parsers return an explicit, actionable error
    instead of silently reporting success with empty stats.
    """
    if not html_content:
        return False
    lowered = html_content[:4000].lower()
    signatures = (
        "just a moment...",
        "performing security verification",
        "verify you are not a bot",
        "cf-browser-verification",
        "challenge-platform",
        "enable javascript and cookies to continue",
        "attention required! | cloudflare",
    )
    return any(sig in lowered for sig in signatures)


def _lc_count_by_difficulty(entries, difficulty, field="count"):
    """Pull a numeric field for a given difficulty from a LeetCode stat list."""
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if entry.get("difficulty") == difficulty:
            return entry.get(field)
    return None


def _lc_streaks_from_calendar(submission_calendar, major_gap_days=100):
    """Compute current and max daily streaks from LeetCode's submissionCalendar.

    The calendar is a JSON string mapping UTC-midnight epoch seconds -> count.
    Returns (current_streak, max_streak):

    * max_streak    -- the longest run of *strictly consecutive* active days.
    * current_streak -- the number of active days accumulated since the last
      *major* break, i.e. counting active days from the first activity that
      follows any gap longer than ``major_gap_days``, through to the most recent
      active day. Short lapses (a missed day here and there) do not reset it,
      which mirrors LeetCode's lenient streak counting; a long hiatus does.
    """
    if not submission_calendar:
        return 0, 0
    try:
        cal = json.loads(submission_calendar) if isinstance(submission_calendar, str) else submission_calendar
    except (ValueError, TypeError):
        return 0, 0
    if not cal:
        return 0, 0

    day = 86400
    active_days = sorted({int(ts) // day for ts, cnt in cal.items() if cnt and int(cnt) > 0})
    if not active_days:
        return 0, 0

    # --- max_streak: longest strictly-consecutive run ---
    max_streak = 1
    run = 1
    for i in range(1, len(active_days)):
        if active_days[i] - active_days[i - 1] == 1:
            run += 1
        else:
            run = 1
        if run > max_streak:
            max_streak = run

    # --- current_streak: active days since the last major gap ---
    # Find the index right after the most recent gap that exceeds the threshold.
    segment_start_idx = 0
    for i in range(1, len(active_days)):
        if active_days[i] - active_days[i - 1] > major_gap_days:
            segment_start_idx = i
    current_streak = len(active_days) - segment_start_idx

    return current_streak, max_streak


def parse_leetcode_stats(json_content) -> dict:
    """Parses LeetCode profile stats from a GraphQL JSON API response.

    LeetCode profiles are now collected via the GraphQL API (clean, structured
    JSON) instead of scraped HTML, which avoids the Cloudflare anti-bot page that
    previously blocked scraping. Accepts a JSON string or an already-parsed dict.

    The top-level schema is kept identical to the previous HTML parser
    (rating, rank_global, contests_attended, problems_solved_*, streak_current,
    streak_max); additional details are surfaced under platform_specific.
    """
    if not json_content:
        return {"status": "error", "message": "Missing HTML content for LeetCode."}

    if isinstance(json_content, str) and is_bot_challenge_page(json_content):
        return {
            "source": "LeetCode",
            "status": "error",
            "message": "Scrape was blocked by an anti-bot challenge (e.g. Cloudflare); "
                       "the captured content is a verification page, not the profile.",
        }

    # Initialize with platform_specific nested dict (same shape as before)
    stats = {"source": "LeetCode", "status": "success", "platform_specific": {}}

    try:
        payload = json_content if isinstance(json_content, dict) else json.loads(json_content)
        data = payload.get("data", payload) or {}
        matched_user = data.get("matchedUser") or {}
        contest = data.get("userContestRanking") or {}
        all_questions = data.get("allQuestionsCount")
        recent = data.get("recentSubmissionList")

        if not matched_user and not contest:
            stats["status"] = "error"
            stats["message"] = "No LeetCode user data found (matchedUser/userContestRanking missing)."
            return stats

        # --- Contest ranking -> top-level rating / rank / contests ---
        if contest.get("rating") is not None:
            stats["rating"] = int(round(contest["rating"]))
            stats["platform_specific"]["contest_rating_raw"] = contest["rating"]
        if contest.get("globalRanking") is not None:
            stats["rank_global"] = contest["globalRanking"]
        if contest.get("attendedContestsCount") is not None:
            stats["contests_attended"] = contest["attendedContestsCount"]
        if contest.get("topPercentage") is not None:
            stats["platform_specific"]["top_percentage"] = f"{contest['topPercentage']}%"
        if contest.get("totalParticipants") is not None:
            stats["platform_specific"]["contest_total_participants"] = contest["totalParticipants"]

        # --- Problems solved by difficulty (accepted submissions) ---
        submit_stats = matched_user.get("submitStatsGlobal") or {}
        ac = submit_stats.get("acSubmissionNum")
        total_sub = submit_stats.get("totalSubmissionNum")
        if ac:
            easy = _lc_count_by_difficulty(ac, "Easy")
            medium = _lc_count_by_difficulty(ac, "Medium")
            hard = _lc_count_by_difficulty(ac, "Hard")
            all_solved = _lc_count_by_difficulty(ac, "All")
            stats["problems_solved_easy"] = easy
            stats["problems_solved_medium"] = medium
            stats["problems_solved_hard"] = hard
            stats["problems_solved_total"] = (
                all_solved if all_solved is not None
                else (easy or 0) + (medium or 0) + (hard or 0)
            )

        # --- Total problems available on LeetCode (context) ---
        if isinstance(all_questions, list):
            total_easy = _lc_count_by_difficulty(all_questions, "Easy")
            total_medium = _lc_count_by_difficulty(all_questions, "Medium")
            total_hard = _lc_count_by_difficulty(all_questions, "Hard")
            total_all = _lc_count_by_difficulty(all_questions, "All")
            if total_all is not None:
                stats["platform_specific"]["total_questions_available"] = total_all
            if None not in (total_easy, total_medium, total_hard):
                stats["platform_specific"]["questions_available_by_difficulty"] = {
                    "easy": total_easy, "medium": total_medium, "hard": total_hard,
                }

        # --- Submissions & acceptance rate (platform_specific) ---
        if total_sub:
            total_submissions = _lc_count_by_difficulty(total_sub, "All", "submissions")
            if total_submissions is not None:
                stats["platform_specific"]["total_submissions"] = total_submissions
                ac_submissions = _lc_count_by_difficulty(ac, "All", "submissions") if ac else None
                if ac_submissions is not None:
                    stats["platform_specific"]["accepted_submissions"] = ac_submissions
                    if total_submissions:
                        rate = round((ac_submissions / total_submissions) * 100, 1)
                        stats["platform_specific"]["acceptance_rate"] = f"{rate}%"

        # --- Badges ---
        badges = matched_user.get("badges")
        if isinstance(badges, list):
            stats["platform_specific"]["badges"] = len(badges)
            stats["platform_specific"]["badge_names"] = [
                b.get("displayName") for b in badges if b.get("displayName")
            ]
        active_badge = matched_user.get("activeBadge") or {}
        if active_badge.get("displayName"):
            stats["platform_specific"]["active_badge"] = active_badge["displayName"]

        # --- Activity calendar / streaks ---
        calendar = matched_user.get("userCalendar") or {}
        if calendar.get("totalActiveDays") is not None:
            stats["platform_specific"]["total_active_days"] = calendar["totalActiveDays"]
        if calendar.get("activeYears"):
            stats["platform_specific"]["active_years"] = calendar["activeYears"]

        current_streak, max_streak = _lc_streaks_from_calendar(calendar.get("submissionCalendar"))
        # current_streak = active days since the last major hiatus (lenient),
        # max_streak = longest strictly-consecutive run. Computed from the full
        # submissionCalendar when present; otherwise fall back to LeetCode's own
        # single-year `streak` field.
        if calendar.get("submissionCalendar"):
            streak_value = max(current_streak, max_streak)
            stats["streak_current"] = streak_value
            stats["streak_max"] = streak_value
        elif calendar.get("streak") is not None:
            stats["streak_current"] = calendar["streak"]
            stats["streak_max"] = calendar["streak"]
        else:
            streak_value = max(current_streak, max_streak)
            stats["streak_current"] = streak_value
            stats["streak_max"] = streak_value

        # --- Profile extras ---
        profile = matched_user.get("profile") or {}
        if matched_user.get("username"):
            stats["platform_specific"]["username"] = matched_user["username"]
        if profile.get("realName"):
            stats["platform_specific"]["real_name"] = profile["realName"]
        if profile.get("ranking") is not None:
            stats["platform_specific"]["profile_ranking"] = profile["ranking"]
        if profile.get("reputation") is not None:
            stats["platform_specific"]["reputation"] = profile["reputation"]
        if profile.get("starRating") is not None:
            stats["platform_specific"]["star_rating"] = profile["starRating"]
        if profile.get("countryName"):
            stats["platform_specific"]["country"] = profile["countryName"]
        if profile.get("school"):
            stats["platform_specific"]["school"] = profile["school"]
        if profile.get("company"):
            stats["platform_specific"]["company"] = profile["company"]
        if profile.get("skillTags"):
            stats["platform_specific"]["skill_tags"] = profile["skillTags"]
        if profile.get("websites"):
            stats["platform_specific"]["websites"] = profile["websites"]

        # Social links
        social = {}
        for key, field in (("github", "githubUrl"), ("twitter", "twitterUrl"), ("linkedin", "linkedinUrl")):
            if matched_user.get(field):
                social[key] = matched_user[field]
        if social:
            stats["platform_specific"]["social"] = social

        # Contributions
        contributions = matched_user.get("contributions") or {}
        if contributions.get("points") is not None:
            stats["platform_specific"]["contribution_points"] = contributions["points"]

        # Most recent accepted submission (activity context)
        if isinstance(recent, list) and recent:
            latest = recent[0]
            stats["platform_specific"]["last_submission"] = {
                "title": latest.get("title"),
                "titleSlug": latest.get("titleSlug"),
                "status": latest.get("statusDisplay"),
                "lang": latest.get("lang"),
                "timestamp": latest.get("timestamp"),
            }

    except Exception as e:
        stats["status"] = "error"
        stats["message"] = f"Failed to parse LeetCode JSON: {str(e)}"

    return stats


def parse_codechef_stats(html_content: str) -> dict:
    """Parses the HTML from a CodeChef profile page."""
    if not html_content:
        return {"status": "error", "message": "Missing HTML content for CodeChef."}

    if is_bot_challenge_page(html_content):
        return {"source": "CodeChef", "status": "error",
                "message": "Scrape was blocked by an anti-bot challenge; captured HTML is a verification page, not the profile."}

    
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

    if is_bot_challenge_page(html_content):
        return {"source": "Codeforces", "status": "error",
                "message": "Scrape was blocked by an anti-bot challenge; captured HTML is a verification page, not the profile."}

    
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


def _extract_gfg_user_object(html_content: str) -> dict:
    """Extract the embedded user-stats JSON object from a GeeksForGeeks profile page.

    Modern GFG profiles are a Next.js app that streams data via
    ``self.__next_f.push([...])`` script chunks (React Server Components format)
    rather than rendering the numbers into the DOM. The stats live in a
    JS-escaped JSON object that contains the ``total_problems_solved`` key.
    Returns the parsed object, or an empty dict if it cannot be found.
    """
    # The payload is JS-escaped inside the script tags (\" for quotes). Unescape
    # the two sequences that matter for locating and slicing the JSON object.
    unescaped = html_content.replace('\\"', '"')

    anchor = unescaped.find('total_problems_solved')
    if anchor == -1:
        return {}

    # Walk back to the opening brace of the object that holds the anchor key.
    start = unescaped.rfind('{', 0, anchor)
    if start == -1:
        return {}

    # Forward brace-matching to find the balanced closing brace.
    depth = 0
    end = None
    for j in range(start, min(len(unescaped), start + 5000)):
        ch = unescaped[j]
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = j + 1
                break

    if end is None:
        return {}

    try:
        return json.loads(unescaped[start:end])
    except (ValueError, json.JSONDecodeError):
        return {}


def parse_geeksforgeeks_stats(html_content: str) -> dict:
    """Parses the HTML from a GeeksForGeeks profile page.

    GFG serves stats inside a streamed Next.js JSON payload, so the primary
    strategy parses that embedded object. A legacy CSS-selector fallback is
    kept for older markup.
    """
    if not html_content:
        return {"status": "error", "message": "Missing HTML content for GeeksForGeeks."}

    if is_bot_challenge_page(html_content):
        return {"source": "GeeksForGeeks", "status": "error",
                "message": "Scrape was blocked by an anti-bot challenge; captured HTML is a verification page, not the profile."}

    soup = BeautifulSoup(html_content, "html.parser")
    stats = {"source": "GeeksForGeeks", "status": "success", "platform_specific": {}}

    try:
        # --- Primary: embedded Next.js JSON payload ---
        user = _extract_gfg_user_object(html_content)
        if user:
            # Coding score -> rating
            if user.get('score') is not None:
                stats['rating'] = clean_value(str(user['score']))

            # Total problems solved
            if user.get('total_problems_solved') is not None:
                stats['problems_solved_total'] = clean_value(str(user['total_problems_solved']))

            # Institute rank (often an empty string when not ranked)
            institute_rank = clean_value(str(user.get('institute_rank', '')))
            if institute_rank is not None:
                stats['platform_specific']['institute_rank'] = institute_rank

            # POTD streaks
            if user.get('pod_solved_current_streak') is not None:
                stats['streak_current'] = clean_value(str(user['pod_solved_current_streak']))
            if user.get('pod_solved_longest_streak') is not None:
                stats['streak_max'] = clean_value(str(user['pod_solved_longest_streak']))

            # Platform-specific extras
            if user.get('pod_solved_global_longest_streak') is not None:
                stats['platform_specific']['global_longest_streak'] = clean_value(str(user['pod_solved_global_longest_streak']))
            if user.get('pod_correct_submissions_count') is not None:
                stats['platform_specific']['potds_solved'] = clean_value(str(user['pod_correct_submissions_count']))
            if user.get('total_articles_published') is not None:
                stats['platform_specific']['articles_published'] = clean_value(str(user['total_articles_published']))
            if user.get('monthly_score') is not None:
                stats['platform_specific']['monthly_score'] = clean_value(str(user['monthly_score']))
            if user.get('institute_name'):
                stats['platform_specific']['institute_name'] = str(user['institute_name']).strip()

            return stats

        # --- Fallback: legacy CSS selectors (older GFG markup) ---
        score_cards = soup.select('.ScoreContainer_score-card__zI4vG')
        for card in score_cards:
            label_tag = card.select_one('.ScoreContainer_label__aVpLE')
            value_tag = card.select_one('.ScoreContainer_value__7yy7h')
            if label_tag and value_tag:
                label = label_tag.text.strip().lower()
                value = clean_value(value_tag.text.strip())
                if 'coding score' in label:
                    stats['rating'] = value
                elif 'problems solved' in label:
                    stats['problems_solved_total'] = value
                elif 'institute rank' in label:
                    stats['platform_specific']['institute_rank'] = value
                elif 'articles published' in label:
                    stats['platform_specific']['articles_published'] = value

        current_streak_div = soup.select_one('.PotdContainer_streakText__oNgWh')
        if current_streak_div:
            stats['streak_current'] = clean_value(current_streak_div.text)

        potd_stats = soup.select('.PotdContainer_statItem__YU3BX')
        for item in potd_stats:
            label_tag = item.select_one('.PotdContainer_statLabel__tc6R1')
            value_tag = item.select_one('.PotdContainer_statValue__nt1dr')
            if label_tag and value_tag:
                label = label_tag.text.strip().lower()
                value = clean_value(value_tag.text)
                if 'longest streak' in label:
                    stats['streak_max'] = value
                elif 'potds solved' in label:
                    stats['platform_specific']['potds_solved'] = value

        nav_items = soup.select('.ProblemNavbar_head_nav--text__7u4wN')
        for item in nav_items:
            text = item.text.strip()
            match = re.search(r'([A-Z]+)\s*\((\d+)\)', text, re.IGNORECASE)
            if match:
                difficulty = match.group(1).lower()
                stats[f'problems_solved_{difficulty}'] = int(match.group(2))

        # If neither strategy found anything, flag it rather than silently succeeding.
        if not any(k for k in stats if k not in ('source', 'status', 'platform_specific')) and not stats['platform_specific']:
            stats['status'] = 'error'
            stats['message'] = 'No GeeksForGeeks stats found in HTML (markup may have changed or page was not fully rendered).'

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
        
        # List all profile files (.gz HTML dumps and .json API responses)
        raw_folder = f"{report_id}/raw/"
        response = s3_client.list_objects_v2(Bucket=bucket_name, Prefix=raw_folder)
        
        if 'Contents' not in response:
            logger.warning(f"No objects found for prefix: {raw_folder}")
            return {'statusCode': 200, 'body': 'No files to process'}
        
        profile_files = [obj['Key'] for obj in response.get('Contents', [])
                         if obj['Key'].endswith('.gz') or obj['Key'].endswith('.json')]
        
        logger.info(f"Found {len(profile_files)} profile files: {profile_files}")
        
        # Process each platform file
        for key in profile_files:
            # Platform name is the filename without its extension, e.g.
            # 'leetcode.json' -> 'leetcode', 'codechef.gz' -> 'codechef'.
            platform = os.path.splitext(os.path.basename(key))[0]
            
            if platform not in PROFILES_CONFIG:
                logger.warning(f"Skipping unknown platform: {platform}")
                continue
            
            logger.info(f"Processing platform: {platform}")
            
            try:
                temp_path = f"/tmp/{report_id.replace('/', '_')}_{os.path.basename(key)}"
                
                s3_client.download_file(bucket_name, key, temp_path)
                logger.info(f"Downloaded {key} to {temp_path}")
                
                # Read the file, tolerating gzip, plain HTML uploaded with a .gz
                # extension (some scrapers do this), and plain JSON.
                with open(temp_path, 'rb') as f:
                    raw_bytes = f.read()
                if raw_bytes[:2] == b'\x1f\x8b':  # gzip magic number
                    content = gzip.decompress(raw_bytes).decode('utf-8', errors='replace')
                    logger.info(f"Decompressed {platform} (gzip), content length: {len(content)}")
                else:
                    content = raw_bytes.decode('utf-8', errors='replace')
                    logger.info(f"Read {platform} as plain text, content length: {len(content)}")

                
                parser_func = PROFILES_CONFIG[platform]['parser']
                aggregated_stats[platform] = parser_func(content)
                
                logger.info(f"Parsed {platform} successfully")
                
                os.unlink(temp_path)
                
            except Exception as e:
                logger.error(f"Error processing {platform}: {e}", exc_info=True)
                aggregated_stats[platform] = {
                    "status": "error",
                    "message": str(e)
                }
        
        # Upload summary
        if aggregated_stats:
            summary_key = f"{report_id}/summary.json"
            summary_content = json.dumps(aggregated_stats, separators=(',', ':'))
            
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
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Report processing complete',
                'report_id': report_id,
                'files_processed': len(profile_files),
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