#!/bin/sh
set -eu
TASK_ROOT=/private/tmp/altt-operation-eval-20261013
REPO_ROOT=/Users/geekaz0id/PycharmProjects/al-torrent-tools
exec docker run --rm --network altt-operations-315-20261013 \
  --mount type=bind,source="$REPO_ROOT/docs/runtime-eval/operations315/runner",target=/bench/runner,readonly \
  --mount type=bind,source="$TASK_ROOT/baseline/backend",target=/bench/baseline,readonly \
  --mount type=bind,source="$TASK_ROOT/mediainfo-workers-candidate/backend",target=/bench/optimized,readonly \
  --mount type=bind,source="$TASK_ROOT/fixtures",target=/bench/fixtures,readonly \
  --mount type=bind,source="$TASK_ROOT/fixtures/generated",target=/bench/media/generated,readonly \
  --mount type=bind,source=/Users/geekaz0id/Downloads/kensetsu-8cbb50a3,target=/bench/media/real,readonly \
  --mount type=bind,source="$TASK_ROOT/results",target=/bench/results \
  --workdir /tmp --entrypoint /opt/runtime-modes/run_regular.sh \
  altt-python-eval:315-vs-315t-pure-20261013 \
  /bench/runner/benchmark_operations315_workers.py "$@"
