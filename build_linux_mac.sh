#!/usr/bin/env bash
# SnP Image Labeler — Build Script (Linux / macOS)
set -euo pipefail

echo ""
echo "============================================================"
echo "  SnP Image Labeler — PyInstaller Build"
echo "============================================================"
echo ""

# ── 1. Python check ────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
  echo "[ERROR] python3 not found. Install Python 3.9+."
  exit 1
fi
echo "[OK] $(python3 --version)"

# ── 2. Dependencies ────────────────────────────────────────────
python3 -c "import PyQt5" 2>/dev/null || {
  echo "[INFO] Installing PyQt5..."
  pip3 install PyQt5 --quiet
}
echo "[OK] PyQt5 present"

python3 -c "import PyInstaller" 2>/dev/null || {
  echo "[INFO] Installing PyInstaller..."
  pip3 install pyinstaller --quiet
}
echo "[OK] PyInstaller $(pyinstaller --version)"

# ── 3. Clean ───────────────────────────────────────────────────
echo ""
echo "[INFO] Cleaning previous build artefacts..."
rm -rf dist/SnP_Image_Labeler build/SnP_Image_Labeler
echo "[OK] Clean complete"

# ── 4. Build ───────────────────────────────────────────────────
echo ""
echo "[INFO] Building (this may take 1-3 minutes)..."
echo ""
pyinstaller SnP_Image_Labeler.spec --noconfirm --clean

# ── 5. Verify ──────────────────────────────────────────────────
PLATFORM="$(uname -s)"
if [[ "$PLATFORM" == "Darwin" ]]; then
  TARGET="dist/SnP_Image_Labeler/SnP_Image_Labeler"
  # macOS may also produce a .app bundle
else
  TARGET="dist/SnP_Image_Labeler/SnP_Image_Labeler"
fi

if [[ ! -f "$TARGET" ]]; then
  echo "[ERROR] Binary not found at $TARGET"
  exit 1
fi

SIZE=$(du -sh dist/SnP_Image_Labeler | cut -f1)

echo ""
echo "============================================================"
echo "  BUILD SUCCESSFUL"
echo "============================================================"
echo "  Output : $TARGET"
echo "  Size   : $SIZE"
echo "  Folder : dist/SnP_Image_Labeler/"
echo ""
echo "  To distribute: zip the entire 'dist/SnP_Image_Labeler' folder."
echo "============================================================"
echo ""
