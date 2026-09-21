#!/usr/bin/env bash
#
# Builds the AWS Lambda deployment package (deployment.zip) for
# lambda-data-phantom-v2 on Linux/macOS/CI.
#
# Stages lambda_function.py + vendored deps (bs4, soupsieve, typing_extensions
# and their .dist-info dirs), strips __pycache__, and zips the CONTENTS at the
# archive root so lambda_function.py sits at the top level (Lambda requirement).
#
# Usage: ./build.sh [output.zip]
set -euo pipefail

cd "$(dirname "$0")"

OUT_FILE="${1:-deployment.zip}"
HANDLER="lambda_function.py"
DEPS=(
    "bs4"
    "soupsieve"
    "typing_extensions.py"
    "beautifulsoup4-4.14.2.dist-info"
    "soupsieve-2.8.dist-info"
    "typing_extensions-4.15.0.dist-info"
)

echo "==> Building Lambda deployment package"

# --- Preflight: verify required items exist ---
missing=()
[ -f "$HANDLER" ] || missing+=("$HANDLER")
for d in "${DEPS[@]}"; do
    [ -e "$d" ] || missing+=("$d")
done
if [ "${#missing[@]}" -gt 0 ]; then
    echo "ERROR: missing required items: ${missing[*]}" >&2
    exit 1
fi

# --- Syntax check the handler ---
echo "==> Compiling $HANDLER"
python3 -m py_compile "$HANDLER"

# --- Stage into a clean temp dir ---
staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT

cp "$HANDLER" "$staging/"
for d in "${DEPS[@]}"; do
    cp -r "$d" "$staging/"
done

# Strip bytecode caches.
find "$staging" -type d -name "__pycache__" -prune -exec rm -rf {} +

# --- Zip the CONTENTS so files land at the archive root ---
rm -f "$OUT_FILE"
abs_out="$(pwd)/$OUT_FILE"
( cd "$staging" && zip -q -r -X "$abs_out" . )

size_kb=$(( $(wc -c < "$OUT_FILE") / 1024 ))
echo "==> Created $OUT_FILE (${size_kb} KB)"
echo "    Deploy with:"
echo "    aws lambda update-function-code --function-name GenerateReportSummary --zip-file fileb://$OUT_FILE"
