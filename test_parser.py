from lambda_function import parse_geeksforgeeks_stats
import json

# 1. Read your local HTML file
filename = "geeksforgeeks.html"
try:
    with open(filename, "r", encoding="utf-8") as f:
        html_content = f.read()
except FileNotFoundError:
    print(f"Error: Could not find {filename}. Make sure it's in the same folder.")
    exit()

# 2. Run the parser function
print(f"Testing parser on {filename}...")
stats = parse_geeksforgeeks_stats(html_content)

# 3. Show the results
print("\n--- Extraction Result ---")
print(json.dumps(stats, indent=4))