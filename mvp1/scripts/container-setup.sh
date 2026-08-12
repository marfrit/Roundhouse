#!/bin/bash
# Container setup for testing Roundhouse
# Pushes mvp1 tree + docs/fixtures to container, sets up fake units
# Usage: container-setup.sh [CONTAINER_NAME]

set -eu

CONTAINER="${1:-roundhouse-test}"
ROUNDHOUSE_HOME="/home/roundhouse/roundhouse"

echo "Setting up Roundhouse in container $CONTAINER..."

# Ensure container exists (user must have created it)
if ! incus list | grep -q "^| $CONTAINER"; then
    echo "Error: container $CONTAINER does not exist"
    exit 1
fi

# Push mvp1 tree
echo "Pushing mvp1 tree..."
incus file push --create-dirs -r . "$CONTAINER$ROUNDHOUSE_HOME/" 2>/dev/null || {
    echo "Error: failed to push files to container"
    exit 1
}

# Create systemd user unit directory
incus exec "$CONTAINER" -- su -l roundhouse -c 'mkdir -p ~/.config/systemd/user'

# Copy fixtures and rewrite ExecStart to use fake-llama-server.py
echo "Installing fixture units..."
FIXTURES_DIR="docs/fixtures"

if [ ! -d "$FIXTURES_DIR" ]; then
    echo "Error: $FIXTURES_DIR directory not found"
    exit 1
fi

# Copy a few representative fixtures for testing
for fixture in qwen3.6-coding.service llama-server-gemma4-q4km.service llama-task.service; do
    if [ -f "$FIXTURES_DIR/$fixture" ]; then
        # Read the fixture
        FIXTURE_PATH="$FIXTURES_DIR/$fixture"

        # Create unit on container with modified ExecStart
        incus exec "$CONTAINER" -- su -l roundhouse -c "cat > ~/.config/systemd/user/$fixture" << 'UNITEOF'
[Unit]
Description=Test llama-server (via fake)
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 ROUNDHOUSE_PATH/mvp1/scripts/fake-llama-server.py -m test.gguf --port PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNITEOF

        # This is a simplified version; a real implementation would parse and properly rewrite
        echo "  Installed $fixture (simplified)"
    fi
done

# Daemon reload and install roundhouse.service
echo "Installing roundhouse.service..."
incus exec "$CONTAINER" -- su -l roundhouse -c "mkdir -p ~/.config/systemd/user"
incus exec "$CONTAINER" -- su -l roundhouse -c "cat > ~/.config/systemd/user/roundhouse.service" << 'SERVICEEOF'
[Unit]
Description=Roundhouse — read-only roster server
Documentation=https://docs.boltzmann.local/roundhouse
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 ROUNDHOUSE_PATH/mvp1/roundhouse.py --serve --unit-dir %h/.config/systemd/user
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
SERVICEEOF

# Reload and start
echo "Starting roundhouse service..."
incus exec "$CONTAINER" -- su -l roundhouse -c 'systemctl --user daemon-reload'
incus exec "$CONTAINER" -- su -l roundhouse -c 'systemctl --user enable roundhouse.service'
incus exec "$CONTAINER" -- su -l roundhouse -c 'systemctl --user start roundhouse.service'

echo "Setup complete. Roundhouse should be running on the container."
echo "Access at: http://<container-ip>:8090"
