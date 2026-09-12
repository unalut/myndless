#!/usr/bin/env bash
# Regenerate myndlink/pb/*_pb2.py from myndlink/proto/*.proto.
set -euo pipefail
cd "$(dirname "$0")/.."

rm -rf myndlink/pb
mkdir -p myndlink/pb
python -m grpc_tools.protoc \
  -I myndlink/proto \
  --python_out=myndlink/pb \
  myndlink/proto/*.proto
touch myndlink/pb/__init__.py
echo "Generated myndlink/pb/ from myndlink/proto/*.proto"
