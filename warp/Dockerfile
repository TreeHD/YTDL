FROM golang:1.26.1-alpine AS builder

RUN apk add --no-cache git make

# Build wireproxy using make
RUN git clone https://github.com/octeep/wireproxy.git /wireproxy \
    && cd /wireproxy \
    && make

FROM alpine:latest

# netcat is required for the HTTP trigger listener
RUN apk add --no-cache netcat-openbsd tzdata ca-certificates bash dos2unix curl

WORKDIR /app

# Copy binaries from builder
COPY --from=builder /wireproxy/wireproxy /usr/bin/wireproxy

# Use external P3TERX script to fetch precompiled wgcf
RUN curl -fsSL https://raw.githubusercontent.com/P3TERX/script/master/wgcf.sh | bash

COPY entrypoint.sh /app/entrypoint.sh
RUN dos2unix /app/entrypoint.sh && chmod +x /app/entrypoint.sh

# Do not run as non-root since wgcf might need to manipulate the working directory
CMD ["/app/entrypoint.sh"]
