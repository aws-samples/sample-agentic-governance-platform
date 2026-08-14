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
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    jq \
    && rm -rf /var/lib/apt/lists/*

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

# Drop privileges. Everything below this line runs as uid 10001.
USER appuser

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["curl", "-f", "http://localhost:8000/ping"]

# Run the application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
