#!/usr/bin/env bash

autopilot_find_developer_dir() {
  if [[ -n "${DEVELOPER_DIR:-}" ]] && [[ -x "$DEVELOPER_DIR/usr/bin/xcodebuild" ]]; then
    printf '%s\n' "$DEVELOPER_DIR"
    return
  fi

  local selected
  selected="$(xcode-select -p 2>/dev/null || true)"
  if [[ -n "$selected" ]] && [[ -x "$selected/usr/bin/xcodebuild" ]] \
    && DEVELOPER_DIR="$selected" xcodebuild -version >/dev/null 2>&1; then
    printf '%s\n' "$selected"
    return
  fi

  local candidate
  for candidate in \
    "/Applications/Xcode.app/Contents/Developer" \
    "$HOME/Applications/Xcode.app/Contents/Developer" \
    "$HOME/Downloads/Xcode.app/Contents/Developer" \
    "$HOME/Downloads/Xcode-beta.app/Contents/Developer"; do
    if [[ -x "$candidate/usr/bin/xcodebuild" ]]; then
      printf '%s\n' "$candidate"
      return
    fi
  done

  return 1
}

autopilot_use_xcode() {
  local developer_dir
  developer_dir="$(autopilot_find_developer_dir || true)"
  if [[ -z "$developer_dir" ]]; then
    echo "Full Xcode is required for iOS simulator work." >&2
    echo "Set DEVELOPER_DIR or install Xcode in /Applications, ~/Applications, or ~/Downloads." >&2
    return 1
  fi
  export DEVELOPER_DIR="$developer_dir"
}
