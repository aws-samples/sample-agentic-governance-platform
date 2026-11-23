# Apply the base-OS security updates upstream did not.
#
# Why this exists: every finding these images attract is a base-layer package
# (openssl, glibc, perl, krb5 — not Langfuse or ClickHouse code). Upstream installs
# named packages without ever running a full upgrade, so base-layer libraries stay at
# whatever the base image shipped even when upstream rebuilds. Measured on 2026-09-02:
# clickhouse-server 25.8.33.6 — the NEWEST 25.8 LTS
# build, and byte-identical to the Docker Official `clickhouse:25.8.33.6` — carried
# openssl 3.0.2-0ubuntu1.29 next to libssl3 3.0.2-0ubuntu1.26. Same source package,
# two versions: the build pulled openssl fresh as a dependency and left the base
# layer's libssl3 untouched. 27 packages were upgradable straight from the Ubuntu
# 22.04 archive, including two Criticals (libssl3 USN-8678-1, perl-base USN-8675-1).
#
# So a tag bump alone cannot clear a finding, and no published ClickHouse image can:
# checked clickhouse/clickhouse-server, Docker Official clickhouse, and 25.8-alpine.
# Upgrading at mirror time is what makes the pin self-healing — the version bump stops
# being the thing that carries security updates, and we stop being hostage to
# upstream's rebuild cadence.
#
# Handles apt (ClickHouse, Ubuntu 22.04) and apk (Alpine, the Langfuse v3 images). An image
# with an rpm database and no package manager goes through patch-base.rpm.Dockerfile.

ARG BASE
FROM ${BASE}

# ARGs declared before FROM are not visible in the build stage — re-declare.
# Upstream's runtime user is restored at the end so this stays a pure package upgrade.
# An empty Config.User means root, which is what both current images report.
ARG ORIG_USER=root

USER root
RUN set -eu; \
    if command -v apt-get >/dev/null 2>&1; then \
        export DEBIAN_FRONTEND=noninteractive; \
        # Upgrade from the DISTRO archive only. ClickHouse ships its own apt repo inside its
        # image, so a blanket `apt-get upgrade` pulls clickhouse-server across major versions —
        # a bare run here moved 25.8.33.6 to 26.8.2.7, off the supported LTS line and into a
        # major upgrade of an EFS-backed store. Parking third-party sources for the duration
        # keeps this a base-OS patch and nothing more; they are restored immediately after.
        mkdir -p /tmp/patch-base-sources; \
        mv /etc/apt/sources.list.d/* /tmp/patch-base-sources/ 2>/dev/null || true; \
        apt-get update; \
        apt-get upgrade -y; \
        apt-get clean; \
        mv /tmp/patch-base-sources/* /etc/apt/sources.list.d/ 2>/dev/null || true; \
        rm -rf /tmp/patch-base-sources /var/lib/apt/lists/*; \
    elif command -v apk >/dev/null 2>&1; then \
        # Alpine repositories are release-pinned, so this stays inside 3.x patch levels.
        apk upgrade --no-cache; \
    else \
        echo "patch-base: neither apt-get nor apk found in the base image" >&2; \
        exit 1; \
    fi

USER ${ORIG_USER}
