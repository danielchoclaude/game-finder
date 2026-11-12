#!/bin/bash
# Stops and removes the background injury watcher.
LABEL=com.gamefinder.injurywatch
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Injury alerts removed."
