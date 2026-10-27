#!/usr/bin/env bash
set -euo pipefail

# Historical run6 finalizer retirement guard.
#
# It previously wrote a proxy into web_viewer/app/public/scene_package, the
# mutable v1.0 Viewer root.  That root is read-only compatibility data now;
# fail before waiting for Docker or resolving any historical asset path.
echo "finalize_dense_geometry.sh is retired: create a new immutable Scene Package through verified MRP + SPZ import." >&2
exit 2
