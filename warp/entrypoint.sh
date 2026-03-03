#!/bin/sh

# Set defaults if not provided in env
WARP_SOCKS_PORT=${WARP_SOCKS_PORT:-1080}
WARP_HTTP_PORT=${WARP_HTTP_PORT:-8080}
WARP_RESTART_PORT=${WARP_RESTART_PORT:-9090}

# Keep the container running and listen for restart signals
while true; do
    echo "[INFO] Starting new WARP session setup..."
    
    # 1. Clean previous configs to force new IP/Identity
    rm -f wgcf-account.toml wgcf-profile.conf
    
    # 2. Register new WARP account
    echo "[INFO] Registering wgcf account..."
    wgcf register --accept-tos
    
    if [ ! -f "wgcf-account.toml" ]; then
        echo "[ERROR] Failed to register wgcf account. Retrying in 5 seconds..."
        sleep 5
        continue
    fi
    
    # 3. Generate WireGuard profile
    echo "[INFO] Generating wgcf profile..."
    wgcf generate
    
    if [ ! -f "wgcf-profile.conf" ]; then
        echo "[ERROR] Failed to generate wgcf profile. Retrying in 5 seconds..."
        sleep 5
        continue
    fi
    
    # 4. Append wireproxy SOCKS5 and HTTP routing to the profile
    echo "" >> wgcf-profile.conf
    echo "[Socks5]" >> wgcf-profile.conf
    echo "BindAddress = 0.0.0.0:${WARP_SOCKS_PORT}" >> wgcf-profile.conf
    
    # Optional authentication
    if [ -n "$WARP_USERNAME" ] && [ -n "$WARP_PASSWORD" ]; then
        echo "Username = ${WARP_USERNAME}" >> wgcf-profile.conf
        echo "Password = ${WARP_PASSWORD}" >> wgcf-profile.conf
    fi
    
    echo "" >> wgcf-profile.conf
    echo "[http]" >> wgcf-profile.conf
    echo "BindAddress = 0.0.0.0:${WARP_HTTP_PORT}" >> wgcf-profile.conf
    if [ -n "$WARP_USERNAME" ] && [ -n "$WARP_PASSWORD" ]; then
        echo "Username = ${WARP_USERNAME}" >> wgcf-profile.conf
        echo "Password = ${WARP_PASSWORD}" >> wgcf-profile.conf
    fi

    # 5. Start wireproxy in the background
    echo "[INFO] Starting wireproxy..."
    wireproxy -c wgcf-profile.conf &
    WIREPROXY_PID=$!
    
    echo "[INFO] wireproxy is running on PID ${WIREPROXY_PID}"
    echo "[INFO] SOCKS5: ${WARP_SOCKS_PORT} | HTTP: ${WARP_HTTP_PORT}"
    echo "[INFO] Listening on port ${WARP_RESTART_PORT} for IP rotation triggers (GET /restart)."
    
    # 6. Listen for HTTP trigger on the restart port using netcat
    # When ytdl-bot receives a 'Sign in to confirm you're not a bot' error, it will curl this port.
    echo -e "HTTP/1.1 200 OK\r\nContent-Length: 17\r\n\r\nRestarting WARP\n" | nc -l -p ${WARP_RESTART_PORT} > /dev/null
    
    echo "[WARN] Restart trigger received! Rotating IP..."
    
    # Kill the background wireproxy cleanly
    kill ${WIREPROXY_PID}
    
    # Give it a moment to release ports
    sleep 2
done
