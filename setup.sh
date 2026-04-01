#!/usr/bin/env bash
# setup.sh — create a virtual environment and install dependencies
set -e

VENV_DIR="${1:-.venv}"

echo "Creating virtual environment in $VENV_DIR ..."
python3 -m venv "$VENV_DIR"

echo "Installing dependencies ..."
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r requirements.txt -q

echo ""
echo "✅ Setup complete."
echo ""
echo "To convert a PDF:"
echo "  $VENV_DIR/bin/python convert.py your_file.pdf"
echo ""
echo "To audit an EPUB:"
echo "  $VENV_DIR/bin/python audit.py your_file.epub"
