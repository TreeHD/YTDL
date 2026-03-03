FROM golang:1.21-alpine AS builder

RUN apk add --no-cache git make

# Build wireproxy from source
RUN git clone https://github.com/pufferffish/wireproxy.git /wireproxy
WORKDIR /wireproxy
RUN make

# Build wgcf from source
RUN go install github.com/ViRb3/wgcf@latest

FROM alpine:latest

# netcat is required for the HTTP trigger listener
RUN apk add --no-cache netcat-openbsd tzdata ca-certificates bash dos2unix

WORKDIR /app

# Copy binaries from builder
COPY --from=builder /wireproxy/wireproxy /usr/bin/wireproxy
COPY --from=builder /go/bin/wgcf /usr/bin/wgcf

COPY entrypoint.sh /app/entrypoint.sh
RUN dos2unix /app/entrypoint.sh && chmod +x /app/entrypoint.sh

# Do not run as non-root since wgcf might need to manipulate the working directory
CMD ["/app/entrypoint.sh"]
