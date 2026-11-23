# Build stage
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install dependencies
COPY platform/control_plane/backend/requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Runtime stage
FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies.
#
# `curl` is required by the HEALTHCHECK below. `unzip` and the AWS CLI v2 install that used it
# were removed (E34/T9): this application talks to AWS exclusively through boto3 — there is no
# `subprocess`/`os.system` call anywhere in `src/`, so nothing in the image ever invoked `aws`.
# Baking the CLI added a second credential-capable binary to the attack surface for no consumer,
# and 70MB of image (measured: 164.5MB -> 93.8MB, a 43% reduction).
#
# `apt-get upgrade` is NOT redundant with re-pulling the base tag, which is what CON-3 assumed when
# it accepted unpinned apt versions ("the base image is rebuilt on each release anyway"). Measured
# on a fresh `python:3.11-slim`: openssl and util-linux arrive patched, but libc6, gzip,
# libpcre2-8-0, libsqlite3-0 and perl-base are one security revision behind what trixie ships, and
# `libssh2-1` comes in as a `curl` dependency. That gap is what Mirador's ECS/CVRE
# "OS container updates required" finding reports. Runs AFTER the installs above so newly pulled
# dependencies are covered too.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    jq \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

# Base-image Python tooling: upgrade setuptools, then REMOVE pip, wheel and ensurepip's bundled
# wheels from the runtime image. `python:3.11-slim` ships pinned copies of all of them, and they
# are what the image scanners report once every apt fix is in — measured 2026-10-16 on a
# `--pull --no-cache` rebuild: pip 24.0 (CVE-2025-8869), setuptools 79.0.1 (CVE-2026-59890) and,
# vendored inside it, wheel 0.45.1 (CVE-2026-24049, HIGH) and jaraco.context 5.3.0
# (CVE-2026-23949, HIGH). Re-pulling the base tag does not move them (the image pins them) and
# `apt-get upgrade` cannot see them.
#
# Upgrading pip is NOT enough: the current pip vendors its own urllib3 2.7.0, msgpack 1.1.2 and
# setuptools 70.3.0 under pip/_vendor (see pip/_vendor/vendor.txt), each carrying HIGH findings
# no pip release has yet absorbed — so a newer pip traded four findings for five. Nothing in
# `src/` shells out to pip (no subprocess/os.system anywhere, see the apt comment above) and the
# application's dependencies are already installed in the builder, so pip has no consumer at
# runtime and is removed, along with `wheel` (build-time only) and ensurepip's `_bundled/`
# wheels (pip-24.0, setuptools-79.0.1 — scanners read those too). setuptools STAYS, upgraded:
# two runtime dependencies in requirements.txt import `pkg_resources`, which it provides.
# System site-packages, as root, before privileges drop; the app's own dependencies live in
# appuser's user site and are untouched.
RUN pip install --no-cache-dir --upgrade setuptools \
    && pip uninstall -y pip wheel \
    && rm -rf /usr/local/lib/python3.11/ensurepip/_bundled

# Run as a non-root user (E34/T9). Created BEFORE the COPYs below so they can `--chown` directly
# rather than needing a second `chown -R` layer over the whole tree.
RUN groupadd --gid 10001 appuser \
    && useradd --uid 10001 --gid appuser --create-home --shell /usr/sbin/nologin appuser

# `/app` must be WRITABLE by appuser, not merely readable. `DATABASE_URL` is set nowhere in the
# Terraform tree, so the runtime falls back to the `core/config.py` default
# `sqlite:///./control_plane.db` — resolved relative to this WORKDIR — and `main.py`'s startup
# hook calls `init_db()` (`Base.metadata.create_all`) and RE-RAISES on failure. A read-only
# `/app` under a non-root user would therefore crash-loop the task on boot rather than degrade.
RUN chown appuser:appuser /app

# Copy Python dependencies from builder into the unprivileged user's home (was /root/.local,
# which is unreadable once USER is not root).
COPY --from=builder --chown=appuser:appuser /root/.local /home/appuser/.local

# Make sure scripts in .local are usable
ENV PATH=/home/appuser/.local/bin:$PATH

# Copy application code
COPY --chown=appuser:appuser platform/control_plane/backend/src/ ./src/

# Copy the agent-template scaffolds (E20 template rollout reads these to create org repos).
# Pinned via AGENT_TEMPLATES_DIR below so resolution never depends on relative path guessing.
COPY --chown=appuser:appuser platform/control_plane/agent-templates/ ./agent-templates/

# Set Python path to find modules
ENV PYTHONPATH=/app/src

# Set templates path environment variables
ENV AGENT_TEMPLATES_DIR=/app/agent-templates

ARG BUILD_SHA=unknown
ENV BUILD_SHA=$BUILD_SHA

# Drop privileges. Everything below this line runs as uid 10001.
USER appuser

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["curl", "-f", "http://localhost:8000/ping"]

# Run the application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
