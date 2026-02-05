#!/bin/bash
# Script to run tests inside the Docker container

docker-compose exec ytdl-bot python3 -m unittest discover tests
