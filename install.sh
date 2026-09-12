#!/usr/bin/env bash

WITHDRAWAL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/Withdrawal"
DESKTOP_ENTRY_DIR="$HOME/.local/share/applications"
ICON_URL="https://raw.githubusercontent.com/indyleo/Withdrawal/refs/heads/main/Withdrawal.svg"
PYTHON_SCRIPT_URL="https://raw.githubusercontent.com/indyleo/Withdrawal/refs/heads/main/main.py"
ICON_PATH="$WITHDRAWAL_DIR/Withdrawal.svg"
PYTHON_SCRIPT_PATH="$WITHDRAWAL_DIR/main.py"
DESKTOP_FILE_PATH="$DESKTOP_ENTRY_DIR/Withdrawal.desktop"

echo "Starting Withdrawal installation..."

echo "Creating directory: $WITHDRAWAL_DIR"
mkdir -p "$WITHDRAWAL_DIR" || { echo "Failed to create $WITHDRAWAL_DIR. Exiting."; exit 1; }

echo "Downloading icon from $ICON_URL to $ICON_PATH"
wget -O "$ICON_PATH" "$ICON_URL" || { echo "Failed to download icon. Exiting."; exit 1; }

echo "Downloading Python script from $PYTHON_SCRIPT_URL to $PYTHON_SCRIPT_PATH"
wget -O "$PYTHON_SCRIPT_PATH" "$PYTHON_SCRIPT_URL" || { echo "Failed to download Python script. Exiting."; exit 1; }

echo "Making Python script executable: $PYTHON_SCRIPT_PATH"
chmod +x "$PYTHON_SCRIPT_PATH" || { echo "Failed to make Python script executable. Exiting."; exit 1; }

echo "Creating desktop entry directory: $DESKTOP_ENTRY_DIR"
mkdir -p "$DESKTOP_ENTRY_DIR" || { echo "Failed to create $DESKTOP_ENTRY_DIR. Exiting."; exit 1; }

echo "Creating desktop file: $DESKTOP_FILE_PATH"
cat << EOF > "$DESKTOP_FILE_PATH"
[Desktop Entry]
Name=Withdrawal
Comment=GUI application for Withdrawal
Exec=python3 $PYTHON_SCRIPT_PATH
Icon=$ICON_PATH
Terminal=false
Type=Application
Categories=Utility;
EOF

echo "Creating symlink to $PYTHON_SCRIPT_PATH in $HOME/.local/bin"
ln -sf "$PYTHON_SCRIPT_PATH" "$HOME/.local/bin/withdrawal" || { echo "Failed to create symlink. Exiting."; exit 1; }

echo "Making desktop file executable: $DESKTOP_FILE_PATH"
chmod +x "$DESKTOP_FILE_PATH" || { echo "Failed to make desktop file executable. Exiting."; exit 1; }

echo "Withdrawal installation complete!"
